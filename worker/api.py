"""Worker REST API (loopback only, Bearer auth, size-limited bodies)."""
import hmac
import logging
import threading
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from worker.config import WorkerSettings
from worker.gemini_session import Busy, GeminiSession
from worker.jobs import JobStore
from worker.pipeline import Runner

log = logging.getLogger("worker.api")

MAX_BODY = 35 * 1024 * 1024  # 3 images × 10 MB + form overhead
MAX_IMAGE = 10 * 1024 * 1024
MAX_PROMPT = 2000  # one clip prompt
MAX_IDEA = 10000  # a plan's prompt (Gemini's chat condenses it into clip prompts)
MAX_SCENES = 12


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
        """Queue a job in which Gemini (chat) splits one idea into `scenes` clip prompts."""
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
        jid = store.create(prompt, aspect, ref, kind="plan", scene_count=scenes)
        log.info("job %s created (plan, scenes=%d)", jid, scenes)
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
            out["scenes"] = job["result"]
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
