"""Start and stop the camera, face and surveillance workers (startup, shutdown, restore)."""

import logging
import threading

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from app.camera.manager import manager
from app.config import settings
from app.models import Camera
from app.pipeline.worker import load_active_gallery, pipeline_manager
from app.surveillance.worker import surveillance_manager

logger = logging.getLogger(__name__)
_fernet = Fernet(settings.fernet_key.encode())
# Serialises start/stop so a restore cannot race a shutdown.
workers_lock = threading.RLock()


def start_workers(db: Session) -> None:
    with workers_lock:
        cameras: list[Camera] = []
        sources: list[tuple[int, str]] = []
        for camera in db.query(Camera).filter(Camera.enabled.is_(True)).all():
            try:
                sources.append((camera.id, _fernet.decrypt(camera.rtsp_url_encrypted.encode()).decode()))
                cameras.append(camera)
            except InvalidToken:
                logger.error(
                    "Camera %s (%s) skipped: its saved URL cannot be decrypted with the "
                    "current FERNET_KEY. Re-enter the camera URL on the Cameras page.",
                    camera.id,
                    camera.name,
                )
        manager.start_enabled(sources)
        pipeline_manager.start_enabled(
            [(camera.id, camera.gate, camera.direction) for camera in cameras if camera.face_enabled],
            load_active_gallery(db),
        )
        surveillance_manager.start_enabled([camera.id for camera in cameras if camera.surveillance_enabled])


def stop_workers() -> None:
    with workers_lock:
        try:
            surveillance_manager.stop_all()
            pipeline_manager.stop_all()
        finally:
            manager.stop_all()
