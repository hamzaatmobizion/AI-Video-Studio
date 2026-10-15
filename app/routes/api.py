import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from app.auth import require_api_user, require_post
from app.enums import Aspect, GenStatus
from app.interfaces import StorageError
from app.media import MAX_IMAGE_BYTES, MAX_IMAGES, image_kind
from app.models import Generation
from app.routes.views import cards_for
from app.services.generation import UserError, drive_name

router = APIRouter()
MAX_PROMPT = 2000


@router.get("/api/generations")
def list_generations(request: Request, user=Depends(require_api_user)):
    cards = cards_for(request, user.id)
    return {"cards": cards, "active": any(c["active"] for c in cards)}


@router.post("/generations")
async def create_generation(request: Request, user=Depends(require_post), prompt: str = Form(""),
                            aspect: str = Form("9:16"), images: list[UploadFile] = File(default=[])):
    prompt = prompt.strip()
    if not prompt:
        raise UserError("Write a prompt first.")
    if len(prompt) > MAX_PROMPT:
        raise UserError(f"Keep the prompt under {MAX_PROMPT} characters.")
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
    gen_id = request.app.state.service.create(user.id, prompt, aspect, checked)
    return {"id": gen_id}


@router.post("/generations/{gen_id}/{action}")
def generation_action(gen_id: int, action: str, request: Request, user=Depends(require_post)):
    svc = request.app.state.service
    actions = {"cancel": svc.cancel, "retry": svc.retry, "dismiss": svc.dismiss, "save": svc.save_again,
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
