import asyncio
import logging
import threading
import time
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
from app.db import SessionLocal, get_db
from app.i18n import TRANSLATIONS
from app.models import Camera, Person
from app.pipeline.overlay import FaceOverlay, face_overlays

logger = logging.getLogger(__name__)
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


# BGR colours per face status.
_COLOURS = {
    "matched": (80, 175, 76),
    "checking": (0, 200, 255),
    "unknown": (40, 40, 220),
    "low_quality": (160, 160, 160),
}
_STATUS_TEXT = {"checking": "Checking...", "unknown": "Unknown", "low_quality": "Low quality"}
NAME_REFRESH_SECONDS = 10.0
_names_lock = threading.Lock()
_names: dict[int, str] = {}
_names_loaded_at = -1e9


def _person_labels() -> dict[int, str]:
    """Person id -> label. cv2.putText only draws ASCII, so non-ASCII names fall back to the code."""
    global _names, _names_loaded_at
    now = time.monotonic()
    with _names_lock:
        if now - _names_loaded_at >= NAME_REFRESH_SECONDS:
            _names_loaded_at = now
            try:
                with SessionLocal() as db:
                    rows = db.execute(select(Person.id, Person.name, Person.code)).all()
                _names = {
                    pid: (name if name.isascii() else code) for pid, name, code in rows
                }
            except Exception:
                logger.exception("Unable to load person names for the live view.")
        return _names


def _label(face: FaceOverlay, names: dict[int, str]) -> str:
    if face.status == "matched" and face.person_id is not None:
        text = names.get(face.person_id, f"#{face.person_id}")
    else:
        text = _STATUS_TEXT[face.status]
    if face.score is not None and face.status != "low_quality":
        text += f" {face.score:.2f}"
    return text


def draw_faces(frame: np.ndarray, faces: list[FaceOverlay], names: dict[int, str]) -> np.ndarray:
    if not faces:
        return frame
    frame = frame.copy()
    height, width = frame.shape[:2]
    thickness = max(2, width // 640)
    font_scale = max(0.5, width / 1600)
    for face in faces:
        x, y, w, h = (int(v) for v in face.box)
        colour = _COLOURS[face.status]
        cv2.rectangle(frame, (x, y), (x + w, y + h), colour, thickness)
        text = _label(face, names)
        (tw, th), baseline = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, font_scale, thickness)
        top = y - th - baseline - 6
        if top < 0:
            top = min(height - th - baseline - 6, y + h)
        cv2.rectangle(frame, (x, top), (x + tw + 8, top + th + baseline + 6), colour, -1)
        cv2.putText(
            frame, text, (x + 4, top + th + 3), cv2.FONT_HERSHEY_SIMPLEX,
            font_scale, (255, 255, 255), thickness, cv2.LINE_AA,
        )
    return frame


def _render(frame: np.ndarray, faces: list[FaceOverlay], names: dict[int, str]) -> bytes | None:
    return _encode(draw_faces(frame, faces, names))


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


async def _mjpeg(request: Request, camera_id: int, faces: bool = True) -> AsyncIterator[bytes]:
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
                    if faces:
                        overlays = face_overlays.latest(camera_id)
                        names = await asyncio.to_thread(_person_labels) if overlays else {}
                        jpeg = await asyncio.to_thread(_render, frame, overlays, names)
                    else:
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
def live_stream(
    camera_id: int, request: Request, faces: int = 1, db: Session = Depends(get_db)
) -> StreamingResponse:
    camera = db.get(Camera, camera_id)
    if camera is None or not camera.enabled:
        raise HTTPException(status_code=404, detail="Camera not available")
    with _streams_lock:
        if _open_streams >= MAX_LIVE_STREAMS:
            raise HTTPException(status_code=503, detail="Too many live streams open")
    return StreamingResponse(
        _mjpeg(request, camera_id, faces=bool(faces)),
        media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
