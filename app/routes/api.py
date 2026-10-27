import math
import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from starlette.background import BackgroundTask

from app.auth import require_api_user, require_post
from app.checks import prompt_notes
from app.enums import Aspect, GenStatus
from app.interfaces import StorageError
from app.media import MAX_IMAGE_BYTES, MAX_IMAGES, MAX_MUSIC_BYTES, audio_kind, image_kind
from app.models import Character, Generation
from app.script import MAX_CAST, TEXT_LIMITS, aspect_from_prompt
from app.routes.views import cards_for
from app.services.generation import UserError, drive_name

router = APIRouter()
MAX_SCENE_TEXT = TEXT_LIMITS["action"]  # one scene's words; the clip prompt adds the format and quality rules
MAX_IDEA = 10000  # one prompt that Gemini's chat turns into scene prompts
MAX_SCENES = 12  # × ~10 s ≈ 2 minutes
SCENE_SECONDS = 10

_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
          "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "half a": 0.5, "half an": 0.5}
# "30 second", "45-sec", "1.5 minutes", "two min", "half a minute". Bare "s"/"m" are left out on purpose:
# "80s style" or "the 1990s" aren't lengths.
_LENGTH = re.compile(r"\b(\d+(?:\.\d+)?|half an?|an?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
                     r"[\s-]*(seconds?|secs?|minutes?|mins?)\b", re.I)
# "1:30" as a length (0–2 minutes only, so "9:16" or "4:30 pm" aren't read as one)
_CLOCK = re.compile(r"\b([0-2]):([0-5]\d)\b(?!\s*[ap]\.?m\b)", re.I)


def duration_from_prompt(text: str) -> int | None:
    """The video length the prompt asks for (e.g. "30 second", "1 minute", "1:30"), as a valid duration:
    rounded up to whole scenes, 10 s to 2 minutes. The largest length mentioned wins; None if there's none."""
    found = []
    for num, unit in _LENGTH.findall(text):
        n = _WORDS.get(" ".join(num.lower().split()))
        n = float(num) if n is None else n
        found.append(n * 60 if unit.lower().startswith("m") else n)
    found += [int(m) * 60 + int(s) for m, s in _CLOCK.findall(text)]
    longest = max((f for f in found if f > 0), default=None)
    if longest is None:
        return None
    scenes = math.ceil(longest / SCENE_SECONDS)
    return min(max(scenes, 1), MAX_SCENES) * SCENE_SECONDS


@router.get("/api/generations")
def list_generations(request: Request, user=Depends(require_api_user)):
    cards = cards_for(request, user.id)
    return {"cards": cards, "active": any(c["active"] for c in cards)}


@router.post("/generations")
async def create_generation(request: Request, user=Depends(require_post), prompt: str = Form(""),
                            duration: str = Form(""), scenes: list[str] = Form(default=[]),
                            aspect: str = Form("9:16"), images: list[UploadFile] = File(default=[]),
                            cast: list[str] = Form(default=[]), style: str = Form(""), language: str = Form(""),
                            review: str = Form(""), genre: str = Form(""), music: UploadFile | None = File(None),
                            lock_frames: str = Form(""), check_clips: str = Form("")):
    """Either one `prompt` + `duration` (Gemini writes the script), or `scenes` written by the user.
    An empty `duration` (Default) takes the length from the prompt, else 10 seconds.
    `cast`: ids of the user's characters to use. `review`: stop for the user to edit and approve the script.
    `genre`: the video type (app/genres.py); empty (Auto) reads it from the prompt. `music`: one track under the
    whole video. `lock_frames`: each scene starts from a still made first; `check_clips`: Gemini checks each clip."""
    plan = None
    review = review.strip().lower() in ("1", "on", "true", "yes")
    if not all(c.strip().isdigit() for c in cast):
        raise UserError("Pick characters from the list.")
    cast_ids = list(dict.fromkeys(int(c) for c in cast))
    if len(cast_ids) > MAX_CAST:
        raise UserError(f"Pick at most {MAX_CAST} characters for one video.")
    if not scenes:
        if duration.strip().lower() in ("", "auto"):
            duration = duration_from_prompt(prompt) or SCENE_SECONDS
        else:
            duration = int(duration) if duration.strip().isdigit() else 0
        if duration % SCENE_SECONDS or not SCENE_SECONDS <= duration <= MAX_SCENES * SCENE_SECONDS:
            raise UserError(f"Duration must be {SCENE_SECONDS} seconds to {MAX_SCENES * SCENE_SECONDS // 60} minutes, "
                            f"in steps of {SCENE_SECONDS} seconds.")
        prompt = prompt.strip()
        if not prompt:
            raise UserError("Write the prompt first.")
        if len(prompt) > MAX_IDEA:
            raise UserError(f"Keep the prompt under {MAX_IDEA:,} characters.")
        count = duration // SCENE_SECONDS
        # Gemini's chat writes the script for a long video, condenses a long prompt for one clip, and
        # drafts it as a script whenever there is a cast to direct or a script to review.
        plan = count if count > 1 or len(prompt) > MAX_SCENE_TEXT or cast_ids or review else None
        scenes = [prompt]
    else:
        scenes = [s.strip() for s in scenes]
        if len(scenes) > MAX_SCENES:
            raise UserError(f"A video can have at most {MAX_SCENES} scenes.")
        for i, text in enumerate(scenes, 1):
            if not text:
                raise UserError(f"Write scene {i} first.")
            if len(text) > MAX_SCENE_TEXT:
                raise UserError(f"Keep scene {i} under {MAX_SCENE_TEXT:,} characters.")
    aspect = aspect_from_prompt("\n".join(scenes)) or aspect  # a format written in the prompt wins
    if aspect not in {a.value for a in Aspect}:
        raise UserError("Aspect must be 9:16 or 16:9.")
    images = [f for f in images if f.filename]
    if len(images) > MAX_IMAGES:
        raise UserError(f"Attach at most {MAX_IMAGES} reference images.")
    checked = []
    for f in images:
        data = await f.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            raise UserError(f"{f.filename} is larger than 10 MB.")
        kind = image_kind(data[:16])
        if kind is None:
            raise UserError(f"{f.filename} isn't a JPG, PNG or WebP image.")
        checked.append((kind, data))
    track = None
    if music is not None and music.filename:
        data = await music.read(MAX_MUSIC_BYTES + 1)
        if len(data) > MAX_MUSIC_BYTES:
            raise UserError(f"{music.filename} is larger than 20 MB.")
        kind = audio_kind(data[:16])
        if kind is None:
            raise UserError(f"{music.filename} isn't an MP3, M4A, WAV or OGG file.")
        track = (kind, data)
    on = lambda v: v.strip().lower() in ("1", "on", "true", "yes")  # noqa: E731
    gen_id = request.app.state.service.create(user.id, scenes, aspect, checked, plan=plan, cast_ids=cast_ids,
                                              style=style, language=language, review=review, genre=genre,
                                              music=track, lock_frames=on(lock_frames), check_clips=on(check_clips))
    return {"id": gen_id, "review": review}


