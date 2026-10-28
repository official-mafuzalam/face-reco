param(
    [ValidateRange(1, 3650)]
    [int]$Keep = 14,
    [string]$Destination = "backups"
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "The project virtual environment was not found at $Python."
}
Set-Location $Root
if ([System.IO.Path]::IsPathRooted($Destination)) {
    $DestinationPath = [System.IO.Path]::GetFullPath($Destination)
} else {
    $DestinationPath = [System.IO.Path]::GetFullPath((Join-Path $Root $Destination))
}
$BackupCode = @'
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
'@
$BackupCode | & $Python - $DestinationPath $Keep
if ($LASTEXITCODE -ne 0) {
    throw "SQLite backup failed."
}
