"""Hand bot settings, from the bot computer's own .env."""
import sys
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

HERE = Path(__file__).resolve().parent
# the system the bot runs on (handbot/system/<SYSTEM>.py): mac, windows or linux
SYSTEM = "mac" if sys.platform == "darwin" else "windows" if sys.platform.startswith("win") else "linux"


class BotSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    APP_URL: str = ""  # the web app, as the bot Mac reaches it; unset: PUBLIC_BASE_URL (the app on this same Mac)
    PUBLIC_BASE_URL: str = "http://localhost:8000"  # the app's own address, from the shared .env
    BOT_API_KEY: str = ""  # must equal the app's BOT_API_KEY
    DOWNLOAD_DIR: Path = Path("~/Downloads").expanduser()  # where Chrome saves downloads
    BOT_DATA_PATH: Path = Path("./handbot-data")
    POLL_SECONDS: int = 30
    # The worker's pacing: at least this long between generations (× 1–1.5), at most this many an hour
    MIN_GAP_SECONDS: int = 60
    MAX_PER_HOUR: int = 12
    GENERATION_TIMEOUT_SECONDS: int = 900
    REFUND_RETRIES: int = 2  # a clip that fails with "credits refunded" is generated again up to this many times
    MATCH_CONFIDENCE: float = 0.85
    # Windows and Linux only (macOS reads the screen with its own Vision): the Tesseract program if it isn't on PATH,
    # the pixels per point screenshots are read at (small text reads better enlarged), and the least sure word kept
    TESSERACT_CMD: str = ""
    OCR_SCALE: float = 2.0
    OCR_MIN_CONFIDENCE: float = 40

    @property
    def app_url(self) -> str:
        return (self.APP_URL or self.PUBLIC_BASE_URL).rstrip("/")

    @property
    def buttons_dir(self) -> Path:
        """The button pictures taken on a Mac (handbot/buttons/)."""
        return HERE / "buttons"

    @property
    def capture_dir(self) -> Path:
        """Where `capture` saves a button's picture: handbot/buttons/ on a Mac, handbot/buttons/windows/ or
        handbot/buttons/linux/ on those (Chrome's tab bar and the file dialog look different there)."""
        return self.buttons_dir if SYSTEM == "mac" else self.buttons_dir / SYSTEM

    def button_path(self, name: str) -> Path:
        """A button's picture: the one taken on this system if there is one, else the Mac's."""
        own = self.capture_dir / f"{name}.png"
        return own if own.exists() else self.buttons_dir / f"{name}.png"

    @property
    def clips_dir(self) -> Path:
        return self.BOT_DATA_PATH / "clips"

    @property
    def logs_dir(self) -> Path:
        return self.BOT_DATA_PATH / "logs"

    @property
    def points_path(self) -> Path:
        return self.BOT_DATA_PATH / "points.json"

    @property
    def pace_path(self) -> Path:
        return self.BOT_DATA_PATH / "pace.json"

    def ensure_dirs(self) -> None:
        for d in (self.BOT_DATA_PATH, self.clips_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)

    def problems(self) -> list[str]:
        out = []
        if len(self.BOT_API_KEY) < 24:
            out.append("BOT_API_KEY must be set (the same value as the app's, at least 24 characters).")
        if not self.DOWNLOAD_DIR.is_dir():
            out.append(f"DOWNLOAD_DIR {self.DOWNLOAD_DIR} doesn't exist.")
        return out


@lru_cache
def get_settings() -> BotSettings:
    return BotSettings()
