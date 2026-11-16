import logging
import sqlite3
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db import Base
from app.jobs.monitor import outages, record_sample, uptime_percent
from app.logs import MaskingFilter, RingBufferHandler, area_of
from app.models import AuditLog, HealthSample, Person
from app.services import backup as backup_service
from app.services.data_io import csv_cell, import_people
from test_camera import web_client  # noqa: F401  (shared fixture)


def _session() -> tuple[sessionmaker[Session], object]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False), engine


def _login_admin(client: TestClient) -> None:
    client.post("/login", data={"username": "admin-camera", "password": "test-password-123"})


# ------------------------------------------------------------------ logs


def test_log_buffer_filters_and_masks_camera_passwords() -> None:
    handler = RingBufferHandler(capacity=10)
    handler.addFilter(MaskingFilter())
    logger = logging.getLogger("app.camera.reader.test")
    logger.propagate = False
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.info("Opening %s", "rtsp://admin:SECRET@10.0.0.5/live")
        logger.warning("Camera 1 reconnected after 31.0 s")
        logging.getLogger("app.web.x").warning("ignored: different logger")
    finally:
        logger.removeHandler(handler)

    records = handler.records()
    assert [record.level for record in records] == ["INFO", "WARNING"]
    assert "SECRET" not in records[0].message
    assert records[0].area == "camera"
    assert [r.message for r in handler.records(min_level=logging.WARNING)] == ["Camera 1 reconnected after 31.0 s"]
    assert handler.records(search="RECONNECTED")[0].seq == 2
    assert handler.records(after=2) == []
    assert area_of("app.pipeline.worker") == "face" and area_of("something.else") == "app"


# ------------------------------------------------------------------ monitor


def _sample(minute: int, status: str, camera_id: int | None = 1) -> HealthSample:
    return HealthSample(at=datetime(2026, 11, 16, 10, 0, tzinfo=UTC) + timedelta(minutes=minute), camera_id=camera_id, status=status)


def test_outages_and_uptime_from_samples() -> None:
    statuses = ["online", "offline", "offline", "online", "online", "offline"]
    samples = [_sample(minute, status) for minute, status in enumerate(statuses)]
    samples.append(_sample(0, "system", camera_id=None))

    found = outages(samples)

    assert [(o.started_at.minute, o.ended_at.minute if o.ended_at else None, o.minutes) for o in found] == [
        (5, None, 1),
        (1, 3, 2),
    ]
    assert uptime_percent(samples) == pytest.approx(50.0)
    assert uptime_percent([]) is None


def test_gap_in_samples_ends_an_outage() -> None:
    # PC switched off between minute 2 and minute 60: not one 58-minute camera outage.
    samples = [_sample(1, "offline"), _sample(2, "offline"), _sample(60, "offline"), _sample(61, "online")]
    assert [(o.started_at.minute, o.minutes) for o in outages(samples)] == [(0, 1), (1, 1)]


def test_record_sample_writes_rows_and_prunes_old_ones() -> None:
    from app.camera.reader import ReaderState
    from app.health import CameraHealth, SystemHealth

    factory, engine = _session()
    now = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
    with factory() as db:
        db.add(HealthSample(at=now - timedelta(days=20), camera_id=None, status="system"))
        db.commit()
        cameras = [
            CameraHealth(1, "Gate", True, ReaderState("online", 12.0, now, None, 1), None, None),
            CameraHealth(2, "Store", False, ReaderState("offline", 0.0, None, None, 0), None, None),
        ]
        record_sample(db, cameras, SystemHealth(12.0, None, None, 100.0, 40.0, 1.0, None), now=now)
        rows = db.query(HealthSample).order_by(HealthSample.id).all()
    assert [(row.camera_id, row.status) for row in rows] == [(1, "online"), (2, "disabled"), (None, "system")]
    assert rows[2].cpu_percent == 12.0
    engine.dispose()


# ------------------------------------------------------------------ backup


