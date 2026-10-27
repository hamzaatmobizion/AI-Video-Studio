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
from app.genres import plan_rules, valid_genre
from app.timeline import script_from_timeline
from app.interfaces import GeneratorError, StorageError
from app.models import Character, Generation, GoogleConnection, Scene, utcnow
from app.script import (CONTINUE_FROM, MAX_PROMPT, MAX_SCENE_IMAGES, STARTS_FROM, cast_sheet, is_structured,
                        merged_sheet,
                        still_prompt, compile_prompt, ensure_cast, normalize_bible, normalize_scene,
                        scene_images, scene_warnings, clean_text, style_from_prompt)
from app.video_join import JoinError, clip_seconds, frame_at, join_clips, last_frame

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
# What each failed clip check means, for the note under the clip
CHECK_LABELS = {"same_people": "different person", "same_clothes": "different clothes", "same_place": "different place",
                "normal_bodies": "extra or missing limbs", "story_happened": "story didn't happen",
                "right_speakers": "wrong speaker", "lips_in_sync": "lips out of sync", "same_voices": "voice changed"}
CHECK_STILLS = (0.15, 0.4, 0.65, 0.9)  # where in a clip its check stills are taken
SCENE_KEEP = timedelta(days=7)  # a finished video's scene clips stay this long, to watch or remake one scene
SWEEP_EVERY = timedelta(hours=1)


