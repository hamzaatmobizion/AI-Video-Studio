"""Generation lifecycle: [plan] → for each scene, submit → poll worker → download; then join → save → READY.

Planning: when the user gives one prompt and a length, Gemini (chat) first writes a script: per scene the
setting, camera, who is where and what they say (app/script.py). Each clip prompt is compiled from that script,
with every library character described in their own words. With `review`, the video waits at REVIEW until the
user has edited and approved the script.

Scenes go to the worker one at a time, so a Gemini limit never fails a queue of them: the generation is
PAUSED with its finished clips kept, and continues only when the user clicks Resume on that video.
Driven by `tick()` (scheduler every 5 s, never overlapping). All state is in the DB, so a restart
simply continues; temp files in var/generated survive restarts.
"""
import logging
import re
import shutil
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select

from app.enums import ACTIVE, GenStatus
from app.factories import Providers
from app.interfaces import GeneratorError, StorageError
from app.models import Character, Generation, GoogleConnection, Scene, utcnow
from app.script import (MAX_PROMPT, cast_sheet, compile_prompt, normalize_scene, scene_images, scene_warnings,
                        clean_text)
from app.video_join import JoinError, join_clips

log = logging.getLogger("app.generation")

JOB_TIMEOUT = timedelta(minutes=30)
MAX_POLL_ERRORS = 10
HISTORY_FILTERS = {
    "ready": [GenStatus.READY],
    "failed": [GenStatus.FAILED],
    "cancelled": [GenStatus.CANCELLED],
    "in_progress": [GenStatus.PLANNING, GenStatus.REVIEW, GenStatus.GENERATING, GenStatus.SAVING,
                    GenStatus.SAVE_FAILED, GenStatus.PAUSED],
}
MAX_UPLOAD_ATTEMPTS = 10
UPLOAD_RETRY = timedelta(seconds=30)
MAX_SCENES = 12


