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
# Same code the web UI uses (app/services/backup.py), so both make identical archives.
& $Python -m app.services.backup --destination $DestinationPath --keep $Keep
if ($LASTEXITCODE -ne 0) {
    throw "SQLite backup failed."
}
