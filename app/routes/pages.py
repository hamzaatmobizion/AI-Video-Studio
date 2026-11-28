import asyncio
import re
import sys
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.auth import (csrf_token, current_user, hash_password, require_page_user, require_post,
                      verify_password)
from app import script as sc
from app.genres import GENRE_LABELS
from app.timeline import missing_lines
from app.enums import ACTIVE, SCENE_SECONDS, GenStatus, Provider
from app.models import Character, Generation, Series, User
from app.routes.views import cards_for, settings_context
from app.script_runs import NO_CREDITS
from app.services.generation import UserError

router = APIRouter()


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx.update(request=request, csrf=csrf_token(request), user=current_user(request))
    return request.app.state.templates.TemplateResponse(request, name, ctx)


@router.get("/", response_class=HTMLResponse)
def studio(request: Request, prompt_from: int | None = None, user=Depends(require_page_user)):
    return _studio(request, user, prompt_from, Provider.GEMINI)


@router.get("/flow", response_class=HTMLResponse)
def flow_studio(request: Request, prompt_from: int | None = None, series: int | None = None,
                user=Depends(require_page_user)):
    """The same Studio, with every clip made in Google Flow (Veo 3.1 - Lite, Flow credits, 8-second scenes).
    `series`: refill the form from a saved series, for its next episode."""
    return _studio(request, user, prompt_from, Provider.FLOW, series)


def _studio(request: Request, user, prompt_from: int | None, provider: Provider,
            series_id: int | None = None) -> HTMLResponse:
    st = request.app.state
    prefill, series, all_series = None, None, []
    if provider == Provider.FLOW:
        with st.db.session() as s:
            all_series = s.scalars(select(Series).where(Series.user_id == user.id).order_by(Series.name)).all()
        series = next((x for x in all_series if x.id == series_id), None)
    if prompt_from is not None:  # "Use prompt again" from History
        with st.db.session() as s:
            prefill = s.get(Generation, prompt_from)
        if prefill is not None and prefill.user_id != user.id:
            prefill = None
    with st.db.session() as s:
        characters = s.scalars(select(Character).where(Character.user_id == user.id).order_by(Character.name)).all()
    return render(request, "studio.html", cards=cards_for(request, user.id, provider), provider=provider,
                  step=SCENE_SECONDS[provider],
                  can_generate=st.providers.generator() is not None,
                  drive_connected=st.providers.connection(user.id) is not None,
                  characters=characters, styles=sc.STYLES, max_cast=sc.MAX_CAST, genres=GENRE_LABELS,
                  **(_series_prefill(series) if series else _prefill(prefill)), series=series, all_series=all_series,
                  prefill_aspect=(series or prefill).aspect if series or prefill else "9:16")


def _series_prefill(series: Series) -> dict:
    """Flow form values from a saved series: its prompt to adapt for the next episode, length, cast and ticks."""
    return {"prefill_prompt": series.prompt, "prefill_duration": series.scenes * SCENE_SECONDS[Provider.FLOW],
            "prefill_scenes": [""], "prefill_manual": False, "prefill_cast": series.cast_ids or [],
            "prefill_style": series.style, "prefill_language": series.language, "prefill_review": series.review,
            "prefill_genre": series.genre, "prefill_lock": series.lock_frames, "prefill_check": series.check_clips,
            "prefill_by_hand": series.by_hand}


def _prefill(gen: Generation | None) -> dict:
    """Studio form values for "Use prompt again": one prompt + duration, or the user's own scenes."""
    if gen is None:
        return {"prefill_prompt": "", "prefill_duration": None, "prefill_scenes": [""], "prefill_manual": False,
                "prefill_cast": [], "prefill_style": "", "prefill_language": "", "prefill_review": None,
                "prefill_genre": "", "prefill_lock": False, "prefill_check": None, "prefill_by_hand": True}
    manual = not gen.planned and len(gen.scenes) > 1
    return {"prefill_prompt": "" if manual else gen.prompt,
            "prefill_duration": max(len(gen.scenes), 1) * SCENE_SECONDS.get(gen.provider, 10),
            "prefill_scenes": [s.action or s.prompt for s in gen.scenes] if manual else [""],
            "prefill_manual": manual, "prefill_cast": gen.cast_ids or [], "prefill_style": gen.style or "",
            "prefill_language": gen.language or "", "prefill_review": bool(gen.review),
            "prefill_genre": gen.genre or "", "prefill_lock": bool(gen.lock_frames),
            "prefill_check": bool(gen.check_clips), "prefill_by_hand": bool(gen.by_hand)}


