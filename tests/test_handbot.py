"""The hand bot's parts that don't need a screen: reading Tesseract's words as lines (Windows and Linux), which button
picture each system uses, and which system part is loaded."""
import importlib

import pytest

from handbot.system.ocr import Word, dedupe, lines


def w(text, x, y, width=None, h=20, conf=90.0):
    return Word(text, x, y, width or len(text) * 10, h, conf)


def test_words_close_together_on_one_line_are_one_line():
    words = [w("What", 100, 500), w("do", 148, 502), w("you", 176, 503), w("want", 214, 500), w("to", 260, 501),
             w("create?", 286, 500)]
    [line] = lines(words, 1.0)
    assert line.text == "What do you want to create?" and (line.x, line.y) == (100, 500)


def test_two_buttons_side_by_side_stay_apart():
    # Windows' file dialog: "Open" and "Cancel" with a button's padding between them
    out = lines([w("Open", 1000, 900), w("Cancel", 1080, 900)], 1.0)
    assert [t.text for t in out] == ["Open", "Cancel"]


def test_words_on_different_lines_stay_apart_and_are_in_reading_order():
    out = lines([w("Download", 300, 240), w("Google", 100, 100), w("Flow", 172, 101)], 1.0)
    assert [t.text for t in out] == ["Google Flow", "Download"]


def test_lines_are_placed_in_mouse_points():
    [line] = lines([w("Generate", 400, 600, width=160, h=40)], 4.0)  # read at 4 pixels per point
    assert (line.x, line.y, line.w, line.h) == (100, 150, 40, 10)


def test_a_word_read_both_ways_is_kept_once_the_surer_reading():
    kept = dedupe([w("Agree", 100, 100, conf=60), w("Agree", 101, 101, conf=95), w("Cancel", 300, 100)])
    assert sorted((k.text, k.conf) for k in kept) == [("Agree", 95), ("Cancel", 90)]


def test_a_system_uses_its_own_button_picture_else_the_macs(tmp_path, monkeypatch):
    from handbot import config

    monkeypatch.setattr(config, "HERE", tmp_path)
    (tmp_path / "buttons" / "windows").mkdir(parents=True)
    (tmp_path / "buttons" / "generate.png").write_bytes(b"mac")
    (tmp_path / "buttons" / "new_project.png").write_bytes(b"mac")
    (tmp_path / "buttons" / "windows" / "generate.png").write_bytes(b"windows")
    monkeypatch.setattr(config, "SYSTEM", "windows")
    s = config.BotSettings(_env_file=None)
    assert s.capture_dir == tmp_path / "buttons" / "windows"
    assert s.button_path("generate").read_bytes() == b"windows"
    assert s.button_path("new_project").read_bytes() == b"mac"  # not captured on Windows yet
    monkeypatch.setattr(config, "SYSTEM", "mac")
    assert s.capture_dir == tmp_path / "buttons"  # a Mac keeps its pictures where they always were
    assert s.button_path("generate").read_bytes() == b"mac"


@pytest.mark.parametrize("name", ["mac", "windows", "linux"])
def test_each_system_part_has_every_name_the_bot_uses(name):
    part = importlib.import_module(f"handbot.system.{name}")
    for attr in ("NAME", "MOD", "SWITCH_KEY", "ZOOM_KEY", "FILE_DIALOG", "TAB_CONTROL", "CHROME_DIR", "BLACK_SHOT",
                 "grab", "screenshot", "read_text", "chrome_front", "show_app_page", "notify", "keep_awake",
                 "put_file_path", "dialog_open", "problems"):
        assert hasattr(part, attr), f"{name} has no {attr}"
    assert part.NAME == name and part.MOD == ("command" if name == "mac" else "ctrl")


def test_the_mac_part_is_loaded_on_a_mac():
    from handbot import config, system

    if config.SYSTEM == "mac":
        assert system.current.NAME == "mac" and system.MOD == "command" and system.TAB_CONTROL


def test_file_dialog_keys_on_each_system(tmp_path, monkeypatch):
    import time

    monkeypatch.setattr(time, "sleep", lambda _s: None)
    path = tmp_path / "3fa2c1-boy.jpg"
    for name, expected in [("mac", [("keys", ("command", "shift", "g")), ("paste", str(path.resolve())),
                                    ("press", "enter"), ("press", "enter")]),
                           ("windows", [("keys", ("alt", "n")), ("paste", str(path.resolve())), ("press", "enter")]),
                           ("linux", [("keys", ("ctrl", "l")), ("paste", str(path.resolve())), ("press", "enter")])]:
        done = []

        class FakeScreen:
            def keys(self, *k):
                done.append(("keys", k))

            def paste(self, text):
                done.append(("paste", text))

            def press(self, key):
                done.append(("press", key))

        importlib.import_module(f"handbot.system.{name}").put_file_path(FakeScreen(), path)
        assert done == expected, name


def test_the_file_dialog_is_seen_on_each_system():
    from handbot.system import linux, mac, windows

    # the Mac: its Cancel and Open buttons, each a line of its own (as before)
    assert mac.dialog_open(["Recents", "Cancel", "Open"])
    assert not mac.dialog_open(["Cancel"]) and not mac.dialog_open(["Open Cancel"])
    # Windows: Tesseract can miss "Open" on its coloured button; "File name:" shows the dialog is open
    assert windows.dialog_open(["File name:", "Cancel"]) and windows.dialog_open(["Open Cancel"])
    assert not windows.dialog_open(["Cancel"]) and not windows.dialog_open(["File name:"])
    # Linux (GTK): Cancel with Open, Location or Recent
    assert linux.dialog_open(["Cancel", "Recent"]) and linux.dialog_open(["Cancel", "Location:"])
    assert not linux.dialog_open(["Cancelled", "Open"])  # whole words only
