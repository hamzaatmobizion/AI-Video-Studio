"""Playwright driver for the Gemini web app: the video tool (Veo) for clips, plain chat for scene plans.

Account-safety rules (non-negotiable):
- One fixed sequence. Nothing on the page or in the prompt changes it.
- Top-level navigation only to the allowlisted hosts in gemini_ui.json.
- No stealth, fingerprinting, proxies, CAPTCHA solving or account rotation. Any limit, sign-out,
  verification or upgrade page raises a typed error and the worker STOPS.
- The only page prompt the worker answers is Gemini's one-time "Creating content from images and
  files" consent (Agree), approved by the account owner so reference images can be attached.
- Logs carry job ids, steps and durations only: never prompts, cookies, tokens or page text.
"""
import base64
import json
import logging
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

from worker.config import HERE, WorkerSettings
from worker.generator import (
    AccountStop,
    Cancelled,
    ClipRequest,
    GenerationError,
    GeneratorError,
    LimitReached,
    PlanRequest,
    SafetyRefusal,
    SignedOut,
    SubscriptionRequired,
    VerificationRequired,
)

log = logging.getLogger("worker.gemini")
MAX_DOWNLOAD = 500 * 1024 * 1024
POLL_SECONDS = 4
PLAN_TIMEOUT = 180  # seconds for Gemini to write the scene list
MAX_SCENE_PROMPT = 2000


# Shot vocabulary for the script. app/script.py offers the same lists in the script editor.
FRAMINGS = ["extreme wide", "wide", "medium", "medium close-up", "close-up", "extreme close-up",
            "over-the-shoulder", "two-shot"]
ANGLES = ["eye level", "low angle", "high angle", "bird's-eye", "dutch angle"]
MOVEMENTS = ["static", "slow push-in", "pull-out", "pan left", "pan right", "tilt up", "tilt down", "tracking",
             "dolly", "handheld", "orbit", "crane"]
LENSES = ["24mm wide-angle lens", "35mm lens", "50mm lens", "85mm lens with shallow depth of field"]
POSITIONS = ["frame left", "center", "frame right", "foreground", "background"]
MAX_SPOKEN_WORDS = 25
_EXAMPLE = ('{"setting": "...", "time_of_day": "...", "characters": [{"name": "...", "position": "..."}], '
            '"shot": {"framing": "...", "angle": "...", "movement": "...", "lens": "..."}, "action": "...", '
            '"dialogue": [{"name": "...", "line": "..."}], "sound": "..."}')


def plan_instruction(req: PlanRequest) -> str:
    total = req.scene_count * req.clip_seconds
    if req.cast:
        sheets = "\n".join(
            f"- {c['name']}: " + "; ".join(x for x in (c.get("appearance"), c.get("outfit")) if x)
            + (f". Voice: {c['voice']}" if c.get("voice") else "")
            + (f". Personality: {c['personality']}" if c.get("personality") else "")
            for c in req.cast)
        cast = ("Cast (use these names exactly, and only these people unless the idea needs extras; their looks "
                "and voices are added to every scene for you, so don't describe them again):\n" + sheets + "\n")
    else:
        cast = ("Invent the characters the idea needs and give each a short name. Each scene is made on its own, "
                "so in every scene's action describe each character's appearance and clothing in the same words.\n")
    style = f"Visual style for every scene: {req.style}\n" if req.style else ""
    language = f"All spoken lines are in {req.language}.\n" if req.language else ""
    return (
        f"You are a film director writing the shooting script for an AI video generator that makes one "
        f"{req.clip_seconds}-second clip per scene. Turn the idea below into exactly {req.scene_count} consecutive "
        f"scenes that together play as one continuous {total}-second {req.aspect} video.\n"
        + cast + style + language +
        "Rules:\n"
        "- The generator never sees the other scenes, so give every scene its full setting and keep places, "
        "props and lighting consistent from scene to scene.\n"
        f"- Describe only what happens in that scene's {req.clip_seconds} seconds and keep the story moving.\n"
        "- Direct it like a film: open with an establishing wide shot, use medium shots for action and close-ups "
        "for emotion and dialogue, and keep screen direction consistent (whoever is frame left in a conversation "
        "stays frame left).\n"
        f"- framing is one of: {', '.join(FRAMINGS)}. angle is one of: {', '.join(ANGLES)}. movement is one of: "
        f"{', '.join(MOVEMENTS)}. lens is one of: {', '.join(LENSES)}. position is one of: {', '.join(POSITIONS)}.\n"
        f"- Spoken lines must fit in {req.clip_seconds} seconds: at most about {MAX_SPOKEN_WORDS} words per scene "
        "in total. Leave dialogue empty when nobody speaks.\n"
        "- Put any on-screen text in the action of the scene where it appears, spelled exactly as in the idea.\n"
        "- Never put double quotes inside a value; write quotes as single quotes ('like this').\n"
        f"- Reply with ONLY a JSON array of {req.scene_count} objects, one per scene, in order, each like:\n"
        f"{_EXAMPLE}\nNo other text.\n\n"
        f"Idea:\n{req.prompt}"
    )


