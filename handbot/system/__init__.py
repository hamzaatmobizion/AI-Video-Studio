"""The parts of the bot that differ by system: macOS (mac.py), Windows (windows.py) or Linux (linux.py). Everything
else (the Flow steps, the queue, the uploads) is the same on all three. Each part has the same names:

NAME, MOD (the shortcut key), SWITCH_KEY, ZOOM_KEY, FILE_DIALOG, TAB_CONTROL, CHROME_DIR, BLACK_SHOT,
grab(), screenshot(path), read_text(image, size), chrome_front(), show_app_page(base, path), notify(title, message),
keep_awake(), put_file_path(screen, path), dialog_open(lines), problems(settings)."""
from handbot.config import SYSTEM

if SYSTEM == "mac":
    from handbot.system.mac import *  # noqa: F403
    from handbot.system import mac as current
elif SYSTEM == "windows":
    from handbot.system.windows import *  # noqa: F403
    from handbot.system import windows as current
else:
    from handbot.system.linux import *  # noqa: F403
    from handbot.system import linux as current
