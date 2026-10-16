"""Worker settings (shares .env with the web app)."""
import ipaddress
import json
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

HERE = Path(__file__).resolve().parent


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    STUDIO_DATA_PATH: Path = Path("./data")
    BROWSER_PROFILE_PATH: Path | None = None
    API_HOST: str = "127.0.0.1"
    API_PORT: int = 8100
    STUDIO_API_KEY: str = ""
    GEMINI_MODEL_LABEL: str | None = None  # None → profile.json; "" → whatever is selected in Gemini
    GENERATION_TIMEOUT_SECONDS: int = 900
    MAX_RETRIES: int = 1
    BROWSER_HEADLESS: bool = True
    BROWSER_CHANNEL: str = ""  # "" = Playwright Chromium, or "chrome"

    @field_validator("BROWSER_PROFILE_PATH", mode="before")
    @classmethod
    def _empty_path(cls, v):
        return None if v in ("", None) else v

    @property
    def data(self) -> Path:
        return self.STUDIO_DATA_PATH

    @property
    def profile_dir(self) -> Path:
        return self.BROWSER_PROFILE_PATH or (self.data / "browser-profile")

    @property
    def db_path(self) -> Path:
        return self.data / "worker.db"

    @property
    def jobs_dir(self) -> Path:
        return self.data / "jobs"

    @property
    def clips_dir(self) -> Path:
        return self.data / "clips"

    @property
    def diagnostics_dir(self) -> Path:
        return self.data / "diagnostics"

    def profile(self) -> dict:
        prof = json.loads((HERE / "profile.json").read_text())
        if self.GEMINI_MODEL_LABEL is not None:
            prof["model_label"] = self.GEMINI_MODEL_LABEL or None
        return prof

    def problems(self) -> list[str]:
        out = []
        try:
            if not ipaddress.ip_address(self.API_HOST).is_loopback:
                out.append("API_HOST must be a loopback address (127.0.0.1 or ::1); the worker is never public.")
        except ValueError:
            if self.API_HOST != "localhost":
                out.append("API_HOST must be a loopback address (127.0.0.1 or ::1).")
        if len(self.STUDIO_API_KEY) < 24:
            out.append("STUDIO_API_KEY must be at least 24 random characters.")
        if not 60 <= self.GENERATION_TIMEOUT_SECONDS <= 3600:
            out.append("GENERATION_TIMEOUT_SECONDS must be between 60 and 3600.")
        if not 0 <= self.MAX_RETRIES <= 3:
            out.append("MAX_RETRIES must be between 0 and 3.")
        if self.BROWSER_CHANNEL not in ("", "chrome"):
            out.append('BROWSER_CHANNEL must be empty or "chrome".')
        return out

    def ensure_dirs(self) -> None:
        for d in (self.data, self.profile_dir, self.jobs_dir, self.clips_dir, self.diagnostics_dir):
            d.mkdir(parents=True, exist_ok=True)
            d.chmod(0o700)


@lru_cache
def get_settings() -> WorkerSettings:
    return WorkerSettings()
