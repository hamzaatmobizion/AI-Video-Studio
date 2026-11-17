"""The hand bot's routes (handbot/ on its own Mac): it takes the by-hand scene that's waiting, makes the clip in
Google Flow with the real mouse and keyboard, and uploads it. Signed in with BOT_API_KEY, not a session."""
from fastapi import APIRouter, Depends, File, Request, UploadFile
from pydantic import BaseModel
from fastapi.responses import Response

from app.auth import require_bot
from app.routes.api import clip_bytes, hand_picture

router = APIRouter(prefix="/bot")


@router.get("/next")
def next_scene(request: Request, user=Depends(require_bot)):
    """The oldest by-hand scene waiting for its clip: its prompt and how many pictures go with it, in order.
    204 when nothing waits."""
    svc = request.app.state.service
    gen_id = svc.next_hand(user.id)
    if gen_id is None:
        return Response(status_code=204)
    hand = svc.hand_scene(gen_id, user.id)
    if hand["scene"] is None:
        return Response(status_code=204)
    return {"gen_id": gen_id, "position": hand["scene"].position, "scenes": len(hand["gen"].scenes),
            "prompt": hand["prompt"],
            "aspect": hand["gen"].aspect, "start_at": hand["start_at"],
            "images": [{"index": i + 1, "label": im["label"]} for i, im in enumerate(hand["images"])]}


@router.get("/generations/{gen_id}/images/{index}")
def image(gen_id: int, index: int, request: Request, user=Depends(require_bot)):
    """One of the waiting scene's pictures (`index` from 1)."""
    return hand_picture(request.app.state.service.hand_image(gen_id, user.id, index - 1), gen_id, index, True)


@router.post("/generations/{gen_id}/scenes/{position}/clip")
async def upload(gen_id: int, position: int, request: Request, user=Depends(require_bot),
                 clip: UploadFile = File(...)):
    """The clip the bot made for the waiting scene (MP4); the app then joins and saves the video as usual."""
    request.app.state.service.upload_clip(gen_id, user.id, position, await clip_bytes(clip))
    return {"ok": True}


class Stopped(BaseModel):
    message: str = ""


@router.post("/generations/{gen_id}/stopped")
def stopped(gen_id: int, body: Stopped, request: Request, user=Depends(require_bot)):
    """The bot couldn't make the clip: why, for the video's page and card (it leaves the video until "Try again")."""
    request.app.state.service.bot_stopped(gen_id, user.id, body.message)
    return {"ok": True}
