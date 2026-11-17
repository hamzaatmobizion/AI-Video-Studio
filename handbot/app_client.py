"""The app's bot routes (app/routes/bot.py)."""
from pathlib import Path

import httpx

from handbot.config import BotSettings

EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}


class AppError(Exception):
    pass


class AppClient:
    def __init__(self, settings: BotSettings):
        self.http = httpx.Client(base_url=settings.app_url, timeout=60,
                                 headers={"Authorization": f"Bearer {settings.BOT_API_KEY}"})

    def _ok(self, r: httpx.Response) -> httpx.Response:
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise AppError(f"{r.request.method} {r.request.url.path}: {r.status_code} {detail}")
        return r

    def next_scene(self) -> dict | None:
        """The by-hand scene waiting for its clip, or None."""
        r = self._ok(self.http.get("/bot/next"))
        return None if r.status_code == 204 else r.json()

    def images(self, job: dict, folder: Path) -> list[Path]:
        """The scene's pictures saved in `folder`, in the app's order."""
        out = []
        for im in job["images"]:
            r = self._ok(self.http.get(f"/bot/generations/{job['gen_id']}/images/{im['index']}"))
            ext = EXT.get(r.headers.get("content-type", "").split(";")[0], "jpg")
            path = folder / f"image-{im['index']}.{ext}"
            path.write_bytes(r.content)
            out.append(path)
        return out

    def upload(self, job: dict, clip: Path) -> None:
        with open(clip, "rb") as f:
            self._ok(self.http.post(f"/bot/generations/{job['gen_id']}/scenes/{job['position']}/clip",
                                    files={"clip": (clip.name, f, "video/mp4")}, timeout=300))

    def stopped(self, job: dict, message: str) -> None:
        """Tell the app why the clip couldn't be made: it shows on the video's page and card, with "Try again"."""
        self._ok(self.http.post(f"/bot/generations/{job['gen_id']}/stopped", json={"message": message}))
