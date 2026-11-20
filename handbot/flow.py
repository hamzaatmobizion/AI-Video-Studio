"""One clip in Google Flow, made with the real mouse and keyboard in your own Chrome: open Flow, start a new project,
pick the clip settings, add the pictures, paste the prompt, generate, wait, download."""
import logging
import random
import re
import shutil
import time
from pathlib import Path

from handbot.config import BotSettings
from handbot.screen import Match, Screen, Text, chrome_front, show_app_page

log = logging.getLogger("handbot")

# Every picture the bot looks for (handbot/buttons/<name>.png): whether it's needed, what to capture, and how to
# look for it. Optional ones are clicked when they show up. BOTTOM: only the lower part of the screen (the prompt bar;
# keeps the prompt's + apart from Library's +). EITHER: a settings option, matched selected (white) or not (dark).
BOTTOM, EITHER = {"area": 0.55}, {"either_way": True}
BUTTONS = {
    "search_result_flow": ("required", "the grey \"https://flow.google.com\" line of Flow's Google search result", {}),
    "new_project": ("required", "\"+ New project\" on Flow's home page (it's see-through over a project picture)",
                    {"outline": True}),
    "flow_tab": ("optional", "Flow's icon and \"Google Flow\" in a Chrome tab's title, in the tab bar",
                 {"outline": True, "box": (0, 0, 100000, 70)}),
    "app_tab": ("optional", "your app's tab icon (the purple ▶, static/img/favicon.png) in Chrome's tab bar",
                {"box": (0, 0, 100000, 70)}),
    "flow_home": ("optional", "the ⌂ home icon at the top left of a Flow project, next to its name", {}),
    "got_it": ("optional", "a \"Got it\" button on Flow's tips", {}),
    "agent_on": ("optional", "the Agent pill next to the + while it's ON (clicked to switch it off)", BOTTOM),
    "prompt_box": ("required", "\"What do you want to create?\" in the empty prompt box", BOTTOM),
    "settings": ("required", "\"Video · 720p · 8s\" on the settings chip left of the → (not its aspect icon)", BOTTOM),
    "opt_video": ("required", "Video (with its camera icon) in the settings panel", EITHER),
    "opt_ingredients": ("required", "Ingredients in the settings panel (used when the clip has pictures)", EITHER),
    "opt_frames": ("required", "Frames with its [ ] icon in the settings panel (a scene that starts from the last "
                               "frame)", EITHER),
    # anywhere on the screen: with a long prompt pasted, the prompt bar grows up and Start goes with it
    "frame_start": ("required", "the word \"Start\" in the Start box of the prompt bar, in Frames mode", {}),
    "aspect_9_16": ("required", "9:16 with its tall icon in the settings panel", EITHER),
    "aspect_16_9": ("required", "16:9 with its wide icon in the settings panel", EITHER),
    "model_option": ("required", "\"Veo 3.1 - Lite\" (the clip model's name, without the ▾)", {}),
    "model_menu": ("required", "the ▾ arrow of the model menu in the settings panel", {}),
    "opt_x1": ("required", "x1 in the settings panel", EITHER),
    "opt_8s": ("optional", "8s, if Flow shows it as an option", EITHER),
    "opt_720p": ("optional", "720p, if Flow shows it as an option", EITHER),
    "add_image": ("required", "the + at the left of the prompt bar", BOTTOM),
    "upload_media": ("required", "\"⤒ Upload media\" at the bottom left of the picker the + opens", {}),
    "picker_all": ("required", "\"All\" with its grid icon, top left in the picker the + opens", {}),
    "add_to_prompt": ("required", "the white \"Add to prompt\" button in the picker", {}),
    "i_agree": ("optional", "\"I agree\" on the \"Rights to use this image\" notice", {}),
    "generate": ("required", "the → at the right of the prompt bar, white once a prompt is in", {**BOTTOM, **EITHER}),
    "generating": ("required", "the % sign of the progress (\"25%\") on a tile while its clip is being made", {}),
    "tile_more": ("required", "the ⋮ button that shows at the top right of a tile while the mouse is over it", {}),
    "download": ("required", "\"Download\" with its icon, in the tile's ⋮ menu", {}),
    "download_720p": ("required", "\"720p / Original size\" in the list of sizes that Download opens", {}),
    "failed": ("required", "the red \"Failed\" mark on a clip Flow refused", {}),
    "failed_refund": ("optional", "the words on a failed clip saying its credits will be refunded (such a clip is "
                                  "generated again)", {}),
    "failed_retry": ("optional", "the ↻ retry button at the bottom of a failed clip (sometimes clicked to generate a "
                                 "refunded clip again)", {}),
    "prompt_clear": ("required", "the ✕ at the top right of the prompt bar (shows once a picture or text is in it)",
                     BOTTOM),
    "captcha": ("optional", "a CAPTCHA / \"I'm not a robot\" box", {}),
    "signed_out": ("optional", "Google's \"Sign in\" button", {}),
}
# What the bot searches Google for, one picked at random each time; every one has Flow (flow.google.com) in its results
SEARCHES = ["google flow", "googleflow", "google flow ai", "flow google", "google labs flow", "google flow veo",
            "flow ai google", "google flow video", "flow by google", "google flow app"]