class UserError(Exception):
    """A problem to show the user as-is (HTTP 400/503)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def drive_name(gen: Generation, when: datetime) -> tuple[str, str]:
    """(subfolder, filename) → <YYYY-MM>/<date> <prompt> (<id>).mp4"""
    first = gen.scenes[0] if gen.scenes and not gen.planned else None
    text = (first.action or first.prompt) if first else gen.prompt  # the user's words, not the compiled prompt
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
               review: bool = False, genre: str = "", music: tuple[str, bytes] | None = None,
               lock_frames: bool = False, check_clips: bool = False) -> int:
        """`scenes`: one prompt per ~10 s clip, already validated. With `plan` = N, `scenes` is the user's
        one prompt and Gemini first writes an N-scene script from it. `images` validated by content: [(ext, bytes)].
        `cast_ids`: the user's characters to use; `review`: wait for the user to approve the script;
        `genre`: the video type (app/genres.py), read from the prompt when it isn't one. `music`: (ext, bytes) of
        one track laid under the whole video; `lock_frames`, `check_clips`: the slower consistency options."""
        if self.p.generator() is None:
            raise UserError("Video generation is off: set WORKER_URL and WORKER_API_KEY in .env.", 503)
        if self.p.storage(user_id) is None:
            raise UserError("Connect Google Drive in Settings first, so the video has somewhere to go.", 503)
        style, language = clean_text(style, 300), clean_text(language, 40)
        genre = valid_genre(genre, "\n".join(scenes))
        track, music = music, ((plan or len(scenes)) == 1 or genre == "music") and not music  # as _prompt_options
        with self.db.session() as s:
            library = self._library(s, user_id)
            known = {c["id"] for c in library}
            cast_ids = list(dict.fromkeys(cast_ids or []))
            if any(cid not in known for cid in cast_ids):
                raise UserError("One of the chosen characters doesn't exist any more. Reload the page.")
            cast = [c for c in library if c["id"] in cast_ids]
            if plan:
                prompt, rows = scenes[0], [Scene(position=i, prompt="") for i in range(1, plan + 1)]
                style = style or style_from_prompt(prompt)  # a style written in the prompt reaches every scene
            else:
                prompt = scenes[0] if len(scenes) == 1 else "\n\n".join(f"Scene {i}: {p}" for i, p in enumerate(scenes, 1))
                opts = {"genre": genre, "music": music}
                rows = [self._scene_row(i, {"action": text, "characters": [{"id": c["id"]} for c in cast]}, cast,
                                        style, language, aspect, compile_now=not review, **opts)
                        if cast or style or review
                        else Scene(position=i, action=text,
                                   prompt=compile_prompt({"action": text}, [], aspect=aspect, fit=True, **opts))
                        for i, text in enumerate(scenes, 1)]
            gen = Generation(user_id=user_id, prompt=prompt, aspect=aspect, ref_images=[], status=GenStatus.GENERATING,
                             poll_errors=0, upload_attempts=0, dismissed=False, planned=bool(plan),
                             review=review, cast_ids=cast_ids, style=style, language=language, genre=genre,
                             bible={}, scenes=rows, lock_frames=lock_frames, check_clips=check_clips)
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
            if track:
                d = self.settings.refs_dir / str(gen.id)
                d.mkdir(parents=True, exist_ok=True)
                (d / f"music.{track[0]}").write_bytes(track[1])
                gen.music_path = str(d / f"music.{track[0]}")
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
                paths, pictured, names, library = gen.ref_images, 0, [], self._library(s, gen.user_id)
                if scene.characters:  # the pictures of who is in this shot first, then the video's own uploads
                    names = _pictured_names(scene, library)
                    pictured = len(names)
                    paths = scene_images(scene.script(), library, gen.ref_images)
                prompt = scene.prompt
                frame = self._previous_frame(gen, scene)
                ref = f"gen-{gen.id}-s{scene.position}-{uuid.uuid4().hex[:6]}"
                if gen.lock_frames and scene.still_path is None and frame is None and 0 < pictured < MAX_SCENE_IMAGES:
                    # "Lock characters", first scene: draw its opening frame from the pictures, then animate it. Later
                    # scenes start from the previous clip's real last frame: a redraw of it would change things.
                    images = [Path(p) for p in paths[:pictured]] + ([frame] if frame else [])
                    job_id = gen_client.still(still_prompt(scene.script(), library, gen.style, gen.bible), gen.aspect,
                                              ref, images, names, previous=bool(frame))
                    gen.worker_job_id, gen.job_kind, gen.submitted_at, gen.poll_errors = \
                        job_id, f"still:{scene.position}", utcnow(), 0
                    gen.status = GenStatus.GENERATING
                    log.info("generation %s scene %d still submitted as %s", gen_id, scene.position, job_id)
                    return
                extra = []  # lines added now; the scene's action is shortened to make room, never these
                start = Path(scene.still_path) if scene.still_path and Path(scene.still_path).exists() else None
                lead = start or frame  # the scene's own first frame, else the previous clip's last one
                if lead and pictured < MAX_SCENE_IMAGES:
                    paths = [*paths[:pictured], str(lead), *paths[pictured:]][:MAX_SCENE_IMAGES]
                    extra.append((STARTS_FROM if start or gen.lock_frames else CONTINUE_FROM).format(n=pictured + 1))
                if scene.redos and scene.check_note.startswith("Remade once: "):  # say what went wrong last time
                    extra.append(f"Fix from the last try: {_fit_text(scene.check_note[13:].rstrip('.'), 300)}.")
                room = MAX_PROMPT - len("\n".join(extra)) - 1
                if extra and len(prompt) > room:
                    prompt = compile_prompt(scene.script(), library, gen.style, gen.language, gen.aspect, fit=True,
                                            room=room, **_prompt_options(gen))
                prompt = "\n".join([prompt, *extra])[:MAX_PROMPT]
                images = [Path(p) for p in paths if Path(p).exists()]
                job_id = gen_client.submit(prompt, gen.aspect, ref, images)
            except GeneratorError as e:
                pause = _worker_pause(gen_client)
                if pause:
                    self._pause(gen, pause)
                else:
                    gen.status, gen.error = GenStatus.FAILED, _scene_error(gen, scene, e.message)
                    log.info("generation %s submit failed", gen_id)
                return
            gen.worker_job_id, gen.job_kind = job_id, ""
            gen.submitted_at = utcnow()
            gen.poll_errors = 0
            gen.status = GenStatus.GENERATING
            log.info("generation %s scene %d submitted as %s", gen_id, scene.position, job_id)

    def _submit_check(self, gen: Generation, scene: Scene, library: list[dict]) -> bool:
        """Ask Gemini to watch the new clip, sound included, and check it against the characters' pictures, the
        previous clip (place, clothes, voices) and what the scene should show and say. False when the check
        couldn't be sent (the clip is kept)."""
        client = self.p.generator()
        names = _pictured_names(scene, library)
        pictures = [Path(p) for p in scene_images(scene.script(), library, [])[:len(names)]]
        before = next((sc for sc in gen.scenes if sc.position == scene.position - 1), None)
        previous = Path(before.clip_path) if before and before.clip_path and Path(before.clip_path).exists() else None
        where = {p["name"]: p.get("position") or "" for p in scene.characters or []}
        lines = [{"name": d["name"], "line": d["line"], "position": where.get(d["name"], "")}
                 for d in scene.dialogue or []]
        clip = Path(scene.clip_path)
        if client is None or not clip.exists():
            return False
        # stills of the new clip too: watching a video, Gemini samples few frames and misses a passing extra hand
        length = clip_seconds(clip)
        stills = [frame_at(clip, clip.with_name(f"{clip.stem}-check-{i}.jpg"), length * at)
                  for i, at in enumerate(CHECK_STILLS, 1)] if length else []
        # clothes the prompt asks for instead of a pictured character's own (app/script.py merged_sheet)
        by_name = {c["name"]: c for c in library}
        outfits = {n: merged_sheet(by_name[n], gen.bible).get("from_prompt", {}).get("outfit", "")
                   for n in names if n in by_name}
        try:
            job_id = client.review_clip(names, scene.action or "", lines, f"gen-{gen.id}-c{scene.position}",
                                        pictures, clip, previous, {n: o for n, o in outfits.items() if o},
                                        [p for p in stills if p])
        except GeneratorError as e:
            scene.check_note = f"Couldn't check this clip ({e.message})"
            return False
        gen.worker_job_id, gen.job_kind, gen.submitted_at, gen.poll_errors = \
            job_id, f"check:{scene.position}", utcnow(), 0
        log.info("generation %s scene %d check submitted as %s", gen.id, scene.position, job_id)
        return True

    def _submit_plan(self, gen_id: int) -> None:
        client = self.p.generator()
        with self.db.session() as s:
            gen = s.get(Generation, gen_id)
            try:
                if client is None:
                    raise GeneratorError("Video generation is off: set WORKER_URL and WORKER_API_KEY.", final=True)
                cast = [cast_sheet(c) for c in self._library(s, gen.user_id) if c["id"] in (gen.cast_ids or [])]
                job_id = client.plan(gen.prompt, gen.aspect, f"gen-{gen.id}-plan-{uuid.uuid4().hex[:6]}",
                                     len(gen.scenes), cast=cast, style=gen.style or "", language=gen.language or "",
                                     rules=plan_rules(gen.genre or "general", len(gen.scenes)))
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
    def redo_scene(self, gen_id: int, user_id: int, position: int) -> None:
        """Make one scene again and rejoin the video, keeping every other scene's clip. A saved video stays in Drive
        until the new one is saved, then goes to the Drive trash."""
        with self.db.session() as s:
            gen = self._get(s, gen_id, user_id)
            if gen.status not in (GenStatus.READY, GenStatus.FAILED):
                raise UserError("A scene can be remade once the video is ready (or has failed).")
            scene = next((sc for sc in gen.scenes if sc.position == position), None)
            if scene is None or not scene.prompt:
                raise UserError("That scene doesn't exist, or has no clip prompt yet.")
            others = [sc for sc in gen.scenes if sc is not scene]
            if any(sc.clip_path and not Path(sc.clip_path).exists() for sc in others):  # made, but no longer kept
                raise UserError("The other scenes' clips are no longer kept, so the whole video has to be retried.")
            if scene.clip_path:
                Path(scene.clip_path).unlink(missing_ok=True)
                Path(scene.clip_path).with_name(f"{Path(scene.clip_path).stem}-last.jpg").unlink(missing_ok=True)
            scene.clip_path, scene.still_path, scene.check_note, scene.redos = None, None, "", 0
            if is_structured(scene.script()) or gen.style:  # rebuilt, so the remake gets today's rules too
                scene.prompt = compile_prompt(scene.script(), self._library(s, user_id), gen.style, gen.language,
                                              gen.aspect, fit=True, **_prompt_options(gen))
            if gen.drive_file_id:
                gen.old_drive_file_id, gen.drive_file_id = gen.drive_file_id, None
            self._drop_temp(gen)
            gen.status, gen.error, gen.dismissed = GenStatus.GENERATING, None, False
            gen.worker_job_id, gen.job_kind, gen.submitted_at, gen.poll_errors = None, "", None, 0
            gen.upload_attempts = 0
        log.info("generation %s: remaking scene %d", gen_id, position)
        self._continue(gen_id)

    def scene_clip(self, gen_id: int, user_id: int, position: int) -> Path | None:
        """The file of one scene's clip, if it's still kept."""
        with self.db.session() as s:
            gen = self._get(s, gen_id, user_id)
            scene = next((sc for sc in gen.scenes if sc.position == position), None)
            path = Path(scene.clip_path) if scene and scene.clip_path else None
        return path if path and path.exists() else None

    def _sweep(self) -> None:
        """Drop the scene clips of videos finished more than SCENE_KEEP ago (at most once an hour)."""
        now = utcnow()
        if getattr(self, "_swept_at", None) and now - self._swept_at < SWEEP_EVERY:
            return
        self._swept_at = now
        with self.db.session() as s:
            old = s.scalars(select(Generation).where(Generation.status.in_([GenStatus.READY, GenStatus.FAILED,
                                                                             GenStatus.CANCELLED]),
                                                     Generation.updated_at < now - SCENE_KEEP)).all()
            for gen in old:
                if any(sc.clip_path for sc in gen.scenes):
                    self._drop_scenes(gen)

    def tick(self) -> None:
        try:
            self._sweep()
        except Exception:
            log.exception("scene sweep failed")
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
            cast = [c for c in self._library(s, gen.user_id) if c["id"] in (gen.cast_ids or [])]
            if st.status == "failed":
                pause = _worker_pause(client)
                if pause:
                    self._pause(gen, pause)
                    return
                # Gemini's chat failed on every attempt: a prompt that already sets out its timeline is split into
                # the scenes here, so the video still gets made. A refusal is never worked around.
                refused = re.search(r"safety|declined|real people", st.error or "", re.I)
                own = None if refused else script_from_timeline(gen.prompt, len(gen.scenes), cast)
                if own is None:
                    gen.status = GenStatus.FAILED
                    gen.error = f"Gemini couldn't plan the scenes: {st.error or 'no reason given.'}"
                    return
                log.info("generation %s: Gemini's planner failed; using the prompt's own timeline", gen_id)
                st.scenes, st.bible = own, {}
            items = [x for x in (st.scenes or []) if isinstance(x, dict) or (isinstance(x, str) and x.strip())]
            if len(items) < len(gen.scenes):
                gen.status, gen.error = GenStatus.FAILED, "Gemini didn't write all the scenes. Retry it."
                return
            gen.bible = normalize_bible(st.bible)
            # Only the characters the story has a role for: a picked one that fits no role is left out, never forced
            # in where Veo would take their picture for someone else.
            script = [ensure_cast(normalize_scene(item, cast), cast, gen.bible) for item in items[:len(gen.scenes)]]
            gen.style = gen.style or gen.bible["style"]  # the look Gemini fixed for every scene, shown in the editor
            for scene, sc in zip(gen.scenes, script):
                for key, value in sc.items():
                    setattr(scene, key, value)
                scene.prompt = "" if gen.review else compile_prompt(sc, cast, gen.style, gen.language, gen.aspect,
                                                                    fit=True, **_prompt_options(gen))
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
            kind = s.get(Generation, gen_id).job_kind or ""
        if kind.startswith(("still:", "check:")):
            self._poll_step(gen_id, kind)
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
                message = st.error or "The video couldn't be made."
                first = _first_version(self._scene_dir(gen) / f"scene-{scene.position}.mp4")
                if scene.redos and first.exists():  # the remake failed: the first version is good enough to keep
                    scene.clip_path = str(first.replace(first.with_name(f"scene-{scene.position}.mp4")))
                    scene.check_note += f" The remake failed ({_fit_text(message, 160)}), so the first version is kept."
                    gen.worker_job_id, gen.submitted_at, gen.poll_errors = None, None, 0
                    log.info("generation %s scene %d remake failed: keeping the first version", gen_id, scene.position)
                    return  # the next tick carries on from here
                if "comes from Gemini" in message:  # its plain error twice in a row on one scene
                    message += (" If this scene keeps failing, Gemini may be refusing it without saying so (it does "
                                "this e.g. when a character could be taken for a child): see the notes under the "
                                "prompt in the Studio.")
                if "real people" in message:  # say whose pictures went with the clip, so the user knows whom to fix
                    pictured = {c["id"]: c["name"] for c in self._library(s, gen.user_id) if c.get("image_path")}
                    names = [pictured[p["id"]] for p in scene.characters or [] if p.get("id") in pictured]
                    if names:
                        message += f" Pictures sent with this scene: {', '.join(names)}."
                gen.status, gen.error = GenStatus.FAILED, _scene_error(gen, scene, message)
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
            _first_version(dest).unlink(missing_ok=True)  # a remake is in: the first version isn't needed
            gen.worker_job_id, gen.submitted_at, gen.poll_errors = None, None, 0
            log.info("generation %s scene %d downloaded (%d bytes)", gen_id, scene.position, size)
            # "Check each clip": once per scene (a remade clip isn't checked again)
            if gen.check_clips and not scene.redos and self._submit_check(gen, scene, self._library(s, gen.user_id)):
                return
        self._continue(gen_id)

    def _poll_step(self, gen_id: int, kind: str) -> None:
        """A scene's locked first frame (still:N) or its clip check (check:N). Neither stops the video when it
        fails: the scene carries on without the still, or keeps its clip unchecked."""
        client = self.p.generator()
        step, _, pos = kind.partition(":")
        with self.db.session() as s:
            gen = s.get(Generation, gen_id)
            scene = next((sc for sc in gen.scenes if sc.position == int(pos or 0)), None)
            st = self._check(gen, client)
            if st is None or st.status in ("queued", "running") or scene is None:
                if scene is None:
                    gen.worker_job_id, gen.job_kind = None, ""
                return
            gen.worker_job_id, gen.job_kind, gen.submitted_at, gen.poll_errors = None, "", None, 0
            if st.status == "failed":
                pause = _worker_pause(client)
                if pause:
                    self._pause(gen, pause)
                    return
                if step == "still":
                    scene.still_path = ""  # tried: the clip goes ahead from the pictures alone
                else:
                    scene.check_note = f"Couldn't check this clip ({st.error or 'no reason given'})"
                log.info("generation %s scene %s %s failed: carrying on", gen_id, pos, step)
            elif step == "still":
                self._scene_dir(gen).mkdir(parents=True, exist_ok=True)
                try:
                    scene.still_path = str(client.download_image(
                        st.image_url, self._scene_dir(gen) / f"scene-{scene.position}-still"))
                except GeneratorError:
                    scene.still_path = ""
            else:
                verdict = st.check or {}
                failed = ", ".join(CHECK_LABELS.get(k, k) for k in verdict.get("failed") or [])
                problems = "; ".join(x for x in (failed, (verdict.get("problems") or "").strip()) if x)
                if verdict.get("ok", True):
                    scene.check_note = "Matches the pictures" + ("" if not problems else f" ({problems})")
                elif not scene.redos:  # one remake, then keep what comes
                    scene.redos += 1
                    scene.check_note = f"Remade once: {problems or 'it did not match the scene'}"
                    clip = Path(scene.clip_path)
                    for p in clip.parent.glob(f"{clip.stem}-*.jpg"):
                        if p.name != f"{clip.stem}-still.jpg":
                            p.unlink(missing_ok=True)
                    clip.replace(_first_version(clip))  # kept until the remake is in, to fall back on
                    scene.clip_path = None
                    log.info("generation %s scene %s failed its check: remaking it", gen_id, pos)
        self._continue(gen_id)

    def _join(self, gen_id: int) -> None:
        with self.db.session() as s:
            gen = s.get(Generation, gen_id)
            paths = [Path(sc.clip_path) for sc in gen.scenes]
            if not all(p.exists() for p in paths):
                gen.status, gen.error = GenStatus.FAILED, "A finished scene went missing before joining. Retry it."
                return
            dest = self.settings.generated_dir / f"{gen.id}-{uuid.uuid4().hex}.mp4"
            music = Path(gen.music_path) if gen.music_path and Path(gen.music_path).exists() else None
        try:  # outside the session: a long ffmpeg run mustn't hold a database transaction
            size = join_clips(paths, dest, music=music)
        except JoinError as e:
            with self.db.session() as s:
                gen = s.get(Generation, gen_id)
                gen.status, gen.error = GenStatus.FAILED, f"{e.message} Retry to join them again."
            log.info("generation %s join failed", gen_id)
            return
        with self.db.session() as s:
            gen = s.get(Generation, gen_id)  # the scene clips stay (SCENE_KEEP), so one scene can be remade
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
            if gen.old_drive_file_id:  # a remade scene: the new video is saved, so the old one goes to the trash
                try:
                    storage.delete(gen.old_drive_file_id)
                except StorageError:
                    log.info("generation %s: couldn't trash the replaced Drive file", gen_id)
                gen.old_drive_file_id = None
            log.info("generation %s ready", gen_id)

    # ---- script ----------------------------------------------------------
    def script(self, gen_id: int, user_id: int) -> tuple[Generation, list[dict]]:
        """The generation (with its scenes) and the user's character library, for the script editor."""
        with self.db.session() as s:
            gen = self._get(s, gen_id, user_id)
            return gen, self._library(s, user_id)

    def save_script(self, gen_id: int, user_id: int, scenes: list, style: str = "", language: str = "",
                    approve: bool = False, bible: dict | None = None, genre: str | None = None) -> list[dict]:
        """Store the edited script of a video at REVIEW, or of one that FAILED (to fix what made Gemini refuse it),
        and return each scene's compiled prompt and warnings. With `approve`, the prompts are fixed and the clips
        start (a failed video is retried: finished scenes are kept). `bible`/`genre` replace the video's when given."""
        if not isinstance(scenes, list) or not 1 <= len(scenes) <= MAX_SCENES:
            raise UserError(f"A script has 1 to {MAX_SCENES} scenes.")
        style, language = clean_text(style, 300), clean_text(language, 40)
        with self.db.session() as s:
            gen = self._get(s, gen_id, user_id)
            failed = gen.status == GenStatus.FAILED
            if gen.status != GenStatus.REVIEW and not failed:
                raise UserError("This script can't be changed now.")
            if failed and len(scenes) != len(gen.scenes):
                raise UserError("A failed video keeps its scenes: edit them, but don't add or remove any.")
            library = self._library(s, user_id)
            if bible is not None:
                gen.bible = normalize_bible(bible)
            if genre is not None:
                gen.genre = valid_genre(genre, gen.prompt)
            script = [normalize_scene(raw, library) for raw in scenes]
            opts = _prompt_options(gen, len(script))
            prompts = [compile_prompt(sc, library, style, language, gen.aspect, **opts) for sc in script]
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
                # a failed video's scenes keep a prompt, or it would be planned all over again
                row.prompt = compile_prompt(sc, library, style, language, gen.aspect, fit=True, **opts) \
                    if failed else prompt if approve else ""
            if approve and not failed:
                gen.status, gen.error = GenStatus.GENERATING, None
            out = [{"prompt": p, "length": len(p), "warnings": scene_warnings(sc, p, library)}
                   for sc, p in zip(script, prompts)]
        if approve and failed:
            log.info("generation %s script edited after failing: retrying", gen_id)
            self.retry(gen_id, user_id)
        elif approve:
            log.info("generation %s script approved (%d scenes)", gen_id, len(script))
            self._continue(gen_id)
        return out

    def _scene_row(self, position: int, raw: dict, cast: list[dict], style: str, language: str, aspect: str,
                   compile_now: bool, genre: str = "", music: bool = True) -> Scene:
        sc = normalize_scene(raw, cast)
        prompt = compile_prompt(sc, cast, style, language, aspect, fit=True, genre=genre, music=music) \
            if compile_now else ""
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

    def _previous_frame(self, gen: Generation, scene: Scene) -> Path | None:
        """The last frame of the clip before `scene`, for it to continue from (None for the first scene)."""
        before = [sc for sc in gen.scenes if sc.position < scene.position and sc.clip_path]
        if not before or before[-1].position != scene.position - 1:
            return None
        clip = Path(before[-1].clip_path)
        return last_frame(clip, clip.with_name(f"{clip.stem}-last.jpg"))

    def _scene_dir(self, gen: Generation) -> Path:
        return self.settings.generated_dir / f"{gen.id}-scenes"

    def _drop_scenes(self, gen: Generation) -> None:
        shutil.rmtree(self._scene_dir(gen), ignore_errors=True)
        for scene in gen.scenes:
            scene.clip_path = None


def _prompt_options(gen: Generation, scene_count: int | None = None) -> dict:
    """The video-wide inputs of every clip prompt: its bible and type, and whether a clip may carry music (only a
    one-clip video: each clip would make up its own tune, which jumps at every cut)."""
    count = len(gen.scenes) if scene_count is None else scene_count
    return {"bible": gen.bible or {}, "genre": gen.genre or "",
            "music": (count == 1 or gen.genre == "music") and not gen.music_path}  # our track replaces the clips


def _first_version(clip: Path) -> Path:
    """Where a scene's first clip waits while it's remade after a failed check."""
    return clip.with_name(f"{clip.stem}-first.mp4")


def _fit_text(text: str, room: int) -> str:
    return text if len(text) <= room else text[:room].rsplit(" ", 1)[0]


def _pictured_names(scene: Scene, library: list[dict]) -> list[str]:
    """The names of the characters in the shot whose pictures go with it, in scene_images order."""
    by_id = {c["id"]: c for c in library}
    names = [by_id[p["id"]]["name"] for p in scene.characters or []
             if p.get("id") in by_id and by_id[p["id"]].get("image_path")]
    return list(dict.fromkeys(names))[:MAX_SCENE_IMAGES]


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
