"""python -m handbot run | capture <button> | mark <point> | check"""
import argparse
import json
import logging
import os
import subprocess
import sys
import time

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
    x0, y0, x1, y1 = (int(round(v / k)) for v in (x, y, x + w, y + h))
    cv2.imwrite(str(dest), shot[y0:y1, x0:x1])
    print(f"Saved {dest} ({x1 - x0}×{y1 - y0})")
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


def cmd_check(_args) -> int:
    """Which button pictures are taken, and which of them are on the screen right now."""
    from handbot.screen import Screen

    s = get_settings()
    for p in s.problems():
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