def parse_script(text: str, count: int) -> list[dict]:
    """The script scenes from Gemini's reply (a JSON array of objects, possibly inside a code block). A reply
    that is a list of plain prompts still works: each one becomes a scene's action."""
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        raise GenerationError("Gemini didn't reply with a list of scenes.")
    body = text[start:end + 1]
    try:
        items = json.loads(body)
    except ValueError:
        try:  # a list of objects: repair quotes inside values; a list of strings: split it by lines
            items = json.loads(_escape_stray_quotes(body)) if body[1:].lstrip().startswith("{") \
                else _loose_scene_list(body[1:-1])
        except ValueError:
            items = None
        if not items:
            raise GenerationError("Gemini's list of scenes wasn't valid JSON.") from None
    scenes = [sc for sc in map(_scene_item, items) if sc] if isinstance(items, list) else []
    if len(scenes) < count:
        raise GenerationError(f"Gemini wrote {len(scenes)} scene(s) instead of {count}.")
    return scenes[:count]


def _scene_item(item) -> dict | None:
    if isinstance(item, str):
        return {"action": item.strip()[:MAX_SCENE_PROMPT]} if item.strip() else None
    if isinstance(item, dict) and any(item.values()):
        return item
    return None


def _escape_stray_quotes(body: str) -> str:
    """Escape double quotes inside JSON strings (Gemini quoting dialogue): a quote that closes a string is
    followed by `:`, `,`, `}` or `]`; any other one inside a string is part of the text."""
    out, in_str, i = [], False, 0
    while i < len(body):
        ch = body[i]
        if in_str and ch == "\\":
            out.append(body[i:i + 2])
            i += 2
            continue
        if ch == '"':
            if not in_str:
                in_str = True
            elif body[i + 1:].lstrip()[:1] in (":", ",", "}", "]", ""):
                in_str = False
            else:
                ch = '\\"'
        out.append(ch)
        i += 1
    return "".join(out)


def _loose_scene_list(body: str) -> list[str]:
    """The items of a one-scene-per-line array whose strings hold unescaped quotes (Gemini quoting dialogue)."""
    body = body.strip().rstrip(",").strip()
    if len(body) < 2 or body[0] != '"' or body[-1] != '"':
        return []
    return [s.replace('\\"', '"') for s in re.split(r'"\s*,\s*\n\s*"', body[1:-1])]


# ---- UI config ---------------------------------------------------------------
def load_ui(path: Path | None = None) -> dict:
    return json.loads((path or HERE / "gemini_ui.json").read_text())


class StopText:
    """Classifies visible text into a stop kind using the regexes in gemini_ui.json."""

    ORDER = ("verification", "limit", "no_subscription", "safety", "error")

    def __init__(self, ui: dict):
        self.patterns = {k: [re.compile(p, re.I | re.M) for p in v] for k, v in ui["patterns"].items()}

    def classify(self, text: str) -> str | None:
        for kind in self.ORDER:
            if any(p.search(text) for p in self.patterns.get(kind, [])):
                return kind
        return None

    def signed_out(self, text: str) -> bool:
        return any(p.search(text) for p in self.patterns.get("signed_out", []))