@router.get("/generations/{gen_id}/script", response_class=HTMLResponse)
def script_page(gen_id: int, request: Request, user=Depends(require_page_user)):
    """The script editor: each scene's setting, camera, who is where, what they do and say."""
    gen, library = request.app.state.service.script(gen_id, user.id)
    scenes = [s.script() for s in gen.scenes]
    # Picked characters the story had no role for (Gemini leaves them out rather than cast them as someone else)
    used = {p.get("id") for sc in scenes for p in sc["characters"]}
    unused = [c["name"] for c in library if c["id"] in (gen.cast_ids or []) and c["id"] not in used]
    # A Flow video's lines that the script dropped or reworded (the scenes start out empty until it's written)
    missing = missing_lines(gen.prompt, scenes) if gen.provider == Provider.FLOW and gen.planned and any(
        s["action"] or s["dialogue"] for s in scenes) else []
    return render(request, "script.html", g=gen, scenes=scenes, library=library, genres=GENRE_LABELS, unused=unused,
                  missing=missing,
                  editable=gen.status in (GenStatus.REVIEW, GenStatus.FAILED),
                  failed=gen.status == GenStatus.FAILED,
                  # a by-hand video waiting for a clip: the scene number its by-hand page makes next
                  next_hand=(sum(1 for s in gen.scenes if s.clip_path) + 1) if gen.status == GenStatus.HAND else None,
                  clips=[bool(s.clip_path and Path(s.clip_path).exists()) for s in gen.scenes],
                  can_redo=gen.status in (GenStatus.READY, GenStatus.FAILED),
                  prompts=[s.prompt for s in gen.scenes], media=_scene_media(gen, library),
                  options={"framing": sc.FRAMINGS, "angle": sc.ANGLES, "movement": sc.MOVEMENTS, "lens": sc.LENSES,
                           "position": sc.POSITIONS, "time_of_day": sc.TIMES_OF_DAY, "style": sc.STYLES},
                  max_words=sc.words_per_clip(SCENE_SECONDS.get(gen.provider, 10)),
                  max_words_more=sc.words_per_clip(SCENE_SECONDS.get(gen.provider, 10), 2), max_prompt=sc.prompt_limit(gen.provider),
                  seconds=SCENE_SECONDS.get(gen.provider, 10))


# A video at these can't take a new start picture or sound: it's joined (a remake opens it again)
JOINED = (GenStatus.CANCELLED, GenStatus.READY, GenStatus.SAVING, GenStatus.SAVE_FAILED)


def _scene_media(gen: Generation, library: list[dict]) -> list[dict]:
    """A Flow video's per-scene first picture and sound effect, for the script and by-hand pages: whether each has
    its own, what its clip starts from, whether they can change now, and its picture prompt to draw in Flow."""
    if gen.provider != Provider.FLOW:
        return []
    out = []
    for scene in gen.scenes:
        start = bool(scene.start_path and Path(scene.start_path).exists())
        making = bool(gen.worker_job_id) and next((x for x in gen.scenes if not x.clip_path), None) is scene
        out.append({
            "start": start, "sound": bool(scene.sound_path and Path(scene.sound_path).exists()),
            "from": "its own start picture" if start
            else "the locked still" if scene.position == 1 and gen.lock_frames and not gen.by_hand
            else "the previous scene's last frame" if scene.position > 1 else "the character pictures",
            "can_start": gen.status not in JOINED and not scene.clip_path and not making,
            "can_sound": gen.status not in JOINED,
            "picture": sc.picture_prompt(scene.script(), library, gen.style, gen.bible, gen.aspect, gen.genre)
            if scene.action or scene.characters else "",
        })
    return out


@router.get("/generations/{gen_id}/by-hand", response_class=HTMLResponse)
def by_hand_page(gen_id: int, request: Request, user=Depends(require_page_user)):
    """A Flow video made by hand: the prompt and pictures for the scene that's next, and where to upload its clip."""
    hand = request.app.state.service.hand_scene(gen_id, user.id)
    gen = hand["gen"]
    svc, settings = request.app.state.service, request.app.state.settings
    library = svc.script(gen_id, user.id)[1] if hand["scene"] is not None else []
    media = _scene_media(gen, library)[hand["scene"].position - 1] if hand["scene"] is not None else None
    return render(request, "by_hand.html", g=gen, scene=hand["scene"], prompt=hand["prompt"], images=hand["images"],
                  media=media,
                  mode=hand["mode"], done=sum(1 for s in gen.scenes if s.clip_path), seconds=SCENE_SECONDS.get(gen.provider, 8),
                  bot_ready=settings.bot_configured,bot_online=svc.bot_online())


