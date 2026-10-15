"""Google Drive storage over plain httpx (no Google SDK).

Only the refresh token is persisted (encrypted, by the caller). Access tokens live in memory.
Scope is `drive.file`: the app sees only files and folders it created.
"""
import hashlib
import json
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

from app.interfaces import RangeStream, StorageError, StoredFile

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"
SCOPES = f"openid email {DRIVE_SCOPE}"


class MissingScopeError(StorageError):
    """The user unticked Drive access on Google's consent screen."""
FOLDER_MIME = "application/vnd.google-apps.folder"

_token_cache: dict[str, tuple[str, float]] = {}
_token_lock = threading.Lock()


def _key(refresh_token: str) -> str:
    return hashlib.sha256(refresh_token.encode()).hexdigest()


def _google_message(r: httpx.Response) -> tuple[str, str]:
    """(reason, message) from a Google error body."""
    try:
        err = r.json().get("error")
    except Exception:
        return "", f"Google Drive answered with HTTP {r.status_code}."
    if isinstance(err, dict):
        reason = ""
        errors = err.get("errors") or []
        if errors:
            reason = errors[0].get("reason", "")
        return reason, err.get("message") or f"Google Drive answered with HTTP {r.status_code}."
    if isinstance(err, str):
        return err, r.json().get("error_description") or err
    return "", f"Google Drive answered with HTTP {r.status_code}."


def _raise_for(r: httpx.Response) -> None:
    if r.status_code < 400:
        return
    reason, msg = _google_message(r)
    if reason in ("storageQuotaExceeded", "quotaExceeded"):
        raise StorageError("Your Google Drive is full. Free some space, then Try saving again.", retryable=False)
    if reason in ("rateLimitExceeded", "userRateLimitExceeded") or r.status_code in (429,) or r.status_code >= 500:
        raise StorageError(f"Google Drive is busy: {msg}", retryable=True)
    if r.status_code == 401:
        raise StorageError("Google Drive access expired. Reconnect Drive in Settings.", retryable=False)
    if r.status_code == 403 and (reason == "insufficientPermissions" or "scope" in msg.lower()):
        raise StorageError("This Drive connection doesn't allow saving files. Disconnect and reconnect "
                           "Google Drive in Settings, ticking the Drive access box, then Try saving again.",
                           retryable=False)
    raise StorageError(f"Google Drive error: {msg}", retryable=False)


class GoogleOAuth:
    """The OAuth web-client dance. Used by the connect/disconnect routes."""

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str,
                 transport: httpx.BaseTransport | None = None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self._http = httpx.Client(transport=transport, timeout=30.0)

    def authorize_url(self, state: str) -> str:
        q = {
            "client_id": self.client_id, "redirect_uri": self.redirect_uri, "response_type": "code",
            "scope": SCOPES, "access_type": "offline", "prompt": "consent", "state": state,
            "include_granted_scopes": "true",
        }
        return f"{AUTH_URL}?{urlencode(q)}"

    def exchange(self, code: str) -> tuple[str, str | None]:
        """Returns (refresh_token, email)."""
        try:
            r = self._http.post(TOKEN_URL, data={
                "code": code, "client_id": self.client_id, "client_secret": self.client_secret,
                "redirect_uri": self.redirect_uri, "grant_type": "authorization_code",
            })
        except httpx.HTTPError as e:
            raise StorageError("Couldn't reach Google. Try connecting again.", retryable=True) from e
        if r.status_code >= 400:
            raise StorageError(f"Google sign-in failed: {_google_message(r)[1]}", retryable=False)
        body = r.json()
        # Google's consent screen lets each scope be unticked; without Drive every upload would 403.
        if DRIVE_SCOPE not in (body.get("scope") or "").split():
            if body.get("refresh_token"):
                self.revoke(body["refresh_token"])
            raise MissingScopeError("Google Drive access wasn't granted.", retryable=False)
        refresh = body.get("refresh_token")
        if not refresh:
            raise StorageError("Google didn't return a refresh token. Remove the app's access at "
                               "myaccount.google.com/permissions and connect again.", retryable=False)
        email = None
        try:
            u = self._http.get(USERINFO_URL, headers={"Authorization": f"Bearer {body['access_token']}"})
            if u.status_code == 200:
                email = u.json().get("email")
        except httpx.HTTPError:
            pass
        return refresh, email

    def revoke(self, refresh_token: str) -> None:
        with _token_lock:
            _token_cache.pop(_key(refresh_token), None)
        try:
            self._http.post(REVOKE_URL, data={"token": refresh_token})
        except httpx.HTTPError:
            pass  # disconnect locally regardless


