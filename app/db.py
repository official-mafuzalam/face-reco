from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


database_path = settings.db_path.expanduser().resolve()
database_path.parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    f"sqlite:///{database_path.as_posix()}",
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def configure_sqlite(connection, _connection_record) -> None:
    cursor = connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as db:
        yield db


# Columns added after the first release. create_all() never alters existing tables,
# so missing columns are added here with SQLite's ALTER TABLE ... ADD COLUMN.
_ADDED_COLUMNS = {
    "cameras": {
        "face_enabled": "BOOLEAN NOT NULL DEFAULT 1",
        "surveillance_enabled": "BOOLEAN NOT NULL DEFAULT 1",
        "deleted_at": "DATETIME",
        "object_confidence": "FLOAT",
    },
    "users": {
        "deleted_at": "DATETIME",
        "totp_secret_encrypted": "TEXT",
        "token_version": "INTEGER NOT NULL DEFAULT 1",
    },
    "events": {
        "reason": "VARCHAR(200) NOT NULL DEFAULT ''",
        "embedding_encrypted": "BLOB",
    },
    "people": {
        "category": "VARCHAR(20) NOT NULL DEFAULT 'employee'",
        "department": "VARCHAR(100) NOT NULL DEFAULT ''",
        "shift_id": "INTEGER REFERENCES shifts(id) ON DELETE SET NULL",
    },
    "surveillance_rules": {
        "extra_params": "TEXT NOT NULL DEFAULT '{}'",
    },
    "incidents": {
        "ai_verdict": "VARCHAR(20)",
        "ai_confidence": "FLOAT",
        "ai_summary_bn": "TEXT",
        "ai_summary_en": "TEXT",
        "ai_checked_at": "DATETIME",
        "review_verdict": "VARCHAR(20)",
    },
}


def migrate(bind=None) -> None:
    from sqlalchemy import inspect, text

    import app.models  # noqa: F401  (registers every table, so a fresh database gets them all)

    bind = bind or engine
    _drop_old_rule_kind_check(bind)
    Base.metadata.create_all(bind)
    inspector = inspect(bind)
    with bind.begin() as connection:
        for table, columns in _ADDED_COLUMNS.items():
            existing = {column["name"] for column in inspector.get_columns(table)}
            for name, ddl in columns.items():
                if name not in existing:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def _drop_old_rule_kind_check(bind) -> None:
    """The first rules table limited `kind` with a CHECK; SQLite can't drop it, so rebuild the table."""
    from sqlalchemy import text

    with bind.begin() as connection:
        row = connection.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='surveillance_rules'")
        ).first()
        if row is None or "ck_rules_kind" not in (row[0] or ""):
            return
        connection.execute(text("ALTER TABLE surveillance_rules RENAME TO surveillance_rules_old"))
        connection.execute(text("DROP INDEX IF EXISTS ix_surveillance_rules_camera_id"))
    Base.metadata.tables["surveillance_rules"].create(bind)
    with bind.begin() as connection:
        from sqlalchemy import inspect
        inspector = inspect(connection)
        old_cols = {col["name"] for col in inspector.get_columns("surveillance_rules_old")}
        target_cols = []
        select_exprs = []
        for col in Base.metadata.tables["surveillance_rules"].columns:
            if col.name in old_cols:
                target_cols.append(col.name)
                select_exprs.append(col.name)
            elif col.name == "extra_params":
                target_cols.append("extra_params")
                select_exprs.append("'{}'")
        target_str = ", ".join(target_cols)
        select_str = ", ".join(select_exprs)
        connection.execute(
            text(f"INSERT INTO surveillance_rules ({target_str}) SELECT {select_str} FROM surveillance_rules_old")
        )
        connection.execute(text("DROP TABLE surveillance_rules_old"))
