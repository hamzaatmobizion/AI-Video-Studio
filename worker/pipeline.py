"""Job runner: one clip at a time through the single browser profile.

Rules:
- QC failure or generator error → bounded retry (MAX_RETRIES), then failed. Never a loop.
- Safety refusal → failed immediately; the prompt is never reworded.
- Account stop (limit, sign-out, verification, subscription) → pause that provider (Gemini or Google Flow), fail
  its queue with the same message; the attempt doesn't count. Resume is manual. Gemini's pause also stops its chat
  (plans, portraits, checks), except a video limit, which leaves the chat free.
- Clip files are written once as clips/<job>/attempt-N.mp4 and made read-only.
"""
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path

from worker.config import WorkerSettings
from worker.gemini_session import ProfileLock
from worker.generator import (
    AccountStop,
    Cancelled,
    ClipRequest,
    GeneratorError,
    ImageLimitReached,
    PlanRequest,
    CheckRequest,
    PortraitRequest,
    SafetyRefusal,
    VideoGenerator,
)
from worker.jobs import JobStore, lane
from worker.quality import check_clip

log = logging.getLogger("worker.pipeline")
BUSY_DELAY_FACTOR = 3  # retry delays when Gemini says it's at full capacity
FLOW_PROJECT_HOURS = 24  # a video's Flow project is reused for its later clips and stills within this long
PLAN_RETRY_DELAY = 30  # seconds before a failed script is asked again: no video quota, and the prompt's own timeline
#                        takes over if Gemini keeps failing, so waiting long only slows the video
PROVIDERS = ("gemini", "flow")  # where a clip can be made
PAUSE_KEYS = {"gemini": "account_pause", "flow": "account_pause:flow"}


