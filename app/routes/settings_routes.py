import secrets

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select

from app.auth import require_api_user, require_page_user, require_post
from app.interfaces import GeneratorError, StorageError
from app.models import GoogleConnection
from app.providers.drive_storage import MissingScopeError
from app.routes.views import gemini_key
from app.services.generation import UserError

router = APIRouter()


# ---- worker / Gemini ----------------------------------------------------
@router.post("/settings/worker/test")
def worker_test(request: Request, user=Depends(require_post)):
    client = request.app.state.providers.generator()
    if client is None:
        raise UserError("Set WORKER_URL and WORKER_API_KEY in .env first.")
    h = client.ping()
    return {"ok": h.ok, "message": h.message}


@router.get("/settings/gemini/state")
def gemini_state(request: Request, user=Depends(require_api_user)):
    """Tiny status for the Settings page to poll while the sign-in window is open or a check runs."""
    client = request.app.state.providers.generator()
    if client is None:
        return {"key": "off", "busy": False}
    try:
        st = client.gemini("GET", "status")
    except GeneratorError:
        return {"key": "unreachable", "busy": False}
    return {"key": gemini_key(st), "busy": bool(st.get("login_window_open") or st.get("checking"))}


GEMINI_ACTIONS = {"login": ("POST", "login"), "done": ("POST", "login/done"),
                  "check": ("POST", "check?background=true"), "resume": ("POST", "resume")}


@router.post("/settings/gemini/{action}")
def gemini_action(action: str, request: Request, user=Depends(require_post)):
    if action not in GEMINI_ACTIONS:
        return JSONResponse({"detail": "Unknown action."}, status_code=404)
    client = request.app.state.providers.generator()
    if client is None:
        raise UserError("Set WORKER_URL and WORKER_API_KEY in .env first.")
    method, path = GEMINI_ACTIONS[action]
    try:
        return client.gemini(method, path)
    except GeneratorError as e:
        raise UserError(e.message, 502) from e


# ---- Google Drive -------------------------------------------------------
@router.get("/auth/google/start")
def google_start(request: Request, user=Depends(require_page_user)):
    oauth = request.app.state.providers.oauth()
    if oauth is None:
        return RedirectResponse("/settings?google=not_configured", status_code=303)
    state = secrets.token_urlsafe(24)
    request.session["google_state"] = state
    return RedirectResponse(oauth.authorize_url(state), status_code=303)


@router.get("/auth/google/callback", response_class=HTMLResponse)
def google_callback(request: Request, state: str = "", code: str = "", error: str = "",
                    user=Depends(require_page_user)):
    expected = request.session.pop("google_state", None)
    if not expected or not secrets.compare_digest(expected, state):
        return RedirectResponse("/settings?google=bad_state", status_code=303)
    if error or not code:
        return RedirectResponse("/settings?google=denied", status_code=303)
    st = request.app.state
    oauth = st.providers.oauth()
    if oauth is None:
        return RedirectResponse("/settings?google=not_configured", status_code=303)
    try:
        refresh, email = oauth.exchange(code)
    except MissingScopeError:
        return RedirectResponse("/settings?google=missing_scope", status_code=303)
    except StorageError:
        return RedirectResponse("/settings?google=failed", status_code=303)
    enc = st.providers.cipher().encrypt(refresh)
    with st.db.session() as s:
        old = s.scalar(select(GoogleConnection).where(GoogleConnection.user_id == user.id))
        folder_id = old.folder_id if old else None
        folder_name = old.folder_name if old else None
        if old:
            s.delete(old)
            s.flush()
        s.add(GoogleConnection(user_id=user.id, refresh_token_enc=enc, email=email, folder_id=folder_id, folder_name=folder_name))
    return RedirectResponse("/settings?google=connected", status_code=303)


def _storage(request: Request, user_id: int):
    storage = request.app.state.providers.storage(user_id)
    if storage is None:
        raise UserError("Connect Google Drive first.")
    return storage


@router.post("/settings/google/folders")
def google_folders(request: Request, user=Depends(require_post)):
    try:
        return {"folders": _storage(request, user.id).list_folders()}
    except StorageError as e:
        raise UserError(e.message, 502) from e


class FolderBody(BaseModel):
    folder_id: str | None = None
    new_name: str | None = None


@router.post("/settings/google/folder")
def google_folder(body: FolderBody, request: Request, user=Depends(require_post)):
    storage = _storage(request, user.id)
    try:
        if body.new_name and body.new_name.strip():
            folder = storage.create_folder(body.new_name.strip()[:200])
        elif body.folder_id:
            folder = next((f for f in storage.list_folders() if f["id"] == body.folder_id), None)
            if folder is None:
                raise UserError("That folder isn't available to this app.")
        else:
            raise UserError("Pick a folder or type a new folder name.")
    except StorageError as e:
        raise UserError(e.message, 502) from e
    with request.app.state.db.session() as s:
        conn = s.scalar(select(GoogleConnection).where(GoogleConnection.user_id == user.id))
        conn.folder_id, conn.folder_name = folder["id"], folder["name"]
    return {"ok": True, "folder": folder}


@router.post("/settings/google/folder/default")
def google_folder_default(request: Request, user=Depends(require_post)):
    _storage(request, user.id)
    with request.app.state.db.session() as s:
        conn = s.scalar(select(GoogleConnection).where(GoogleConnection.user_id == user.id))
        conn.folder_id, conn.folder_name = None, None
    return {"ok": True}


@router.post("/settings/google/disconnect")
def google_disconnect(request: Request, user=Depends(require_post)):
    st = request.app.state
    conn = st.providers.connection(user.id)
    if conn is None:
        return {"ok": True}
    oauth = st.providers.oauth()
    if oauth is not None:
        try:
            oauth.revoke(st.providers.cipher().decrypt(conn.refresh_token_enc))
        except RuntimeError:
            pass  # undecryptable token: still remove it locally
    with st.db.session() as s:
        row = s.scalar(select(GoogleConnection).where(GoogleConnection.user_id == user.id))
        if row:
            s.delete(row)
    return {"ok": True}
