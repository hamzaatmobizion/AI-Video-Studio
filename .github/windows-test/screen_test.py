"""Windows test of the hand bot's own Windows parts on a local page (not Google): Chrome brought forward, Ctrl+L and
a pasted address, Tesseract reading the screen, a click by what it read, the Windows file dialog (system.dialog_open,
system.put_file_path) and the picture really picked. Screenshots go to handbot-data/shots/."""
import sys
import time
from pathlib import Path

from handbot import system
from handbot.config import get_settings
from handbot.screen import Screen

s = get_settings()
s.ensure_dirs()
sc = Screen(s)
shots = s.BOT_DATA_PATH / "shots"
shots.mkdir(exist_ok=True)
page = Path(".github/windows-test/test.html").resolve()
pic = (s.BOT_DATA_PATH / "3fa2c1-boy.png").resolve()
pic.write_bytes(Path("static/img/favicon.png").read_bytes())
results = {}


def read(text: str, tries: int = 5):
    lines, took = [], 0.0
    for _ in range(tries):
        t0 = time.monotonic()
        lines = sc.texts()
        took = time.monotonic() - t0
        hit = next((t for t in lines if text.lower() in t.text.lower()), None)
        if hit:
            return hit, lines, took
        time.sleep(1.5)
    return None, lines, took


print("screen:", sc.size(), "scaling:", system.current.scaling(), "%")
print("problems:", system.problems(s))
t0 = time.monotonic()
system.chrome_front()
print(f"1. chrome_front {time.monotonic() - t0:.1f}s")
time.sleep(3)
sc.save(shots / "0-chrome.jpg")
sc.keys(system.MOD, "l")
sc.paste(page.as_uri())
sc.press("enter")
time.sleep(4)
sc.save(shots / "1-page.jpg")
hit, lines, took = read("Upload media")
print(f"2. OCR {took:.1f}s, {len(lines)} lines: {[t.text for t in lines][:25]}")
results["page_opened_and_read"] = hit is not None
results["prompt_box_read"] = any("what do you want" in t.text.lower() for t in lines)
if hit:
    sc.click_at(*hit.point())
    opened = False
    for _ in range(10):
        time.sleep(1)
        lines = sc.texts()
        if system.dialog_open([t.text for t in lines]):
            opened = True
            break
    sc.save(shots / "2-dialog.jpg")
    print("3. dialog lines:", [t.text for t in lines][:30])
    results["file_dialog_seen"] = opened
    if opened:
        system.put_file_path(sc, pic)
        time.sleep(3)
        sc.save(shots / "3-after.jpg")
        hit, lines, _ = read("Chosen")
        print("4. page says:", [t.text for t in lines if "chosen" in t.text.lower() or "picked" in t.text.lower()])
        results["picture_picked"] = hit is not None
for k, v in results.items():
    print(f"RESULT {k}: {'PASS' if v else 'FAIL'}")
sys.exit(0 if results and all(results.values()) else 1)