POINTS: dict[str, str] = {}  # spots marked with `mark` (none needed: the new clip is found by its %)
# Around the % of the new clip's progress (top right of its tile): where its ⋮ shows when the mouse is over the tile
# (the page's header has ⋮ buttons too), and where the % is looked for again once found.
NEAR_PCT = (-60, -30, 40, 30)
# Spots on the new clip's tile, from its %: x and y ranges (points) that stay on a tall (9:16) tile and a wide one
ON_TILE = ((-105, 5), (15, 120))


# A settings option's background when it's picked (white, or lighter grey: 242 or 60) and when it isn't (32)
PICKED_SHADE = 46


# The first item in the picker's list (the newest upload), from the top-left of its "All" entry, in points
FIRST_ITEM = (237, 15)
# The picker's list of uploads (names start here), from the same spot: x and y ranges (points)
LIST_BOX = ((100, 340), (-10, 430))
# A picker: the button its list is found from, the list's x and y ranges from it, and the first item's spot from it.
# The + opens one with "All" top left; Start (Frames mode) opens "Select a frame image", which has no "All": its list
# is found from its "Upload media" (the rows are 170 points right of it and 140–430 above it)
PICKER = ("picker_all", LIST_BOX, FIRST_ITEM)
FRAME_PICKER = ("upload_media", ((120, 380), (-455, -20)), (173, -425))
ADD_TRIES = 4  # Add to prompt clicks before the bot gives up on a picture whose upload Flow is still processing


# Words read off the screen. A pop-up button the bot clicks (the whole button says just this):
AGREE = {"i agree", "agree", "accept", "accept all", "got it", "ok", "okay", "continue", "dismiss", "i understand"}
# ...never next to these (it stops for you instead), a CAPTCHA, and a refused clip
MONEY = re.compile(r"\b(buy|purchase|payment|pay|billing|card|subscri\w*|upgrade|plan|price|checkout|credits?)\b", re.I)
CAPTCHA = re.compile(r"not a robot|unusual traffic|verify (that )?you('| a)re (a )?human|recaptcha|captcha", re.I)
FAILED = re.compile(r"^\s*failed\b", re.I)
REFUNDED = re.compile(r"be refunded", re.I)  # one line a failed clip ("You will be refunded…"), not its "refund to appear"
UNUSUAL = re.compile(r"unusual activity", re.I)
LOAD_FAILED = re.compile(r"failed to load", re.I)  # "The video failed to load.": made again with its tile's ↻


def _plain(s: str) -> str:
    return re.sub(r"[^a-z ]", "", s.lower()).strip()


class Stop(Exception):
    """Something only you can sort out (a flag, a CAPTCHA, signed out, a missing button). The screen is left as is."""

    def __init__(self, message: str, shot: Path | None = None):
        super().__init__(message)
        self.message, self.shot = message, shot


class Refunded(Exception):
    """Flow failed the clip with a passing error and refunded its credits: worth generating it again."""


class LoadFailed(Refunded):
    """Flow made the clip but it "failed to load": always made again with the failed tile's ↻."""


