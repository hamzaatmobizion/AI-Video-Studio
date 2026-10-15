"""Generation lifecycle: submit → poll worker → download → save to Drive → READY.

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
from app.models import Generation, GoogleConnection, utcnow

log = logging.getLogger("app.generation")

JOB_TIMEOUT = timedelta(minutes=30)
MAX_POLL_ERRORS = 10
HISTORY_FILTERS = {
    "ready": [GenStatus.READY],
    "failed": [GenStatus.FAILED],
    "cancelled": [GenStatus.CANCELLED],
    "in_progress": [GenStatus.GENERATING, GenStatus.SAVING, GenStatus.SAVE_FAILED],
}
MAX_UPLOAD_ATTEMPTS = 10
UPLOAD_RETRY = timedelta(seconds=30)


class UserError(Exception):
    """A problem to show the user as-is (HTTP 400/503)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def drive_name(gen: Generation, when: datetime) -> tuple[str, str]:
    """(subfolder, filename) → <YYYY-MM>/<date> <prompt> (<id>).mp4"""
    words = re.sub(r"[\\/:*?\"<>|\n\r\t]+", " ", gen.prompt).strip()
    words = re.sub(r"\s+", " ", words)[:60].rstrip() or "video"
    return when.strftime("%Y-%m"), f"{when:%Y-%m-%d} {words} ({gen.id}).mp4"


class GenerationService:
    def __init__(self, providers: Providers):
        self.p = providers
        self.db = providers.db
        self.settings = providers.settings

    # ---- user actions ----------------------------------------------------
    def create(self, user_id: int, prompt: str, aspect: str, images: list[tuple[str, bytes]]) -> int:
        """`images` already validated by content: [(ext, bytes)]."""
        if self.p.generator() is None:
            raise UserError("Video generation is off: set WORKER_URL and WORKER_API_KEY in .env.", 503)
        if self.p.storage(user_id) is None:
            raise UserError("Connect Google Drive in Settings first, so the video has somewhere to go.", 503)
        with self.db.session() as s:
            gen = Generation(user_id=user_id, prompt=prompt, aspect=aspect, ref_images=[], status=GenStatus.GENERATING,
                             poll_errors=0, upload_attempts=0, dismissed=False)
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
        self._submit(gen_id)
        return gen_id

    def _submit(self, gen_id: int) -> None:
        gen_client = self.p.generator()
        with self.db.session() as s:
            gen = s.get(Generation, gen_id)
            try:
                if gen_client is None:
                    raise GeneratorError("Video generation is off: set WORKER_URL and WORKER_API_KEY.", final=True)
                images = [Path(p) for p in gen.ref_images if Path(p).exists()]
                job_id = gen_client.submit(gen.prompt, gen.aspect, f"gen-{gen.id}-{uuid.uuid4().hex[:6]}", images)
            except GeneratorError as e:
                gen.status, gen.error = GenStatus.FAILED, e.message
                log.info("generation %s submit failed", gen_id)
                return
            gen.worker_job_id = job_id
            gen.submitted_at = utcnow()
            gen.status = GenStatus.GENERATING
            log.info("generation %s submitted as %s", gen_id, job_id)

    def cancel(self, gen_id: int, user_id: int) -> None:
        with self.db.session() as s:
            gen = self._get(s, gen_id, user_id)
            if gen.status == GenStatus.GENERATING and gen.worker_job_id:
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
            elif gen.status not in (GenStatus.SAVING, GenStatus.SAVE_FAILED):
                raise UserError("This video can't be cancelled now.")
            self._drop_temp(gen)
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
        self._submit(gen_id)

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
        if status == GenStatus.GENERATING:
            self._poll(gen_id)
        elif status == GenStatus.SAVING:
            self._upload(gen_id)

    def _poll(self, gen_id: int) -> None:
        client = self.p.generator()
        with self.db.session() as s:
            gen = s.get(Generation, gen_id)
            if client is None:
                gen.status, gen.error = GenStatus.FAILED, "Video generation is off: WORKER_URL/WORKER_API_KEY not set."
                return
            if not gen.worker_job_id:  # submit never succeeded (e.g. crash mid-submit)
                gen.status, gen.error = GenStatus.FAILED, "The video was never handed to the worker. Retry it."
                return
            try:
                st = client.check(gen.worker_job_id)
            except GeneratorError as e:
                if e.final:
                    gen.status, gen.error = GenStatus.FAILED, e.message
                    return
                gen.poll_errors += 1
                if gen.poll_errors >= MAX_POLL_ERRORS:
                    gen.status, gen.error = GenStatus.FAILED, f"Lost contact with the video worker. ({e.message})"
                return
            gen.poll_errors = 0
            if st.final_prompt:
                gen.final_prompt = st.final_prompt
            if st.status == "failed":
                gen.status, gen.error = GenStatus.FAILED, st.error or "The video couldn't be made."
                log.info("generation %s failed at worker", gen_id)
                return
            if st.status in ("queued", "running"):
                if gen.submitted_at and utcnow() - gen.submitted_at > JOB_TIMEOUT:
                    client.cancel(gen.worker_job_id)
                    gen.status, gen.error = GenStatus.FAILED, "The video didn't finish in time."
                return
            if not st.video_url:
                gen.status, gen.error = GenStatus.FAILED, "The worker finished but gave no video link."
                return
            self.settings.generated_dir.mkdir(parents=True, exist_ok=True)
            dest = self.settings.generated_dir / f"{gen.id}-{uuid.uuid4().hex}.mp4"
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
            gen.temp_path, gen.file_size = str(dest), size
            gen.status, gen.upload_attempts, gen.next_upload_at = GenStatus.SAVING, 0, utcnow()
            log.info("generation %s downloaded (%d bytes)", gen_id, size)
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