class Runner:
    def __init__(self, settings: WorkerSettings, store: JobStore, generator: VideoGenerator, lock: ProfileLock,
                 flow: VideoGenerator | None = None):
        """`generator`: Gemini (clips, plans, portraits, checks); `flow`: Google Flow (clips only)."""
        self.settings = settings
        self.store = store
        self.generator = generator
        self.generators = {"gemini": generator, **({"flow": flow} if flow is not None else {})}
        self.lock = lock
        self.profile = generator.profile

    # ---- one Flow project per video ------------------------------------------
    @staticmethod
    def _video_key(job: dict) -> str | None:
        """The video a job belongs to, from the web app's ref ("gen-79-s2-1a2b3c" → "gen-79")."""
        m = re.match(r"(gen-\d+)-", job.get("ref") or "")
        return m.group(1) if m else None

    def _flow_project(self, job: dict) -> str:
        """The Flow project this job's video already used (its later clips and stills go back into it), else ""."""
        key = self._video_key(job)
        saved = self.store.kv_get(f"flow_project:{key}") if key and job.get("provider") == "flow" else None
        if not saved or time.time() - saved.get("at", 0) > FLOW_PROJECT_HOURS * 3600:
            return ""
        return saved.get("url") or ""

    def _remember_flow_project(self, job: dict, url: str) -> None:
        key = self._video_key(job)
        if key and url and job.get("provider") == "flow":
            self.store.kv_set(f"flow_project:{key}", {"url": url, "at": time.time()})

    def profile_for(self, provider: str) -> dict | None:
        gen = self.generators.get(provider)
        return gen.profile if gen is not None else None

    def _process_plan(self, job: dict) -> None:
        """Gemini (chat) writes the scene prompts for a long video. Same stop/retry rules as clips."""
        jid = job["id"]
        self.store.update(jid, status="running", error=None)
        self.store.event(jid, "attempt_started", "plan")
        opts = job.get("options") or {}
        req = PlanRequest(job_id=jid, prompt=job["prompt"], aspect=job["aspect"], scene_count=job["scene_count"],
                          clip_seconds=int(opts.get("clip_seconds") or self.profile.get("max_clip_duration") or 10),
                          cast=opts.get("cast") or [], style=opts.get("style") or "",
                          language=opts.get("language") or "", rules=opts.get("rules") or "",
                          attempt=job["attempts"] + 1,
                          model=self.settings.plan_model(job["attempts"] + 1, self.store.kv_get("plan_model_ok", "")))

        def should_cancel() -> bool:
            j = self.store.get(jid)
            return bool(j and j["cancel_requested"])

        try:
            result = self.generator.plan(req, should_cancel)
        except Cancelled:
            self.store.update(jid, status="cancelled", error="Cancelled.")
            return
        except SafetyRefusal as e:
            self.store.update(jid, status="failed", error=e.user_message)
            self.store.event(jid, "safety_refusal")
            return
        except AccountStop as e:
            self.pause(e.kind, e.user_message, chat=True)
            self.store.update(jid, status="failed", error=e.user_message)
            return
        except GeneratorError as e:
            self._retry_or_fail(jid, e.user_message, type(e).__name__)
            return
        except Exception as e:
            log.error("job %s plan unexpected %s", jid, type(e).__name__)
            self._retry_or_fail(jid, "Something unexpected went wrong while planning the scenes.", type(e).__name__)
            return
        if req.model:  # the next plan starts on the model that just worked
            self.store.kv_set("plan_model_ok", req.model)
        if not isinstance(result, dict):
            result = {"bible": {}, "scenes": result}
        scenes = result["scenes"]
        self.store.update(jid, status="done", result=result)
        self.store.event(jid, "done", f"plan {len(scenes)} scenes")
        log.info("job %s plan done (%d scenes)", jid, len(scenes))

    def _process_portrait(self, job: dict) -> None:
        """One image: Gemini (chat) draws a character portrait or a scene's first frame; Google Flow's Image mode
        draws a Flow video's first frame. Same stop/retry rules as clips."""
        jid = job["id"]
        provider = job.get("provider") or "gemini"
        generator = self.generators.get(provider)
        if generator is None:
            self.store.update(jid, status="failed", error=f"This worker can't draw pictures with {provider}.")
            return
        self.store.update(jid, status="running", error=None)
        self.store.event(jid, "attempt_started", "portrait")
        tmp = self.settings.jobs_dir / jid / "tmp"
        req = PortraitRequest(job_id=jid, prompt=job["prompt"], out_dir=tmp, images=[Path(p) for p in job["images"]],
                              aspect=job["aspect"], project_url=self._flow_project(job))

        def should_cancel() -> bool:
            j = self.store.get(jid)
            return bool(j and j["cancel_requested"])

        try:
            try:
                produced = generator.portrait(req, should_cancel)
            finally:
                self._remember_flow_project(job, req.project_url)
        except Cancelled:
            self.store.update(jid, status="cancelled", error="Cancelled.")
            return
        except SafetyRefusal as e:
            self.store.update(jid, status="failed", error=e.user_message)
            self.store.event(jid, "safety_refusal")
            return
        except ImageLimitReached as e:  # portraits wait for the image quota; videos carry on, so no pause
            self.store.update(jid, status="failed", error=e.user_message)
            self.store.event(jid, "image_limit")
            return
        except AccountStop as e:
            self.pause(e.kind, e.user_message, provider, chat=provider == "gemini")
            self.store.update(jid, status="failed", error=e.user_message)
            return
        except GeneratorError as e:
            self._retry_or_fail(jid, e.user_message, type(e).__name__)
            return
        except Exception as e:
            log.error("job %s portrait unexpected %s", jid, type(e).__name__)
            self._retry_or_fail(jid, "Something unexpected went wrong while drawing the portrait.", type(e).__name__)
            return
        final_dir = self.settings.clips_dir / jid
        final_dir.mkdir(parents=True, exist_ok=True)
        dest = final_dir / f"portrait{produced.suffix}"
        shutil.move(str(produced), dest)
        os.chmod(dest, 0o444)
        shutil.rmtree(tmp, ignore_errors=True)
        self.store.update(jid, status="done", clip_path=str(dest))
        self.store.event(jid, "done", "portrait")
        log.info("job %s portrait done", jid)

    def _process_check(self, job: dict) -> None:
        """Gemini (chat) compares a new clip's frames with the reference pictures. Same stop/retry rules."""
        jid = job["id"]
        self.store.update(jid, status="running", error=None)
        self.store.event(jid, "attempt_started", "check")
        req = CheckRequest(job_id=jid, prompt=job["prompt"], images=[Path(p) for p in job["images"]])

        def should_cancel() -> bool:
            j = self.store.get(jid)
            return bool(j and j["cancel_requested"])

        try:
            result = self.generator.check(req, should_cancel)
        except Cancelled:
            self.store.update(jid, status="cancelled", error="Cancelled.")
            return
        except AccountStop as e:
            self.pause(e.kind, e.user_message, chat=True)
            self.store.update(jid, status="failed", error=e.user_message)
            return
        except GeneratorError as e:
            self._retry_or_fail(jid, e.user_message, type(e).__name__)
            return
        except Exception as e:
            log.error("job %s check unexpected %s", jid, type(e).__name__)
            self._retry_or_fail(jid, "Something unexpected went wrong while checking the clip.", type(e).__name__)
            return
        self.store.update(jid, status="done", result=result)
        self.store.event(jid, "done", "check")

    # ---- pause / resume ------------------------------------------------------
    def paused(self, provider: str = "gemini") -> dict | None:
        return self.store.kv_get(PAUSE_KEYS[provider])

    @staticmethod
    def _blocks(provider: str, pause: dict) -> set[str]:
        """The lanes (worker/jobs.py) a provider's pause stops. Gemini's video limit leaves its chat free (Flow videos
        still get their scripts), unless the chat itself hit it."""
        if provider == "gemini" and (pause.get("kind") != "limit_reached" or pause.get("chat")):
            return {"gemini", "chat"}
        return {provider}

    def blocked_lanes(self) -> set[str]:
        out = set()
        for provider in PROVIDERS:
            pause = self.paused(provider)
            if pause:
                out |= self._blocks(provider, pause)
        return out

    def lane_pause(self, name: str) -> dict | None:
        """The pause that stops jobs in lane `name` (gemini | flow | chat), if any."""
        for provider in PROVIDERS:
            pause = self.paused(provider)
            if pause and name in self._blocks(provider, pause):
                return pause
        return None

    def pause(self, kind: str, message: str, provider: str = "gemini", chat: bool = False) -> None:
        """`chat`: a Gemini chat job (plan, portrait, check) hit the stop, so the chat stops too."""
        pause = {"kind": kind, "message": message, "at": time.time(), "provider": provider, "chat": chat}
        self.store.kv_set(PAUSE_KEYS[provider], pause)
        self.store.event(None, "account_paused", f"{provider} {kind}")
        stopped = self._blocks(provider, pause)
        for job in self.store.by_status("queued"):
            if lane(job) in stopped:
                self.store.update(job["id"], status="failed", error=message)
        log.warning("%s generation paused: %s", provider, kind)

    def resume(self, provider: str = "gemini") -> None:
        self.store.kv_del(PAUSE_KEYS[provider])
        self.store.event(None, "account_resumed", provider)
        log.info("%s generation resumed", provider)

    # ---- jobs ---------------------------------------------------------------
    def cancel(self, jid: str) -> bool:
        job = self.store.get(jid)
        if job is None:
            return False
        if job["status"] == "queued":
            self.store.update(jid, status="cancelled", error="Cancelled.")
        elif job["status"] == "running":
            self.store.update(jid, cancel_requested=1)
        return True

    def tick(self) -> bool:
        """Run at most one job. Returns True if a job was processed."""
        job = self.store.next_queued(skip_lanes=tuple(sorted(self.blocked_lanes())))
        if job is None:
            return False
        if not self.lock.try_acquire("generation"):
            return False  # sign-in window or session check holds the profile
        try:
            self._process(job)
        finally:
            self.lock.release("generation")
        return True

    def run_forever(self, stop: threading.Event, interval: float = 2.0) -> None:
        while not stop.is_set():
            try:
                busy = self.tick()
            except Exception as e:
                log.error("runner tick crashed: %s", type(e).__name__)
                busy = False
            if not busy:
                stop.wait(interval)

    def _next_attempt(self, jid: str) -> int:
        d = self.settings.clips_dir / jid
        n = 1
        while (d / f"attempt-{n}.mp4").exists():
            n += 1
        return n

    def _process(self, job: dict) -> None:
        if job.get("kind") == "plan":
            self._process_plan(job)
            return
        if job.get("kind") == "portrait":
            self._process_portrait(job)
            return
        if job.get("kind") == "check":
            self._process_check(job)
            return
        jid = job["id"]
        provider = job.get("provider") or "gemini"
        generator = self.generators.get(provider)
        if generator is None:
            self.store.update(jid, status="failed", error=f"This worker can't make clips with {provider}.")
            return
        attempt = self._next_attempt(jid)
        self.store.update(jid, status="running", error=None)
        self.store.event(jid, "attempt_started", str(attempt))
        log.info("job %s attempt %d started", jid, attempt)
        tmp = self.settings.jobs_dir / jid / "tmp"
        req = ClipRequest(job_id=jid, attempt=attempt, prompt=job["prompt"], aspect=job["aspect"],
                          images=[Path(p) for p in job["images"]], out_dir=tmp,
                          frame=(job.get("options") or {}).get("frame"), project_url=self._flow_project(job))

        def should_cancel() -> bool:
            j = self.store.get(jid)
            return bool(j and j["cancel_requested"])

        started = time.monotonic()
        try:
            try:
                produced = generator.generate(req, should_cancel)
            finally:
                self._remember_flow_project(job, req.project_url)
        except Cancelled:
            self.store.update(jid, status="cancelled", error="Cancelled.")
            log.info("job %s cancelled", jid)
            return
        except SafetyRefusal as e:
            self.store.update(jid, status="failed", error=e.user_message)
            self.store.event(jid, "safety_refusal")
            log.info("job %s refused by generator (safety)", jid)
            return
        except AccountStop as e:
            # This attempt doesn't count against the job; the provider stops. Pause first, so anyone
            # who sees this job failed also sees the pause (the web app keeps a paused video's scenes).
            self.pause(e.kind, e.user_message, provider)
            self.store.update(jid, status="failed", error=e.user_message)
            return
        except GeneratorError as e:
            self._retry_or_fail(jid, e.user_message, type(e).__name__)
            return
        except Exception as e:
            log.error("job %s unexpected %s", jid, type(e).__name__)
            self._retry_or_fail(jid, "Something unexpected went wrong while making the video.", type(e).__name__)
            return
        finally:
            log.info("job %s attempt %d took %.0fs", jid, attempt, time.monotonic() - started)

        final_dir = self.settings.clips_dir / jid
        final_dir.mkdir(parents=True, exist_ok=True)
        dest = final_dir / f"attempt-{attempt}.mp4"
        shutil.move(str(produced), dest)
        os.chmod(dest, 0o444)  # originals are never modified
        shutil.rmtree(tmp, ignore_errors=True)
        if should_cancel():
            self.store.update(jid, status="cancelled", error="Cancelled.")
            return
        qc = check_clip(dest, job["aspect"], min_duration=float(generator.profile["min_clip_duration"]) - 1.0)
        if not qc.ok:
            self._retry_or_fail(jid, "Quality check failed: " + " ".join(qc.reasons), "qc_failed")
            return
        self.store.update(jid, status="done", clip_path=str(dest), warnings=qc.warnings, error=None)
        self.store.event(jid, "done", f"{qc.width}x{qc.height} {qc.duration:.1f}s")
        log.info("job %s done %dx%d %.1fs", jid, qc.width, qc.height, qc.duration)

    def _retry_or_fail(self, jid: str, message: str, kind: str) -> None:
        job = self.store.get(jid)
        attempts = job["attempts"] + 1
        self.store.event(jid, "attempt_failed", kind)
        # A plan spends no video from the daily limit, so it gets one more go (Gemini's chat stalls now and then)
        # Gemini at full capacity needs longer than a passing error before it's worth asking again
        delay = self.settings.RETRY_DELAY_SECONDS * (BUSY_DELAY_FACTOR if kind == "GeminiBusy" else 1)
        if job.get("kind") == "plan":
            delay = min(delay, PLAN_RETRY_DELAY)
        if attempts > self.settings.MAX_RETRIES + (job.get("kind") == "plan"):
            self.store.update(jid, status="failed", attempts=attempts, error=message)
            log.info("job %s failed after %d attempts (%s)", jid, attempts, kind)
        else:
            self.store.update(jid, status="queued", attempts=attempts, error=message,
                              not_before=time.time() + delay)
            log.info("job %s will retry in %ds (%s)", jid, delay, kind)