class FlowHand:
    def __init__(self, settings: BotSettings, screen: Screen):
        self.s, self.sc = settings, screen
        self.project_gen: int | None = None  # the video whose Flow project that tab shows
        self.uploaded: set[str] = set()  # the pictures uploaded to that project, by file name
        self.last_pct: tuple[int, int] | None = None  # where the clip being waited for showed its %
    def _stop(self, message: str, tag: str) -> Stop:
        return Stop(message, self.sc.save(self.s.logs_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{tag}.jpg"))

    def _maybe(self, name: str, timeout: float = 2) -> bool:
        """Click an optional button if it shows within `timeout`."""
        if not self.sc.has(name) or self.sc.find(name, timeout, **BUTTONS[name][2]) is None:
            return False
        self.sc.click(name, 1, **BUTTONS[name][2])
        log.info("clicked %s", name)
        time.sleep(random.uniform(0.6, 1.2))
        return True

    def _click(self, name: str, timeout: float = 10, why: str = "", **how) -> None:
        try:
            self.sc.click(name, timeout, **{**BUTTONS[name][2], **how})
        except LookupError:
            raise self._stop(f"Couldn't see {BUTTONS[name][1]}{why}. Retake it with  python -m handbot capture "
                             f"{name}  if Flow's look changed.", name)
        log.info("clicked %s", name)
        time.sleep(random.uniform(0.6, 1.3))

    def _blocked(self, texts: list[Text] | None = None) -> None:
        """Stop for what only you can do: a CAPTCHA (never solved by the bot) or Chrome signed out of Google."""
        texts = self.sc.texts() if texts is None else texts
        said = [x.text for x in texts]
        if any(CAPTCHA.search(s) for s in said) or self.sc.has("captcha") and self.sc.locate("captcha"):
            raise self._stop("Google is showing a CAPTCHA (\"I'm not a robot\"). Solve it in Chrome, then press Try "
                             "again.", "captcha")
        if self.sc.has("signed_out") and self.sc.locate("signed_out"):
            raise self._stop("Chrome isn't signed in to Google. Sign in in Chrome, then press Try again.", "signed_out")

    def _agree(self, texts: list[Text] | None = None) -> bool:
        """Click a pop-up's agreeing button ("I agree", "Accept", "Got it", "OK"…), read off the screen. Never one in a
        pop-up about paying, credits or plans: that stops the bot for you instead."""
        texts = self.sc.texts() if texts is None else texts
        for b in texts:
            if _plain(b.text) not in AGREE:
                continue
            near = [x.text for x in texts if abs(x.y - b.y) < 320 and abs(x.x - b.x) < 700]
            if any(MONEY.search(x) for x in near):
                raise self._stop("Flow is asking about payment, credits or a plan: \"" + " / ".join(near[:4])
                                 + "\". The bot never answers those. Check Chrome.", "payment")
            self.sc.click_at(*b.point())
            log.info("clicked \"%s\" on a pop-up", b.text)
            time.sleep(random.uniform(0.8, 1.5))
            return True
        return False

    def _failures(self, texts: list[Text] | None = None) -> tuple[int, int, int]:
        """How many failed clips the project shows, how many of those say their credits are refunded, and how many
        say the video failed to load: from the pictures of the "Failed" mark when taken, and from the words on the
        screen. Counted before each Generate, so failed tiles left from earlier tries don't count again."""
        texts = self.sc.texts() if texts is None else texts
        shot = self.sc.grab()
        said = [x.text for x in texts]
        failed = max(self.sc.count("failed", shot), sum(1 for s in said if FAILED.search(s)))
        refunded = max(self.sc.count("failed_refund", shot), sum(1 for s in said if REFUNDED.search(s)))
        return failed, refunded, sum(1 for s in said if LOAD_FAILED.search(s))

    def _check_failed(self, before: tuple[int, int, int], texts: list[Text] | None = None) -> None:
        texts = self.sc.texts() if texts is None else texts
        now = self._failures(texts)
        if any(a > b for a, b in zip(now, before)):  # read the screen once more: a misread isn't a failed clip
            time.sleep(3)
            texts = self.sc.texts()
            now = tuple(min(a, b) for a, b in zip(now, self._failures(texts)))
        failed, refunded, load = now
        if load > before[2]:
            raise LoadFailed()
        if refunded > before[1]:
            raise Refunded()
        if failed > before[0]:
            flagged = any(UNUSUAL.search(x.text) for x in texts)
            raise self._stop("Flow flagged unusual activity on the clip. Wait an hour or two, then press Try again."
                             if flagged else "Flow refused the clip (\"Failed\"). Read its reason in Chrome.", "failed")

    # ---- the steps -------------------------------------------------------
    def open_project(self) -> None:
        """Start a new Flow project. If a Chrome tab has Flow open (the one in front, or one clicked in the tab bar), go
        to Flow's home page there, no search. Only with no Flow tab (or if its home page can't be reached) search
        Google for Flow and click its result (Flow's address is never typed): in the Flow tab, else a new tab."""
        self.uploaded = set()  # a new project has none of the pictures yet
        on_flow = self._shows_flow() or self._to_flow_tab()
        if on_flow and self._flow_home():
            log.info("Flow is open: starting a new project there (no search)")
            self._click("new_project")
            time.sleep(random.uniform(2, 3.5))
            self._maybe("got_it") or self._agree()
            self._maybe("agent_on")
            return
        log.info("searching Google for Flow (%s)", "couldn't reach Flow's home page in its tab" if on_flow
                 else "no Flow tab open")
        self.sc.keys("command", "l" if on_flow else "t")  # never search in a tab that isn't Flow's (yours)
        self._search(random.choice(SEARCHES))
        if not self._reach_flow():  # the search or suggestion led elsewhere: search the plain way once
            log.info("that search didn't lead to Flow; searching again")
            self.sc.keys("command", "l")
            self._search("google flow", suggest=False)
            if not self._reach_flow():
                self._blocked()
                raise self._stop(f"Couldn't find Flow from Google's results ({BUTTONS['search_result_flow'][1]}).",
                                 "search")
        self._click("new_project")
        time.sleep(random.uniform(2, 3.5))
        self._maybe("got_it") or self._agree()
        self._maybe("agent_on")

    def _shows_flow(self) -> bool:
        """Whether the tab in front shows Flow (its home page, or a project)."""
        return self.sc.locate("new_project", **BUTTONS["new_project"][2]) is not None \
            or self.sc.has("flow_home") and self.sc.locate("flow_home") is not None

    def _in_project(self) -> bool:
        """Whether a Flow project is in front (its tab clicked in the tab bar if need be), not Flow's home page."""
        if not (self._shows_flow() or self._to_flow_tab()):
            return False
        return self.sc.locate("new_project", **BUTTONS["new_project"][2]) is None

    def _to_flow_tab(self) -> bool:
        """Click a Google Flow tab in Chrome's tab bar, if there is one. True once it shows Flow."""
        if not self._maybe("flow_tab", 1):
            return False
        end = time.monotonic() + 15
        while time.monotonic() < end:
            if self._shows_flow():
                return True
            time.sleep(1)
        return False

    def _flow_home(self) -> bool:
        """Get to Flow's home page from Flow (clicking ⌂ on a project page). False if it isn't reached."""
        if self.sc.locate("new_project", **BUTTONS["new_project"][2]):
            return True
        if not self._maybe("flow_home", 2):
            return False
        return self.sc.find("new_project", 20, **BUTTONS["new_project"][2]) is not None

    def _search(self, text: str, suggest: bool = True) -> None:
        """Type a search in the address bar. About 4 times in 10, type only the start of it and take one of Chrome's
        suggestions with the arrow keys, as people often do; otherwise type it all and press Enter."""
        if suggest and random.random() < 0.4:
            self.sc.type_slowly(text[:random.randint(min(6, len(text) - 1), len(text) - 1)])
            time.sleep(random.uniform(0.8, 1.8))  # reading the suggestions
            for _ in range(random.choice([0, 1, 1, 2])):
                self.sc.press("down")
        else:
            self.sc.type_slowly(text)
        self.sc.press("enter")

    def _reach_flow(self) -> bool:
        """Get to Flow's home page from wherever the search went: click Flow's result on Google's page (its grey
        flow.google.com line, or a "Google Flow" link such as the one under Google's own result when the search was
        just "google"), scrolling down once if it isn't in view, or nothing if a suggestion opened Flow itself. False
        if Flow isn't reached."""
        end = time.monotonic() + 15
        while time.monotonic() < end:
            if self.sc.locate("new_project"):
                return True
            if self._click_flow_result():
                return self.sc.find("new_project", 40) is not None
            time.sleep(1)
        self.sc.scroll(-random.randint(4, 7))
        time.sleep(random.uniform(1, 2))
        if self._click_flow_result():
            return self.sc.find("new_project", 40) is not None
        return False

    def _click_flow_result(self) -> bool:
        """Click Flow's result on Google's page: its grey flow.google.com line (`search_result_flow`), else a link
        that reads just "Google Flow", read off the screen below the search box (not the tab bar, the address bar
        or the search typed in the box)."""
        if self.sc.locate("search_result_flow"):
            self._click("search_result_flow")
            return True
        top = self.sc.size()[1] * 0.2
        links = [t for t in self.sc.texts() if t.y > top and _plain(t.text) == "google flow"]
        if not links:
            return False
        self.sc.click_at(*links[0].point())
        log.info("clicked the \"Google Flow\" link in Google's results")
        time.sleep(random.uniform(0.6, 1.3))
        return True

    def choose_settings(self, aspect: str, pictures: bool, frames: bool = False) -> None:
        """Open the settings chip and look: Video, Ingredients (with pictures) or Frames (`frames`), the aspect, the
        model and x1. Video and the mode come first; the aspect, model and x1 in a random order. An option that isn't
        picked is clicked; one that already is, is mostly left alone (now and then clicked anyway, as a person checking
        would). Then the panel is closed (Esc or the chip again)."""
        self._click("settings")
        time.sleep(random.uniform(0.5, 1.2))  # looking the panel over
        for name in ["opt_video", *(["opt_frames"] if frames else ["opt_ingredients"] if pictures else [])]:
            self._pick(name)
        rest = ["aspect_9_16" if aspect == "9:16" else "aspect_16_9", "model", "opt_x1"]
        random.shuffle(rest)
        for name in rest:
            if name != "model":
                self._pick(name)
            elif self.sc.find("model_option", 2) is None:  # the menu shows another model: pick ours from its list
                self._click("model_menu", 5)
                self._click("model_option", 5, " (the model is never swapped for another)")
        self._maybe("opt_8s", 1)
        self._maybe("opt_720p", 1)
        if random.random() < 0.5:  # close the settings
            self.sc.press("escape")
        else:
            self._click("settings", 3)
        time.sleep(0.6)
        # the chip spells out the clip about to be made: still "Video · 720p · 8s", or nothing is generated
        if self.sc.find("settings", 3, **BUTTONS["settings"][2]) is None:
            raise self._stop("The settings chip doesn't say \"Video · 720p · 8s\" any more, so the clip wouldn't be an "
                             "8-second video. Set it in Chrome.", "settings_chip")

    def _pick(self, name: str) -> None:
        """Make sure a settings option is picked: click it if it isn't (its background is dark then), and now and then
        if it already is."""
        m = self.sc.find(name, 5, **BUTTONS[name][2])
        if m is None:
            raise self._stop(f"Couldn't see {BUTTONS[name][1]}.", name)
        picked = self.sc.shade(m) > PICKED_SHADE
        if picked and random.random() > 0.25:
            log.info("%s already picked", name)
            time.sleep(random.uniform(0.2, 0.6))
            return
        self.sc.click_at(*m.point())
        log.info("clicked %s%s", name, " (already picked)" if picked else "")
        time.sleep(random.uniform(0.6, 1.3))

    def add_images(self, images: list[Path], typed: bool = False) -> None:
        """Each picture into the prompt bar as an Ingredient, in order, with the + and its picker (`_put_picture`).
        `typed`: the prompt is already pasted."""
        for path in images:
            self._put_picture(path, PICKER, "add_image")
        # the prompt bar shows its ✕ once something is in it: with nothing typed yet, only the pictures can show it
        if images and not typed and self.sc.find("prompt_clear", 8, **BUTTONS["prompt_clear"][2]) is None:
            raise self._stop("The picture didn't arrive in the prompt bar, so the clip would be made without the "
                             "character. Check Flow's picker in Chrome.", "pictures")

    def set_start_frame(self, path: Path) -> None:
        """Frames mode: the previous clip's last frame into Start (its own picker, "Select a frame image")."""
        self._put_picture(path, FRAME_PICKER, "frame_start")
        if not self.sc.wait_gone("frame_start", 8):  # the Start box shows the picture instead of its word
            self.sc.save(self.s.logs_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-start-frame.jpg")
            log.warning("the Start box still shows \"Start\" after adding %s", path.name)

    def _put_picture(self, path: Path, picker: tuple, opener: str) -> None:
        """One picture in through a picker that `opener` opens (+, or Start in Frames mode). One uploaded to this
        project before (a character in an earlier scene) is found by its name in the picker's list and picked; a new
        one (such as the previous clip's last frame), or one that can't be picked, is uploaded: Upload media, then the
        Mac file dialog (the path pasted after ⌘⇧G), and the upload is picked in the list. Then Add to prompt."""
        self._click(opener)
        picked = self._pick_uploaded(path, picker) if path.name in self.uploaded else None
        if picked == "added":  # clicking the row added it at once
            return
        if picked == "picked":
            self._add_to_prompt(path)
            return
        if self.sc.find("upload_media", 3) is None:  # the picker closed meanwhile: open it again
            self._click(opener)
        self._open_file_dialog(path)
        self.sc.keys("command", "shift", "g")
        time.sleep(0.6)
        # pasted, not typed: key by key, the temp folder's "__" opened the Emoji viewer, which took the rest
        self.sc.paste(str(path.resolve()))
        self.sc.press("enter")
        time.sleep(random.uniform(0.8, 1.4))
        self.sc.press("enter")  # Open
        time.sleep(random.uniform(4, 6))  # the upload; it's put first in the list
        anchor = self.sc.find(picker[0], 10)
        if anchor is None:
            raise self._stop(f"The picker didn't come back after uploading {path.name}.", "picker")
        row = self._listed(path, anchor, picker[1])
        first = (anchor.x + picker[2][0] + random.randint(-40, 40), anchor.y + picker[2][1] + random.randint(-6, 6))
        self.sc.click_at(*(row.point() if row else first))
        log.info("picked the upload %s%s", path.name, "" if row else " (first in the list)")
        time.sleep(random.uniform(1, 2))
        self._add_to_prompt(path)
        self.uploaded.add(path.name)

    def _open_file_dialog(self, path: Path) -> None:
        """Click Upload media and wait for the Mac file dialog (its Cancel and Open buttons read off the screen), so
        the path is never typed into Flow's page instead. Clicked once more if it doesn't open."""
        for attempt in range(2):
            self._click("upload_media", 6)
            end = time.monotonic() + 8
            while time.monotonic() < end:
                time.sleep(1)
                words = {t.text.strip().lower() for t in self.sc.texts()}
                if {"cancel", "open"} <= words:
                    time.sleep(random.uniform(0.3, 0.8))
                    return
            log.info("the file dialog didn't open for %s (try %d)", path.name, attempt + 1)
        raise self._stop(f"The Mac file dialog didn't open after clicking Upload media for {path.name}.", "file_dialog")

    def _pick_uploaded(self, path: Path, picker: tuple) -> str | None:
        """In the open picker, click the row of a picture uploaded to this project before (the list shows every upload
        by name). "picked": Add to prompt shows for it. "added": the picker closed at once, with the picture in the
        prompt bar (the picker opens with its top row selected, and clicking a selected row adds it). None if it isn't
        listed or nothing happened: then it's uploaded."""
        anchor = self.sc.find(picker[0], 10)
        if anchor is None:
            raise self._stop("Flow's picker didn't open.", "picker")
        row = self._listed(path, anchor, picker[1])
        if row is None:
            log.info("%s isn't in the picker's list; uploading it", path.name)
            return None
        self.sc.click_at(*row.point())
        end = time.monotonic() + 5
        while time.monotonic() < end:
            time.sleep(0.7)
            if self.sc.locate("add_to_prompt"):
                log.info("picked %s, already in the project (not uploaded again)", path.name)
                time.sleep(random.uniform(0.5, 1.2))
                return "picked"
            if self.sc.locate("upload_media") is None:
                log.info("clicking %s added it to the prompt at once (not uploaded again)", path.name)
                time.sleep(random.uniform(0.5, 1.2))
                return "added"
        self.sc.save(self.s.logs_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-reuse-{path.stem}.jpg")
        log.info("picking %s in the list did nothing; uploading it", path.name)
        return None

    def _listed(self, path: Path, anchor: Match, box: tuple) -> Text | None:
        """The picture's row in the picker's list (`box` from the picker's `anchor` button), found by its name's 6 content digits read
        off the screen. Letters that the text recognition mixes up with digits are read as those digits."""
        def plain(s: str) -> str:
            return s.lower().translate(str.maketrans("oil", "011"))

        key = plain(path.name.split("-")[0])
        x0, x1, y0, y1 = (anchor.x + box[0][0], anchor.x + box[0][1], anchor.y + box[1][0], anchor.y + box[1][1])
        for t in self.sc.texts():
            if key in plain(t.text) and x0 <= t.x <= x1 and y0 <= t.y <= y1:
                return t
        return None

    def _add_to_prompt(self, path: Path) -> None:
        """Click Add to prompt until the picker closes. While Flow is still processing the upload it ignores the click
        and the picker stays open, and then its + shows as ✕, so the next picture's + can't be found."""
        for attempt in range(1, ADD_TRIES + 1):
            end = time.monotonic() + (20 if attempt == 1 else 2)  # it shows once the upload is picked
            while self.sc.locate("add_to_prompt") is None:
                if self.sc.locate("upload_media") is None:  # the picker closed: the picture went in (or a slow close)
                    log.info("the picker closed by itself with %s", path.name)
                    return
                if time.monotonic() >= end:
                    raise self._stop(f"Couldn't see {BUTTONS['add_to_prompt'][1]}.", "add_to_prompt")
                time.sleep(0.7)
            self._click("add_to_prompt", 2)
            time.sleep(random.uniform(1, 2))
            self._maybe("i_agree", 1) or self._agree()  # "Rights to use this image": I agree
            if self.sc.wait_gone("add_to_prompt", 6):
                return
            log.info("the picker is still open after Add to prompt for %s (try %d)", path.name, attempt)
            time.sleep(random.uniform(3, 5))  # the upload is still being processed
        raise self._stop(f"Flow's picker stayed open after Add to prompt for {path.name}: the upload may not have "
                         "finished. Check the picker in Chrome.", "add_to_prompt")

    def paste_prompt(self, prompt: str) -> None:
        self._click("prompt_box")
        self.sc.paste(prompt)
        time.sleep(random.uniform(1, 2))

    def generate(self) -> None:
        self._click("generate")
        self.sc.save(self.s.logs_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-sent.jpg")

    def wait_and_download(self, dest: Path, failed_before: tuple[int, int, int]) -> Path:
        """Wait for the new clip to be made, then download it. While it's being made its tile is the only thing showing
        a % (its ▶ shows then too, so that can't tell); the clip is done once that % has gone."""
        before = {p.name for p in self.s.DOWNLOAD_DIR.glob("*.mp4")}
        start = time.monotonic()
        end = start + self.s.GENERATION_TIMEOUT_SECONDS
        pct, gone = None, 0  # where the new clip's % is, once seen
        self.last_pct = None  # kept for a retry: a failed clip's tile is where its % was
        time.sleep(8)
        while time.monotonic() < end:
            texts = self.sc.texts()
            self._blocked(texts)
            self.last_pct = pct
            self._check_failed(failed_before, texts)
            self._agree(texts)
            near = None if pct is None else (pct[0] + NEAR_PCT[0], pct[1] + NEAR_PCT[1],
                                             pct[0] + NEAR_PCT[2], pct[1] + NEAR_PCT[3])
            m = self.sc.locate("generating", box=near)
            if m:
                if pct is None:
                    log.info("the clip is being made")
                pct, gone = (m.x, m.y), 0
            elif pct is None and time.monotonic() - start > 120:
                raise self._stop("The new clip never showed its progress (%). Check that it started in Chrome.",
                                 "progress")
            elif pct is not None:
                gone += 1
                if gone >= 2:  # gone twice in a row: done, not a moment's flicker
                    log.info("the clip is done; downloading it")
                    return self._download(pct, before, dest)
            if random.random() < 0.3:  # now and then the mouse drifts, as a waiting hand's does
                w, h = self.sc.size()
                self.sc.move(int(w * random.uniform(0.3, 0.8)), int(h * random.uniform(0.3, 0.7)))
            time.sleep(random.uniform(5, 9))
        raise self._stop(f"No clip after {self.s.GENERATION_TIMEOUT_SECONDS // 60} minutes.", "timeout")

    def _download(self, pct: tuple[int, int], before: set[str], dest: Path) -> Path:
        """Hover the new clip's tile (just below and left of where its % was), open its ⋮ menu, Download, then 720p.
        If no download starts, the menu is opened and 720p clicked once more."""
        near = (pct[0] + NEAR_PCT[0], pct[1] + NEAR_PCT[1], pct[0] + NEAR_PCT[2], pct[1] + NEAR_PCT[3])
        for attempt in range(2):
            if attempt:
                log.info("no download started; opening the menu again")
                self.sc.press("escape")
                time.sleep(random.uniform(1, 2))
            time.sleep(random.uniform(1, 3))
            for _ in range(random.randint(1, 3)):  # onto the video, drifting over it a little, as a hand does
                self.sc.hover(pct[0] + random.randint(*ON_TILE[0]), pct[1] + random.randint(*ON_TILE[1]))
                time.sleep(random.uniform(0.3, 1.0))
            self._click("tile_more", 4, box=near)
            self._click("download", 4)
            # into the list of sizes: straight along the Download row first, then down to 720p, so the list stays open
            size = self.sc.find("download_720p", 4)
            if size is None:
                raise self._stop(f"Couldn't see {BUTTONS['download_720p'][1]}.", "download_720p")
            x, y = size.point()
            self.sc.move_straight(x, self.sc.position()[1])
            self.sc.move_straight(x, y)
            time.sleep(random.uniform(0.3, 0.6))
            self.sc.mouse_click()
            log.info("clicked download_720p")
            if self._download_started(before):
                return self._downloaded(before, dest)
        raise self._stop(f"Clicked 720p twice but no download started in {self.s.DOWNLOAD_DIR}. Check Chrome's download "
                         "folder (Settings → Downloads) is that folder and doesn't ask where to save.", "download")

    def _download_started(self, before: set[str]) -> bool:
        """Whether a new file (finished, or still being written by Chrome) shows up in Downloads within 30 s."""
        end = time.monotonic() + 30
        while time.monotonic() < end:
            if any(p.name not in before and (p.suffix in (".mp4", ".crdownload"))
                   for p in self.s.DOWNLOAD_DIR.iterdir()):
                return True
            time.sleep(1)
        return False

    def _downloaded(self, before: set[str], dest: Path) -> Path:
        """The new MP4 in Downloads once Chrome has finished writing it, moved to `dest`."""
        end, last = time.monotonic() + 300, None
        while time.monotonic() < end:
            new = [p for p in self.s.DOWNLOAD_DIR.glob("*.mp4") if p.name not in before]
            if new:
                f = max(new, key=lambda p: p.stat().st_mtime)
                size = f.stat().st_size
                if size and size == last and not f.with_name(f.name + ".crdownload").exists():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(f), dest)
                    log.info("clip saved: %s", dest)
                    return dest
                last = size
            time.sleep(2)
        raise self._stop("The clip didn't finish downloading into DOWNLOAD_DIR.", "download")

    def show_app(self, app_url: str, path: str = "/flow") -> None:
        """Back to your app: click its tab in Chrome's tab bar (by its purple ▶ icon), load `path` there (the Flow page,
        or between scenes the video's by-hand page with the next scene), then scroll down a few steps at a time. With
        no tab of the app open, the page is opened in a new tab."""
        chrome_front()
        if self.sc.has("app_tab") and self.sc.find("app_tab", 2, **BUTTONS["app_tab"][2]):
            self._click("app_tab", 2)
        else:
            log.info("no tab of your app in the tab bar; opening %s", path)
        show_app_page(app_url, path)
        time.sleep(random.uniform(2, 4))  # the page updates itself once the video is in
        for _ in range(random.randint(2, 4)):  # down to the video, a few steps at a time
            self.sc.scroll(-random.randint(2, 5))
        if random.random() < 0.4:  # a little back up now and then, as people do
            self.sc.scroll(random.randint(1, 3))

    def _retry_failed(self) -> bool:
        """Click the ↻ on the failed clip's tile (below and left of where its % was), which makes it again with the
        same prompt and pictures. If its % was never seen, the ↻ is clicked only when it's the one on the screen. False
        if it isn't there (then the prompt bar is filled in again)."""
        if not self.sc.has("failed_retry"):
            return False
        if self.last_pct is None:
            if self.sc.count("failed_retry") != 1:
                return False
            m = self.sc.find("failed_retry", 3)
            if m is None:
                return False
            x, y = m.x - random.randint(40, 120), m.y - random.randint(80, 200)
        else:
            x, y = self.last_pct
            m = self.sc.find("failed_retry", 3, box=(x - 160, y + 60, x + 20, y + 330))
            if m is None:
                return False
            x, y = x - random.randint(20, 100), y + random.randint(40, 140)
        self.sc.hover(x, y)  # onto the failed tile first
        time.sleep(random.uniform(0.4, 1.0))
        self.sc.click_at(*m.point())
        log.info("clicked the failed clip's ↻ to make it again")
        return True

    def _fill(self, prompt: str, aspect: str, images: list[Path], settings: bool, frames: bool = False) -> None:
        """Set the settings (Video, aspect…), add the pictures (or the Start frame, `frames`) and paste the prompt, in
        a random order each time, as a person does it differently from one clip to the next. The pictures may come
        before the settings only when the prompt bar already shows the + (Ingredients picked in this project before):
        a new project opens in Frames, with Start and End but no +. The Start frame always comes after the settings,
        which pick Frames. Generate is clicked after."""
        steps = ["prompt", *(["images"] if images else []), *(["settings"] if settings else [])]
        random.shuffle(steps)
        if "settings" in steps and "images" in steps and steps.index("settings") > steps.index("images") and (
                frames or self.sc.locate("add_image", **BUTTONS["add_image"][2]) is None):
            steps.remove("settings")
            steps.insert(steps.index("images"), "settings")
        log.info("this clip's order: %s%s", ", ".join(steps), " (from the start frame)" if frames else "")
        typed = False
        for step in steps:
            if step == "settings":
                self.choose_settings(aspect, bool(images), frames)
            elif step == "images" and frames:
                self.set_start_frame(images[0])
            elif step == "images":
                self.add_images(images, typed)
            else:
                self.paste_prompt(prompt)
                typed = True
            time.sleep(random.uniform(0.5, 1.5))

    def make_clip(self, gen_id: int, prompt: str, aspect: str, images: list[Path], dest: Path,
                  frames: bool = False) -> Path:
        """One scene's clip. A video's scenes all go in one Flow project: only its first scene opens Flow and starts
        the project; the next ones are made in the same project, in the same tab. `frames`: made in Frames mode, from
        the Start frame `images[0]` (the previous clip's last frame), else with the pictures as Ingredients."""
        chrome_front()
        try:
            if self.project_gen == gen_id and not self._in_project():
                log.info("the video's Flow project isn't showing any more; starting a new one")
                self.project_gen = None
            if self.project_gen != gen_id:
                self.open_project()
                self.project_gen = gen_id
            else:
                log.info("next scene, in the same Flow project")
                self._maybe("prompt_clear", 2)  # anything Flow left in the prompt bar from the scene before
            load = False  # the last try's video failed to load
            for attempt in range(self.s.REFUND_RETRIES + 1):
                if attempt:
                    log.info("%s; generating it again (try %d)", "The video failed to load" if load
                             else "Flow refunded the clip", attempt + 1)
                    time.sleep(random.uniform(5, 12) if load else random.uniform(20, 40))
                # a refunded clip is made again either with its tile's ↻ (about half the time), or from the prompt bar;
                # one that failed to load always with its ↻
                if attempt and (load or random.random() < 0.5) and self._retry_failed():
                    time.sleep(random.uniform(3, 5))  # the tile turns back into a clip being made
                    failed_before = self._failures()
                else:
                    if attempt:  # the same prompt and pictures again, from an empty prompt bar
                        self._maybe("prompt_clear", 2)
                    self._fill(prompt, aspect, images, settings=not attempt, frames=frames)
                    failed_before = self._failures()
                    self.generate()
                try:
                    return self.wait_and_download(dest, failed_before)
                except Refunded as e:
                    load = isinstance(e, LoadFailed)
                    continue
            raise self._stop(f"Flow failed the clip {self.s.REFUND_RETRIES + 1} times (refunded, or the video failed "
                             "to load). Try it in Chrome yourself.", "refunded")
        except Stop:
            self.project_gen = None  # the project may be half filled in: the retry starts a fresh one
            raise
