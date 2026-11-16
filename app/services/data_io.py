"""CSV export of records and CSV import of people."""

import csv
import io
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog, Person

_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
MAX_IMPORT_ROWS = 5000


def csv_cell(value: object) -> str:
    """Neutralise text Excel would run as a formula (CSV injection)."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_START) else text


def csv_stream(header: list[str], rows: Iterable[Iterable[object]]) -> Iterator[bytes]:
    """UTF-8 with BOM so Excel shows Bangla correctly."""
    yield b"\xef\xbb\xbf"
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(header)
    yield buffer.getvalue().encode("utf-8")
    for row in rows:
        buffer.seek(0)
        buffer.truncate(0)
        writer.writerow([csv_cell(value) for value in row])
        yield buffer.getvalue().encode("utf-8")


@dataclass
class ImportResult:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    errors: list[tuple[int, str]] = field(default_factory=list)  # (line number, translation key)


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("import_encoding")


def import_people(db: Session, raw: bytes, user_id: int | None) -> ImportResult:
    """Columns: code, name, and optionally active (1/0, yes/no). Existing codes are updated.

    Everything is validated first and written in one transaction, so a bad file changes nothing.
    """
    text = _decode(raw)
    reader = csv.DictReader(io.StringIO(text))
    headers = {(name or "").strip().lower() for name in reader.fieldnames or []}
    if not {"code", "name"} <= headers:
        raise ValueError("import_columns")
    result = ImportResult()
    rows: list[tuple[int, str, str, bool | None]] = []
    seen: set[str] = set()
    for line, row in enumerate(reader, start=2):
        if line - 1 > MAX_IMPORT_ROWS:
            raise ValueError("import_too_many")
        clean = {(key or "").strip().lower(): (value or "").strip() for key, value in row.items()}
        code, name = clean.get("code", ""), clean.get("name", "")
        if not code and not name:
            continue
        if not code or not name:
            result.errors.append((line, "person_required"))
            continue
        if len(code) > 100 or len(name) > 200:
            result.errors.append((line, "import_too_long"))
            continue
        if code.casefold() in seen:
            result.errors.append((line, "import_duplicate"))
            continue
        seen.add(code.casefold())
        active_text = clean.get("active", "").lower()
        active = None if not active_text else active_text in ("1", "yes", "true", "y", "হ্যাঁ")
        rows.append((line, code, name, active))
    if result.errors:
        return result

    existing = {person.code: person for person in db.scalars(select(Person))}
    for _line, code, name, active in rows:
        person = existing.get(code)
        if person is None:
            db.add(Person(code=code, name=name, active=True if active is None else active))
            result.created += 1
            continue
        changed = person.name != name or (active is not None and person.active != active)
        if changed:
            person.name = name
            if active is not None:
                person.active = active
            result.updated += 1
        else:
            result.unchanged += 1
    db.add(
        AuditLog(
            user_id=user_id,
            action="people.import",
            detail=f"created={result.created} updated={result.updated} unchanged={result.unchanged}",
        )
    )
    db.commit()
    return result
