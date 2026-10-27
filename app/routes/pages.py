import asyncio
import re
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.auth import (csrf_token, current_user, hash_password, require_page_user, require_post,
                      verify_password)
from app import script as sc
from app.genres import GENRE_LABELS
from app.enums import ACTIVE, GenStatus
from app.models import Character, Generation, User
from app.routes.views import cards_for, settings_context

router = APIRouter()


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx.update(request=request, csrf=csrf_token(request), user=current_user(request))
    return request.app.state.templates.TemplateResponse(request, name, ctx)


@router.get("/", response_class=HTMLResponse)
def studio(request: Request, prompt_from: int | None = None, user=Depends(require_page_user)):
    st = request.app.state
    prefill = None
    if prompt_from is not None:  # "Use prompt again" from History
        with st.db.session() as s:
            prefill = s.get(Generation, prompt_from)
        if prefill is not None and prefill.user_id != user.id:
            prefill = None
    with st.db.session() as s:
        characters = s.scalars(select(Character).where(Character.user_id == user.id).order_by(Character.name)).all()
    return render(request, "studio.html", cards=cards_for(request, user.id),
                  can_generate=st.providers.generator() is not None,
                  drive_connected=st.providers.connection(user.id) is not None,
                  characters=characters, styles=sc.STYLES, max_cast=sc.MAX_CAST, genres=GENRE_LABELS,
                  **_prefill(prefill),
                  prefill_aspect=prefill.aspect if prefill else "9:16")


def _prefill(gen: Generation | None) -> dict:
    """Studio form values for "Use prompt again": one prompt + duration, or the user's own scenes."""
    if gen is None:
        return {"prefill_prompt": "", "prefill_duration": None, "prefill_scenes": [""], "prefill_manual": False,
                "prefill_cast": [], "prefill_style": "", "prefill_language": "", "prefill_review": True,
                "prefill_genre": "", "prefill_lock": False, "prefill_check": False}
    manual = not gen.planned and len(gen.scenes) > 1
    return {"prefill_prompt": "" if manual else gen.prompt,
            "prefill_duration": max(len(gen.scenes), 1) * 10,
            "prefill_scenes": [s.action or s.prompt for s in gen.scenes] if manual else [""],
            "prefill_manual": manual, "prefill_cast": gen.cast_ids or [], "prefill_style": gen.style or "",
            "prefill_language": gen.language or "", "prefill_review": bool(gen.review),
            "prefill_genre": gen.genre or "", "prefill_lock": bool(gen.lock_frames),
            "prefill_check": bool(gen.check_clips)}


@router.get("/generations/{gen_id}/script", response_class=HTMLResponse)
def script_page(gen_id: int, request: Request, user=Depends(require_page_user)):
    """The script editor: each scene's setting, camera, who is where, what they do and say."""
    gen, library = request.app.state.service.script(gen_id, user.id)
    scenes = [s.script() for s in gen.scenes]
    # Picked characters the story had no role for (Gemini leaves them out rather than cast them as someone else)
    used = {p.get("id") for sc in scenes for p in sc["characters"]}
    unused = [c["name"] for c in library if c["id"] in (gen.cast_ids or []) and c["id"] not in used]
    return render(request, "script.html", g=gen, scenes=scenes, library=library, genres=GENRE_LABELS, unused=unused,
                  editable=gen.status in (GenStatus.REVIEW, GenStatus.FAILED),
                  failed=gen.status == GenStatus.FAILED,
                  clips=[bool(s.clip_path and Path(s.clip_path).exists()) for s in gen.scenes],
                  can_redo=gen.status in (GenStatus.READY, GenStatus.FAILED),
                  prompts=[s.prompt for s in gen.scenes],
                  options={"framing": sc.FRAMINGS, "angle": sc.ANGLES, "movement": sc.MOVEMENTS, "lens": sc.LENSES,
                           "position": sc.POSITIONS, "time_of_day": sc.TIMES_OF_DAY, "style": sc.STYLES},
                  max_words=sc.MAX_SPOKEN_WORDS, max_prompt=sc.MAX_PROMPT)


HISTORY_STATUSES = {"all": "All", "ready": "Ready", "in_progress": "In progress", "failed": "Failed",
                    "cancelled": "Cancelled"}