class UserError(Exception):
    """A problem to show the user as-is (HTTP 400/503)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def drive_name(gen: Generation, when: datetime) -> tuple[str, str]:
    """(subfolder, filename) → <YYYY-MM>/<date> <prompt> (<id>).mp4"""
    text = gen.scenes[0].prompt if gen.scenes and not gen.planned and not gen.scenes[0].action else gen.prompt
    words = re.sub(r"[\\/:*?\"<>|\n\r\t]+", " ", text).strip()
    words = re.sub(r"\s+", " ", words)[:60].rstrip() or "video"
    return when.strftime("%Y-%m"), f"{when:%Y-%m-%d} {words} ({gen.id}).mp4"


class GenerationService:
    def __init__(self, providers: Providers):
        self.p = providers
        self.db = providers.db
        self.settings = providers.settings

    # ---- user actions ----------------------------------------------------
    def create(self, user_id: int, scenes: list[str], aspect: str, images: list[tuple[str, bytes]],
               plan: int | None = None, cast_ids: list[int] | None = None, style: str = "", language: str = "",
               review: bool = False) -> int:
        """`scenes`: one prompt per ~10 s clip, already validated. With `plan` = N, `scenes` is the user's
        one prompt and Gemini first writes an N-scene script from it. `images` validated by content: [(ext, bytes)].
        `cast_ids`: the user's characters to use; `review`: wait for the user to approve the script."""
        if self.p.generator() is None:
            raise UserError("Video generation is off: set WORKER_URL and WORKER_API_KEY in .env.", 503)
        if self.p.storage(user_id) is None:
            raise UserError("Connect Google Drive in Settings first, so the video has somewhere to go.", 503)
        style, language = clean_text(style, 300), clean_text(language, 40)
        with self.db.session() as s:
            library = self._library(s, user_id)
            known = {c["id"] for c in library}
            cast_ids = list(dict.fromkeys(cast_ids or []))
            if any(cid not in known for cid in cast_ids):
                raise UserError("One of the chosen characters doesn't exist any more. Reload the page.")
            cast = [c for c in library if c["id"] in cast_ids]
            if plan:
                prompt, rows = scenes[0], [Scene(position=i, prompt="") for i in range(1, plan + 1)]
            else:
                prompt = scenes[0] if len(scenes) == 1 else "\n\n".join(f"Scene {i}: {p}" for i, p in enumerate(scenes, 1))
                rows = [self._scene_row(i, {"action": text, "characters": [{"id": c["id"]} for c in cast]}, cast,
                                        style, language, compile_now=not review)
                        if cast or style or review else Scene(position=i, prompt=text)
                        for i, text in enumerate(scenes, 1)]
            gen = Generation(user_id=user_id, prompt=prompt, aspect=aspect, ref_images=[], status=GenStatus.GENERATING,
                             poll_errors=0, upload_attempts=0, dismissed=False, planned=bool(plan),
                             review=review, cast_ids=cast_ids, style=style, language=language, scenes=rows)
            s.add(gen)
            s.flush()
            paths = []
            if images:
                d = self.settings.refs_dir / str(gen.id)
                d.mkdir(parents=True, exist_ok=True)
                for i, (ext, data) in enumerate(images, 1):
                    path = d / f"ref-{i}.{ext}"
                    path.write_bytes(data)
                    paths.append(str(path))
            gen.ref_images = paths
            gen_id = gen.id
        self._continue(gen_id)
        return gen_id

    def _continue(self, gen_id: int) -> None:
        """Plan the scenes if needed, else submit the next scene without a clip, else join them."""
        with self.db.session() as s:
            gen = s.get(Generation, gen_id)
            if not gen.scenes:  # made before scenes existed
                gen.scenes.append(Scene(position=1, prompt=gen.prompt))
            unplanned = any(not sc.prompt for sc in gen.scenes)
            pending = _next_scene(gen) is not None
            drafted = gen.review and any(sc.action or sc.dialogue for sc in gen.scenes)
            if unplanned and drafted:  # the script is written; the user approves it, then the clips are made
                gen.status, gen.worker_job_id, gen.submitted_at = GenStatus.REVIEW, None, None
                return
        if unplanned:
            self._submit_plan(gen_id)
        elif pending:
            self._submit(gen_id)
        else:
            self._join(gen_id)

    def _submit(self, gen_id: int) -> None:
        gen_client = self.p.generator()
        with self.db.session() as s:
            gen = s.get(Generation, gen_id)
            scene = _next_scene(gen)
            try:
                if gen_client is None:
                    raise GeneratorError("Video generation is off: set WORKER_URL and WORKER_API_KEY.", final=True)
                paths = gen.ref_images
                if scene.characters:  # the pictures of who is in this shot first, then the video's own uploads
                    paths = scene_images(scene.script(), self._library(s, gen.user_id), gen.ref_images)
                images = [Path(p) for p in paths if Path(p).exists()]
                job_id = gen_client.submit(scene.prompt, gen.aspect,
                                           f"gen-{gen.id}-s{scene.position}-{uuid.uuid4().hex[:6]}", images)
            except GeneratorError as e:
                pause = _worker_pause(gen_client)
                if pause:
                    self._pause(gen, pause)
                else:
                    gen.status, gen.error = GenStatus.FAILED, _scene_error(gen, scene, e.message)
                    log.info("generation %s submit failed", gen_id)
                return
            gen.worker_job_id = job_id
            gen.submitted_at = utcnow()
            gen.poll_errors = 0
            gen.status = GenStatus.GENERATING
            log.info("generation %s scene %d submitted as %s", gen_id, scene.position, job_id)

    def _submit_plan(self, gen_id: int) -> None:
        client = self.p.generator()
        with self.db.session() as s:
            gen = s.get(Generation, gen_id)
            try:
                if client is None:
                    raise GeneratorError("Video generation is off: set WORKER_URL and WORKER_API_KEY.", final=True)
                cast = [cast_sheet(c) for c in self._library(s, gen.user_id) if c["id"] in (gen.cast_ids or [])]
                job_id = client.plan(gen.prompt, gen.aspect, f"gen-{gen.id}-plan-{uuid.uuid4().hex[:6]}",
                                     len(gen.scenes), cast=cast, style=gen.style or "", language=gen.language or "")
            except GeneratorError as e:
                pause = _worker_pause(client)
                if pause:
                    self._pause(gen, pause)
                else:
                    gen.status, gen.error = GenStatus.FAILED, f"Gemini couldn't plan the scenes: {e.message}"
                return
            gen.worker_job_id, gen.submitted_at, gen.poll_errors = job_id, utcnow(), 0
            gen.status = GenStatus.PLANNING
            log.info("generation %s planning %d scenes as %s", gen_id, len(gen.scenes), job_id)

    def _pause(self, gen: Generation, pause: dict) -> None:
        """Gemini stopped the whole account: keep finished scenes and wait until the user resumes this video."""
        done, total = _scene_counts(gen)
        gen.status, gen.worker_job_id, gen.submitted_at, gen.poll_errors = GenStatus.PAUSED, None, None, 0
        if pause.get("kind") == "limit_reached":
            gen.error = "Gemini's video limit has been reached. Click Resume on this video once the limit resets."
        else:
            gen.error = (pause.get("message") or "Gemini stopped generating.") + " Then click Resume on this video."
        if total > 1 and done:
            gen.error += f" {done} of {total} scenes are done and kept."
        log.info("generation %s paused (%s)", gen.id, pause.get("kind"))

    def cancel(self, gen_id: int, user_id: int) -> None:
        with self.db.session() as s:
            gen = self._get(s, gen_id, user_id)
            if gen.status in (GenStatus.PLANNING, GenStatus.GENERATING) and gen.worker_job_id:
                client = self.p.generator()
                if client:
                    client.cancel(gen.worker_job_id)
            elif gen.status == GenStatus.READY and gen.drive_file_id:
                storage = self.p.storage(user_id)
                if storage is None:
                    raise UserError("Reconnect Google Drive to move this video to the Drive trash.")
                try:
                    storage.delete(gen.drive_file_id)
                except StorageError as e:
                    raise UserError(e.message, 502) from e
            elif gen.status not in (GenStatus.SAVING, GenStatus.SAVE_FAILED, GenStatus.PAUSED, GenStatus.REVIEW):
                raise UserError("This video can't be cancelled now.")
            self._drop_temp(gen)
            self._drop_scenes(gen)
            gen.status, gen.error = GenStatus.CANCELLED, None

    def retry(self, gen_id: int, user_id: int) -> None:
        with self.db.session() as s:
            gen = self._get(s, gen_id, user_id)
            if gen.status not in (GenStatus.FAILED, GenStatus.CANCELLED):
                raise UserError("Only failed or cancelled videos can be retried.")
            gen.status, gen.error, gen.dismissed = GenStatus.GENERATING, None, False
            gen.worker_job_id = gen.final_prompt = gen.drive_file_id = None
            gen.poll_errors = gen.upload_attempts = 0
            gen.submitted_at = None
            for scene in gen.scenes:  # finished clips that are still on disk aren't made (or paid for) again
                if scene.clip_path and not Path(scene.clip_path).exists():
                    scene.clip_path = None
        self._continue(gen_id)

    def resume(self, gen_id: int, user_id: int) -> None:
        """Continue a paused video from its next scene. Paused videos never restart on their own."""
        client = self.p.generator()
        with self.db.session() as s:
            gen = self._get(s, gen_id, user_id)
            if gen.status != GenStatus.PAUSED:
                raise UserError("Only paused videos can be resumed.")
            if client is None:
                raise UserError("Video generation is off: set WORKER_URL and WORKER_API_KEY in .env.", 503)
            if _worker_pause(client):  # lift the worker's pause too, so one click is enough
                try:
                    client.gemini("POST", "resume")
                except GeneratorError as e:
                    raise UserError(e.message, 502) from e
            gen.status, gen.error = GenStatus.GENERATING, None
        log.info("generation %s resumed by the user", gen_id)
        self._continue(gen_id)

    def save_again(self, gen_id: int, user_id: int) -> None:
        with self.db.session() as s:
            gen = self._get(s, gen_id, user_id)
            if gen.status != GenStatus.SAVE_FAILED:
                raise UserError("This video isn't waiting to be saved.")
            if not gen.temp_path or not Path(gen.temp_path).exists():
                raise UserError("The local copy is gone; use Retry to generate it again.")
            gen.status, gen.error = GenStatus.SAVING, None
            gen.upload_attempts, gen.next_upload_at = 0, utcnow()

    def dismiss(self, gen_id: int, user_id: int) -> None:
        with self.db.session() as s:
            gen = self._get(s, gen_id, user_id)
            if gen.status in ACTIVE:
                raise UserError("Cancel it first; it's still in progress.")
            gen.dismissed = True

    def restore(self, gen_id: int, user_id: int) -> None:
        with self.db.session() as s:
            self._get(s, gen_id, user_id).dismissed = False

    def delete(self, gen_id: int, user_id: int) -> None:
        """Remove from history (and local leftovers). The Drive copy, if any, is left alone."""
        with self.db.session() as s:
            gen = self._get(s, gen_id, user_id)
            if gen.status in ACTIVE:
                raise UserError("Cancel it first; it's still in progress.")
            self._drop_temp(gen)
            self._drop_scenes(gen)
            shutil.rmtree(self.settings.refs_dir / str(gen.id), ignore_errors=True)
            s.delete(gen)

    def history(self, user_id: int, q: str = "", status: str = "all", page: int = 1,
                per_page: int = 20) -> tuple[list[Generation], int]:
        """All of the user's generations (dismissed too), newest first, filtered and paged."""
        cond = [Generation.user_id == user_id]
        if q:
            literal = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            cond.append(Generation.prompt.ilike(f"%{literal}%", escape="\\"))
        if status in HISTORY_FILTERS:
            cond.append(Generation.status.in_(HISTORY_FILTERS[status]))
        with self.db.session() as s:
            total = s.scalar(select(func.count()).select_from(Generation).where(*cond))
            rows = s.scalars(select(Generation).where(*cond).order_by(Generation.id.desc())
                             .offset((page - 1) * per_page).limit(per_page)).all()
        return list(rows), total

    # ---- background ------------------------------------------------------
    def tick(self) -> None:
        with self.db.session() as s:
            ids = s.scalars(select(Generation.id).where(Generation.status.in_(list(ACTIVE)))).all()
        for gen_id in ids:
            try:
                self._advance(gen_id)
            except Exception:
                log.exception("generation %s tick failed", gen_id)

    def _advance(self, gen_id: int) -> None:
        with self.db.session() as s:
            status = s.get(Generation, gen_id).status
        if status == GenStatus.PLANNING:
            self._poll_plan(gen_id)
        elif status == GenStatus.GENERATING:
            self._poll(gen_id)
        elif status == GenStatus.SAVING:
            self._upload(gen_id)

    def _poll_plan(self, gen_id: int) -> None:
        client = self.p.generator()
        with self.db.session() as s:
            gen = s.get(Generation, gen_id)
            no_job = not gen.worker_job_id
        if no_job:  # crash before the plan was handed over
            self._continue(gen_id)
            return
        with self.db.session() as s:
            gen = s.get(Generation, gen_id)
            st = self._check(gen, client)
            if st is None or st.status in ("queued", "running"):
                return
            if st.status == "failed":
                pause = _worker_pause(client)
                if pause:
                    self._pause(gen, pause)
                else:
                    gen.status = GenStatus.FAILED
                    gen.error = f"Gemini couldn't plan the scenes: {st.error or 'no reason given.'}"
                return
            items = [x for x in (st.scenes or []) if isinstance(x, dict) or (isinstance(x, str) and x.strip())]
            if len(items) < len(gen.scenes):
                gen.status, gen.error = GenStatus.FAILED, "Gemini didn't write all the scenes. Retry it."
                return
            cast = [c for c in self._library(s, gen.user_id) if c["id"] in (gen.cast_ids or [])]
            for scene, item in zip(gen.scenes, items):
                sc = normalize_scene(item, cast)
                for key, value in sc.items():
                    setattr(scene, key, value)
                scene.prompt = "" if gen.review else compile_prompt(sc, cast, gen.style, gen.language)[:MAX_PROMPT]
            gen.worker_job_id, gen.submitted_at, gen.poll_errors = None, None, 0
            gen.status = GenStatus.REVIEW if gen.review else GenStatus.GENERATING
            log.info("generation %s planned", gen_id)
            if gen.review:
                return
        self._continue(gen_id)

    def _check(self, gen: Generation, client):
        """The worker's status for the generation's current job, or None after recording a problem
        (FAILED on a final error, a lost worker or a timeout; a transient error just counts)."""
        if client is None:
            gen.status, gen.error = GenStatus.FAILED, "Video generation is off: WORKER_URL/WORKER_API_KEY not set."
            return None
        try:
            st = client.check(gen.worker_job_id)
        except GeneratorError as e:
            if e.final:
                gen.status, gen.error = GenStatus.FAILED, e.message
                return None
            gen.poll_errors += 1
            if gen.poll_errors >= MAX_POLL_ERRORS:
                gen.status, gen.error = GenStatus.FAILED, f"Lost contact with the video worker. ({e.message})"
            return None
        gen.poll_errors = 0
        if st.status in ("queued", "running") and gen.submitted_at and utcnow() - gen.submitted_at > JOB_TIMEOUT:
            client.cancel(gen.worker_job_id)
            gen.status, gen.error = GenStatus.FAILED, "The video didn't finish in time."
            return None
        return st

    def _poll(self, gen_id: int) -> None:
        client = self.p.generator()
        with self.db.session() as s:
            gen = s.get(Generation, gen_id)
            if client is None:
                gen.status, gen.error = GenStatus.FAILED, "Video generation is off: WORKER_URL/WORKER_API_KEY not set."
                return
            no_job = not gen.worker_job_id
        if no_job:  # between scenes, or a crash mid-submit/mid-join: carry on from the database state
            self._continue(gen_id)
            return
        with self.db.session() as s:
            gen = s.get(Generation, gen_id)
            scene = _next_scene(gen)
            if scene is None:  # every scene already has its clip; this job is stale
                gen.worker_job_id = None
                return
            st = self._check(gen, client)
            if st is None:
                return
            if st.final_prompt:
                gen.final_prompt = st.final_prompt
            if st.status == "failed":
                pause = _worker_pause(client)
                if pause:
                    self._pause(gen, pause)
                    return
                gen.status, gen.error = GenStatus.FAILED, _scene_error(gen, scene, st.error or "The video couldn't be made.")
                log.info("generation %s failed at worker", gen_id)
                return
            if st.status in ("queued", "running"):
                return
            if not st.video_url:
                gen.status, gen.error = GenStatus.FAILED, "The worker finished but gave no video link."
                return
            dest = self._scene_dir(gen) / f"scene-{scene.position}.mp4"
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                size = client.download(st.video_url, dest)
            except GeneratorError as e:
                if e.final:
                    gen.status, gen.error = GenStatus.FAILED, e.message
                else:
                    gen.poll_errors += 1
                    if gen.poll_errors >= MAX_POLL_ERRORS:
                        gen.status, gen.error = GenStatus.FAILED, e.message
                return
            scene.clip_path = str(dest)
            gen.worker_job_id, gen.submitted_at, gen.poll_errors = None, None, 0
            log.info("generation %s scene %d downloaded (%d bytes)", gen_id, scene.position, size)
        self._continue(gen_id)

    def _join(self, gen_id: int) -> None:
        with self.db.session() as s:
            gen = s.get(Generation, gen_id)
            paths = [Path(sc.clip_path) for sc in gen.scenes]
            if not all(p.exists() for p in paths):
                gen.status, gen.error = GenStatus.FAILED, "A finished scene went missing before joining. Retry it."
                return
            dest = self.settings.generated_dir / f"{gen.id}-{uuid.uuid4().hex}.mp4"
        try:  # outside the session: a long ffmpeg run mustn't hold a database transaction
            size = join_clips(paths, dest)
        except JoinError as e:
            with self.db.session() as s:
                gen = s.get(Generation, gen_id)
                gen.status, gen.error = GenStatus.FAILED, f"{e.message} Retry to join them again."
            log.info("generation %s join failed", gen_id)
            return
        with self.db.session() as s:
            gen = s.get(Generation, gen_id)
            self._drop_scenes(gen)
            gen.temp_path, gen.file_size = str(dest), size
            gen.status, gen.upload_attempts, gen.next_upload_at = GenStatus.SAVING, 0, utcnow()
            log.info("generation %s joined %d scene(s) (%d bytes)", gen_id, len(paths), size)
        self._upload(gen_id)

    def _upload(self, gen_id: int) -> None:
        with self.db.session() as s:
            user_id = s.get(Generation, gen_id).user_id
        storage = self.p.storage(user_id)
        with self.db.session() as s:
            gen = s.get(Generation, gen_id)
            if gen.next_upload_at and gen.next_upload_at > utcnow():
                return
            temp = Path(gen.temp_path) if gen.temp_path else None
            if temp is None or not temp.exists():
                gen.status, gen.error = GenStatus.FAILED, "The downloaded video went missing before it was saved."
                return
            if storage is None:
                gen.status = GenStatus.SAVE_FAILED
                gen.error = "Google Drive isn't connected. Connect it in Settings, then Try saving again."
                return
            sub, name = drive_name(gen, gen.created_at)
            try:
                stored = storage.save(temp, name, sub)
            except StorageError as e:
                gen.upload_attempts += 1
                if not e.retryable or gen.upload_attempts >= MAX_UPLOAD_ATTEMPTS:
                    gen.status, gen.error = GenStatus.SAVE_FAILED, e.message
                else:
                    gen.error = f"Saving to Drive failed, retrying… ({e.message})"
                    gen.next_upload_at = utcnow() + UPLOAD_RETRY
                log.info("generation %s upload attempt %d failed", gen_id, gen.upload_attempts)
                return
            # remember the folder we used (default folder gets created on first upload)
            conn = s.scalar(select(GoogleConnection).where(GoogleConnection.user_id == user_id))
            if conn and storage.folder_id and conn.folder_id != storage.folder_id:
                conn.folder_id = storage.folder_id
                conn.folder_name = conn.folder_name or storage.folder_name
            gen.drive_file_id, gen.file_size = stored.file_id, stored.size
            gen.status, gen.error = GenStatus.READY, None
            self._drop_temp(gen)
            log.info("generation %s ready", gen_id)

    # ---- script ----------------------------------------------------------
    def script(self, gen_id: int, user_id: int) -> tuple[Generation, list[dict]]:
        """The generation (with its scenes) and the user's character library, for the script editor."""
        with self.db.session() as s:
            gen = self._get(s, gen_id, user_id)
            return gen, self._library(s, user_id)

    def save_script(self, gen_id: int, user_id: int, scenes: list, style: str = "", language: str = "",
                    approve: bool = False) -> list[dict]:
        """Store the edited script of a video at REVIEW and return each scene's compiled prompt and warnings.
        With `approve`, the prompts are fixed and the clips start."""
        if not isinstance(scenes, list) or not 1 <= len(scenes) <= MAX_SCENES:
            raise UserError(f"A script has 1 to {MAX_SCENES} scenes.")
        style, language = clean_text(style, 300), clean_text(language, 40)
        with self.db.session() as s:
            gen = self._get(s, gen_id, user_id)
            if gen.status != GenStatus.REVIEW:
                raise UserError("This script can't be changed now.")
            library = self._library(s, user_id)
            script = [normalize_scene(raw, library) for raw in scenes]
            prompts = [compile_prompt(sc, library, style, language) for sc in script]
            if approve:
                for i, (sc, prompt) in enumerate(zip(script, prompts), 1):
                    if not (sc["action"] or sc["dialogue"]):
                        raise UserError(f"Scene {i} is empty: describe what happens in it.")
                    if len(prompt) > MAX_PROMPT:
                        raise UserError(f"Scene {i}'s clip prompt is {len(prompt):,} characters; "
                                        f"shorten it to under {MAX_PROMPT:,}.")
            gen.style, gen.language = style, language
            used = [p["id"] for sc in script for p in sc["characters"] + sc["dialogue"] if p["id"]]
            gen.cast_ids = list(dict.fromkeys([*(gen.cast_ids or []), *used]))
            while len(gen.scenes) > len(script):
                gen.scenes.pop()
            for i, (sc, prompt) in enumerate(zip(script, prompts), 1):
                if i > len(gen.scenes):
                    gen.scenes.append(Scene(position=i, prompt=""))
                row = gen.scenes[i - 1]
                for key, value in sc.items():
                    setattr(row, key, value)
                row.prompt = prompt if approve else ""
            if approve:
                gen.status, gen.error = GenStatus.GENERATING, None
            out = [{"prompt": p, "length": len(p), "warnings": scene_warnings(sc, p)} for sc, p in zip(script, prompts)]
        if approve:
            log.info("generation %s script approved (%d scenes)", gen_id, len(script))
            self._continue(gen_id)
        return out

    def _scene_row(self, position: int, raw: dict, cast: list[dict], style: str, language: str,
                   compile_now: bool) -> Scene:
        sc = normalize_scene(raw, cast)
        prompt = compile_prompt(sc, cast, style, language)[:MAX_PROMPT] if compile_now else ""
        return Scene(position=position, prompt=prompt, **sc)

    def _library(self, s, user_id: int) -> list[dict]:
        rows = s.scalars(select(Character).where(Character.user_id == user_id).order_by(Character.name)).all()
        return [c.sheet() for c in rows]

    # ---- helpers ---------------------------------------------------------
    def _get(self, s, gen_id: int, user_id: int) -> Generation:
        gen = s.get(Generation, gen_id)
        if gen is None or gen.user_id != user_id:
            raise UserError("Not found.", 404)
        return gen

    def _drop_temp(self, gen: Generation) -> None:
        if gen.temp_path:
            Path(gen.temp_path).unlink(missing_ok=True)
            gen.temp_path = None

    def _scene_dir(self, gen: Generation) -> Path:
        return self.settings.generated_dir / f"{gen.id}-scenes"

    def _drop_scenes(self, gen: Generation) -> None:
        shutil.rmtree(self._scene_dir(gen), ignore_errors=True)
        for scene in gen.scenes:
            scene.clip_path = None


def _next_scene(gen: Generation) -> Scene | None:
    return next((sc for sc in gen.scenes if not sc.clip_path), None)


def _scene_counts(gen: Generation) -> tuple[int, int]:
    return sum(1 for sc in gen.scenes if sc.clip_path), len(gen.scenes)


def _scene_error(gen: Generation, scene: Scene | None, message: str) -> str:
    return f"Scene {scene.position}: {message}" if scene and len(gen.scenes) > 1 else message


def _worker_pause(client) -> dict | None:
    """The worker's account pause (limit reached, signed out, …), or None if it isn't paused or can't be asked."""
    if client is None:
        return None
    health = client.ping()
    return (health.details.get("account_paused") or None) if health.ok else None