class CheckBody(BaseModel):
    prompt: str = ""
    cast: list[int] = []
    style: str = ""
    language: str = ""
    seconds: int = 0


@router.post("/api/prompt-check")
def prompt_check(body: CheckBody, request: Request, user=Depends(require_api_user)):
    """Notes on a prompt and its picked characters before it goes to Gemini (app/checks.py). Changes nothing."""
    with request.app.state.db.session() as s:
        cast = [c.sheet() for c in s.scalars(select(Character).where(Character.user_id == user.id,
                                                                    Character.id.in_(body.cast[:MAX_CAST])))]
    return {"notes": prompt_notes(body.prompt[:MAX_IDEA], cast, body.style[:300], body.language[:40],
                                   max(0, min(body.seconds, MAX_SCENES * SCENE_SECONDS)))}


class ScriptBody(BaseModel):
    scenes: list
    style: str = ""
    language: str = ""
    approve: bool = False
    bible: dict | None = None
    genre: str | None = None


@router.post("/generations/{gen_id}/script")
def save_script(gen_id: int, body: ScriptBody, request: Request, user=Depends(require_post)):
    """Save the edited script (and get each scene's clip prompt back); `approve` starts the clips."""
    scenes = request.app.state.service.save_script(gen_id, user.id, body.scenes, body.style, body.language,
                                                   approve=body.approve, bible=body.bible, genre=body.genre)
    return {"ok": True, "scenes": scenes}


@router.post("/generations/{gen_id}/scenes/{position}/redo")
def redo_scene(gen_id: int, position: int, request: Request, user=Depends(require_post)):
    """Make one scene again (the others are kept) and rejoin the video."""
    request.app.state.service.redo_scene(gen_id, user.id, position)
    return {"ok": True}


@router.get("/generations/{gen_id}/scenes/{position}/clip")
def scene_clip(gen_id: int, position: int, request: Request, user=Depends(require_api_user)):
    """One scene's clip, to watch it before or after the video is joined."""
    path = request.app.state.service.scene_clip(gen_id, user.id, position)
    if path is None:
        return JSONResponse({"detail": "This scene's clip isn't kept any more."}, status_code=404)
    return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": "private, no-cache"})


@router.post("/generations/{gen_id}/{action}")
def generation_action(gen_id: int, action: str, request: Request, user=Depends(require_post)):
    svc = request.app.state.service
    actions = {"cancel": svc.cancel, "retry": svc.retry, "resume": svc.resume, "dismiss": svc.dismiss,
               "save": svc.save_again,
               "restore": svc.restore, "delete": svc.delete}
    if action not in actions:
        return JSONResponse({"detail": "Unknown action."}, status_code=404)
    actions[action](gen_id, user.id)
    return {"ok": True}


_RANGE = re.compile(r"bytes=\d*-\d*")


@router.get("/generations/{gen_id}/file")
def generation_file(gen_id: int, request: Request, download: bool = False, user=Depends(require_api_user)):
    st = request.app.state
    with st.db.session() as s:
        gen = s.get(Generation, gen_id)
    if gen is None or gen.user_id != user.id or gen.status != GenStatus.READY or not gen.drive_file_id:
        return JSONResponse({"detail": "Not found."}, status_code=404)
    storage = st.providers.storage(user.id)
    if storage is None:
        return JSONResponse({"detail": "Google Drive isn't connected."}, status_code=503)
    rng = request.headers.get("range")
    if rng and not _RANGE.fullmatch(rng.strip()):
        rng = None  # multi-range or junk: send the whole file
    try:
        stream = storage.open(gen.drive_file_id, rng)
    except StorageError as e:
        if "range" in e.message.lower():
            return Response(status_code=416, headers={"Content-Range": f"bytes */{gen.file_size or 0}"})
        return JSONResponse({"detail": e.message}, status_code=502)
    headers = dict(stream.headers)
    if download:
        name = drive_name(gen, gen.created_at)[1]
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(name)}"
    return StreamingResponse(stream.chunks, status_code=stream.status, headers=headers,
                             background=BackgroundTask(stream.close))