STOP_ERRORS: dict[str, Callable[[], GeneratorError]] = {
    "verification": lambda: VerificationRequired(
        "Google wants to confirm it's you (CAPTCHA or security check). Generation is paused: open the "
        "Gemini sign-in window in Settings, complete the check by hand, then resume."),
    "limit": lambda: LimitReached(
        "Gemini's video limit has been reached. Generation is paused; resume in Settings once the limit resets."),
    "no_subscription": lambda: SubscriptionRequired(
        "This Google account can't make videos in Gemini (an upgrade is required). Generation is paused."),
    "safety": lambda: SafetyRefusal(
        "Gemini declined this prompt for safety reasons. Edit the prompt yourself and try again."),
    "error": lambda: GenerationError("Gemini reported an error while making the video."),
    "signed_out": lambda: SignedOut(
        "Gemini is signed out. Open the Gemini sign-in window in Settings, sign in, then resume."),
}


def stop_error(kind: str) -> GeneratorError:
    return STOP_ERRORS[kind]()


# ---- browser launch ------------------------------------------------------------
def _chrome_path() -> str:
    if sys.platform == "darwin":
        return "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    return "google-chrome"


def launch_kwargs(settings: WorkerSettings, headless: bool | None = None) -> dict:
    args = ["--no-first-run", "--no-default-browser-check"]
    if sys.platform.startswith("linux"):
        args.append("--password-store=basic")  # same cookie encryption as the plain sign-in window
    kw = {
        "user_data_dir": str(settings.profile_dir),
        "headless": settings.BROWSER_HEADLESS if headless is None else headless,
        "args": args,
        # Playwright's mock keychain would encrypt cookies with a different key than the plain
        # sign-in window uses, so the automated launch couldn't read the human's session.
        "ignore_default_args": ["--use-mock-keychain"],
        # Keep Chrome's sandbox on (Playwright's default adds --no-sandbox, which Chrome warns about).
        "chromium_sandbox": True,
        "accept_downloads": True,
        "viewport": {"width": 1280, "height": 900},
    }
    if settings.BROWSER_CHANNEL:
        kw["channel"] = settings.BROWSER_CHANNEL
    return kw


PROFILE_IN_USE = ("The Gemini browser window is still open. Quit it (Chrome menu → Quit, or ⌘Q in that "
                  "window), then click Check session.")


def profile_browser_pid(settings: WorkerSettings) -> int | None:
    """PID of a Chrome still running on the profile (from its SingletonLock "host-pid" link), else None."""
    try:
        target = os.readlink(settings.profile_dir / "SingletonLock")
        pid = int(target.rsplit("-", 1)[1])
        os.kill(pid, 0)
    except (OSError, ValueError, IndexError):
        return None
    return pid


def launch_context(p, settings: WorkerSettings, wait: float = 20.0, attempts: int = 3,
                   headless: bool | None = None):
    """launch_persistent_context, but first wait for any other Chrome on the profile to exit.

    A second Chrome on the same profile hands over to the running one and quits at once, which
    Playwright reports as an opaque TargetClosedError. That also happens for a moment while a
    just-closed Chrome is still shutting down, so a closed-at-launch is retried after a pause."""
    for attempt in range(1, attempts + 1):
        deadline = time.monotonic() + wait
        while profile_browser_pid(settings) is not None:
            if time.monotonic() >= deadline:
                raise GenerationError(PROFILE_IN_USE)
            time.sleep(0.5)
        try:
            return p.chromium.launch_persistent_context(**launch_kwargs(settings, headless))
        except Exception as e:
            if type(e).__name__ != "TargetClosedError" or attempt == attempts:
                raise
            log.info("browser closed during launch (attempt %d); retrying", attempt)
            time.sleep(3.0)


