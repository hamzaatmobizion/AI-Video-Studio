"""Playwright driver for Google Flow (labs.google/fx/tools/flow): one clip per job with Veo 3.1 - Lite, and a Flow
video's "Lock characters" first-frame stills with its image model, all paid with Flow credits. Scene plans, character
portraits and clip checks stay with Gemini's chat (worker/gemini_browser.py).

Same account-safety rules as the Gemini driver:
- One fixed sequence: open, session check, new project, agent off (direct mode), clip settings (Video, mode, aspect,
  model, 720p, 8 s, x1), pictures, type, send, poll every 4 s, download. Nothing on the page or in the prompt
  changes it.
- Top-level navigation only to the allowlisted hosts in flow_ui.json.
- No stealth, fingerprinting, proxies, CAPTCHA solving or account rotation. Running out of credits, a sign-out or
  a verification page raises a typed error and Flow generation STOPS (Gemini carries on).
- The only page prompts answered are Flow's own tips ("Got it") and its "Rights to use this image" notice (I agree),
  which the account owner has approved, like Gemini's upload consent. Nothing else is agreed to: a CAPTCHA or
  verification is never solved, it stops Flow generation for the owner to complete by hand.
- The model is never swapped for another: if "Veo 3.1 - Lite" can't be picked the job fails, so no other model's
  credits are spent. The count is set to x1, so one clip costs one clip's credits.
- Logs carry job ids, steps and durations only: never prompts, cookies, tokens or page text.
"""
import json
import logging
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

from worker.config import HERE, WorkerSettings
from worker.gemini_browser import (
    POLL_SECONDS,
    Page,
    StopText,
    cancel_point,
    fetch_src,
    image_kind,
    install_navigation_guard,
    launch_context,
    model_named,
    plain_browser_command,
    run_in_browser,
    save_video,
)
from worker.generator import (
    AccountStop,
    CheckRequest,
    ClipRequest,
    GeminiBusy,
    GenerationError,
    GeneratorError,
    LimitReached,
    PlanRequest,
    PortraitRequest,
    SafetyRefusal,
    SignedOut,
    SubscriptionRequired,
    VerificationRequired,
)

log = logging.getLogger("worker.flow")
SITE = "Google Flow"
CHECK = "Run `python -m worker check-flow`."

STOP_ERRORS: dict[str, Callable[[], GeneratorError]] = {
    "verification": lambda: VerificationRequired(
        "Google Flow flagged this browser (\u201cunusual activity\u201d) or wants to confirm it's you. Flow generation is "
        "paused (Gemini isn't affected). Give it a while, open the Flow window in Settings to check nothing is "
        "waiting for you, then resume Flow."),
    "limit": lambda: LimitReached(
        "Google Flow is out of credits. Flow videos are paused (Gemini videos aren't affected); resume Flow in "
        "Settings once you have credits again."),
    "no_subscription": lambda: SubscriptionRequired(
        "This Google account can't make videos in Google Flow (it needs a Google AI plan with Flow). "
        "Flow generation is paused."),
    "real_people": lambda: SafetyRefusal(
        "Google Flow won't make this clip because it takes the people in the pictures for real people. If they're "
        "your own made-up characters, give them cartoon-style portraits (Characters → Generate portrait), then retry."),
    "safety": lambda: SafetyRefusal(
        "Google Flow declined this clip under its policies. Change that moment, then retry."),
    "busy": lambda: GeminiBusy(
        "Google Flow is under high demand right now. It's tried again after a few minutes on its own."),
    "error": lambda: GenerationError(
        "Google Flow failed to generate this clip. That comes from Google Flow, not this app; it usually passes: "
        "retry in a few minutes."),
    "signed_out": lambda: SignedOut(
        "Google Flow is signed out. Open the Flow sign-in window in Settings, sign in, then resume Flow."),
}


def stop_error(kind: str) -> GeneratorError:
    return STOP_ERRORS[kind]()


def load_ui(path: Path | None = None) -> dict:
    return json.loads((path or HERE / "flow_ui.json").read_text())


