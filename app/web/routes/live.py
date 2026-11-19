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
from app.pipeline.overlay import FaceOverlay, ObjectOverlay, face_overlays, object_overlays
from app.surveillance.rules import RuleProvider

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/live", dependencies=[Depends(get_current_user)])
templates = Jinja2Templates(directory=Path(__file__).resolve().parents[1] / "templates")

LIVE_FPS = 8.0
LIVE_MAX_WIDTH = 1280
LIVE_JPEG_QUALITY = 75
LIVE_REMOTE_FPS = 4.0
LIVE_REMOTE_MAX_WIDTH = 640
LIVE_REMOTE_JPEG_QUALITY = 60
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
_CATEGORY_COLOURS = {"blacklist": (0, 0, 255), "vip": (0, 190, 255)}
_CATEGORY_PREFIX = {"blacklist": "BLACKLIST ", "vip": "VIP "}
_OBJECT_COLOURS = {"person": (235, 180, 60), "vehicle": (200, 120, 230)}
_ZONE_COLOUR = (230, 200, 0)
_ZONE_HIT_COLOUR = (0, 0, 255)
_STATUS_TEXT = {"checking": "Checking...", "unknown": "Unknown", "low_quality": "Low quality"}
NAME_REFRESH_SECONDS = 10.0
LAYERS = ("faces", "bodies", "zones")
_names_lock = threading.Lock()
_names: dict[int, str] = {}
_categories: dict[int, str] = {}
_names_loaded_at = -1e9
_rule_providers: dict[int, RuleProvider] = {}


def _load_people() -> None:
    global _names, _categories, _names_loaded_at
    now = time.monotonic()
    with _names_lock:
        if now - _names_loaded_at < NAME_REFRESH_SECONDS:
            return
        _names_loaded_at = now
        try:
            with SessionLocal() as db:
                rows = db.execute(select(Person.id, Person.name, Person.code, Person.category)).all()
            # cv2.putText only draws ASCII, so non-ASCII names fall back to the code.
            _names = {pid: (name if name.isascii() else code) for pid, name, code, _ in rows}
            _categories = {pid: category for pid, _, _, category in rows}
        except Exception:
            logger.exception("Unable to load person names for the live view.")


def _person_labels() -> dict[int, str]:
    _load_people()
    return _names


def _person_categories() -> dict[int, str]:
    _load_people()
    return _categories


def _label(face: FaceOverlay, names: dict[int, str], categories: dict[int, str] | None = None) -> str:
    if face.status == "matched" and face.person_id is not None:
        text = names.get(face.person_id, f"#{face.person_id}")
        text = _CATEGORY_PREFIX.get((categories or {}).get(face.person_id, ""), "") + text
    else:
        text = _STATUS_TEXT[face.status]
    if face.score is not None and face.status != "low_quality":
        text += f" {face.score:.2f}"
    return text


def _tag(frame: np.ndarray, text: str, x: int, y: int, h: int, colour, thickness: int, font_scale: float) -> None:
    height = frame.shape[0]
    (tw, th), baseline = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, font_scale, thickness)
    top = y - th - baseline - 6
    if top < 0:
        top = min(height - th - baseline - 6, y + h)
    cv2.rectangle(frame, (x, top), (x + tw + 8, top + th + baseline + 6), colour, -1)
    cv2.putText(
        frame, text, (x + 4, top + th + 3), cv2.FONT_HERSHEY_SIMPLEX,
        font_scale, (255, 255, 255), thickness, cv2.LINE_AA,
    )


