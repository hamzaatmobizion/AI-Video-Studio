"""Fernet encryption for stored tokens."""
from cryptography.fernet import Fernet, InvalidToken


class TokenCipher:
    def __init__(self, key: str):
        if not key:
            raise RuntimeError("TOKEN_ENCRYPTION_KEY is not set.")
        self._f = Fernet(key.encode())

    def encrypt(self, value: str) -> str:
        return self._f.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        try:
            return self._f.decrypt(value.encode()).decode()
        except InvalidToken as e:
            raise RuntimeError("Stored token can't be decrypted (TOKEN_ENCRYPTION_KEY changed?). Reconnect.") from e