def check_session(pg: Page, stop: StopText, timeout: float = 20.0) -> None:
    """Raise a typed AccountStop if the page isn't a signed-in, usable Flow; else return."""
    url = pg.page.url
    if (urlparse(url).hostname or "") == "accounts.google.com":
        raise stop_error("signed_out")
    if "/sorry/" in url or captcha_showing(pg):
        raise stop_error("verification")
    if stop.classify(pg.body_text()) == "verification":
        raise stop_error("verification")
    # New project (the landing page) or the prompt box (a project) only show when signed in.
    if pg.find("new_project", timeout=timeout) is not None or pg.find("prompt_input", timeout=1) is not None:
        return
    if pg.find("sign_in_button", timeout=2) is not None or stop.signed_out(pg.body_text()):
        raise stop_error("signed_out")
    raise GenerationError(f"Couldn't find Flow's New project button; the page may have changed. {CHECK}")


def captcha_showing(pg: Page) -> bool:
    """A CAPTCHA challenge on screen. Flow keeps an invisible reCAPTCHA on every page, so being there isn't one:
    only a visible challenge counts."""
    return pg.find("captcha", timeout=0) is not None


def dismiss_tips(pg: Page) -> None:
    """Close Flow's own tips and tours ("Got it"), which cover the prompt box on a first visit."""
    for _ in range(3):
        btn = pg.find("dismiss", timeout=0.5)
        if btn is None:
            return
        btn.click()
        pg.page.wait_for_timeout(500)


def open_project(pg: Page, settings: WorkerSettings) -> None:
    """A new, empty project (spends nothing), with Flow's agent panel closed so its own prompt box shows."""
    if pg.find("prompt_input", timeout=1) is None:
        btn = pg.find("new_project", timeout=10)
        if btn is None:
            raise GenerationError(f"Couldn't find Flow's New project button. {CHECK}")
        btn.click()
        if pg.find("prompt_input", timeout=30) is None:
            pg.screenshot(settings, "flow-no-prompt-box")
            raise GenerationError(f"Flow's new project has no prompt box. {CHECK}")
    pg.page.wait_for_timeout(1500)
    if pg.find("agent_panel", timeout=2) is not None:
        close = pg.find("agent_panel_close", timeout=3)
        if close is None:
            raise GenerationError(f"Couldn't close Flow's agent panel. {CHECK}")
        close.click()
        pg.page.wait_for_timeout(1500)
    dismiss_tips(pg)


def direct_mode(pg: Page) -> None:
    """Switch Flow's Agent toggle off: clips are made straight from the prompt, never rewritten or planned by Flow's
    agent. Direct mode shows the clip settings button; agent mode doesn't."""
    toggle = pg.find("agent_toggle", timeout=5)
    if toggle is not None and toggle.get_attribute("aria-pressed") == "true":  # only ever switched off, never on
        toggle.click()
        pg.page.wait_for_timeout(1500)
    elif toggle is None and pg.find("settings_trigger", timeout=3) is None:
        raise GenerationError(f"Couldn't find Flow's Agent switch to make clips directly. {CHECK}")
    if pg.find("settings_trigger", timeout=5) is None:
        raise GenerationError(f"Google Flow stayed in Agent mode. {CHECK}")


def _project_url_ok(url: str) -> bool:
    """A Flow project page on one of Flow's own hosts (never navigate anywhere else)."""
    try:
        parsed = urlparse(url or "")
    except ValueError:
        return False
    return parsed.scheme == "https" and parsed.hostname in ("flow.google.com", "labs.google") and "/project/" in parsed.path


def _without(text: str, prompt: str) -> str:
    """Page text minus the prompt itself, so a prompt about "real people" isn't taken for Flow's refusal."""
    return text.replace(prompt, " ") if prompt and len(prompt) >= 12 else text


