import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.camera.probe import inspect_frame_quality, probe_source
from app.db import Base
from app.main import app
from app.models import Camera, Incident, User
from app.services.feedback import get_feedback_summary, record_feedback
from tools.benchmark import check_release_gate


@pytest.fixture
def test_db(tmp_path: Path) -> sessionmaker[Session]:
    db_file = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_file.as_posix()}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def test_record_feedback_updates_incident_and_exports_label(test_db: sessionmaker[Session], tmp_path: Path) -> None:
    labels_dir = tmp_path / "labels"
    with test_db() as db:
        user = User(username="admin", password_hash="hash", role="admin")
        cam = Camera(name="Gate Cam", gate="Main", direction="IN", rtsp_url_encrypted="enc")
        db.add_all([user, cam])
        db.commit()

        inc = Incident(
            camera_id=cam.id,
            kind="intrusion",
            severity="warning",
            started_at=datetime.now(UTC),
            ended_at=datetime.now(UTC),
            snapshot_path="data/incidents/snap.jpg",
        )
        db.add(inc)
        db.commit()

        export_file = record_feedback(
            db, inc, reviewer_id=user.id, verdict="false_alarm", note="Tree shadow movement", labels_dir=labels_dir
        )

        assert inc.review_verdict == "false_alarm"
        assert inc.reviewed_by == user.id
        assert inc.review_note == "Tree shadow movement"
        assert inc.reviewed_at is not None

        assert export_file.is_file()
        exported_data = json.loads(export_file.read_text(encoding="utf-8"))
        assert exported_data["incident_id"] == inc.id
        assert exported_data["verdict"] == "false_alarm"
        assert exported_data["note"] == "Tree shadow movement"


def test_record_feedback_rejects_invalid_verdict(test_db: sessionmaker[Session]) -> None:
    with test_db() as db:
        inc = Incident(camera_id=1, kind="intrusion", severity="warning", started_at=datetime.now(UTC))
        db.add(inc)
        db.commit()
        with pytest.raises(ValueError):
            record_feedback(db, inc, reviewer_id=1, verdict="invalid_choice")


def test_get_feedback_summary_and_autotune(test_db: sessionmaker[Session]) -> None:
    with test_db() as db:
        # Camera 1: 4 incidents, 3 false alarms -> false alarm rate 75%
        for _ in range(3):
            inc = Incident(
                camera_id=1, kind="running", severity="warning", started_at=datetime.now(UTC),
                review_verdict="false_alarm", reviewed_by=1, reviewed_at=datetime.now(UTC),
            )
            db.add(inc)
        inc_conf = Incident(
            camera_id=1, kind="running", severity="warning", started_at=datetime.now(UTC),
            review_verdict="confirmed", reviewed_by=1, reviewed_at=datetime.now(UTC),
        )
        db.add(inc_conf)

        # Camera 2: 2 missed incidents
        for _ in range(2):
            inc_m = Incident(
                camera_id=2, kind="tailgating", severity="critical", started_at=datetime.now(UTC),
                review_verdict="missed", reviewed_by=1, reviewed_at=datetime.now(UTC),
            )
            db.add(inc_m)
        db.commit()

        summary = get_feedback_summary(db)
        assert summary["total_reviewed"] == 6
        assert summary["by_verdict"]["false_alarm"] == 3
        assert summary["by_verdict"]["confirmed"] == 1
        assert summary["by_verdict"]["missed"] == 2

        # Verify recommendations
        recs = summary["recommendations"]
        assert len(recs) >= 2
        cam1_rec = next(r for r in recs if r["camera_id"] == 1)
        assert cam1_rec["false_alarm_rate"] == 0.75
        assert cam1_rec["suggested_confidence"] > cam1_rec["current_confidence"]

        cam2_rec = next(r for r in recs if r["camera_id"] == 2)
        assert cam2_rec["suggested_confidence"] < cam2_rec["current_confidence"]


def test_inspect_frame_quality_advisories() -> None:
    # 1. Low resolution frame (e.g. 640x480)
    low_res_frame = np.ones((480, 640, 3), dtype=np.uint8) * 128
    advisories = inspect_frame_quality(low_res_frame)
    assert "cam_advisory_low_res" in advisories

    # 2. Dark frame
    dark_frame = np.ones((720, 1280, 3), dtype=np.uint8) * 20
    advisories = inspect_frame_quality(dark_frame)
    assert "cam_advisory_low_light" in advisories

    # 3. Blindingly bright / glare frame
    bright_frame = np.ones((720, 1280, 3), dtype=np.uint8) * 240
    advisories = inspect_frame_quality(bright_frame)
    assert "cam_advisory_glare" in advisories

    # 4. Perfectly clear HD frame
    # Create textured noise with high variance to avoid blur/contrast flags
    rng = np.random.default_rng(42)
    textured = rng.integers(50, 200, (720, 1280, 3), dtype=np.uint8)
    advisories = inspect_frame_quality(textured)
    assert "cam_advisory_low_res" not in advisories
    assert "cam_advisory_low_light" not in advisories
    assert "cam_advisory_glare" not in advisories


def test_check_release_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    history_file = tmp_path / "history.csv"
    history_file.write_text(
        "date,commit,note,tests_passed,tests_failed,detector_ms,person_count_recall,person_count_precision,person_count_mae,vehicle_count_recall,peak_people,max_people_truth,incidents,false_alarms,clip_gb_per_hour,max_cameras_at_2fps,face_top1,face_far,face_frr\n"
        "2026-11-19,abc1234,base,180,0,12.0,0.92,0.95,0.2,0.90,5,5,10,2,0.5,10.0,0.98,0.001,0.02\n",
        encoding="utf-8",
    )
    from tools import benchmark
    monkeypatch.setattr(benchmark, "HISTORY", history_file)

    # 1. Passing metrics
    good_current = {
        "tests_failed": 0,
        "person_count_recall": 0.93,
        "person_count_precision": 0.94,
        "false_alarms": 2,
    }
    assert check_release_gate(good_current) == []

    # 2. Degraded recall
    bad_current = {
        "tests_failed": 0,
        "person_count_recall": 0.80,  # 0.92 -> 0.80 (> 0.05 drop)
        "person_count_precision": 0.94,
        "false_alarms": 2,
    }
    failures = check_release_gate(bad_current)
    assert len(failures) == 1
    assert "Person recall degraded" in failures[0]
