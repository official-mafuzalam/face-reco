from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.camera.reader import ReaderState
from app.health import CameraHealth, SystemHealth, last_backup_time, problems
from app.loop_stats import LoopStats


def _system(free_gb: float = 50.0, backup_days: float | None = 0.5) -> SystemHealth:
    backup = None if backup_days is None else datetime.now(UTC) - timedelta(days=backup_days)
    return SystemHealth(10.0, None, None, 100.0, free_gb, 5.0, backup)


def _camera(status: str = "online", age: float | None = 0.1, enabled: bool = True) -> CameraHealth:
    last = datetime.now(UTC) if age is not None else None
    return CameraHealth(1, "Gate", enabled, ReaderState(status, 12.0, last, None, 1, frame_age_seconds=age), None, None)


def test_healthy_system_has_no_problems() -> None:
    assert problems([_camera()], _system()) == []


def test_problems_flag_offline_camera_low_disk_and_old_backup() -> None:
    found = problems([_camera("offline", age=150.0)], _system(free_gb=5.0, backup_days=3.2))

    assert [key for key, _ in found] == ["health_camera_offline", "health_disk_low", "health_backup_old"]
    assert found[0][1] == {"name": "Gate", "minutes": 2}


def test_short_outage_disabled_camera_and_missing_backup() -> None:
    assert problems([_camera("offline", age=20.0), _camera("offline", age=500.0, enabled=False)], _system()) == []
    assert [key for key, _ in problems([], _system(backup_days=None))] == ["health_backup_old"]


def test_last_backup_time_reads_newest_zip(tmp_path: Path) -> None:
    assert last_backup_time(tmp_path) is None
    (tmp_path / "gate-face-1.zip").write_bytes(b"x")
    (tmp_path / "other.zip").write_bytes(b"x")
    assert last_backup_time(tmp_path) is not None


def test_loop_stats_reports_recent_rate_and_cost() -> None:
    stats = LoopStats(window_seconds=10.0)
    for second in range(20):
        stats.record(0.1, now=float(second) / 2)  # 2 frames/s, 100 ms each, for 10 s

    snapshot = stats.snapshot(now=10.0)
    assert snapshot.fps == pytest.approx(2.0, abs=0.15)
    assert snapshot.avg_ms == pytest.approx(100.0)
    assert stats.snapshot(now=100.0).fps == 0.0


def test_more_cameras_than_the_profile_allows_is_a_problem() -> None:
    system = SystemHealth(10.0, None, None, 100.0, 50.0, 5.0, datetime.now(UTC), hardware_profile="cpu_small", profile_max_cameras=1)
    found = problems([_camera(), _camera()], system)
    assert found == [("health_overloaded", {"cameras": 2, "profile": "cpu_small", "limit": 1})]
    assert problems([_camera(), _camera(enabled=False)], system) == []  # a disabled camera costs nothing


def test_frame_rate_comes_from_the_profile_unless_set(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings
    from app.surveillance.detector import effective_surveillance_fps

    monkeypatch.setattr(settings, "surveillance_fps", None)
    monkeypatch.setattr(settings, "hardware_profile", "cuda")
    assert effective_surveillance_fps() == 5.0
    monkeypatch.setattr(settings, "hardware_profile", "no-such-profile")
    assert effective_surveillance_fps() == 2.0  # unknown names fall back to cpu_small
    monkeypatch.setattr(settings, "surveillance_fps", 1.0)
    assert effective_surveillance_fps() == 1.0  # an explicit SURVEILLANCE_FPS always wins