class FlowBrowserGenerator:
    def __init__(self, settings: WorkerSettings, ui: dict | None = None):
        self.settings = settings
        self.ui = ui or load_ui()
        self.stop = StopText(self.ui)
        self.profile = settings.flow_profile()
        self.step = ""  # the step in progress, named when the browser fails
        self._failed_before = 0  # "Failed" tiles on the page before the prompt went in

    def generate(self, req: ClipRequest, should_cancel: Callable[[], bool]) -> Path:
        started = time.monotonic()
        req.out_dir.mkdir(parents=True, exist_ok=True)
        self.step = "opening Flow"
        out = run_in_browser(self.settings, self.ui, req.job_id, lambda pg, ctx: self._run(pg, ctx, req, should_cancel),
                             site=SITE, where=lambda: self.step)
        log.info("job %s flow clip ready in %.0fs", req.job_id, time.monotonic() - started)
        return out

    # Flow makes clips and a Flow video's first-frame stills; plans, character portraits and checks are Gemini's.
    def plan(self, req: PlanRequest, should_cancel: Callable[[], bool]) -> dict:
        raise GenerationError("Google Flow doesn't write scripts.")

    def portrait(self, req: PortraitRequest, should_cancel: Callable[[], bool]) -> Path:
        """A Flow video's first-frame still ("Lock characters"), drawn in Flow's Image mode with the image model and
        Flow credits, so a Flow video uses no Gemini image."""
        started = time.monotonic()
        req.out_dir.mkdir(parents=True, exist_ok=True)
        self.step = "opening Flow"
        out = run_in_browser(self.settings, self.ui, req.job_id,
                             lambda pg, ctx: self._run_still(pg, ctx, req, should_cancel), site=SITE,
                             where=lambda: self.step)
        log.info("job %s flow still ready in %.0fs", req.job_id, time.monotonic() - started)
        return out

    def check(self, req: CheckRequest, should_cancel: Callable[[], bool]) -> dict:
        raise GenerationError("Google Flow doesn't check clips.")

    # The fixed sequence. Do not add branches that depend on page content beyond stop detection.
    def _run(self, pg: Page, ctx, req: ClipRequest, should_cancel) -> Path:
        jid = req.job_id
        self._open(pg, req, jid)
        cancel_point(should_cancel)
        log.info("job %s step=direct_mode", jid)
        direct_mode(pg)
        cancel_point(should_cancel)

        mode, pictures = self._mode_for(req)
        log.info("job %s step=settings mode=%s", jid, mode)
        self.step = "choosing the clip settings"
        self._choose_settings(pg, mode, req.aspect)
        cancel_point(should_cancel)
        if mode == "frames":  # the clip starts exactly from this picture
            log.info("job %s step=start_frame", jid)
            self.step = "adding the start frame"
            self._upload(pg, pictures[0], slot="start_frame")
        elif pictures:
            log.info("job %s step=attach n=%d", jid, len(pictures))
            self.step = "adding the pictures"
            for path in pictures:
                self._upload(pg, path)
        cancel_point(should_cancel)

        before = self._thumbnails(pg)
        baseline = self.stop.classify(_without(pg.body_text(), req.prompt))
        self._failed_before = self._failed_tiles(pg)
        self.step = "sending the prompt"
        self._send_prompt(pg, req.prompt, jid)
        self.step = "waiting for the clip"
        thumb = self._wait_for_video(pg, before, baseline, req.prompt, should_cancel, jid)
        self.step = "downloading the clip"
        log.info("job %s step=download", jid)
        dest = req.out_dir / f"attempt-{req.attempt}.mp4"
        self._download(pg, ctx, thumb, dest)
        return dest

    # The still sequence: the same page and checks as a clip, in Image mode, the picture downloaded.
    def _run_still(self, pg: Page, ctx, req: PortraitRequest, should_cancel) -> Path:
        jid = req.job_id
        self._open(pg, req, jid)
        cancel_point(should_cancel)
        direct_mode(pg)
        log.info("job %s step=image_settings", jid)
        self._choose_image_settings(pg, req.aspect)
        cancel_point(should_cancel)
        pictures = [p for p in req.images if p.exists()][:int(self.ui.get("max_images", 3))]
        if pictures:
            log.info("job %s step=attach n=%d", jid, len(pictures))
            for path in pictures:
                self._upload(pg, path)
        cancel_point(should_cancel)
        before = self._thumbnails(pg, "result_image")
        baseline = self.stop.classify(_without(pg.body_text(), req.prompt))
        self._failed_before = self._failed_tiles(pg)
        self.step = "sending the prompt"
        self._send_prompt(pg, req.prompt, jid)
        self.step = "waiting for the picture"
        thumb = self._wait_for_video(pg, before, baseline, req.prompt, should_cancel, jid, key="result_image",
                                     what="the picture")
        log.info("job %s step=download", jid)
        return self._save_image(pg, thumb, req.out_dir)

    def _choose_image_settings(self, pg: Page, aspect: str) -> None:
        """Flow's settings for one picture: Image, the aspect, the image model, one image. All checked, as a clip's."""
        trigger = pg.find("settings_trigger", timeout=5)
        if trigger is None:
            raise GenerationError(f"Couldn't find Flow's settings button. {CHECK}")
        trigger.click()
        pg.page.wait_for_timeout(800)
        image = self.ui["image_settings"]
        self._radio(pg, image["kind"])
        self._radio(pg, self.ui["aspect_names"].get(aspect, aspect))
        self._choose_model(pg, self.profile["image_model_label"])
        self._radio(pg, image["count"])
        pg.page.keyboard.press("Escape")
        pg.page.wait_for_timeout(600)
        shown = trigger.inner_text() if pg.find("settings_trigger", timeout=3) is not None else ""
        if image["count"].lower() not in (shown or "").lower() or not model_named(shown or "",
                                                                                 self.profile["image_model_label"]):
            pg.screenshot(self.settings, "flow-image-settings-not-set")
            raise GenerationError(f"Google Flow didn't keep the picture settings (it shows “{(shown or '').strip()}”).")

    def _save_image(self, pg: Page, thumb: str, out_dir: Path) -> Path:
        """The new picture behind tile `thumb`, as JPG/PNG/WebP. The tile's own picture, unless it's only a small
        preview: then the full one from the viewer's Download media."""
        tile = pg.page.locator(f"img[src='{thumb}']").first
        data = None
        try:
            data = fetch_src(pg, pg.page.context, thumb)
        except Exception:
            data = None
        ext = image_kind(data[:16]) if data else None
        if ext is not None and _image_width(data, ext) >= 700:
            dest = out_dir / f"portrait.{ext}"
            dest.write_bytes(data)
            return dest
        log.info("step=download picture via the viewer")
        tile.click()
        pg.page.wait_for_timeout(3000)
        btn = pg.find("download_button", timeout=5)
        if btn is None:
            if ext is None:
                raise GenerationError(f"Couldn't download the picture from Google Flow. {CHECK}")
            dest = out_dir / f"portrait.{ext}"  # the preview is better than nothing
            dest.write_bytes(data)
            return dest
        part = out_dir / "portrait.part"
        with pg.page.expect_download(timeout=120_000) as d:
            btn.click()
            item = pg.find("image_download_option", timeout=3)
            if item is not None:
                item.click()
        d.value.save_as(str(part))
        head = part.read_bytes()[:16]
        ext = image_kind(head)
        if ext is None:
            part.unlink(missing_ok=True)
            raise GenerationError("What Google Flow returned isn't a picture.")
        dest = out_dir / f"portrait.{ext}"
        part.replace(dest)
        return dest

    def _open(self, pg: Page, req, jid: str) -> None:
        """The video's own Flow project when it already has one (`req.project_url`), with its prompt box cleared of
        the last clip's pictures; else a new project. Either way `req.project_url` ends as the project used, so the
        video's next clip and still go back into it: one project per video, far fewer page actions than one per job."""
        if _project_url_ok(req.project_url):
            log.info("job %s step=open_project (the video's own)", jid)
            self.step = "opening the video's Flow project"
            pg.page.goto(req.project_url, wait_until="domcontentloaded", timeout=60_000)
            pg.page.wait_for_timeout(3000)
            check_session(pg, self.stop)
            dismiss_tips(pg)
            if pg.find("prompt_input", timeout=10) is not None and self._clear_inputs(pg):
                open_project(pg, self.settings)  # in the project already: only closes the agent panel if it's open
                return
            log.info("job %s the video's project can't be reused; opening a new one", jid)
        log.info("job %s step=open", jid)
        self.step = "opening Flow"
        pg.page.goto(self.ui["app_url"], wait_until="domcontentloaded", timeout=60_000)
        pg.page.wait_for_timeout(3000)
        log.info("job %s step=session", jid)
        check_session(pg, self.stop)
        dismiss_tips(pg)
        log.info("job %s step=new_project", jid)
        self.step = "opening a new Flow project"
        open_project(pg, self.settings)
        req.project_url = pg.page.url if _project_url_ok(pg.page.url) else ""

    def _composer_pictures(self, pg: Page) -> int:
        """Pictures in the prompt box (a start frame, ingredients), e.g. left from the video's last clip."""
        box = pg.find("prompt_input", timeout=3)
        if box is None:
            return 0
        try:
            composer = box.locator("xpath=ancestor::*[.//button[@aria-label='Start generation']][1]")
            return composer.locator("img").count()
        except Exception:
            return 0

    def _clear_inputs(self, pg: Page) -> bool:
        """Remove the pictures left in the prompt box, so a clip gets only its own. False when they won't go (the
        caller then opens a new project instead)."""
        for _ in range(6):
            if self._composer_pictures(pg) == 0:
                return True
            remove = pg.find("remove_input", timeout=2)
            if remove is None:
                break
            remove.click()
            pg.page.wait_for_timeout(600)
        return self._composer_pictures(pg) == 0

    def _mode_for(self, req: ClipRequest) -> tuple[str, list[Path]]:
        """Which Flow mode makes this clip, and the pictures it takes. A scene with a first frame (a locked still, or
        the previous clip's last frame: `req.frame`) starts exactly from it in Frames mode, so the place, people and
        style carry on; Flow's Frames mode takes no other pictures, and the frame already shows the characters.
        Otherwise Ingredients, with the characters' pictures as references; with no pictures, plain text to video."""
        images = [p for p in req.images if p.exists()]
        if req.frame is not None and 0 <= req.frame < len(req.images) and req.images[req.frame].exists():
            return "frames", [req.images[req.frame]]
        if not images:
            return "text", []
        return "ingredients", images[:int(self.ui.get("max_images", 3))]

    def _choose_settings(self, pg: Page, mode: str, aspect: str) -> None:
        """Flow's clip settings, all in one panel: Video, the mode, aspect, model, 720p, 8 s, one clip. Every one is
        checked, so a clip never goes out on another model, length or count (other credits)."""
        trigger = pg.find("settings_trigger", timeout=5)
        if trigger is None:
            raise GenerationError(f"Couldn't find Flow's clip settings button. {CHECK}")
        trigger.click()
        pg.page.wait_for_timeout(800)
        clip = self.ui["clip_settings"]
        self._radio(pg, clip["kind"])
        self._radio(pg, self.ui["modes"][mode])
        self._radio(pg, self.ui["aspect_names"].get(aspect, aspect))
        self._choose_model(pg, self.profile["model_label"])
        # Veo 3.1 - Lite is fixed at 720p and 8 s (no choice shown); other models offer both
        self._radio(pg, clip["resolution"], required=False)
        self._radio(pg, clip["duration"], required=False)
        self._radio(pg, clip["count"])
        pg.page.keyboard.press("Escape")
        pg.page.wait_for_timeout(600)
        shown = (pg.find("settings_trigger", timeout=3).inner_text() or "") if pg.find("settings_trigger", timeout=3) \
            else ""
        for want in (clip["kind"], clip["duration"], clip["count"]):
            if want.lower() not in shown.lower():
                pg.screenshot(self.settings, "flow-settings-not-set")
                raise GenerationError(f"Google Flow didn't keep the clip setting “{want}” (it shows “{shown.strip()}”).")

    def _radio(self, pg: Page, name: str, required: bool = True) -> None:
        """Pick the settings panel's option `name` ("Video", "9:16", "8s", "x1") and check it took. A missing option
        that isn't `required` is one the model doesn't offer (the clip settings are checked afterwards)."""
        option = pg.find("settings_radio", timeout=5 if required else 1, name=name)
        if option is None and not required:
            return
        if option is None:
            pg.screenshot(self.settings, "flow-no-option")
            raise GenerationError(f"Flow's clip settings have no “{name}” option. {CHECK}")
        if option.get_attribute("aria-checked") != "true":
            option.click()
            pg.page.wait_for_timeout(500)
            if option.get_attribute("aria-checked") == "false":
                raise GenerationError(f"Google Flow didn't switch its clip setting to “{name}”.")

    def _upload(self, pg: Page, path: Path, slot: str = "add_image") -> None:
        """Add one picture as an ingredient: the + button, Upload media, the file chooser, I agree on Flow's "Rights
        to use this image" notice if it shows (approved by the account owner), then Add to prompt. With `slot`
        start_frame, the picture goes into Frames mode's Start slot instead: the clip's exact first frame."""
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        add = pg.find(slot, timeout=5)
        if add is None:
            what = "Start frame slot" if slot == "start_frame" else "button to add a picture"
            raise GenerationError(f"Couldn't find Flow's {what}. {CHECK}")
        add.click()
        pg.page.wait_for_timeout(1200)
        upload = pg.find("upload_media", timeout=5)
        if upload is None:
            pg.page.keyboard.press("Escape")
            raise GenerationError(f"Couldn't find Flow's Upload media button. {CHECK}")
        try:
            with pg.page.expect_file_chooser(timeout=10_000) as fc:
                upload.click()
            fc.value.set_files(str(path))
        except PlaywrightTimeoutError as e:
            pg.page.keyboard.press("Escape")
            raise GenerationError(f"Flow didn't open its file picker for the picture. {CHECK}") from e
        self._accept_rights_notice(pg)
        # The upload lands in the project's media, selected, with Add to prompt under its preview.
        add_it = pg.find("add_to_prompt", timeout=30)
        if add_it is None:
            pg.screenshot(self.settings, "flow-upload-not-listed")
            raise GenerationError(f"Flow didn't offer to add the uploaded picture to the prompt. {CHECK}")
        add_it.click()
        pg.page.wait_for_timeout(1500)
        if pg.find("upload_media", timeout=1) is not None:  # the picker should close once it's added
            pg.screenshot(self.settings, "flow-picture-not-added")
            raise GenerationError(f"Google Flow didn't add the picture to the prompt. {CHECK}")
        if slot == "start_frame" and pg.find("start_frame_set", timeout=5) is None:
            pg.screenshot(self.settings, "flow-start-frame-not-set")
            raise GenerationError(f"Google Flow didn't put the picture in the Start frame slot. {CHECK}")

    def _accept_rights_notice(self, pg: Page, timeout: float = 3.0) -> bool:
        """Click I agree on Flow's "Rights to use this image" notice, if it's showing: approved by the account owner,
        like Gemini's rights reminder. Only this named notice; never a CAPTCHA, payment or credits confirmation."""
        if pg.find("rights_notice", timeout=timeout) is None:
            return False
        agree = pg.find("rights_agree", timeout=3)
        if agree is None:
            pg.screenshot(self.settings, "flow-rights-notice")
            raise GenerationError(f"Couldn't find I agree on Flow's “Rights to use this image” notice. {CHECK}")
        log.info("step=rights_notice")
        agree.click()
        pg.page.wait_for_timeout(1500)
        if pg.find("rights_notice", timeout=1) is not None:
            raise GenerationError("Google Flow's “Rights to use this image” notice didn't close after I agree.")
        return True

    def _choose_model(self, pg: Page, label: str) -> None:
        """Pick exactly `label` ("Veo 3.1 - Lite"). Never falls back to another model: that would spend
        different credits than the user chose."""
        switch = pg.find("model_switch", timeout=5)
        if switch is None:
            raise GenerationError(f"Couldn't find Flow's model menu to choose “{label}”. {CHECK}")
        if model_named(switch.inner_text() or "", label):
            return
        switch.click()
        pg.page.wait_for_timeout(600)
        option = self._option(pg, "model_option", label, label=label)
        if option is None:
            pg.page.keyboard.press("Escape")
            pg.screenshot(self.settings, "flow-no-model")
            raise GenerationError(f"Model “{label}” isn't offered in Google Flow on this account. "
                                  "Set FLOW_MODEL_LABEL in .env to the name shown in Flow's model menu.")
        option.click()
        pg.page.wait_for_timeout(800)
        switch = pg.find("model_switch", timeout=5)
        if switch is None or not model_named(switch.inner_text() or "", label):
            raise GenerationError(f"Google Flow didn't switch to model “{label}”.")

    @staticmethod
    def _option(pg: Page, key: str, text: str, **fmt):
        """The visible menu entry naming exactly `text` ("Veo 3.1 - Lite", not "Veo 3.1 - Lite Pro")."""
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            for s in pg.sel.get(key, []):
                loc = pg.page.locator(s.format(**fmt))
                try:
                    for i in range(min(loc.count(), 8)):
                        item = loc.nth(i)
                        # the whole entry: its first line is an icon ("volume_up") before the name
                        if item.is_visible() and model_named(item.inner_text() or "", text):
                            return item
                except Exception:
                    continue
            pg.page.wait_for_timeout(400)
        return None

    def _send_prompt(self, pg: Page, text: str, jid: str) -> None:
        log.info("job %s step=prompt", jid)
        box = pg.find("prompt_input", timeout=10)
        if box is None:
            raise GenerationError(f"Flow's prompt box disappeared. {CHECK}")
        box.click()
        pg.page.keyboard.insert_text(text)
        pg.page.wait_for_timeout(500)
        send = pg.find("send_button", timeout=10)
        if send is None:
            raise GenerationError(f"Couldn't find Flow's Start generation button. {CHECK}")
        for _ in range(20):
            if send.is_enabled():
                break
            pg.page.wait_for_timeout(500)
        log.info("job %s step=send", jid)
        send.click()

    def _thumbnails(self, pg: Page, key: str = "result_tile") -> set[str]:
        """The thumbnails of the project's finished clips (`key` result_tile) or pictures (result_image). Flow shows a
        finished clip as a picture tile; its video player only appears on hover, and one still being made has no
        thumbnail yet."""
        out = set()
        for s in self.ui["selectors"][key]:
            loc = pg.page.locator(s)
            try:
                for i in range(min(loc.count(), 40)):
                    src = loc.nth(i).get_attribute("src") or ""
                    if src:
                        out.add(src)
            except Exception:
                continue
        return out

    def _download(self, pg: Page, ctx, thumb: str, dest: Path) -> None:
        """The finished clip behind thumbnail `thumb`: its video's own URL (shown on hover), else the viewer's
        Download media button."""
        tile = pg.page.locator(f"img[src='{thumb}']").first
        src = ""
        try:
            tile.hover()
            for _ in range(20):
                pg.page.wait_for_timeout(500)
                video = tile.locator("xpath=ancestor::flow-video-tile[1]").locator("video")
                src = _src(video.first) if video.count() else ""
                if src:
                    break
        except Exception:
            src = ""
        try:
            save_video(pg, ctx, src, dest, site=SITE, menu_key="download_option")
            return
        except GenerationError:
            if not src:
                raise
        log.info("step=download via the viewer")  # the hover URL wasn't the video itself
        tile.click()
        pg.page.wait_for_timeout(3000)
        save_video(pg, ctx, "", dest, site=SITE, menu_key="download_option")

    def _failed_tiles(self, pg: Page) -> int:
        n = 0
        for s in self.ui["selectors"].get("failed_tile", []):
            try:
                n = max(n, pg.page.locator(s).count())
            except Exception:
                continue
        return n

    def _raise_if_failed(self, pg: Page, prompt: str, jid: str) -> None:
        """Flow shows a refused or failed generation as a "Failed" tile with its reason. Stop at once with that reason
        (a flag or verification pauses Flow) instead of waiting the whole timeout for a video that won't come."""
        if self._failed_tiles(pg) <= getattr(self, "_failed_before", 0):
            return
        reason = ""
        for s in self.ui["selectors"].get("failed_tile", []):
            loc = pg.page.locator(s)
            try:
                if loc.count():
                    reason = loc.last.inner_text(timeout=2000)
                    break
            except Exception:
                continue
        reason = re.sub(r"\s+", " ", _without(reason, prompt)).strip()
        pg.screenshot(self.settings, f"{jid}-flow-failed")
        kind = self.stop.classify(reason)
        if kind:
            raise stop_error(kind)
        why = re.sub(r"^\W*failed\W*", "", reason, flags=re.I)[:300].rstrip(" .")
        raise GenerationError(f"Google Flow failed this clip{f': {why}' if why else ''}. Retry it, or change the "
                              "scene if it keeps failing.")

    def _alert_text(self, pg: Page) -> str:
        parts = []
        for s in self.ui["selectors"].get("alert", []):
            loc = pg.page.locator(s)
            try:
                for i in range(min(loc.count(), 5)):
                    if loc.nth(i).is_visible():
                        parts.append(loc.nth(i).inner_text(timeout=2000))
            except Exception:
                continue
        return "\n".join(parts)

    def _wait_for_video(self, pg: Page, before: set[str], baseline: str | None, prompt: str, should_cancel,
                        jid: str, key: str = "result_tile", what: str = "the video") -> str:
        """Wait for the new clip's (or with `key` result_image, picture's) tile; return its thumbnail's src."""
        timeout = self.settings.GENERATION_TIMEOUT_SECONDS
        deadline = time.monotonic() + timeout
        while True:
            cancel_point(should_cancel)
            if time.monotonic() > deadline:
                pg.screenshot(self.settings, f"{jid}-flow-timeout")
                raise GenerationError(f"Google Flow didn't finish {what} within {timeout // 60} minutes.")
            pg.page.wait_for_timeout(POLL_SECONDS * 1000)
            url = pg.page.url
            if (urlparse(url).hostname or "") == "accounts.google.com":
                raise stop_error("signed_out")
            if "/sorry/" in url or captcha_showing(pg):
                raise stop_error("verification")
            new = self._thumbnails(pg, key) - before
            if new:
                return sorted(new)[0]
            self._raise_if_failed(pg, prompt, jid)
            # Flow's own messages (credits, refusals, failures): its alerts first, then the page, leaving out what
            # was already on it before the prompt went in.
            kind = self.stop.classify(_without(self._alert_text(pg), prompt))
            if kind is None:
                kind = self.stop.classify(_without(pg.body_text(), prompt))
                kind = None if kind == baseline else kind
            if kind:
                pg.screenshot(self.settings, f"{jid}-flow-stop-{kind}")
                raise stop_error(kind)


