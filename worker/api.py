"""Worker REST API (loopback only, Bearer auth, size-limited bodies)."""
import hmac
import json
import logging
import re
import threading
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from worker.config import WorkerSettings
from worker.gemini_browser import check_instruction, portrait_instruction, still_instruction
from worker.gemini_session import Busy, GeminiSession
from worker.jobs import JobStore
from worker.pipeline import Runner

log = logging.getLogger("worker.api")

MAX_BODY = 35 * 1024 * 1024  # 3 images × 10 MB + form overhead
MAX_IMAGE = 10 * 1024 * 1024
MAX_PROMPT = 2000  # one clip prompt
MAX_IDEA = 10000  # a plan's prompt (Gemini's chat condenses it into clip prompts)
MAX_SCENES = 12
MAX_CAST = 8
MAX_RULES = 3000  # the video type's planner rules
MAX_CHECK_VIDEO = 30 * 1024 * 1024  # one clip sent to be checked
CAST_LIMITS = {"name": 60, "appearance": 1000, "outfit": 200, "voice": 150, "personality": 200}


def _plan_options(body: dict) -> dict:
    """The plan's optional cast, style, language and video-type rules, bounded."""
    cast = body.get("cast") or []
    if not isinstance(cast, list) or len(cast) > MAX_CAST or not all(isinstance(c, dict) for c in cast):
        raise HTTPException(400, f"Cast must be a list of at most {MAX_CAST} characters.")
    cast = [{k: str(c.get(k) or "").strip()[:n] for k, n in CAST_LIMITS.items()} for c in cast]
    if any(not c["name"] for c in cast):
        raise HTTPException(400, "Every cast member needs a name.")
    opts = {"cast": cast, "style": str(body.get("style") or "").strip()[:300],
            "language": str(body.get("language") or "").strip()[:40],
            "rules": str(body.get("rules") or "").strip()[:MAX_RULES]}
    return {k: v for k, v in opts.items() if v}


class _TooLarge(Exception):
    pass


class BodyLimit:
    """Rejects bodies over `limit` bytes, whether or not Content-Length is sent."""

    def __init__(self, app, limit: int):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        for k, v in scope.get("headers", []):
            if k == b"content-length" and v.isdigit() and int(v) > self.limit:
                return await self._reject(send)
        total = 0
        started = False

        async def limited_receive():
            nonlocal total
            msg = await receive()
            if msg["type"] == "http.request":
                total += len(msg.get("body", b""))
                if total > self.limit:
                    raise _TooLarge()
            return msg

        async def tracking_send(msg):
            nonlocal started
            if msg["type"] == "http.response.start":
                started = True
            await send(msg)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _TooLarge:
            if not started:
                await self._reject(send)

    @staticmethod
    async def _reject(send):
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"detail":"Request body too large."}'})


IMAGE_TYPES = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}


