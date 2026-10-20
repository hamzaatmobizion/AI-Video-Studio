"""Web app settings, loaded from .env. Problems are reported, never papered over."""
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    SECRET_KEY: str = ""
    DATABASE_URL: str = "sqlite:///./var/app.db"
    COOKIE_SECURE: bool = False

    WORKER_URL: str = "http://127.0.0.1:8100"
    WORKER_API_KEY: str = ""

    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_DRIVE_FOLDER_NAME: str = "VideoGeneration"

    PUBLIC_BASE_URL: str = "http://localhost:8000"
    TOKEN_ENCRYPTION_KEY: str = ""
    VAR_DIR: Path = Path("./var")
    SCHEDULER_ENABLED: bool = True

    # ---- derived -------------------------------------------------------
    @property
    def generated_dir(self) -> Path:
        return self.VAR_DIR / "generated"

    @property
    def refs_dir(self) -> Path:
        return self.VAR_DIR / "refs"

    @property
    def characters_dir(self) -> Path:
        return self.VAR_DIR / "characters"

    @property
    def google_redirect_uri(self) -> str:
        return self.PUBLIC_BASE_URL.rstrip("/") + "/auth/google/callback"

    @property
    def worker_configured(self) -> bool:
        return bool(self.WORKER_URL and self.WORKER_API_KEY)

    @property
    def encryption_configured(self) -> bool:
        return bool(self.TOKEN_ENCRYPTION_KEY)

    @property
    def google_configured(self) -> bool:
        return bool(self.GOOGLE_CLIENT_ID and self.GOOGLE_CLIENT_SECRET and self.encryption_configured)

    def fatal_problems(self) -> list[str]:
        """Problems that stop the app from starting."""
        out = []
        if len(self.SECRET_KEY) < 32:
            out.append("SECRET_KEY must be set to a random value of at least 32 characters.")
        return out

    def problems(self) -> list[str]:
        """Everything worth telling the owner about (shown by `doctor`)."""
        out = self.fatal_problems()
        if not self.worker_configured:
            out.append("WORKER_URL / WORKER_API_KEY not set: video generation is disabled.")
        if not self.encryption_configured:
            out.append("TOKEN_ENCRYPTION_KEY not set: Google Drive cannot be connected.")
        else:
            try:
                from cryptography.fernet import Fernet

                Fernet(self.TOKEN_ENCRYPTION_KEY.encode())
            except Exception:
                out.append("TOKEN_ENCRYPTION_KEY is not a valid Fernet key.")
        if not (self.GOOGLE_CLIENT_ID and self.GOOGLE_CLIENT_SECRET):
            out.append("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET not set: Google Drive is disabled.")
        public = urlparse(self.PUBLIC_BASE_URL)
        if public.scheme not in ("http", "https") or not public.netloc:
            out.append("PUBLIC_BASE_URL must be a full URL such as http://localhost:8000.")
        elif public.scheme == "http" and public.hostname not in ("localhost", "127.0.0.1"):
            out.append("PUBLIC_BASE_URL should use https outside localhost.")
        if public.scheme == "https" and not self.COOKIE_SECURE:
            out.append("COOKIE_SECURE should be true when PUBLIC_BASE_URL uses https.")
        return out


@lru_cache
def get_settings() -> Settings:
    return Settings()
