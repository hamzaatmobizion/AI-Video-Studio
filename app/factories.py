"""Provider selection. Each returns None when the feature isn't configured (the UI says why)."""
import httpx
from sqlalchemy import select

from app.config import Settings
from app.crypto import TokenCipher
from app.db import Database
from app.models import GoogleConnection
from app.providers.drive_storage import GoogleDriveStorage, GoogleOAuth
from app.providers.worker_client import WorkerClient


class Providers:
    def __init__(self, settings: Settings, db: Database, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self.db = db
        self.transport = transport  # only tests pass one

    def generator(self) -> WorkerClient | None:
        s = self.settings
        if not s.worker_configured:
            return None
        return WorkerClient(s.WORKER_URL, s.WORKER_API_KEY, transport=self.transport)

    def oauth(self) -> GoogleOAuth | None:
        s = self.settings
        if not s.google_configured:
            return None
        return GoogleOAuth(s.GOOGLE_CLIENT_ID, s.GOOGLE_CLIENT_SECRET, s.google_redirect_uri, transport=self.transport)

    def cipher(self) -> TokenCipher:
        return TokenCipher(self.settings.TOKEN_ENCRYPTION_KEY)

    def connection(self, user_id: int) -> GoogleConnection | None:
        with self.db.session() as s:
            return s.scalar(select(GoogleConnection).where(GoogleConnection.user_id == user_id))

    def storage(self, user_id: int) -> GoogleDriveStorage | None:
        s = self.settings
        if not s.google_configured:
            return None
        conn = self.connection(user_id)
        if conn is None:
            return None
        return GoogleDriveStorage(
            s.GOOGLE_CLIENT_ID, s.GOOGLE_CLIENT_SECRET, self.cipher().decrypt(conn.refresh_token_enc),
            conn.folder_id, conn.folder_name or s.GOOGLE_DRIVE_FOLDER_NAME, transport=self.transport,
        )