def plain_browser_command(settings: WorkerSettings, ui: dict) -> list[str]:
    """A normal, non-automated browser on the same profile, for a human to sign in."""
    if settings.BROWSER_CHANNEL == "chrome":
        exe = _chrome_path()
    else:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            exe = p.chromium.executable_path
    cmd = [exe, f"--user-data-dir={settings.profile_dir}", "--no-first-run", "--no-default-browser-check"]
    if sys.platform.startswith("linux"):
        cmd.append("--password-store=basic")
    cmd.append(ui["app_url"])
    return cmd


def install_navigation_guard(context, allowed_hosts: list[str]) -> None:
    allowed = set(allowed_hosts)

    def guard(route, request):
        try:
            top = request.is_navigation_request() and request.frame.parent_frame is None
        except Exception:
            top = False
        if top:
            host = urlparse(request.url).hostname or ""
            if request.url.startswith("http") and host not in allowed:
                log.warning("blocked navigation to a non-allowlisted host")
                route.abort()
                return
        route.continue_()

    context.route("**/*", guard)


# ---- page helpers --------------------------------------------------------------
class Page:
    """Thin wrapper that resolves selector keys from gemini_ui.json."""

    def __init__(self, page, ui: dict):
        self.page = page
        self.ui = ui
        self.sel = ui["selectors"]

    def find(self, key: str, timeout: float = 5.0, **fmt):
        deadline = time.monotonic() + timeout
        while True:
            for s in self.sel.get(key, []):
                loc = self.page.locator(s.format(**fmt) if fmt else s)
                try:
                    for i in range(min(loc.count(), 5)):  # first *visible* match, not just the first
                        if loc.nth(i).is_visible():
                            return loc.nth(i)
                except Exception:
                    continue
            if time.monotonic() >= deadline:
                return None
            self.page.wait_for_timeout(400)

    def matches(self, key: str) -> str | None:
        """First candidate that matches at all (for diagnostics)."""
        for s in self.sel.get(key, []):
            try:
                if self.page.locator(s).count():
                    return s
            except Exception:
                continue
        return None

    def body_text(self) -> str:
        try:
            return self.page.inner_text("body", timeout=5000)
        except Exception:
            return ""

    def screenshot(self, settings: WorkerSettings, name: str) -> str | None:
        settings.diagnostics_dir.mkdir(parents=True, exist_ok=True)
        path = settings.diagnostics_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{name}.png"
        try:
            self.page.screenshot(path=str(path))
            path.chmod(0o600)
            return str(path)
        except Exception:
            return None


def check_session(pg: Page, stop: StopText, prompt_timeout: float = 20.0) -> None:
    """Raise a typed AccountStop if the page isn't a signed-in, usable Gemini; else return."""
    url = pg.page.url
    host = urlparse(url).hostname or ""
    if host == "accounts.google.com":
        raise stop_error("signed_out")
    if "/sorry/" in url or pg.matches("captcha"):
        raise stop_error("verification")
    text = pg.body_text()
    if stop.classify(text) == "verification":
        raise stop_error("verification")
    box = pg.find("prompt_input", timeout=prompt_timeout)
    # Gemini lets signed-out visitors chat too, so require a positive signed-in signal (account avatar).
    signed_in = pg.find("account_button", timeout=3) is not None
    if not signed_in or pg.find("sign_in_button", timeout=0.5) is not None:
        raise stop_error("signed_out")
    if box is None:
        raise GenerationError("Couldn't find Gemini's prompt box; the page may have changed. "
                              "Run `python -m worker check-gemini`.")


