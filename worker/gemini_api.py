"""Gemini API (free tier, text only) for a Flow video's script: the same instruction and parsing as Gemini's chat
(worker/gemini_browser.py), sent as one HTTP request, so no browser is opened.

- Models in GEMINI_API_PLAN_MODELS are tried in turn: the next one when a model is out of quota (429), busy or errors.
- Any failure is a GenerationError, and the runner then writes the script in Gemini's chat instead.
- Logs carry job ids, models, status codes and durations only: never the prompt, the reply or the key.
"""
import logging
import time
from collections.abc import Callable

import httpx

from worker.config import WorkerSettings
from worker.gemini_browser import parse_plan, plan_instruction
from worker.generator import Cancelled, GenerationError, PlanRequest

log = logging.getLogger("worker.gemini_api")
API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
API_TIMEOUT = 150  # seconds for one model to write the script (3 scenes took 28s; a model that hangs is left sooner)


class GeminiApiPlanner:
    def __init__(self, settings: WorkerSettings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self.models = [m.strip() for m in settings.GEMINI_API_PLAN_MODELS.split(",") if m.strip()]
        self._http = httpx.Client(timeout=API_TIMEOUT, transport=transport)

    def plan(self, req: PlanRequest, should_cancel: Callable[[], bool]) -> dict:
        body = {"contents": [{"role": "user", "parts": [{"text": plan_instruction(req)}]}],
                "generationConfig": {"responseMimeType": "application/json"}}
        problems = []
        for model in self.models:
            if should_cancel():
                raise Cancelled()
            started = time.monotonic()
            try:
                plan = parse_plan(self._ask(model, body), req.scene_count)
            except GenerationError as e:
                log.info("job %s api plan with %s failed in %.0fs: %s", req.job_id, model,
                         time.monotonic() - started, e.user_message)
                problems.append(f"{model}: {e.user_message}")
                continue
            log.info("job %s api plan ready with %s (%d scenes) in %.0fs", req.job_id, model, len(plan["scenes"]),
                     time.monotonic() - started)
            return plan
        raise GenerationError("The Gemini API couldn't write the script (" + "; ".join(problems) + ").")

    def _ask(self, model: str, body: dict) -> str:
        """The reply's text from one model, or a GenerationError saying why there's none."""
        try:
            r = self._http.post(API_URL.format(model=model), json=body,
                                headers={"x-goog-api-key": self.settings.GEMINI_API_KEY})
        except httpx.HTTPError as e:
            raise GenerationError(f"no answer ({type(e).__name__})") from None
        if r.status_code == 429:
            raise GenerationError("free quota used up for now")
        if r.status_code in (401, 403):
            raise GenerationError("the API key was refused")
        if r.status_code >= 400:
            raise GenerationError(f"error {r.status_code}")
        try:
            data = r.json()
        except ValueError:
            raise GenerationError("the reply wasn't JSON") from None
        blocked = (data.get("promptFeedback") or {}).get("blockReason")
        if blocked:
            raise GenerationError(f"the idea was blocked ({blocked})")
        cand = (data.get("candidates") or [{}])[0]
        text = "".join(p.get("text") or "" for p in (cand.get("content") or {}).get("parts") or []
                       if not p.get("thought"))
        if cand.get("finishReason") not in (None, "STOP") or not text.strip():
            raise GenerationError(f"no script (finish reason {cand.get('finishReason') or 'none'})")
        return text
