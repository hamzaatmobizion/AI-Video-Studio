"""Windows test: make a test account, sign in to the running app, press AutoFlow's "Start the bot", and report
what the AutoFlow page and the bot's log say."""
import re
import sys
import time
from pathlib import Path

import httpx
from sqlalchemy import select

from app.auth import hash_password
from app.config import get_settings
from app.db import Database
from app.models import User

EMAIL, PW = "bot@example.com", "windows-test-password-1"
settings = get_settings()
db = Database(settings.DATABASE_URL)
with db.session() as s:
    if not s.scalar(select(User).where(User.email == EMAIL)):
        s.add(User(email=EMAIL, password_hash=hash_password(PW)))
        s.commit()

c = httpx.Client(base_url="http://127.0.0.1:8000", follow_redirects=True, timeout=30)


def csrf(html: str) -> str:
    return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)


def chips(html: str) -> list[str]:
    return [re.sub(r"<[^>]+>", "", x).strip() for x in re.findall(r'class="af-chip[^"]*">(.*?)</span>', html, re.S)]


r = c.post("/login", json={"email": EMAIL, "password": PW}, headers={"X-CSRF-Token": csrf(c.get("/login").text)})
print("login:", r.status_code)
page = c.get("/autoflow").text
print("before:", chips(page))
r = c.post("/autoflow/bot", headers={"X-CSRF-Token": csrf(page)})
print("start the bot:", r.status_code, r.text[:200])
running = False
for _ in range(20):
    time.sleep(3)
    page = c.get("/autoflow").text
    if any("bot is running" in x for x in chips(page)):
        running = True
        break
print("after:", chips(page))
m = re.search(r"The bot exited.*?</details>", page, re.S)
if m:
    print("exited block:", re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(0)))[:900])
log = Path(settings.VAR_DIR) / "logs" / "handbot.log"
print("handbot.log:", log.read_text(errors="replace")[-1500:] if log.exists() else "(none)")
print("RESULT autoflow_start_bot:", "PASS" if running else "FAIL")
sys.exit(0 if running else 1)