def _image_ext(head: bytes) -> str | None:
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def create_app(settings: WorkerSettings, store: JobStore, runner: Runner, session: GeminiSession,
               start_background: bool = True) -> FastAPI:
    stop = threading.Event()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        recovered = store.recover()
        if recovered:
            log.info("recovered %d interrupted job(s)", len(recovered))
        thread = None
        if start_background:
            thread = threading.Thread(target=runner.run_forever, args=(stop,), name="runner", daemon=True)
            thread.start()
            session.check_in_background()  # fresh Gemini status after every start (headless, no window)
        yield
        stop.set()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(BodyLimit, limit=MAX_BODY)
    expected = f"Bearer {settings.STUDIO_API_KEY}".encode()

    def auth(request: Request) -> None:
        sent = request.headers.get("authorization", "").encode()
        if not settings.STUDIO_API_KEY or not hmac.compare_digest(sent, expected):
            raise HTTPException(401, "Missing or wrong API key.")

    @app.exception_handler(Busy)
    async def _busy(request: Request, exc: Busy):
        return JSONResponse({"detail": exc.message}, status_code=409)

    # ---- /videos (the web app's contract) ---------------------------------
    @app.post("/videos", dependencies=[Depends(auth)])
    async def create_video(request: Request):
        pause = runner.paused()
        if pause:
            raise HTTPException(409, pause["message"])
        ctype = request.headers.get("content-type", "")
        uploads = []
        if ctype.startswith("application/json"):
            try:
                body = await request.json()
            except ValueError:
                raise HTTPException(400, "Body must be JSON.")
            if not isinstance(body, dict):
                raise HTTPException(400, "Body must be a JSON object.")
        elif ctype.startswith("multipart/form-data"):
            form = await request.form(max_files=3, max_fields=10)
            body = {k: form.get(k) for k in ("prompt", "aspect", "ref")}
            uploads = [f for f in form.getlist("images") if hasattr(f, "read")]
        else:
            raise HTTPException(415, "Send JSON or multipart/form-data.")
        prompt = str(body.get("prompt") or "").strip()
        aspect = str(body.get("aspect") or "")
        ref = str(body.get("ref") or "")[:100] or None
        if not prompt or len(prompt) > MAX_PROMPT:
            raise HTTPException(400, f"The prompt must be 1–{MAX_PROMPT} characters.")
        if aspect not in runner.profile["aspects"]:
            raise HTTPException(400, f"Aspect must be one of {', '.join(runner.profile['aspects'])}.")
        if len(uploads) > 3:
            raise HTTPException(400, "At most 3 reference images.")
        images = []
        for f in uploads:
            data = await f.read(MAX_IMAGE + 1)
            if len(data) > MAX_IMAGE:
                raise HTTPException(400, "Each reference image must be 10 MB or less.")
            ext = _image_ext(data[:16])
            if ext is None:
                raise HTTPException(400, "Reference images must be JPG, PNG or WebP.")
            images.append((ext, data))
        jid = uuid.uuid4().hex[:16]
        paths = []
        if images:
            d = settings.jobs_dir / jid / "refs"
            d.mkdir(parents=True, exist_ok=True)
            for i, (ext, data) in enumerate(images, 1):
                p = d / f"upload-{i}.{ext}"
                p.write_bytes(data)
                paths.append(str(p))
        store.create(prompt, aspect, ref, paths, job_id=jid)
        log.info("job %s created (images=%d)", jid, len(paths))
        return {"job_id": jid}

    @app.post("/plans", dependencies=[Depends(auth)])
    async def create_plan(request: Request):
        """Queue a job in which Gemini (chat) writes a `scenes`-scene script from one idea."""
        pause = runner.paused()
        if pause:
            raise HTTPException(409, pause["message"])
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "Body must be JSON.")
        if not isinstance(body, dict):
            raise HTTPException(400, "Body must be a JSON object.")
        prompt = str(body.get("prompt") or "").strip()
        aspect = str(body.get("aspect") or "")
        ref = str(body.get("ref") or "")[:100] or None
        scenes = body.get("scenes")
        if not prompt or len(prompt) > MAX_IDEA:
            raise HTTPException(400, f"The prompt must be 1–{MAX_IDEA} characters.")
        if aspect not in runner.profile["aspects"]:
            raise HTTPException(400, f"Aspect must be one of {', '.join(runner.profile['aspects'])}.")
        if not isinstance(scenes, int) or isinstance(scenes, bool) or not 1 <= scenes <= MAX_SCENES:
            raise HTTPException(400, f"Scenes must be a number from 1 to {MAX_SCENES}.")
        jid = store.create(prompt, aspect, ref, kind="plan", scene_count=scenes, options=_plan_options(body))
        log.info("job %s created (plan, scenes=%d)", jid, scenes)
        return {"job_id": jid}

    async def _form_with_images(request: Request, max_images: int, max_videos: int = 0, max_stills: int = 0
                                ) -> tuple[dict, list[tuple[str, bytes]], list[bytes], list[tuple[str, bytes]]]:
        """A multipart body: the JSON in `body`, pictures as `images`, MP4 clips as `videos`, a clip's frames as
        `stills`."""
        if not request.headers.get("content-type", "").startswith("multipart/form-data"):
            raise HTTPException(415, "Send multipart/form-data.")
        form = await request.form(max_files=max_images + max_videos + max_stills, max_fields=5)
        try:
            body = json.loads(form.get("body") or "")
        except (ValueError, TypeError):
            raise HTTPException(400, "The body field must be JSON.")
        if not isinstance(body, dict):
            raise HTTPException(400, "The body field must be a JSON object.")
        async def pictures(field: str, most: int) -> list[tuple[str, bytes]]:
            out = []
            for f in [f for f in form.getlist(field) if hasattr(f, "read")][:most]:
                data = await f.read(MAX_IMAGE + 1)
                ext = _image_ext(data[:16]) if len(data) <= MAX_IMAGE else None
                if ext is None:
                    raise HTTPException(400, "Pictures must be JPG, PNG or WebP of 10 MB or less.")
                out.append((ext, data))
            return out

        images = await pictures("images", max_images)
        stills = await pictures("stills", max_stills)
        videos = []
        for f in [f for f in form.getlist("videos") if hasattr(f, "read")][:max_videos]:
            data = await f.read(MAX_CHECK_VIDEO + 1)
            if len(data) > MAX_CHECK_VIDEO or data[4:8] != b"ftyp":
                raise HTTPException(400, "Clips must be MP4 files of 30 MB or less.")
            videos.append(data)
        return body, images, videos, stills

    def _save_images(jid: str, images: list[tuple[str, bytes]]) -> list[str]:
        d = settings.jobs_dir / jid / "refs"
        paths = []
        for i, (ext, data) in enumerate(images, 1):
            d.mkdir(parents=True, exist_ok=True)
            p = d / f"upload-{i}.{ext}"
            p.write_bytes(data)
            paths.append(str(p))
        return paths

    @app.post("/stills", dependencies=[Depends(auth)])
    async def create_still(request: Request):
        """Queue a job in which Gemini (chat) draws one scene's first frame from `description`, with the attached
        pictures (characters, then the previous shot's last frame) as the exact look."""
        pause = runner.paused()
        if pause:
            raise HTTPException(409, pause["message"])
        body, images, _, _ = await _form_with_images(request, 4)
        description = str(body.get("description") or "").strip()
        aspect = str(body.get("aspect") or "")
        if not description or len(description) > MAX_PROMPT:
            raise HTTPException(400, f"The description must be 1–{MAX_PROMPT} characters.")
        if aspect not in runner.profile["aspects"]:
            raise HTTPException(400, f"Aspect must be one of {', '.join(runner.profile['aspects'])}.")
        names = [str(n)[:60] for n in body.get("names") or []][:3]
        jid = uuid.uuid4().hex[:16]
        store.create(still_instruction(description, aspect, names, bool(body.get("previous"))), aspect,
                     str(body.get("ref") or "")[:100] or None, _save_images(jid, images), job_id=jid, kind="portrait")
        log.info("job %s created (still, images=%d)", jid, len(images))
        return {"job_id": jid}

    @app.post("/checks", dependencies=[Depends(auth)])
    async def create_check(request: Request):
        """Queue a job in which Gemini (chat, text only) watches a new clip, sound included, and checks it against
        the characters' pictures, the previous clip and what the scene should show and say."""
        pause = runner.paused()
        if pause:
            raise HTTPException(409, pause["message"])
        body, images, videos, stills = await _form_with_images(request, 3, 2, 4)
        names = [re.sub(r"[^\w -]", "", str(n))[:60].strip() or f"character {i}"
                 for i, n in enumerate(body.get("names") or [], 1)][:3]
        previous = bool(body.get("previous"))
        if len(images) != len(names) or len(videos) != 1 + previous:
            raise HTTPException(400, "Send one picture per name, the previous clip if any, then the new clip.")
        lines = [{k: str(x.get(k) or "")[:400] for k in ("name", "line", "position")}
                 for x in body.get("lines") or [] if isinstance(x, dict)][:8]
        action = str(body.get("action") or "")[:1500]
        jid = uuid.uuid4().hex[:16]
        d = settings.jobs_dir / jid / "refs"
        d.mkdir(parents=True, exist_ok=True)
        paths = []  # named for what they are: Gemini sees the file names
        for name, (ext, data) in zip(names, images):
            paths.append(d / f"{name}.{ext}")
            paths[-1].write_bytes(data)
        for name, data in zip((["previous-shot"] if previous else []) + ["new-shot"], videos):
            paths.append(d / f"{name}.mp4")
            paths[-1].write_bytes(data)
        for i, (ext, data) in enumerate(stills, 1):
            paths.append(d / f"new-shot-frame-{i}.{ext}")
            paths[-1].write_bytes(data)
        outfits = {str(k)[:60]: str(v)[:200] for k, v in (body.get("outfits") or {}).items()} \
            if isinstance(body.get("outfits"), dict) else {}
        store.create(check_instruction(names, previous, action, lines, outfits, len(stills)), "",
                     str(body.get("ref") or "")[:100] or None,
                     [str(p) for p in paths], job_id=jid, kind="check")
        log.info("job %s created (check, pictures=%d, clips=%d, stills=%d)", jid, len(images), len(videos),
                 len(stills))
        return {"job_id": jid}

    @app.post("/portraits", dependencies=[Depends(auth)])
    async def create_portrait(request: Request):
        """Queue a job in which Gemini (chat) draws one character portrait of `description` in `style`."""
        pause = runner.paused()
        if pause:
            raise HTTPException(409, pause["message"])
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "Body must be JSON.")
        if not isinstance(body, dict):
            raise HTTPException(400, "Body must be a JSON object.")
        description = str(body.get("description") or "").strip()
        style = str(body.get("style") or "").strip()[:300]
        ref = str(body.get("ref") or "")[:100] or None
        if not description or len(description) > MAX_PROMPT:
            raise HTTPException(400, f"The description must be 1–{MAX_PROMPT} characters.")
        jid = store.create(portrait_instruction(description, style), "3:4", ref, kind="portrait")
        log.info("job %s created (portrait)", jid)
        return {"job_id": jid}

    def _job(jid: str) -> dict:
        job = store.get(jid)
        if job is None:
            raise HTTPException(404, "No such job.")
        return job

    @app.get("/videos/{jid}", dependencies=[Depends(auth)])
    def video_status(jid: str):
        job = _job(jid)
        st = job["status"]
        out = {"status": st, "error": None, "video_url": None, "final_prompt": job["prompt"]}
        if st == "cancelled":
            out.update(status="failed", error=job["error"] or "Cancelled.")
        elif st == "failed":
            out["error"] = job["error"] or "The video couldn't be made."
        elif st == "done" and job["kind"] == "plan":
            result = job["result"]  # {"bible", "scenes"}; plans made before the bible are a bare list
            out["scenes"] = result.get("scenes") if isinstance(result, dict) else result
            out["bible"] = result.get("bible") if isinstance(result, dict) else None
        elif st == "done" and job["kind"] == "check":
            out["check"] = job["result"]
        elif st == "done" and job["kind"] == "portrait":
            out["image_url"] = f"/videos/{jid}/file"
        elif st == "done":
            out["video_url"] = f"/videos/{jid}/file"
        if job["warnings"]:
            out["warnings"] = job["warnings"]
        return out

    @app.get("/videos/{jid}/file", dependencies=[Depends(auth)])
    def video_file(jid: str):
        job = _job(jid)
        if job["status"] != "done" or not job["clip_path"]:
            raise HTTPException(404, "The video isn't ready.")
        if job["kind"] == "portrait":
            with open(job["clip_path"], "rb") as f:
                ext = _image_ext(f.read(16))
            return FileResponse(job["clip_path"], media_type=IMAGE_TYPES.get(ext, "application/octet-stream"))
        return FileResponse(job["clip_path"], media_type="video/mp4")

    @app.delete("/videos/{jid}", dependencies=[Depends(auth)])
    def video_cancel(jid: str):
        if not runner.cancel(jid):
            raise HTTPException(404, "No such job.")
        return {"ok": True}

    # ---- health / Gemini ---------------------------------------------------
    @app.get("/health", dependencies=[Depends(auth)])
    def health():
        pause = runner.paused()
        return {
            "ok": True,
            "account_paused": pause,
            "queued": store.count("queued"),
            "running": store.count("running"),
            "gemini": session.status(),
            "login_window_open": session.window_open,
            "profile": runner.profile.get("name"),
            "max_clip_duration": runner.profile.get("max_clip_duration"),
        }

    @app.get("/gemini/status", dependencies=[Depends(auth)])
    def gemini_status():
        return session.status()

    @app.post("/gemini/login", dependencies=[Depends(auth)])
    def gemini_login():
        return session.open_window()

    @app.post("/gemini/login/done", dependencies=[Depends(auth)])
    def gemini_login_done():
        return session.done(background=True)

    @app.post("/gemini/check", dependencies=[Depends(auth)])
    def gemini_check(background: bool = False):
        return session.start_check() if background else session.check()

    @app.post("/gemini/resume", dependencies=[Depends(auth)])
    def gemini_resume():
        runner.resume()
        return session.status()

    return app
