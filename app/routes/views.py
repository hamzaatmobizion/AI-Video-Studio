"""Shared view helpers: card rendering and settings-page context."""
import hashlib

from fastapi import Request
from sqlalchemy import select

from app.enums import ACTIVE
from app.models import Generation


def render_card(request: Request, gen: Generation) -> str:
    tpl = request.app.state.templates.get_template("_card.html")
    return tpl.render(g=gen, active=gen.status in ACTIVE)


def cards_for(request: Request, user_id: int, provider: str | None = None) -> list[dict]:
    """The user's latest videos as cards; with `provider`, only those made there (each screen shows its own)."""
    cond = [Generation.user_id == user_id, Generation.dismissed.is_(False)]
    if provider:
        cond.append(Generation.provider == provider)
    with request.app.state.db.session() as s:
        gens = s.scalars(select(Generation).where(*cond).order_by(Generation.id.desc()).limit(50)).all()
    out = []
    for g in gens:
        html = render_card(request, g)
        out.append({"id": g.id, "html": html, "hash": hashlib.sha1(html.encode()).hexdigest()[:12],
                    "active": g.status in ACTIVE})
    return out


GEMINI_LABELS = {
    "connected": "Connected",
    "signed_out": "Signed out",
    "needs_attention": "Needs attention",
    "limit_reached": "Limit reached",
    "unknown": "Not checked yet",
}


GOOGLE_RESULTS = {
    "connected": ("ok", "Google Drive connected."),
    "missing_scope": ("bad", "Drive access wasn't granted. Connect again and tick the box that lets "
                             "the app see, edit and create its own Google Drive files."),
    "denied": ("bad", "Google sign-in was cancelled."),
    "bad_state": ("bad", "That sign-in link expired. Click Connect Google Drive again."),
    "failed": ("bad", "Google sign-in failed. Try connecting again."),
    "not_configured": ("bad", "Google Drive isn't configured in .env yet."),
}


def site_key(st: dict) -> str:
    """Changes whenever the Gemini (or Flow) panel would render differently; the Settings page polls it."""
    return f"{st.get('state')}|{bool(st.get('login_window_open'))}|{bool(st.get('checking'))}|{st.get('checked_at')}"


def settings_context(request: Request, user_id: int) -> dict:
    st = request.app.state
    s = st.settings
    client = st.providers.generator()
    health = client.ping() if client else None
    gemini = (health.details.get("gemini") or {}) if health and health.ok else {}
    flow = (health.details.get("flow") or {}) if health and health.ok else {}
    conn = st.providers.connection(user_id)
    return {
        "worker_configured": client is not None,
        "health": health,
        "gemini": gemini,
        "gemini_label": GEMINI_LABELS.get(gemini.get("state", "unknown"), "Unknown"),
        "pause": (health.details.get("account_paused") if health and health.ok else None),
        "login_window_open": bool(health and health.ok and health.details.get("login_window_open")),
        "gemini_key": site_key(gemini),
        "flow": flow,
        "flow_label": GEMINI_LABELS.get(flow.get("state", "unknown"), "Unknown").replace("Limit reached",
                                                                                         "Out of credits"),
        "flow_pause": ((health.details.get("pauses") or {}).get("flow") if health and health.ok else None),
        "flow_key": site_key(flow),
        "flow_model": ((health.details.get("providers") or {}).get("flow") or {}).get("model")
                      if health and health.ok else None,
        "google_configured": s.google_configured,
        "google_missing": [k for k in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "TOKEN_ENCRYPTION_KEY")
                           if not getattr(s, k)],
        "drive": conn,
        "default_folder": s.GOOGLE_DRIVE_FOLDER_NAME,
        "redirect_uri": s.google_redirect_uri,
        "google_result": GOOGLE_RESULTS.get(request.query_params.get("google", "")),
    }
