#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PYTHON="$ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  echo "Project virtual environment not found at $PYTHON" >&2
  exit 1
fi

KEEP="${1:-14}"
DESTINATION="${2:-backups}"
DESTINATION_PATH="$("$PYTHON" -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' "$DESTINATION")"

"$PYTHON" - "$DESTINATION_PATH" "$KEEP" <<'PY'
import sqlite3
import sys
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from app.config import settings

root = Path.cwd()
destination = Path(sys.argv[1]).resolve()
keep = int(sys.argv[2])
if keep < 1:
    raise SystemExit("Keep must be at least one.")
destination.mkdir(parents=True, exist_ok=True)
db_path = settings.db_path.expanduser()
if not db_path.is_absolute():
    db_path = root / db_path
if not db_path.is_file():
    raise SystemExit(f"Database file not found: {db_path}")

stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
archive = destination / f"gate-face-{stamp}.zip"
with tempfile.TemporaryDirectory(prefix="gate-face-backup-") as temp_dir:
    db_backup = Path(temp_dir) / "gate.db"
    with sqlite3.connect(str(db_path)) as source, sqlite3.connect(str(db_backup)) as target:
        source.backup(target)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.write(db_backup, "database/gate.db")
        faces = root / "data" / "faces"
        if faces.is_dir():
            for path in faces.rglob("*"):
                if path.is_file():
                    output.write(path, Path("data/faces") / path.relative_to(faces))

archives = sorted(destination.glob("gate-face-*.zip"), key=lambda item: item.name, reverse=True)
for old_archive in archives[keep:]:
    old_archive.unlink()
print(archive)
PY
