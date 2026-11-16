import asyncio
import threading
from collections.abc import AsyncIterator
from pathlib import Path

import cv2
import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.camera.manager import manager
from app.db import get_db
from app.i18n import TRANSLATIONS
from app.models import Camera

router = APIRouter(prefix="/live", dependencies=[Depends(get_current_user)])
templates = Jinja2Templates(directory=Path(__file__).resolve().parents[1] / "templates")

LIVE_FPS = 8.0
LIVE_MAX_WIDTH = 1280
LIVE_JPEG_QUALITY = 75
# Every open stream costs a JPEG encode per frame; cap them so a few open
# browser tabs cannot starve face recognition of CPU.
MAX_LIVE_STREAMS = 8
BOUNDARY = "frame"

_streams_lock = threading.Lock()
_open_streams = 0


def _lang(request: Request) -> str:
    return "bn" if request.query_params.get("lang") == "bn" else "en"


def _encode(frame: np.ndarray) -> bytes | None:
    height, width = frame.shape[:2]
    if width > LIVE_MAX_WIDTH:
        scale = LIVE_MAX_WIDTH / width
        frame = cv2.resize(frame, (LIVE_MAX_WIDTH, int(height * scale)), interpolation=cv2.INTER_AREA)
    ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, LIVE_JPEG_QUALITY])
    return encoded.tobytes() if ok else None


def _acquire_stream() -> bool:
    global _open_streams
    with _streams_lock:
        if _open_streams >= MAX_LIVE_STREAMS:
            return False
        _open_streams += 1
        return True


def _release_stream() -> None:
    global _open_streams
    with _streams_lock:
        _open_streams = max(0, _open_streams - 1)


async def _mjpeg(request: Request, camera_id: int) -> AsyncIterator[bytes]:
    # Counted inside the generator so a stream that never starts never leaks a slot.
    if not _acquire_stream():
        return
    interval = 1.0 / LIVE_FPS
    last_sent = None
    try:
        while not await request.is_disconnected():
            state = manager.get_state(camera_id)
            if state.last_frame_at is not None and state.last_frame_at != last_sent:
                frame = manager.get_latest_frame(camera_id)
                if frame is not None:
                    jpeg = await asyncio.to_thread(_encode, frame)
                    if jpeg is not None:
                        last_sent = state.last_frame_at
                        yield (
                            f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                            f"Content-Length: {len(jpeg)}\r\n\r\n"
                        ).encode() + jpeg + b"\r\n"
            await asyncio.sleep(interval)
    finally:
        _release_stream()


@router.get("", response_class=HTMLResponse)
def live_page(request: Request, camera: int | None = None, db: Session = Depends(get_db)) -> HTMLResponse:
    language = _lang(request)
    t = TRANSLATIONS[language]
    cameras = list(db.scalars(select(Camera).where(Camera.enabled.is_(True)).order_by(Camera.id)))
    selected = next((item for item in cameras if item.id == camera), None)
    return templates.TemplateResponse(
        request=request,
        name="live.html",
        context={
            "language": language,
            "t": t,
            "title": t["live_title"],
            "cameras": [selected] if selected else cameras,
            "single": selected is not None,
        },
    )


@router.get("/{camera_id}.mjpg")
def live_stream(camera_id: int, request: Request, db: Session = Depends(get_db)) -> StreamingResponse:
    camera = db.get(Camera, camera_id)
    if camera is None or not camera.enabled:
        raise HTTPException(status_code=404, detail="Camera not available")
    with _streams_lock:
        if _open_streams >= MAX_LIVE_STREAMS:
            raise HTTPException(status_code=503, detail="Too many live streams open")
    return StreamingResponse(
        _mjpeg(request, camera_id),
        media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
