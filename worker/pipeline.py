"""Job runner: one clip at a time through the single browser profile.

Rules:
- QC failure or generator error → bounded retry (MAX_RETRIES), then failed. Never a loop.
- Safety refusal → failed immediately; the prompt is never reworded.
- Account stop (limit, sign-out, verification, subscription) → pause ALL generation, fail the queue
  with the same message; the attempt doesn't count. Resume is manual.
- Clip files are written once as clips/<job>/attempt-N.mp4 and made read-only.
"""
import logging
import os
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
from worker.jobs import JobStore
from worker.quality import check_clip

log = logging.getLogger("worker.pipeline")
BUSY_DELAY_FACTOR = 3  # retry delays when Gemini says it's at full capacity


class Runner:
    def __init__(self, settings: WorkerSettings, store: JobStore, generator: VideoGenerator, lock: ProfileLock):
        self.settings = settings
        self.store = store
        self.generator = generator
        self.lock = lock
        self.profile = generator.profile

    def _process_plan(self, job: dict) -> None:
        """Gemini (chat) writes the scene prompts for a long video. Same stop/retry rules as clips."""
        jid = job["id"]
        self.store.update(jid, status="running", error=None)
        self.store.event(jid, "attempt_started", "plan")
        opts = job.get("options") or {}
        req = PlanRequest(job_id=jid, prompt=job["prompt"], aspect=job["aspect"], scene_count=job["scene_count"],
                          clip_seconds=int(self.profile.get("max_clip_duration") or 10),
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
            self.pause(e.kind, e.user_message)
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
        """Gemini (chat) draws one character portrait. Same stop/retry rules as clips."""
        jid = job["id"]
        self.store.update(jid, status="running", error=None)
        self.store.event(jid, "attempt_started", "portrait")
        tmp = self.settings.jobs_dir / jid / "tmp"
        req = PortraitRequest(job_id=jid, prompt=job["prompt"], out_dir=tmp, images=[Path(p) for p in job["images"]])

        def should_cancel() -> bool:
            j = self.store.get(jid)
            return bool(j and j["cancel_requested"])

        try:
            produced = self.generator.portrait(req, should_cancel)
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
            self.pause(e.kind, e.user_message)
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
            self.pause(e.kind, e.user_message)
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
    def paused(self) -> dict | None:
        return self.store.kv_get("account_pause")

    def pause(self, kind: str, message: str) -> None:
        self.store.kv_set("account_pause", {"kind": kind, "message": message, "at": time.time()})
        self.store.event(None, "account_paused", kind)
        for job in self.store.by_status("queued"):
            self.store.update(job["id"], status="failed", error=message)
        log.warning("generation paused: %s", kind)

    def resume(self) -> None:
        self.store.kv_del("account_pause")
        self.store.event(None, "account_resumed")
        log.info("generation resumed")

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
        if self.paused():
            return False
        job = self.store.next_queued()
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
        attempt = self._next_attempt(jid)
        self.store.update(jid, status="running", error=None)
        self.store.event(jid, "attempt_started", str(attempt))
        log.info("job %s attempt %d started", jid, attempt)
        tmp = self.settings.jobs_dir / jid / "tmp"
        req = ClipRequest(job_id=jid, attempt=attempt, prompt=job["prompt"], aspect=job["aspect"],
                          images=[Path(p) for p in job["images"]], out_dir=tmp)

        def should_cancel() -> bool:
            j = self.store.get(jid)
            return bool(j and j["cancel_requested"])

        started = time.monotonic()
        try:
            produced = self.generator.generate(req, should_cancel)
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
            # This attempt doesn't count against the job; the whole account stops. Pause first, so anyone
            # who sees this job failed also sees the pause (the web app keeps a paused video's scenes).
            self.pause(e.kind, e.user_message)
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
        qc = check_clip(dest, job["aspect"], min_duration=float(self.profile["min_clip_duration"]) - 1.0)
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
        if attempts > self.settings.MAX_RETRIES + (job.get("kind") == "plan"):
            self.store.update(jid, status="failed", attempts=attempts, error=message)
            log.info("job %s failed after %d attempts (%s)", jid, attempts, kind)
        else:
            self.store.update(jid, status="queued", attempts=attempts, error=message,
                              not_before=time.time() + delay)
            log.info("job %s will retry in %ds (%s)", jid, delay, kind)
