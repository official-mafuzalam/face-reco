"""System health numbers for the health page and the dashboard warning banner."""

import os
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.camera.manager import manager
from app.camera.reader import ReaderState
from app.config import settings
from app.loop_stats import LoopSnapshot
from app.models import Camera

try:  # optional: only needed for memory figures
    import psutil
except ImportError:  # pragma: no cover - depends on the installation
    psutil = None

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CAMERA_OFFLINE_WARN_SECONDS = 60.0
DISK_FREE_WARN_RATIO = 0.10
BACKUP_WARN_DAYS = 2.0


@dataclass(frozen=True)
class CameraHealth:
    camera_id: int
    name: str
    enabled: bool
    state: ReaderState
    face: LoopSnapshot | None
    surveillance: LoopSnapshot | None

    @property
    def offline_seconds(self) -> float | None:
        """How long an enabled camera has been without frames (None while healthy)."""
        if not self.enabled or self.state.status == "online":
            return None
        return self.state.frame_age_seconds


@dataclass(frozen=True)
class SystemHealth:
    cpu_percent: float | None  # this app's share of all CPU cores
    memory_mb: float | None
    system_memory_percent: float | None
    disk_total_gb: float
    disk_free_gb: float
    db_size_mb: float
    last_backup_at: datetime | None

    @property
    def disk_free_ratio(self) -> float:
        return self.disk_free_gb / self.disk_total_gb if self.disk_total_gb else 1.0

    @property
    def backup_age_days(self) -> float | None:
        if self.last_backup_at is None:
            return None
        return (datetime.now(UTC) - self.last_backup_at).total_seconds() / 86400


class _CpuSampler:
    """Process CPU % from the change in process time between two calls (stdlib only)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last: tuple[float, float] | None = None

    def sample(self) -> float | None:
        now_wall, now_cpu = time.monotonic(), time.process_time()
        with self._lock:
            last, self._last = self._last, (now_wall, now_cpu)
        if last is None or now_wall - last[0] < 0.5:
            return None
        cores = os.cpu_count() or 1
        return max(0.0, 100.0 * (now_cpu - last[1]) / (now_wall - last[0]) / cores)


_cpu = _CpuSampler()
_cpu.sample()  # prime, so the first page load already has a reading after a short while


def _resolve(path: Path) -> Path:
    return path if path.is_absolute() else PROJECT_ROOT / path


def _file_size_mb(*paths: Path) -> float:
    total = 0
    for path in paths:
        try:
            total += path.stat().st_size
        except OSError:
            pass
    return total / (1024 * 1024)


def last_backup_time(backup_dir: Path) -> datetime | None:
    try:
        newest = max((p.stat().st_mtime for p in backup_dir.glob("gate-face-*.zip")), default=None)
    except OSError:
        return None
    return datetime.fromtimestamp(newest, UTC) if newest is not None else None


def system_health() -> SystemHealth:
    db_path = _resolve(settings.db_path)
    usage = shutil.disk_usage(db_path.parent if db_path.parent.exists() else PROJECT_ROOT)
    memory_mb = system_memory = None
    if psutil is not None:
        memory_mb = psutil.Process().memory_info().rss / (1024 * 1024)
        system_memory = psutil.virtual_memory().percent
    return SystemHealth(
        cpu_percent=_cpu.sample(),
        memory_mb=memory_mb,
        system_memory_percent=system_memory,
        disk_total_gb=usage.total / 1024**3,
        disk_free_gb=usage.free / 1024**3,
        db_size_mb=_file_size_mb(db_path, Path(f"{db_path}-wal")),
        last_backup_at=last_backup_time(_resolve(settings.backup_dir)),
    )


def camera_health(db: Session) -> list[CameraHealth]:
    from app.pipeline.worker import pipeline_manager
    from app.surveillance.worker import surveillance_manager

    return [
        CameraHealth(
            camera.id,
            camera.name,
            camera.enabled,
            manager.get_state(camera.id),
            pipeline_manager.stats(camera.id),
            surveillance_manager.stats(camera.id),
        )
        for camera in db.scalars(select(Camera).order_by(Camera.id))
    ]


def problems(cameras: list[CameraHealth], system: SystemHealth) -> list[tuple[str, dict]]:
    """Translation keys (with format values) for anything an operator must act on."""
    found: list[tuple[str, dict]] = []
    for camera in cameras:
        offline = camera.offline_seconds
        if offline is None and camera.enabled and camera.state.status != "online" and camera.state.last_frame_at is None:
            offline = CAMERA_OFFLINE_WARN_SECONDS  # never delivered a frame since start
        if offline is not None and offline >= CAMERA_OFFLINE_WARN_SECONDS:
            found.append(("health_camera_offline", {"name": camera.name, "minutes": int(offline // 60)}))
    if system.disk_free_ratio < DISK_FREE_WARN_RATIO:
        found.append(("health_disk_low", {"free": f"{system.disk_free_gb:.1f}"}))
    age = system.backup_age_days
    if age is None or age > BACKUP_WARN_DAYS:
        found.append(("health_backup_old", {"days": "—" if age is None else f"{age:.0f}"}))
    return found
