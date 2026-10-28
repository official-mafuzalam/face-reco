$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Activate = Join-Path $Root ".venv\Scripts\Activate.ps1"

if (-not (Test-Path $Activate)) {
    throw "The project virtual environment was not found. Create it and install requirements first."
}

Set-Location $Root
. $Activate
$ServerSettings = @(& python -c "from app.config import settings; print(settings.host); print(settings.port)")
if ($LASTEXITCODE -ne 0 -or $ServerSettings.Count -lt 2) {
    throw "Could not read HOST and PORT from .env/application settings."
}

$HostAddress = $ServerSettings[0].Trim()
$Port = $ServerSettings[1].Trim()
& python -m uvicorn app.main:app --host $HostAddress --port $Port
exit $LASTEXITCODE