def _image_width(data: bytes, ext: str) -> int:
    """A JPG/PNG/WebP's width in pixels (0 if it can't be read), to tell a small preview from the full picture."""
    try:
        if ext == "png":
            return int.from_bytes(data[16:20], "big")
        if ext == "webp" and data[12:16] == b"VP8X":
            return int.from_bytes(data[24:27], "little") + 1
        if ext == "webp" and data[12:16] == b"VP8 ":
            return int.from_bytes(data[26:28], "little") & 0x3FFF
        if ext == "jpg":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    return 0
                marker, size = data[i + 1], int.from_bytes(data[i + 2:i + 4], "big")
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    return int.from_bytes(data[i + 7:i + 9], "big")
                i += 2 + size
    except Exception:
        return 0
    return 0


def _src(video) -> str:
    try:
        src = video.get_attribute("src") or ""
        if not src:
            source = video.locator("source")
            src = (source.first.get_attribute("src") or "") if source.count() else ""
        return src
    except Exception:
        return ""


# ---- diagnostics / session check ---------------------------------------------
def inspect(settings: WorkerSettings, ui: dict | None = None, in_project: bool = False) -> dict:
    """Open Flow, classify the session, report which selectors match, save a screenshot.

    Read-only unless `in_project` (the check-flow command): then it also opens a new, empty project, switches the
    Agent toggle off (as every clip does) and opens the clip settings to look for the model menu. That spends no
    credits.
    """
    from playwright.sync_api import sync_playwright

    ui = ui or load_ui()
    stop = StopText(ui)
    label = settings.flow_profile()["model_label"]
    result: dict = {"state": "needs_attention", "message": "", "model": None, "selectors": {}, "screenshot": None}
    with sync_playwright() as p:
        ctx = launch_context(p, settings, headless=True)
        try:
            install_navigation_guard(ctx, ui["allowed_hosts"])
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            pg = Page(page, ui)
            page.goto(ui["app_url"], wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(3000)
            try:
                check_session(pg, stop)
                result["state"], result["message"] = "connected", "Signed in to Google Flow."
            except SignedOut as e:
                result["state"], result["message"] = "signed_out", e.user_message
            except LimitReached as e:
                result["state"], result["message"] = "limit_reached", e.user_message
            except (AccountStop, GeneratorError) as e:
                result["message"] = e.user_message
            if result["state"] == "connected" and in_project:
                dismiss_tips(pg)
                try:
                    open_project(pg, settings)
                    direct_mode(pg)
                    pg.find("settings_trigger", timeout=5).click()
                    page.wait_for_timeout(800)
                    video = pg.find("settings_radio", timeout=3, name=ui["clip_settings"]["kind"])
                    if video is not None and video.get_attribute("aria-checked") != "true":
                        video.click()  # the panel shows the video models once Video is picked
                        page.wait_for_timeout(800)
                except GeneratorError as e:
                    result["message"] += f" {e.user_message}"
            sw = pg.find("model_switch", timeout=2)
            if sw is not None:
                result["model"] = (sw.inner_text() or "").strip().splitlines()[0][:40] or None
                if result["state"] == "connected" and not model_named(result["model"] or "", label):
                    result["message"] += f" Flow shows “{result['model']}”; each clip switches to “{label}”."
            for key in ui["selectors"]:
                if key in ("model_option", "settings_radio"):
                    continue
                result["selectors"][key] = pg.matches(key)
            result["screenshot"] = pg.screenshot(settings, "flow-check")
            page.keyboard.press("Escape")
        finally:
            ctx.close()
    return result


def open_plain_window(settings: WorkerSettings, ui: dict | None = None) -> subprocess.Popen:
    cmd = plain_browser_command(settings, ui or load_ui())
    return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
