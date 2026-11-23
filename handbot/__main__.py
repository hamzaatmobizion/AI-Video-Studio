"""python -m handbot run | capture <button> | mark <point> | check"""
import argparse
import ctypes
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from handbot.config import get_settings
from handbot.flow import BUTTONS, POINTS


def cmd_run(_args) -> int:
    from handbot.loop import run

    s = get_settings()
    if s.problems():
        for p in s.problems():
            print(f"✗ {p}")
        return 1
    # keep the screen awake and unlocked while the bot runs: it can't see or click a sleeping screen
    subprocess.Popen(["caffeinate", "-di", "-w", str(os.getpid())])
    try:
        run(s)
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


def cmd_capture(args) -> int:
    """After a countdown (time to bring Chrome to the front, hover a tile or open a menu), take a screenshot and show it;
    the box dragged round the button is saved as the picture the bot looks for."""
    if args.name not in BUTTONS:
        print(f"Unknown button '{args.name}'. One of: {', '.join(BUTTONS)}")
        return 1
    import cv2

    s = get_settings()
    s.buttons_dir.mkdir(exist_ok=True)
    dest = s.buttons_dir / f"{args.name}.png"
    print(f"Show {BUTTONS[args.name][1]} in Chrome now (⌘Tab to it; hover or open a menu if needed)…")
    for n in range(args.delay, 0, -1):
        print(n, end=" ", flush=True)
        time.sleep(1)
    shot_path = s.BOT_DATA_PATH / "capture.png"
    s.ensure_dirs()
    subprocess.run(["screencapture", "-x", str(shot_path)], check=True)
    shot = cv2.imread(str(shot_path))
    shot_path.unlink(missing_ok=True)
    if shot is None or not shot.any():
        print("\n✗ The screenshot is black: allow Screen Recording for this terminal, then restart it.")
        return 1
    k = min(1.0, 1500 / shot.shape[1])  # fit the window on the screen; the picture is cut from the full-size shot
    view = cv2.resize(shot, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
    print(f"\nIn the window: drag a tight box round {args.name}, then press Enter (Esc or c cancels).")
    x, y, w, h = cv2.selectROI(f"capture {args.name}", view, showCrosshair=False)
    cv2.destroyAllWindows()
    cv2.waitKey(1)
    if w < 3 or h < 3:
        print("Cancelled.")
        return 1
    import pyautogui

    from handbot.screen import PICTURE_SCALE, to_scale

    x0, y0, x1, y1 = (int(round(v / k)) for v in (x, y, x + w, y + h))
    # kept at a Retina screen's scale whatever this screen's, so the picture works on every Mac
    picture = to_scale(shot[y0:y1, x0:x1], PICTURE_SCALE / (shot.shape[1] / pyautogui.size().width))
    cv2.imwrite(str(dest), picture)
    print(f"Saved {dest} ({picture.shape[1]}×{picture.shape[0]})")
    return 0


def cmd_mark(args) -> int:
    """Remember where the mouse is after a 5-second countdown."""
    if args.name not in POINTS:
        print(f"Unknown point '{args.name}'. One of: {', '.join(POINTS)}")
        return 1
    import pyautogui

    s = get_settings()
    s.ensure_dirs()
    print(f"Put the mouse on {POINTS[args.name]}…")
    for n in range(5, 0, -1):
        print(n, end=" ", flush=True)
        time.sleep(1)
    x, y = pyautogui.position()
    try:
        points = json.loads(s.points_path.read_text())
    except (OSError, ValueError):
        points = {}
    points[args.name] = [x, y]
    s.points_path.write_text(json.dumps(points))
    print(f"\nSaved {args.name} at {x},{y}")
    return 0


def _mac_problems(s) -> list[str]:
    """What this Mac still needs: the terminal's Accessibility and Screen Recording permissions, and Chrome saving
    downloads into DOWNLOAD_DIR without asking, at 100% zoom on Flow."""
    out = []
    ax = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
    ax.AXIsProcessTrusted.restype = ctypes.c_bool
    if not ax.AXIsProcessTrusted():
        out.append("Allow Accessibility for this terminal (System Settings → Privacy & Security), then restart it.")
    cg = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
    if not cg.CGPreflightScreenCaptureAccess():
        out.append("Allow Screen Recording for this terminal (System Settings → Privacy & Security), then restart it.")
    chrome = Path("~/Library/Application Support/Google/Chrome").expanduser()
    try:
        profile = json.loads((chrome / "Local State").read_text()).get("profile", {}).get("last_used") or "Default"
        prefs = json.loads((chrome / profile / "Preferences").read_text())
    except (OSError, ValueError):
        return out + ["Open Chrome once and sign in to Google and to Flow (no Chrome settings found)."]
    download = prefs.get("download", {})
    if download.get("prompt_for_download"):
        out.append("In Chrome, Settings → Downloads: turn off \"Ask where to save each file\".")
    folder = Path(download.get("default_directory") or "~/Downloads").expanduser()
    if folder.resolve() != s.DOWNLOAD_DIR.resolve():
        out.append(f"Chrome saves downloads in {folder}, not {s.DOWNLOAD_DIR}: change one of them "
                   "(DOWNLOAD_DIR in .env).")
    zoom = prefs.get("partition", {})
    default = zoom.get("default_zoom_level", {}).get("x", 0)
    flow = zoom.get("per_host_zoom_levels", {}).get("x", {}).get("flow.google.com", {})
    if abs(flow.get("zoom_level", default)) > 0.01:
        out.append("Chrome's zoom on Flow isn't 100%: press ⌘0 on Flow's page.")
    return out


def cmd_check(_args) -> int:
    """What this Mac still needs, which button pictures are taken, and which of them are on the screen right now."""
    from handbot.screen import Screen

    s = get_settings()
    for p in s.problems() + _mac_problems(s):
        print(f"✗ {p}")
    sc = Screen(s)
    shot = sc.grab()
    if not shot.any():
        print("✗ The screenshot is black: allow Screen Recording for this terminal (System Settings → Privacy & "
              "Security), then restart it.")
        return 1
    for name, (need, what, how) in BUTTONS.items():
        if not sc.has(name):
            print(f"  {'✗' if need == 'required' else '·'} {name:20s} not taken yet ({what})")
            continue
        m = sc.locate(name, shot, **how)
        print(f"  {'✓' if m else ' '} {name:20s} {f'on screen ({m.score:.2f})' if m else 'not on screen now'}")
    for name, what in POINTS.items():
        p = sc.point(name)
        print(f"  {'✓' if p else '✗'} point {name:14s} {p or f'not marked ({what})'}")
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="python -m handbot")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run").set_defaults(fn=cmd_run)
    p = sub.add_parser("capture")
    p.add_argument("name")
    p.add_argument("--delay", type=int, default=5, help="seconds before the screenshot (default 5)")
    p.set_defaults(fn=cmd_capture)
    p = sub.add_parser("mark")
    p.add_argument("name")
    p.set_defaults(fn=cmd_mark)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