PER_PAGE = 20


@router.get("/rules", response_class=HTMLResponse)
def rules_page(request: Request, user=Depends(require_page_user)):
    """How to write a prompt for the best video, and a format any AI chat can fill in."""
    return render(request, "rules.html")


@router.get("/history", response_class=HTMLResponse)
def history_page(request: Request, q: str = "", status: str = "all", page: int = 1,
                 user=Depends(require_page_user)):
    q = q.strip()[:200]
    status = status if status in HISTORY_STATUSES else "all"
    page = max(page, 1)
    rows, total = request.app.state.service.history(user.id, q, status, page, PER_PAGE)
    pages = max((total + PER_PAGE - 1) // PER_PAGE, 1)

    def link(p: int) -> str:
        return "/history?" + urlencode({k: v for k, v in (("q", q), ("status", status), ("page", p))
                                        if v and v != "all" and v != 1})

    return render(request, "history.html", rows=rows, total=total, q=q, status=status,
                  statuses=HISTORY_STATUSES, page=page, pages=pages,
                  prev_url=link(page - 1) if page > 1 else None,
                  next_url=link(page + 1) if page < pages else None, active=ACTIVE)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user=Depends(require_page_user)):
    return render(request, "settings.html", **settings_context(request, user.id))


@router.get("/account", response_class=HTMLResponse)
def account_page(request: Request, user=Depends(require_page_user)):
    return render(request, "account.html")


@router.get("/health")
def health():
    return {"ok": True}


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html")


class LoginBody(BaseModel):
    email: str
    password: str


@router.post("/login")
async def login(request: Request, body: LoginBody):
    from app.auth import check_csrf

    check_csrf(request)
    with request.app.state.db.session() as s:
        user = s.scalar(select(User).where(User.email == body.email.strip().lower()))
    if user is None or not verify_password(body.password, user.password_hash):
        await asyncio.sleep(1.0)
        return JSONResponse({"detail": "Wrong email or password."}, status_code=400)
    _sign_in(request, user.id)
    return {"ok": True}


def _sign_in(request: Request, user_id: int) -> None:
    request.session.clear()
    request.session["uid"] = user_id
    csrf_token(request)


def _password_problem(password: str) -> str | None:
    if not 10 <= len(password.encode()) <= 72:
        return "Password must be 10 to 72 characters."
    return None


@router.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request):
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    return render(request, "signup.html")


_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


@router.post("/signup")
async def signup(request: Request, body: LoginBody):
    from app.auth import check_csrf

    check_csrf(request)
    email = body.email.strip().lower()
    if len(email) > 320 or not _EMAIL.fullmatch(email):
        return JSONResponse({"detail": "Enter a valid email address."}, status_code=400)
    if problem := _password_problem(body.password):
        return JSONResponse({"detail": problem}, status_code=400)
    try:
        with request.app.state.db.session() as s:
            user = User(email=email, password_hash=hash_password(body.password))
            s.add(user)
            s.flush()
            user_id = user.id
    except IntegrityError:
        await asyncio.sleep(1.0)
        return JSONResponse({"detail": "An account with this email already exists. Sign in instead."},
                            status_code=400)
    _sign_in(request, user_id)
    return {"ok": True}


class PasswordBody(BaseModel):
    current_password: str
    new_password: str


@router.post("/account/password")
async def change_password(request: Request, body: PasswordBody, user=Depends(require_post)):
    if not verify_password(body.current_password, user.password_hash):
        await asyncio.sleep(1.0)
        return JSONResponse({"detail": "Current password is wrong."}, status_code=400)
    if problem := _password_problem(body.new_password):
        return JSONResponse({"detail": "New " + problem[0].lower() + problem[1:]}, status_code=400)
    if body.new_password == body.current_password:
        return JSONResponse({"detail": "New password must differ from the current one."}, status_code=400)
    with request.app.state.db.session() as s:
        s.get(User, user.id).password_hash = hash_password(body.new_password)
    return {"ok": True, "message": "Password changed."}


@router.post("/logout")
def logout(request: Request, user=Depends(require_post)):
    request.session.clear()
    return {"ok": True}