@pytest.fixture
def backup_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway live database and face folder, so tests never touch data/gate.db."""
    live = tmp_path / "live.db"
    with sqlite3.connect(live) as connection:
        connection.executescript(
            "CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT);"
            "CREATE TABLE people(id INTEGER PRIMARY KEY, code TEXT, name TEXT);"
            "CREATE TABLE embeddings(id INTEGER PRIMARY KEY, vector_encrypted BLOB);"
            "CREATE TABLE cameras(id INTEGER PRIMARY KEY, rtsp_url_encrypted TEXT);"
            "CREATE TABLE events(id INTEGER PRIMARY KEY);"
            "INSERT INTO users(username) VALUES ('admin');"
            "INSERT INTO people(code, name) VALUES ('E1', 'Mafuz');"
        )
        token = Fernet(settings.fernet_key.encode()).encrypt(b"vector")
        connection.execute("INSERT INTO embeddings(vector_encrypted) VALUES (?)", (token,))
    faces = tmp_path / "faces"
    (faces / "1").mkdir(parents=True)
    (faces / "1" / "1.jpg").write_bytes(b"jpg")
    monkeypatch.setattr(backup_service, "database_path", lambda: live)
    monkeypatch.setattr(backup_service, "faces_dir", lambda: faces)
    monkeypatch.setattr(backup_service, "backup_dir", lambda: tmp_path / "backups")
    return tmp_path


def test_backup_round_trip_restores_database_and_faces(backup_env: Path) -> None:
    archive = backup_service.create_backup()
    assert archive.parent == backup_env / "backups"
    with zipfile.ZipFile(archive) as content:
        assert set(content.namelist()) == {"database/gate.db", "data/faces/1/1.jpg"}

    with sqlite3.connect(backup_env / "live.db") as connection:  # data changes after the backup
        connection.execute("UPDATE people SET name = 'Changed'")
    (backup_env / "faces" / "1" / "1.jpg").unlink()
    (backup_env / "faces" / "2.jpg").write_bytes(b"new")

    work = backup_env / "work"
    work.mkdir()
    db_copy, summary = backup_service.inspect_backup(archive, work)
    assert (summary.people, summary.embeddings, summary.users, summary.face_files) == (1, 1, 1, 1)
    backup_service.restore_database(db_copy, work)

    with sqlite3.connect(backup_env / "live.db") as connection:
        assert connection.execute("SELECT name FROM people").fetchone()[0] == "Mafuz"
    assert sorted(p.name for p in (backup_env / "faces").rglob("*") if p.is_file()) == ["1.jpg"]
    assert backup_service.backup_by_name(archive.name) == archive
    assert backup_service.backup_by_name("../live.db") is None


def test_backup_keeps_only_the_newest(backup_env: Path) -> None:
    for _ in range(3):
        backup_service.create_backup(keep=2)
    assert len(backup_service.list_backups()) == 2


def _zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as output:
        for name, data in members.items():
            output.writestr(name, data)
    return path


def test_inspect_rejects_foreign_unsafe_and_wrong_key_archives(backup_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work = backup_env / "w"
    work.mkdir()
    db_bytes = (backup_env / "live.db").read_bytes()

    def code(archive: Path) -> str:
        target = work / archive.stem
        target.mkdir()
        with pytest.raises(backup_service.BackupError) as error:
            backup_service.inspect_backup(archive, target)
        return error.value.code

    assert code(_zip(backup_env / "a.zip", {"other.txt": b"x"})) == "backup_invalid"
    assert code(_zip(backup_env / "b.zip", {"database/gate.db": db_bytes, "../evil.txt": b"x"})) == "backup_invalid"
    assert code(_zip(backup_env / "c.zip", {"database/gate.db": b"not sqlite at all" * 100})) == "backup_corrupt"
    (backup_env / "d.zip").write_bytes(b"not a zip")
    assert code(backup_env / "d.zip") == "backup_invalid"

    monkeypatch.setattr(settings, "fernet_key", Fernet.generate_key().decode())
    assert code(_zip(backup_env / "e.zip", {"database/gate.db": db_bytes})) == "backup_wrong_key"


# ------------------------------------------------------------------ data import/export


def test_csv_cells_cannot_become_excel_formulas() -> None:
    assert csv_cell("=HYPERLINK(\"x\")") == "'=HYPERLINK(\"x\")"
    assert csv_cell("@SUM(1)") == "'@SUM(1)"
    assert csv_cell("Mafuz") == "Mafuz"
    assert csv_cell(None) == ""


def test_people_import_adds_updates_and_is_all_or_nothing() -> None:
    factory, engine = _session()
    with factory() as db:
        db.add(Person(code="E1", name="Old name"))
        db.commit()

        result = import_people(db, "code,name,active\nE1,Mafuz,1\nE2,রহিম,\nE3,Karim,0\n".encode("utf-8-sig"), None)
        assert (result.created, result.updated, result.unchanged, result.errors) == (2, 1, 0, [])
        people = {p.code: (p.name, p.active) for p in db.query(Person)}
        assert people == {"E1": ("Mafuz", True), "E2": ("রহিম", True), "E3": ("Karim", False)}
        assert db.query(AuditLog).filter_by(action="people.import").count() == 1

        bad = import_people(db, b"code,name\nE4,New\nE5,\nE4,Again\n", None)
        assert bad.errors == [(3, "person_required"), (4, "import_duplicate")]
        assert db.query(Person).filter_by(code="E4").count() == 0

        with pytest.raises(ValueError, match="import_columns"):
            import_people(db, b"id,fullname\n1,x\n", None)
    engine.dispose()


def test_system_tool_pages_are_admin_only_and_work(web_client) -> None:  # noqa: F811
    client, test_session = web_client
    for path in ("/system/monitor", "/system/logs", "/system/audit", "/system/backup", "/system/data"):
        assert client.get(path).status_code == 401
    client.post("/login", data={"username": "security-camera", "password": "test-password-123"})
    assert client.get("/system/logs").status_code == 403
    client.post("/logout")
    _login_admin(client)

    for path in ("/system/monitor?period=7d", "/system/logs?level=WARNING&area=camera&q=x",
                 "/system/logs/fragment", "/system/audit?action=person", "/system/backup", "/system/data"):
        response = client.get(path + ("&" if "?" in path else "?") + "lang=bn")
        assert response.status_code == 200, path

    imported = client.post(
        "/system/data/people-import",
        files={"file": ("people.csv", "code,name\n=EVIL,=cmd|' /C calc'!A0\n".encode(), "text/csv")},
    )
    assert imported.status_code == 200
    exported = client.get("/system/data/people.csv")
    assert exported.status_code == 200
    assert "'=EVIL" in exported.content.decode("utf-8-sig")
    audit = client.get("/system/audit/export.csv")
    assert "people.import" in audit.content.decode("utf-8-sig")

    refused = client.post(
        "/system/backup/restore",
        files={"archive": ("x.zip", b"zip", "application/zip")},
        data={"confirm": "no"},
    )
    assert refused.status_code == 200
    assert "RESTORE" in refused.text
    assert client.get("/system/backup/file/..%2F..%2F.env").status_code == 404