def _sizes(frame: np.ndarray) -> tuple[int, float]:
    width = frame.shape[1]
    return max(2, width // 640), max(0.5, width / 1600)


def draw_zones(frame: np.ndarray, rules: list, hit_rule_ids: set[int]) -> np.ndarray:
    """Rule areas as a light fill with an outline; lines as lines. A rule that just fired turns red."""
    if not rules:
        return frame
    height, width = frame.shape[:2]
    thickness, font_scale = _sizes(frame)
    fill = frame.copy()
    outlines = []
    for rule in rules:
        points = np.array([(int(px * width), int(py * height)) for px, py in rule.points], dtype=np.int32)
        if len(points) < 2:
            continue
        colour = _ZONE_HIT_COLOUR if rule.id in hit_rule_ids else _ZONE_COLOUR
        if rule.kind != "line" and len(points) >= 3:
            cv2.fillPoly(fill, [points], colour)
        outlines.append((rule, points, colour))
    frame = cv2.addWeighted(fill, 0.18, frame, 0.82, 0)
    for rule, points, colour in outlines:
        cv2.polylines(frame, [points], rule.kind != "line", colour, thickness, cv2.LINE_AA)
        x, y = int(points[:, 0].min()), int(points[:, 1].min())
        label = rule.name if rule.name.isascii() else f"#{rule.id}"
        _tag(frame, label, x, max(y, 0), 0, colour, max(1, thickness - 1), font_scale * 0.8)
    return frame


def draw_objects(frame: np.ndarray, objects: list[ObjectOverlay]) -> np.ndarray:
    if not objects:
        return frame
    frame = frame.copy()
    thickness, font_scale = _sizes(frame)
    for item in objects:
        x, y, w, h = (int(v) for v in item.box)
        colour = _OBJECT_COLOURS.get(item.category, (200, 200, 200))
        cv2.rectangle(frame, (x, y), (x + w, y + h), colour, max(1, thickness - 1))
        _tag(frame, f"{item.label} #{item.track_id}", x, y, h, colour, max(1, thickness - 1), font_scale * 0.8)
    return frame


def draw_faces(
    frame: np.ndarray,
    faces: list[FaceOverlay],
    names: dict[int, str],
    categories: dict[int, str] | None = None,
) -> np.ndarray:
    if not faces:
        return frame
    frame = frame.copy()
    thickness, font_scale = _sizes(frame)
    for face in faces:
        x, y, w, h = (int(v) for v in face.box)
        colour = _COLOURS[face.status]
        if face.status == "matched" and face.person_id is not None:
            colour = _CATEGORY_COLOURS.get((categories or {}).get(face.person_id, ""), colour)
        cv2.rectangle(frame, (x, y), (x + w, y + h), colour, thickness)
        _tag(frame, _label(face, names, categories), x, y, h, colour, thickness, font_scale)
    return frame


def _rules_for(camera_id: int) -> list:
    provider = _rule_providers.get(camera_id)
    if provider is None:
        provider = _rule_providers.setdefault(camera_id, RuleProvider(camera_id))
    return provider()


def _compose(
    frame: np.ndarray,
    camera_id: int,
    layers: frozenset[str],
    max_width: int = LIVE_MAX_WIDTH,
    quality: int = LIVE_JPEG_QUALITY,
) -> bytes | None:
    """Runs in a worker thread: everything here may touch the database or do image work."""
    if "zones" in layers or "bodies" in layers:
        objects, hit_rule_ids = object_overlays.latest(camera_id)
        if "zones" in layers:
            frame = draw_zones(frame, _rules_for(camera_id), hit_rule_ids)
        if "bodies" in layers:
            frame = draw_objects(frame, objects)
    if "faces" in layers:
        faces = face_overlays.latest(camera_id)
        if faces:
            frame = draw_faces(frame, faces, _person_labels(), _person_categories())
    return _encode(frame, max_width=max_width, quality=quality)


def _encode(
    frame: np.ndarray,
    max_width: int = LIVE_MAX_WIDTH,
    quality: int = LIVE_JPEG_QUALITY,
) -> bytes | None:
    height, width = frame.shape[:2]
    if width > max_width:
        scale = max_width / width
        frame = cv2.resize(frame, (max_width, int(height * scale)), interpolation=cv2.INTER_AREA)
    ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
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


async def _mjpeg(
    request: Request, camera_id: int, layers: frozenset[str] = frozenset({"faces"})
) -> AsyncIterator[bytes]:
    # Counted inside the generator so a stream that never starts never leaks a slot.
    if not _acquire_stream():
        return
    headers = getattr(request, "headers", {})
    query_params = getattr(request, "query_params", {})
    is_remote = bool(headers.get("cf-connecting-ip")) or query_params.get("low") == "1"
    fps = LIVE_REMOTE_FPS if is_remote else LIVE_FPS
    max_width = LIVE_REMOTE_MAX_WIDTH if is_remote else LIVE_MAX_WIDTH
    quality = LIVE_REMOTE_JPEG_QUALITY if is_remote else LIVE_JPEG_QUALITY
    interval = 1.0 / fps
    last_sent = None
    try:
        while not await request.is_disconnected():
            state = manager.get_state(camera_id)
            if state.last_frame_at is not None and state.last_frame_at != last_sent:
                frame = manager.get_latest_frame(camera_id)
                if frame is not None:
                    jpeg = await asyncio.to_thread(_compose, frame, camera_id, layers, max_width, quality)
                    if jpeg is not None:
                        last_sent = state.last_frame_at
                        yield (
                            f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                            f"Content-Length: {len(jpeg)}\r\n\r\n"
                        ).encode() + jpeg + b"\r\n"
            await asyncio.sleep(interval)
    finally:
        _release_stream()


def _parse_layers(layers: str | None, faces: int = 1) -> frozenset[str]:
    """`layers=faces,bodies,zones`; the older `faces=0/1` switch still works."""
    if layers is None:
        return frozenset({"faces"}) if faces else frozenset()
    return frozenset(item for item in layers.split(",") if item in LAYERS)


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
    camera_id: int,
    request: Request,
    faces: int = 1,
    layers: str | None = None,
    db: Session = Depends(get_db),
) -> StreamingResponse:
    camera = db.get(Camera, camera_id)
    if camera is None or not camera.enabled:
        raise HTTPException(status_code=404, detail="Camera not available")
    with _streams_lock:
        if _open_streams >= MAX_LIVE_STREAMS:
            raise HTTPException(status_code=503, detail="Too many live streams open")
    return StreamingResponse(
        _mjpeg(request, camera_id, _parse_layers(layers, faces)),
        media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
