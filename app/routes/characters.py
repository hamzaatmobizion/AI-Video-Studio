"""The character library: reusable cast members (look, outfit, voice, reference picture)."""
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from sqlalchemy import select

from app.auth import require_api_user, require_page_user, require_post
from app.media import MAX_IMAGE_BYTES, image_kind
from app.models import Character
from app.routes.pages import render
from app.script import CHARACTER_LIMITS, clean_text
from app.services.generation import UserError

router = APIRouter()
MAX_CHARACTERS = 50


@router.get("/characters", response_class=HTMLResponse)
def characters_page(request: Request, user=Depends(require_page_user)):
    with request.app.state.db.session() as s:
        rows = s.scalars(select(Character).where(Character.user_id == user.id).order_by(Character.name)).all()
    return render(request, "characters.html", characters=rows, limits=CHARACTER_LIMITS)


def _fields(name: str, appearance: str, outfit: str, voice: str, personality: str) -> dict:
    values = {"name": name, "appearance": appearance, "outfit": outfit, "voice": voice, "personality": personality}
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
    path = d / f"{ch.id}.{image[0]}"
    path.write_bytes(image[1])
    ch.image_path = str(path)


def _drop_image(ch: Character) -> None:
    if ch.image_path:
        Path(ch.image_path).unlink(missing_ok=True)
        ch.image_path = None


def _own(s, char_id: int, user_id: int) -> Character:
    ch = s.get(Character, char_id)
    if ch is None or ch.user_id != user_id:
        raise UserError("Not found.", 404)
    return ch


@router.post("/characters")
async def create_character(request: Request, user=Depends(require_post), name: str = Form(""),
                           appearance: str = Form(""), outfit: str = Form(""), voice: str = Form(""),
                           personality: str = Form(""), image: UploadFile | None = File(None)):
    fields = _fields(name, appearance, outfit, voice, personality)
    picture = await _image(image)
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
                           personality: str = Form(""), remove_image: str = Form(""),
                           image: UploadFile | None = File(None)):
    fields = _fields(name, appearance, outfit, voice, personality)
    picture = await _image(image)
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
    return FileResponse(ch.image_path, headers={"Cache-Control": "private, no-cache"})