# the system this app (and a bot it starts) runs on, as handbot/config.py names it: mac, windows or linux
PLATFORM = "mac" if sys.platform == "darwin" else "windows" if sys.platform.startswith("win") else "linux"


@router.get("/autoflow", response_class=HTMLResponse)
def scripts_page(request: Request, user=Depends(require_page_user)):
    """The scripts in the scripts folder, and the run that has the hand bot make them one after another."""
    st = request.app.state
    runs = st.script_runs
    run = runs.run_of(user.id) or {}
    error = run.get("error") or ""
    # the run stopped for Flow's credits: AutoFlow stopped the bot itself, so it didn't exit on its own
    no_credits = not run.get("started") and bool(NO_CREDITS.search(error))
    return render(request, "scripts.html", scripts=runs.scripts(user.id), running=bool(run.get("started")),
                  error=error, folder=runs.folder.resolve(), bot_ready=st.settings.bot_configured,
                  bot_online=st.bot.running(), bot_exited="" if no_credits else st.bot.exited(),
                  no_credits=no_credits, autostart=st.settings.BOT_AUTOSTART, platform=PLATFORM)


@router.get("/autoflow/script", response_class=HTMLResponse)
def script_preview(request: Request, name: str, user=Depends(require_page_user)):
    """One script of the AutoFlow list: its character pictures, what Flow gets for each clip, and the whole script."""
    sc = request.app.state.script_runs.preview(name)
    if sc is None:
        raise HTTPException(404, "That script isn't in the scripts folder any more.")
    return render(request, "script_preview.html", sc=sc)


@router.get("/autoflow/picture")
def script_picture(request: Request, name: str, n: int, user=Depends(require_page_user)):
    """A folder script's character picture, for its preview."""
    path = request.app.state.script_runs.picture(name, n)
    if path is None:
        raise HTTPException(404, "No such picture.")
    return FileResponse(path, headers={"Cache-Control": "private, no-cache"})


@router.post("/autoflow/bot")
def start_bot(request: Request, user=Depends(require_post)):
    """AutoFlow's "Start the bot": runs the hand bot on this computer, so no terminal is needed."""
    st = request.app.state
    if not st.settings.bot_configured:
        raise UserError("The hand bot isn't set up: BOT_API_KEY and BOT_USER_EMAIL aren't set.")
    if not st.settings.BOT_AUTOSTART:
        raise UserError("BOT_AUTOSTART is off in .env: start the bot on its own computer.")
    st.bot.ensure(force=True)
    return {"ok": True}


@router.post("/autoflow/add")
async def add_script(request: Request, user=Depends(require_post), script: UploadFile = File(...),
                     pictures: list[UploadFile] = File(default=[])):
    """AutoFlow's "Add a script": a script file from the device, with its character pictures if any, saved into the
    scripts folder (read at most a little over the limits, so a wrong file isn't read whole)."""
    from app.media import MAX_IMAGE_BYTES
    from app.script_runs import MAX_SCRIPT_BYTES
    data = await script.read(MAX_SCRIPT_BYTES + 1)
    pics = [(f.filename, await f.read(MAX_IMAGE_BYTES + 1)) for f in pictures if f.filename]
    name = request.app.state.script_runs.add((script.filename or "", data), pics)
    return {"ok": True, "name": name}


@router.post("/autoflow/start")
def start_scripts(request: Request, user=Depends(require_post)):
    request.app.state.script_runs.start(user.id)
    return {"ok": True}


@router.post("/autoflow/stop")
def stop_scripts(request: Request, user=Depends(require_post)):
    request.app.state.script_runs.stop(user.id)
    return {"ok": True}


@router.get("/frames", response_class=HTMLResponse)
def frames_tool(request: Request, user=Depends(require_page_user)):
    """Upload any video and get its first and last frame (nothing is kept)."""
    return render(request, "frames_tool.html")


@router.get("/generations/{gen_id}/frames", response_class=HTMLResponse)
def frames_page(gen_id: int, request: Request, user=Depends(require_page_user)):
    """Each scene's first and last frame, to download and use as a Start or End frame in Google Flow."""
    gen, _ = request.app.state.service.script(gen_id, user.id)
    return render(request, "frames.html", g=gen, scenes=[s for s in gen.scenes if s.clip_path],
                  seconds=SCENE_SECONDS.get(gen.provider, 8))


HISTORY_STATUSES = {"all": "All", "ready": "Ready", "in_progress": "In progress", "failed": "Failed",
                    "cancelled": "Cancelled"}
PER_PAGE = 20


@router.get("/rules", response_class=HTMLResponse)
def rules_page(request: Request, user=Depends(require_page_user)):
    """The prompt rules for each screen, Google Flow and Gemini Studio, each with a full example to copy."""
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
