"""The character library: reusable cast members (look, outfit, voice, reference picture), and the
portraits Gemini draws for them. A portrait is made before the character is saved, so the user can try a
few; saving the character with `portrait_id` makes that portrait its reference picture."""
import uuid
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import select

from app.auth import require_api_user, require_page_user, require_post
from app.interfaces import GeneratorError
from app.media import MAX_IMAGE_BYTES, image_kind
from app.models import Character, Portrait, utcnow
from app.routes.pages import render
from app.script import CHARACTER_LIMITS, STYLES, clean_text
from app.services.generation import UserError

router = APIRouter()
MAX_CHARACTERS = 50
PORTRAIT_STYLES = ["Realistic photo", *STYLES]
PORTRAIT_TIMEOUT = timedelta(minutes=20)  # it may wait behind video clips in the worker's queue
PORTRAIT_KEEP = timedelta(days=1)  # unused portraits are cleared after this


@router.get("/characters", response_class=HTMLResponse)
def characters_page(request: Request, user=Depends(require_page_user)):
    st = request.app.state
    with st.db.session() as s:
        rows = s.scalars(select(Character).where(Character.user_id == user.id).order_by(Character.name)).all()
    return render(request, "characters.html", characters=rows, limits=CHARACTER_LIMITS,
                  max_characters=MAX_CHARACTERS, portrait_styles=PORTRAIT_STYLES,
                  can_draw=st.providers.generator() is not None)


def _fields(name: str, appearance: str, outfit: str, voice: str, personality: str, locked: str = "",
            never: str = "") -> dict:
    values = {"name": name, "appearance": appearance, "outfit": outfit, "voice": voice, "personality": personality,
              "locked": locked, "never": never}
    out = {k: clean_text(v, CHARACTER_LIMITS[k]) for k, v in values.items()}
    if not out["name"]:
        raise UserError("Give the character a name.")
    if not out["appearance"]:
        raise UserError("Describe how the character looks, so every scene shows the same person.")
    return out


async def _image(f: UploadFile | None) -> tuple[str, bytes] | None:
    if f is None or not f.filename:
        return None
    data = await f.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise UserError(f"{f.filename} is larger than 10 MB.")
    kind = image_kind(data[:16])
    if kind is None:
        raise UserError(f"{f.filename} isn't a JPG, PNG or WebP image.")
    return kind, data


def _store_image(request: Request, ch: Character, image: tuple[str, bytes]) -> None:
    d = request.app.state.settings.characters_dir
    d.mkdir(parents=True, exist_ok=True)
    _drop_image(ch)
    path = d / f"{ch.id}-{uuid.uuid4().hex[:8]}.{image[0]}"  # a new name per picture, so browsers refetch it
    path.write_bytes(image[1])
    ch.image_path = str(path)


def _drop_image(ch: Character) -> None:
    if ch.image_path:
        Path(ch.image_path).unlink(missing_ok=True)
        ch.image_path = None


def _portrait_picture(request: Request, portrait_id: str, user_id: int) -> tuple[str, bytes] | None:
    """The image of the user's ready portrait `portrait_id`, to become a character's picture."""
    if not portrait_id.strip():
        return None
    if not portrait_id.strip().isdigit():
        raise UserError("That portrait doesn't exist.")
    with request.app.state.db.session() as s:
        p = s.get(Portrait, int(portrait_id))
    if p is None or p.user_id != user_id or p.status != "ready" or not p.image_path or not Path(p.image_path).exists():
        raise UserError("That portrait isn't available any more. Generate it again.")
    data = Path(p.image_path).read_bytes()
    return image_kind(data[:16]) or "png", data


def _own(s, char_id: int, user_id: int) -> Character:
    ch = s.get(Character, char_id)
    if ch is None or ch.user_id != user_id:
        raise UserError("Not found.", 404)
    return ch


@router.post("/characters")
async def create_character(request: Request, user=Depends(require_post), name: str = Form(""),
                           appearance: str = Form(""), outfit: str = Form(""), voice: str = Form(""),
                           personality: str = Form(""), locked: str = Form(""), never: str = Form(""),
                           portrait_id: str = Form(""), image: UploadFile | None = File(None)):
    fields = _fields(name, appearance, outfit, voice, personality, locked, never)
    picture = await _image(image) or _portrait_picture(request, portrait_id, user.id)
    with request.app.state.db.session() as s:
        mine = s.scalars(select(Character).where(Character.user_id == user.id)).all()
        if len(mine) >= MAX_CHARACTERS:
            raise UserError(f"You can keep up to {MAX_CHARACTERS} characters.")
        if any(c.name.lower() == fields["name"].lower() for c in mine):
            raise UserError(f"You already have a character called {fields['name']}.")
        ch = Character(user_id=user.id, **fields)
        s.add(ch)
        s.flush()
        if picture:
            _store_image(request, ch, picture)
        return {"id": ch.id}


@router.post("/characters/{char_id}")
async def update_character(char_id: int, request: Request, user=Depends(require_post), name: str = Form(""),
                           appearance: str = Form(""), outfit: str = Form(""), voice: str = Form(""),
                           personality: str = Form(""), locked: str = Form(""), never: str = Form(""),
                           remove_image: str = Form(""), portrait_id: str = Form(""),
                           image: UploadFile | None = File(None)):
    fields = _fields(name, appearance, outfit, voice, personality, locked, never)
    picture = await _image(image) or _portrait_picture(request, portrait_id, user.id)
    with request.app.state.db.session() as s:
        ch = _own(s, char_id, user.id)
        others = s.scalars(select(Character).where(Character.user_id == user.id, Character.id != char_id)).all()
        if any(c.name.lower() == fields["name"].lower() for c in others):
            raise UserError(f"You already have a character called {fields['name']}.")
        for k, v in fields.items():
            setattr(ch, k, v)
        if picture:
            _store_image(request, ch, picture)
        elif remove_image:
            _drop_image(ch)
    return {"ok": True}


