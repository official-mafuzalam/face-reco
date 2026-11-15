import argparse
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.db import SessionLocal
from app.models import AuditLog, Event, Incident


logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DAY_SECONDS = 24 * 60 * 60


def run_retention(
    db: Session,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    snapshot_dir: Path | None = None,
    incident_media_dir: Path | None = None,
) -> dict[str, int]:
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    else:
        reference = reference.astimezone(UTC)

    snapshot_cutoff = reference - timedelta(days=settings.snapshot_retention_days)
    event_cutoff = reference - timedelta(days=settings.event_retention_days)
    snapshot_root = (snapshot_dir or _PROJECT_ROOT / "data" / "snapshots").resolve()
    snapshot_candidates: list[Path] = []
    if snapshot_root.is_dir():
        for path in snapshot_root.rglob("*"):
            if not path.is_file():
                continue
            resolved_path = path.resolve()
            if snapshot_root not in resolved_path.parents:
                continue
            modified_at = datetime.fromtimestamp(path.stat().st_mtime, UTC)
            if modified_at < snapshot_cutoff:
                snapshot_candidates.append(path)

    event_candidates = db.scalar(
        select(func.count(Event.id)).where(Event.at < event_cutoff.replace(tzinfo=None))
    ) or 0

    media_cutoff = reference - timedelta(days=settings.incident_media_retention_days)
    media_root = (incident_media_dir or _PROJECT_ROOT / "data" / "incidents").resolve()
    media_candidates: list[Path] = []
    if media_root.is_dir():
        for path in media_root.rglob("*"):
            if path.is_file() and media_root in path.resolve().parents:
                if datetime.fromtimestamp(path.stat().st_mtime, UTC) < media_cutoff:
                    media_candidates.append(path)
    incident_candidates = db.scalar(
        select(func.count(Incident.id)).where(Incident.started_at < event_cutoff.replace(tzinfo=None))
    ) or 0

    if not dry_run:
        for path in snapshot_candidates + media_candidates:
            path.unlink(missing_ok=True)
        db.execute(
            delete(Event)
            .where(Event.at < event_cutoff.replace(tzinfo=None))
            .execution_options(synchronize_session=False)
        )
        db.execute(
            delete(Incident)
            .where(Incident.started_at < event_cutoff.replace(tzinfo=None))
            .execution_options(synchronize_session=False)
        )

    db.add(
        AuditLog(
            user_id=None,
            action="retention.run",
            detail=(
                f"dry_run={str(dry_run).lower()}; "
                f"snapshot_candidates={len(snapshot_candidates)}; "
                f"event_candidates={event_candidates}; "
                f"incident_media_candidates={len(media_candidates)}; "
                f"incident_candidates={incident_candidates}"
            ),
            at=reference,
        )
    )
    db.commit()
    return {
        "snapshots": len(snapshot_candidates),
        "events": int(event_candidates),
        "incident_media": len(media_candidates),
        "incidents": int(incident_candidates),
    }


def _retention_loop(
    stop_event: threading.Event,
    session_factory: Callable[[], Session],
    interval_seconds: float,
) -> None:
    while not stop_event.is_set():
        try:
            with session_factory() as db:
                run_retention(db)
        except Exception:
            logger.exception("Daily retention job failed.")
        if stop_event.wait(interval_seconds):
            return


def start_daily_retention(
    session_factory: Callable[[], Session] = SessionLocal,
) -> tuple[threading.Event, threading.Thread]:
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_retention_loop,
        args=(stop_event, session_factory, _DAY_SECONDS),
        name="gate-face-retention",
        daemon=True,
    )
    thread.start()
    return stop_event, thread


def stop_daily_retention(
    stop_event: threading.Event,
    thread: threading.Thread,
) -> None:
    stop_event.set()
    thread.join()


def main() -> None:
    parser = argparse.ArgumentParser(description="Prune expired gate-face snapshots and events.")
    parser.add_argument("--dry-run", action="store_true", help="Report candidates without deleting them.")
    args = parser.parse_args()
    with SessionLocal() as db:
        counts = run_retention(db, dry_run=args.dry_run)
    print(
        f"Retention {'dry-run' if args.dry_run else 'complete'}: "
        f"{counts['snapshots']} snapshot(s), {counts['events']} event(s)."
    )


if __name__ == "__main__":
    main()