# ---- generator -----------------------------------------------------------------
class GeminiBrowserGenerator:
    def __init__(self, settings: WorkerSettings, ui: dict | None = None):
        self.settings = settings
        self.ui = ui or load_ui()
        self.stop = StopText(self.ui)
        self.profile = settings.profile()

    def generate(self, req: ClipRequest, should_cancel: Callable[[], bool]) -> Path:
        started = time.monotonic()
        req.out_dir.mkdir(parents=True, exist_ok=True)
        out = self._in_browser(req.job_id, lambda pg, ctx: self._run(pg, ctx, req, should_cancel))
        log.info("job %s clip ready in %.0fs", req.job_id, time.monotonic() - started)
        return out

    def plan(self, req: PlanRequest, should_cancel: Callable[[], bool]) -> list[dict]:
        started = time.monotonic()
        scenes = self._in_browser(req.job_id, lambda pg, ctx: self._run_plan(pg, req, should_cancel))
        log.info("job %s plan ready (%d scenes) in %.0fs", req.job_id, len(scenes), time.monotonic() - started)
        return scenes

    def _in_browser(self, jid: str, work):
        """Launch the profile, run `work(pg, ctx)`, always close the browser."""
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            ctx = launch_context(p, self.settings)
            try:
                install_navigation_guard(ctx, self.ui["allowed_hosts"])
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                pg = Page(page, self.ui)
                try:
                    return work(pg, ctx)
                except (GeneratorError, Cancelled):
                    raise
                except PlaywrightError as e:
                    pg.screenshot(self.settings, f"{jid}-browser-error")
                    log.warning("job %s browser error: %s", jid, type(e).__name__)
                    raise GenerationError("The browser hit an error while talking to Gemini.") from e
            finally:
                try:
                    ctx.close()
                except Exception:
                    pass

    # The fixed sequence. Do not add branches that depend on page content beyond stop detection.
    def _run(self, pg: Page, ctx, req: ClipRequest, should_cancel) -> Path:
        jid = req.job_id
        log.info("job %s step=open", jid)
        pg.page.goto(self.ui["app_url"], wait_until="domcontentloaded", timeout=60_000)
        pg.page.wait_for_timeout(2500)

        log.info("job %s step=session", jid)
        check_session(pg, self.stop)
        self._cancel_point(should_cancel)

        log.info("job %s step=video_tool", jid)
        self._choose_video_tool(pg)

        label = self.profile.get("model_label")
        if label:
            log.info("job %s step=model", jid)
            self._choose_model(pg, label)

        if req.images:
            log.info("job %s step=attach n=%d", jid, len(req.images))
            self._attach(pg, req.images)
        self._cancel_point(should_cancel)

        before = self._send_prompt(pg, req.prompt, jid)
        src = self._wait_for_video(pg, before, should_cancel, jid)
        log.info("job %s step=download", jid)
        dest = req.out_dir / f"attempt-{req.attempt}.mp4"
        self._download(pg, ctx, src, dest)
        return dest

    # The plan sequence: same page and checks, plain chat (no tool), reply is a JSON list of script scenes.
    def _run_plan(self, pg: Page, req: PlanRequest, should_cancel) -> list[dict]:
        jid = req.job_id
        log.info("job %s step=open", jid)
        pg.page.goto(self.ui["app_url"], wait_until="domcontentloaded", timeout=60_000)
        pg.page.wait_for_timeout(2500)
        log.info("job %s step=session", jid)
        check_session(pg, self.stop)
        self._cancel_point(should_cancel)
        before = self._send_prompt(pg, plan_instruction(req), jid)
        return self._wait_for_plan(pg, before, req.scene_count, should_cancel, jid)

    def _send_prompt(self, pg: Page, text: str, jid: str) -> int:
        """Type `text` and send it; return how many responses were on the page before."""
        log.info("job %s step=prompt", jid)
        box = pg.find("prompt_input", timeout=10)
        if box is None:
            raise GenerationError("Gemini's prompt box disappeared. Run `python -m worker check-gemini`.")
        box.click()
        pg.page.keyboard.insert_text(text)
        pg.page.wait_for_timeout(500)

        responses = self._responses(pg)
        before = responses.count() if responses is not None else 0
        send = pg.find("send_button", timeout=10)
        if send is None:
            raise GenerationError("Couldn't find Gemini's send button. Run `python -m worker check-gemini`.")
        for _ in range(20):
            if send.is_enabled():
                break
            pg.page.wait_for_timeout(500)
        log.info("job %s step=send", jid)
        send.click()
        return before

    def _wait_for_plan(self, pg: Page, before: int, count: int, should_cancel, jid: str) -> list[dict]:
        """Wait until the reply parses as the scene list and has stopped changing (it streams in)."""
        deadline = time.monotonic() + PLAN_TIMEOUT
        last_text, error, stable = None, None, 0
        while time.monotonic() < deadline:
            self._cancel_point(should_cancel)
            pg.page.wait_for_timeout(2000)
            url = pg.page.url
            if (urlparse(url).hostname or "") == "accounts.google.com":
                raise stop_error("signed_out")
            if "/sorry/" in url or pg.matches("captcha"):
                raise stop_error("verification")
            responses = self._responses(pg)
            n = responses.count()
            if n <= before:
                continue
            try:
                text = responses.nth(n - 1).inner_text(timeout=3000)
            except Exception:
                continue
            try:
                scenes = parse_script(text, count)
            except GenerationError as e:
                scenes, error = None, e
            stable = stable + 1 if text == last_text else 0
            last_text = text
            if scenes and stable:
                return scenes
            if scenes is None and stable and text.strip():
                kind = self.stop.classify(text)  # a finished reply that isn't a list: limit, refusal, …
                if kind:
                    pg.screenshot(self.settings, f"{jid}-stop-{kind}")
                    raise stop_error(kind)
                if stable >= 3:  # unchanged for ~6 s and still not a list: Gemini is done, just wrong
                    pg.screenshot(self.settings, f"{jid}-plan-unreadable")
                    raise error
        pg.screenshot(self.settings, f"{jid}-plan-timeout")
        raise error or GenerationError("Gemini didn't finish writing the scenes in time.")

    @staticmethod
    def _cancel_point(should_cancel) -> None:
        if should_cancel():
            raise Cancelled()

    def _raise_from_text(self, text: str, fallback: GeneratorError) -> None:
        kind = self.stop.classify(text)
        if kind:
            raise stop_error(kind)
        raise fallback

    def _choose_video_tool(self, pg: Page) -> None:
        tool = find_video_tool(pg)
        if tool is None:
            pg.screenshot(self.settings, "no-video-tool")
            self._raise_from_text(pg.body_text(), GenerationError(
                "Couldn't find Gemini's Video tool. Run `python -m worker check-gemini`."))
        disabled = tool.is_disabled() or (tool.get_attribute("aria-disabled") == "true")
        if disabled:
            pg.screenshot(self.settings, "video-tool-disabled")
            self._raise_from_text(pg.body_text(), GenerationError("Gemini's Video tool is unavailable right now."))
        tool.click()
        pg.page.wait_for_timeout(800)
        if self.ui["selectors"].get("video_tool_active") and pg.find("video_tool_active", timeout=5) is None:
            pg.screenshot(self.settings, "video-tool-not-active")
            self._raise_from_text(pg.body_text(), GenerationError(
                "Couldn't confirm the Video tool is selected. Run `python -m worker check-gemini`."))

    def _choose_model(self, pg: Page, label: str) -> None:
        switch = pg.find("model_switch", timeout=5)
        if switch is None:
            raise GenerationError(f"Couldn't find Gemini's model picker to choose “{label}”.")
        if label.lower() in (switch.inner_text() or "").lower():
            return
        switch.click()
        pg.page.wait_for_timeout(600)
        option = pg.find("model_option", timeout=5, label=label)
        if option is None:
            pg.page.keyboard.press("Escape")
            raise GenerationError(f"Model “{label}” isn't offered in Gemini. Check GEMINI_MODEL_LABEL.")
        option.click()
        pg.page.wait_for_timeout(800)
        switch = pg.find("model_switch", timeout=5)
        if switch is None or label.lower() not in (switch.inner_text() or "").lower():
            raise GenerationError(f"Gemini didn't switch to model “{label}”.")

    def _accept_upload_consent(self, pg: Page, timeout: float = 1.0) -> bool:
        """Click Agree on Gemini's one-time image-upload consent dialog, if it's showing."""
        agree = pg.find("upload_consent_agree", timeout=timeout)
        if agree is None:
            return False
        log.info("step=upload_consent")
        agree.click()
        pg.page.wait_for_timeout(800)
        return True

    def _open_file_chooser(self, pg: Page, files: list[str]) -> None:
        """Upload button → "Upload files" → file chooser, accepting the consent dialog if it appears."""
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        def click_upload_item(timeout_ms: int):
            btn = pg.find("upload_button", timeout=5)
            if btn is None:
                raise GenerationError("Couldn't find Gemini's upload button for the reference images.")
            btn.click()
            item = pg.find("upload_menu_item", timeout=5)
            if item is None:
                raise GenerationError("Couldn't find Gemini's “Upload files” option.")
            with pg.page.expect_file_chooser(timeout=timeout_ms) as fc:
                item.click()
            return fc.value

        try:
            chooser = click_upload_item(5_000)
        except PlaywrightTimeoutError:
            agree = pg.find("upload_consent_agree", timeout=2)
            if agree is None:
                raise
            log.info("step=upload_consent")
            try:  # Agree may open the file chooser itself
                with pg.page.expect_file_chooser(timeout=5_000) as fc:
                    agree.click()
                chooser = fc.value
            except PlaywrightTimeoutError:
                chooser = click_upload_item(10_000)
        chooser.set_files(files)

    def _attach(self, pg: Page, images: list[Path]) -> None:
        files = [str(p) for p in images]
        inp = pg.page.locator(self.ui["selectors"]["file_input"][0])
        if inp.count():
            inp.first.set_input_files(files)
        else:
            self._open_file_chooser(pg, files)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            for s in self.ui["selectors"]["attachment_preview"]:
                if pg.page.locator(s).count() >= len(files):
                    pg.page.wait_for_timeout(1000)
                    return
            self._accept_upload_consent(pg, timeout=0)  # the dialog can also follow a direct upload
            pg.page.wait_for_timeout(1000)
        raise GenerationError("Gemini didn't accept the reference images within a minute.")

    def _responses(self, pg: Page):
        for s in self.ui["selectors"]["response"]:
            loc = pg.page.locator(s)
            if loc.count():
                return loc
        return pg.page.locator(self.ui["selectors"]["response"][0])

    def _wait_for_video(self, pg: Page, before: int, should_cancel, jid: str) -> str:
        timeout = self.settings.GENERATION_TIMEOUT_SECONDS
        deadline = time.monotonic() + timeout
        video_sel = ", ".join(self.ui["selectors"]["response_video"])
        while True:
            self._cancel_point(should_cancel)
            if time.monotonic() > deadline:
                pg.screenshot(self.settings, f"{jid}-timeout")
                raise GenerationError(f"Gemini didn't finish the video within {timeout // 60} minutes.")
            pg.page.wait_for_timeout(POLL_SECONDS * 1000)
            url = pg.page.url
            if (urlparse(url).hostname or "") == "accounts.google.com":
                raise stop_error("signed_out")
            if "/sorry/" in url or pg.matches("captcha"):
                raise stop_error("verification")
            responses = self._responses(pg)
            n = responses.count()
            if n <= before:
                continue
            last = responses.nth(n - 1)
            vids = last.locator(video_sel)
            if vids.count():
                v = vids.first
                src = v.get_attribute("src") or ""
                if not src:
                    source = v.locator("source")
                    src = (source.first.get_attribute("src") or "") if source.count() else ""
                if src:
                    return src
                continue
            try:
                text = last.inner_text(timeout=3000)
            except Exception:
                text = ""
            kind = self.stop.classify(text)
            if kind:
                pg.screenshot(self.settings, f"{jid}-stop-{kind}")
                raise stop_error(kind)

    def _download(self, pg: Page, ctx, src: str, dest: Path) -> None:
        part = dest.with_suffix(".part")
        data: bytes | None = None
        if src.startswith("blob:"):
            b64 = pg.page.evaluate(
                """async (u) => { const r = await fetch(u); const b = await r.blob();
                   return await new Promise((res, rej) => { const fr = new FileReader();
                     fr.onload = () => res(String(fr.result).split(',')[1]); fr.onerror = rej; fr.readAsDataURL(b); }); }""",
                src)
            data = base64.b64decode(b64) if b64 else None
        elif src.startswith("https://"):
            r = ctx.request.get(src, timeout=180_000)
            if r.ok:
                data = r.body()
        if data:
            if len(data) > MAX_DOWNLOAD:
                raise GenerationError("Gemini's video is larger than 500 MB.")
            part.write_bytes(data)
        else:
            btn = pg.find("download_button", timeout=5)
            if btn is None:
                raise GenerationError("Couldn't download the video from Gemini.")
            with pg.page.expect_download(timeout=120_000) as d:
                btn.click()
            d.value.save_as(str(part))
        with open(part, "rb") as f:
            head = f.read(16)
        if not (head[4:8] == b"ftyp" or head.startswith(b"\x1a\x45\xdf\xa3")):
            part.unlink(missing_ok=True)
            raise GenerationError("What Gemini returned isn't a video file.")
        os.replace(part, dest)