class GoogleDriveStorage:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str, folder_id: str | None,
                 folder_name: str, transport: httpx.BaseTransport | None = None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.folder_id = folder_id
        self.folder_name = folder_name
        self._http = httpx.Client(transport=transport, timeout=httpx.Timeout(60.0, read=600.0, write=600.0))

    # ---- auth ------------------------------------------------------------
    def _access_token(self, force: bool = False) -> str:
        k = _key(self.refresh_token)
        with _token_lock:
            cached = _token_cache.get(k)
            if cached and not force and cached[1] > time.time() + 60:
                return cached[0]
        try:
            r = self._http.post(TOKEN_URL, data={
                "client_id": self.client_id, "client_secret": self.client_secret,
                "refresh_token": self.refresh_token, "grant_type": "refresh_token",
            })
        except httpx.HTTPError as e:
            raise StorageError("Couldn't reach Google.", retryable=True) from e
        if r.status_code >= 500:
            raise StorageError("Google sign-in service is unavailable.", retryable=True)
        if r.status_code >= 400:
            raise StorageError("Google Drive access was revoked or expired. Reconnect Drive in Settings.",
                               retryable=False)
        body = r.json()
        token = body["access_token"]
        with _token_lock:
            _token_cache[k] = (token, time.time() + int(body.get("expires_in", 3600)))
        return token

    def _call(self, method: str, url: str, **kw) -> httpx.Response:
        for attempt in (0, 1):
            headers = dict(kw.pop("headers", {}) or {})
            headers["Authorization"] = f"Bearer {self._access_token(force=attempt == 1)}"
            try:
                r = self._http.request(method, url, headers=headers, **kw)
            except httpx.HTTPError as e:
                raise StorageError("Lost connection to Google Drive.", retryable=True) from e
            if r.status_code == 401 and attempt == 0:
                kw["headers"] = {k: v for k, v in headers.items() if k != "Authorization"}
                continue
            return r
        return r

    # ---- folders ---------------------------------------------------------
    def list_folders(self) -> list[dict]:
        q = f"mimeType='{FOLDER_MIME}' and trashed=false and 'root' in parents"
        r = self._call("GET", f"{API}/files", params={"q": q, "fields": "files(id,name)", "pageSize": 100,
                                                       "orderBy": "name"})
        _raise_for(r)
        return r.json().get("files", [])

    def create_folder(self, name: str, parent: str | None = None) -> dict:
        meta = {"name": name, "mimeType": FOLDER_MIME}
        if parent:
            meta["parents"] = [parent]
        r = self._call("POST", f"{API}/files", json=meta, params={"fields": "id,name"})
        _raise_for(r)
        return r.json()

    def _find_folder(self, name: str, parent: str) -> str | None:
        safe = name.replace("\\", "\\\\").replace("'", "\\'")
        q = f"mimeType='{FOLDER_MIME}' and trashed=false and name='{safe}' and '{parent}' in parents"
        r = self._call("GET", f"{API}/files", params={"q": q, "fields": "files(id)", "pageSize": 1})
        _raise_for(r)
        files = r.json().get("files", [])
        return files[0]["id"] if files else None

    def root_folder_id(self) -> str:
        """The chosen folder, or the default-named folder (created on first use)."""
        if self.folder_id:
            r = self._call("GET", f"{API}/files/{self.folder_id}", params={"fields": "id,trashed"})
            if r.status_code == 200 and not r.json().get("trashed"):
                return self.folder_id
            if r.status_code not in (200, 404):
                _raise_for(r)
        found = self._find_folder(self.folder_name, "root")
        self.folder_id = found or self.create_folder(self.folder_name)["id"]
        return self.folder_id

    # ---- VideoStorage ----------------------------------------------------
    def save(self, local_path: Path, name: str, subfolder: str) -> StoredFile:
        root = self.root_folder_id()
        parent = self._find_folder(subfolder, root) or self.create_folder(subfolder, root)["id"]
        size = local_path.stat().st_size
        md5 = hashlib.md5()
        with open(local_path, "rb") as f:
            for block in iter(lambda: f.read(1024 * 1024), b""):
                md5.update(block)
        r = self._call("POST", UPLOAD, params={"uploadType": "resumable"},
                       headers={"Content-Type": "application/json; charset=UTF-8",
                                "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)},
                       content=json.dumps({"name": name, "parents": [parent]}))
        _raise_for(r)
        session_url = r.headers.get("location")
        if not session_url:
            raise StorageError("Google Drive didn't start the upload.", retryable=True)
        with open(local_path, "rb") as f:
            try:
                up = self._http.put(session_url, content=f, headers={"Content-Length": str(size),
                                                                       "Content-Type": "video/mp4"})
            except httpx.HTTPError as e:
                raise StorageError("Lost connection while uploading to Google Drive.", retryable=True) from e
        _raise_for(up)
        file_id = up.json().get("id")
        if not file_id:
            raise StorageError("Google Drive didn't return a file id.", retryable=True)
        meta = self.get(file_id)
        if meta.get("trashed") or int(meta.get("size", -1)) != size or meta.get("md5Checksum") != md5.hexdigest():
            raise StorageError("The uploaded copy didn't match the original. Retrying.", retryable=True)
        return StoredFile(file_id=file_id, size=size)

    def get(self, file_id: str) -> dict:
        r = self._call("GET", f"{API}/files/{file_id}", params={"fields": "id,name,size,md5Checksum,trashed"})
        if r.status_code == 404:
            raise StorageError("The video is no longer in Google Drive.", retryable=False)
        _raise_for(r)
        return r.json()

    def open(self, file_id: str, range_header: str | None) -> RangeStream:
        headers = {"Range": range_header} if range_header else {}
        token = self._access_token()
        headers["Authorization"] = f"Bearer {token}"
        req = self._http.build_request("GET", f"{API}/files/{file_id}", params={"alt": "media"}, headers=headers)
        try:
            r = self._http.send(req, stream=True)
        except httpx.HTTPError as e:
            raise StorageError("Lost connection to Google Drive.", retryable=True) from e
        if r.status_code >= 400:
            r.read()
            r.close()
            if r.status_code == 404:
                raise StorageError("The video is no longer in Google Drive.", retryable=False)
            if r.status_code == 416:
                raise StorageError("Requested range not satisfiable.", retryable=False)
            _raise_for(r)
        out = {"Content-Type": "video/mp4", "Accept-Ranges": "bytes"}
        for h in ("content-length", "content-range"):
            if h in r.headers:
                out[h.title()] = r.headers[h]
        return RangeStream(chunks=r.iter_bytes(64 * 1024), status=r.status_code, headers=out, close=r.close)

    def delete(self, file_id: str) -> None:
        """Move to Drive trash (recoverable for 30 days)."""
        r = self._call("PATCH", f"{API}/files/{file_id}", json={"trashed": True})
        if r.status_code == 404:
            return
        _raise_for(r)