@router.post("/characters/{char_id}/delete")
def delete_character(char_id: int, request: Request, user=Depends(require_post)):
    """Videos that used the character keep its name; new clips no longer get its description."""
    with request.app.state.db.session() as s:
        ch = _own(s, char_id, user.id)
        _drop_image(ch)
        s.delete(ch)
    return {"ok": True}


@router.get("/characters/{char_id}/image")
def character_image(char_id: int, request: Request, user=Depends(require_api_user)):
    with request.app.state.db.session() as s:
        ch = s.get(Character, char_id)
    if ch is None or ch.user_id != user.id or not ch.image_path or not Path(ch.image_path).exists():
        return JSONResponse({"detail": "Not found."}, status_code=404)
    return FileResponse(ch.image_path, headers={"Cache-Control": "private, max-age=86400"})


# ---- portraits -------------------------------------------------------------------------------
class PortraitBody(BaseModel):
    appearance: str = ""
    outfit: str = ""
    style: str = ""


def _portrait_json(p: Portrait) -> dict:
    return {"id": p.id, "status": p.status, "error": p.error,
            "image_url": f"/portraits/{p.id}/image" if p.status == "ready" else None}


def _refresh(request: Request, p: Portrait) -> None:
    """Move a working portrait on: ask the worker, and download the image once it's drawn."""
    if p.status != "working":
        return
    st = request.app.state
    client = st.providers.generator()
    if client is None:
        p.status, p.error = "failed", "Video generation is off: set WORKER_URL and WORKER_API_KEY in .env."
        return
    try:
        job = client.check(p.job_id)
        if job.status in ("queued", "running"):
            if utcnow() - p.created_at > PORTRAIT_TIMEOUT:
                client.cancel(p.job_id)
                p.status, p.error = "failed", "Gemini didn't draw the portrait in time. Try again."
            return
        if job.status == "failed":
            p.status, p.error = "failed", job.error or "Gemini couldn't draw the portrait."
            return
        if not job.image_url:
            p.status, p.error = "failed", "The worker finished but gave no image."
            return
        st.settings.portraits_dir.mkdir(parents=True, exist_ok=True)
        dest = client.download_image(job.image_url, st.settings.portraits_dir / f"{p.id}-{uuid.uuid4().hex[:8]}")
    except GeneratorError as e:
        if e.final:
            p.status, p.error = "failed", e.message
        return  # transient: try again on the next poll
    p.status, p.image_path, p.error = "ready", str(dest), None


def _clear_old_portraits(s, user_id: int) -> None:
    old = s.scalars(select(Portrait).where(Portrait.user_id == user_id, Portrait.status != "working",
                                           Portrait.created_at < utcnow() - PORTRAIT_KEEP)).all()
    for p in old:
        if p.image_path:
            Path(p.image_path).unlink(missing_ok=True)
        s.delete(p)


@router.post("/portraits")
def create_portrait(body: PortraitBody, request: Request, user=Depends(require_post)):
    """Have Gemini draw a portrait from the character's look. One at a time per user."""
    appearance = clean_text(body.appearance, CHARACTER_LIMITS["appearance"])
    outfit = clean_text(body.outfit, CHARACTER_LIMITS["outfit"])
    style = clean_text(body.style, 300)
    if not appearance:
        raise UserError("Describe how the character looks first.")
    st = request.app.state
    client = st.providers.generator()
    if client is None:
        raise UserError("Video generation is off: set WORKER_URL and WORKER_API_KEY in .env.", 503)
    with st.db.session() as s:
        _clear_old_portraits(s, user.id)
        for p in s.scalars(select(Portrait).where(Portrait.user_id == user.id, Portrait.status == "working")).all():
            _refresh(request, p)
            if p.status == "working":
                raise UserError("Gemini is still drawing your last portrait. Wait for it to finish.", 409)
        description = appearance + (f". Outfit: {outfit}" if outfit else "")
        try:
            job_id = client.portrait(description, style, f"portrait-u{user.id}-{uuid.uuid4().hex[:6]}")
        except GeneratorError as e:
            raise UserError(e.message, 502) from e
        p = Portrait(user_id=user.id, job_id=job_id, status="working")
        s.add(p)
        s.flush()
        return _portrait_json(p)


@router.get("/portraits/{portrait_id}")
def portrait_status(portrait_id: int, request: Request, user=Depends(require_api_user)):
    """Polled by the Characters page while Gemini draws."""
    with request.app.state.db.session() as s:
        p = s.get(Portrait, portrait_id)
        if p is None or p.user_id != user.id:
            raise UserError("Not found.", 404)
        _refresh(request, p)
        return _portrait_json(p)


@router.get("/portraits/{portrait_id}/image")
def portrait_image(portrait_id: int, request: Request, user=Depends(require_api_user)):
    with request.app.state.db.session() as s:
        p = s.get(Portrait, portrait_id)
    if p is None or p.user_id != user.id or not p.image_path or not Path(p.image_path).exists():
        return JSONResponse({"detail": "Not found."}, status_code=404)
    return FileResponse(p.image_path, headers={"Cache-Control": "private, max-age=86400"})