def find_video_tool(pg: Page):
    """The Video tool button, opening the Tools menu (and its "More tools") if needed; None if absent."""
    tool = pg.find("video_tool", timeout=2)
    if tool is None:
        btn = pg.find("tools_button", timeout=5)
        if btn is not None:
            btn.click()
            pg.page.wait_for_timeout(800)
            tool = pg.find("video_tool", timeout=4)
            if tool is None:
                more = pg.find("more_tools", timeout=2)
                if more is not None:
                    more.click()
                    pg.page.wait_for_timeout(800)
                    tool = pg.find("video_tool", timeout=4)
    return tool


# ---- diagnostics / session check ---------------------------------------------
def inspect(settings: WorkerSettings, ui: dict | None = None, open_tools: bool = True) -> dict:
    """Open Gemini, classify the session, report which selectors match, save a screenshot.

    Read-only: it may open the Tools menu to look for the Video tool, then presses Escape. Also reports
    `plan` ("free"/"paid", from the Upgrade button) and `video` ("available"/"unavailable").
    """
    from playwright.sync_api import sync_playwright

    ui = ui or load_ui()
    stop = StopText(ui)
    result: dict = {"state": "needs_attention", "message": "", "model": None, "plan": None, "video": None,
                    "selectors": {}, "screenshot": None}
    with sync_playwright() as p:
        ctx = launch_context(p, settings, headless=True)  # a check needs no window on screen
        try:
            install_navigation_guard(ctx, ui["allowed_hosts"])
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            pg = Page(page, ui)
            page.goto(ui["app_url"], wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(3000)
            try:
                check_session(pg, stop)
                result["state"], result["message"] = "connected", "Signed in to Gemini."
            except SignedOut as e:
                result["state"], result["message"] = "signed_out", e.user_message
            except LimitReached as e:
                result["state"], result["message"] = "limit_reached", e.user_message
            except (AccountStop, GeneratorError) as e:
                result["message"] = e.user_message
            sw = pg.find("model_switch", timeout=2)
            if sw is not None:
                result["model"] = (sw.inner_text() or "").strip().splitlines()[0][:40] or None
            for key in ui["selectors"]:
                if key == "model_option":
                    continue
                result["selectors"][key] = pg.matches(key)
            if result["state"] == "connected":
                # Free accounts get an "Upgrade" button; paid (Google AI Pro/Ultra) accounts don't.
                result["plan"] = "free" if pg.find("upgrade_button", timeout=2) is not None else "paid"
                if open_tools:
                    tool = find_video_tool(pg)
                    if tool is None:
                        result["video"] = "unavailable"
                    else:
                        result["selectors"]["video_tool"] = pg.matches("video_tool")
                        off = tool.is_disabled() or tool.get_attribute("aria-disabled") == "true"
                        result["video"] = "unavailable" if off else "available"
                    page.keyboard.press("Escape")
                    page.keyboard.press("Escape")
            result["screenshot"] = pg.screenshot(settings, "check")
        finally:
            ctx.close()
    return result


def open_plain_window(settings: WorkerSettings, ui: dict | None = None) -> subprocess.Popen:
    ui = ui or load_ui()
    cmd = plain_browser_command(settings, ui)
    return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
