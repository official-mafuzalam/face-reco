"""Backup and restore of the database and enrolled face images.

The archive layout is shared with scripts/backup.ps1:
    database/gate.db    consistent SQLite copy (online backup API)
    data/faces/...      enrolled face thumbnails
Clips and snapshots are not included; they are large and expire on their own.
"""

import argparse
from contextlib import closing
import logging
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_MEMBER = "database/gate.db"
FACES_PREFIX = "data/faces/"
REQUIRED_TABLES = {"users", "people", "embeddings", "cameras", "events"}
MAX_UPLOAD_BYTES = 2 * 1024**3


def _resolve(path: Path) -> Path:
    path = path.expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def database_path() -> Path:
    return _resolve(settings.db_path)


def backup_dir() -> Path:
    return _resolve(settings.backup_dir)


def faces_dir() -> Path:
    return PROJECT_ROOT / "data" / "faces"


@dataclass(frozen=True)
class BackupFile:
    name: str
    path: Path
    size_mb: float
    created_at: datetime


def create_backup(destination: Path | None = None, keep: int = 14, label: str = "") -> Path:
    destination = destination or backup_dir()
    destination.mkdir(parents=True, exist_ok=True)
    db_path = database_path()
    if not db_path.is_file():
        raise FileNotFoundError(f"Database file not found: {db_path}")
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    suffix = f"-{label}" if label else ""
    archive = destination / f"gate-face-{stamp}{suffix}.zip"
    with tempfile.TemporaryDirectory(prefix="gate-face-backup-") as temp_dir:
        copy = Path(temp_dir) / "gate.db"
        with closing(sqlite3.connect(str(db_path))) as source, closing(sqlite3.connect(str(copy))) as target:
            source.backup(target)
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
            output.write(copy, DB_MEMBER)
            faces = faces_dir()
            if faces.is_dir():
                for path in faces.rglob("*"):
                    if path.is_file():
                        output.write(path, (PurePosixPath("data/faces") / path.relative_to(faces).as_posix()).as_posix())
    _prune(destination, keep)
    logger.info("Backup written to %s.", archive)
    return archive


def _prune(destination: Path, keep: int) -> None:
    archives = sorted(destination.glob("gate-face-*.zip"), key=lambda item: item.name, reverse=True)
    for old in archives[keep:]:
        old.unlink(missing_ok=True)


def list_backups(destination: Path | None = None) -> list[BackupFile]:
    destination = destination or backup_dir()
    if not destination.is_dir():
        return []
    items = []
    for path in destination.glob("gate-face-*.zip"):
        stat = path.stat()
        items.append(BackupFile(path.name, path, stat.st_size / 1024**2, datetime.fromtimestamp(stat.st_mtime, UTC)))
    return sorted(items, key=lambda item: item.name, reverse=True)


def backup_by_name(name: str, destination: Path | None = None) -> Path | None:
    """Only plain names listed in the backup folder; never a path from the request."""
    return next((item.path for item in list_backups(destination) if item.name == name), None)


class BackupError(ValueError):
    """`code` is a translation key; the message is for logs."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class BackupSummary:
    people: int
    embeddings: int
    cameras: int
    events: int
    users: int
    face_files: int


def _safe_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members = []
    for info in archive.infolist():
        if info.is_dir():
            continue
        name = PurePosixPath(info.filename)
        if name.is_absolute() or ".." in name.parts or "\\" in info.filename:
            raise BackupError("backup_invalid", f"Unsafe path in archive: {info.filename}")
        if info.filename != DB_MEMBER and not info.filename.startswith(FACES_PREFIX):
            raise BackupError("backup_invalid", f"Unexpected file in archive: {info.filename}")
        members.append(info)
    if DB_MEMBER not in {info.filename for info in members}:
        raise BackupError("backup_invalid", "Archive has no database/gate.db.")
    return members


def inspect_backup(archive_path: Path, work_dir: Path) -> tuple[Path, BackupSummary]:
    """Extract and check an archive without touching the live data.

    Rejects anything that is not one of our backups, a corrupt database, or a database
    encrypted with a different FERNET_KEY (camera passwords and face data would be unreadable).
    """
    try:
        archive = zipfile.ZipFile(archive_path)
    except zipfile.BadZipFile as exc:
        raise BackupError("backup_invalid", "Not a zip file.") from exc
    with archive:
        members = _safe_members(archive)
        if sum(info.file_size for info in members) > MAX_UPLOAD_BYTES * 4:
            raise BackupError("backup_invalid", "Archive expands too much.")
        archive.extractall(work_dir, members=members)
    db_copy = work_dir / DB_MEMBER
    try:
        with closing(sqlite3.connect(str(db_copy))) as connection:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupError("backup_corrupt")
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not REQUIRED_TABLES <= tables:
                raise BackupError("backup_invalid", f"Missing tables: {REQUIRED_TABLES - tables}")
            count = lambda table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: E731
            sample = connection.execute(
                "SELECT vector_encrypted FROM embeddings LIMIT 1"
            ).fetchone() or connection.execute("SELECT rtsp_url_encrypted FROM cameras LIMIT 1").fetchone()
            summary = BackupSummary(
                count("people"), count("embeddings"), count("cameras"), count("events"), count("users"),
                sum(1 for info in members if info.filename.startswith(FACES_PREFIX)),
            )
    except sqlite3.DatabaseError as exc:
        raise BackupError("backup_corrupt", str(exc)) from exc
    if summary.users == 0:
        raise BackupError("backup_invalid", "Backup has no user accounts.")
    if sample is not None:
        token = sample[0] if isinstance(sample[0], bytes) else str(sample[0]).encode()
        try:
            Fernet(settings.fernet_key.encode()).decrypt(token)
        except InvalidToken as exc:
            raise BackupError("backup_wrong_key") from exc
    return db_copy, summary


def restore_database(db_copy: Path, extracted_root: Path) -> None:
    """Copy a checked backup over the live database and face folder.

    The caller must have stopped the workers. SQLite's backup API overwrites the live
    file page by page under its own locking, so open connections stay valid.
    """
    with closing(sqlite3.connect(str(db_copy))) as source, closing(
        sqlite3.connect(str(database_path()), timeout=30)
    ) as target:
        source.backup(target)
    faces = faces_dir()
    new_faces = extracted_root / "data" / "faces"
    if faces.exists():
        shutil.rmtree(faces)
    if new_faces.is_dir():
        shutil.copytree(new_faces, faces)
    else:
        faces.mkdir(parents=True, exist_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Back up the Gate Face database and face images.")
    parser.add_argument("--destination", default=None)
    parser.add_argument("--keep", type=int, default=14)
    args = parser.parse_args()
    destination = _resolve(Path(args.destination)) if args.destination else None
    print(create_backup(destination, keep=args.keep))


if __name__ == "__main__":
    main()
