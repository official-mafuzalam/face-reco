param(
    [string]$ServiceName = "GateFace",
    [string]$NssmPath,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$LogDirectory = Join-Path $Root "data\logs"

if (-not $NssmPath) {
    $BundledNssm = Join-Path $PSScriptRoot "nssm.exe"
    if (Test-Path $BundledNssm) {
        $NssmPath = $BundledNssm
    } else {
        $NssmCommand = Get-Command "nssm.exe" -ErrorAction SilentlyContinue
        if ($NssmCommand) {
            $NssmPath = $NssmCommand.Source
        }
    }
}

if (-not $NssmPath -or -not (Test-Path $NssmPath)) {
    throw "NSSM was not found. Download nssm.exe from https://nssm.cc/download, then pass -NssmPath <path> or put it on PATH. This script does not bundle NSSM."
}
$NssmPath = (Resolve-Path $NssmPath).Path

function Invoke-Nssm {
    param([string[]]$Arguments)
    & $NssmPath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "NSSM command failed: $($Arguments -join ' ')"
    }
}

if ($Uninstall) {
    & $NssmPath stop $ServiceName
    & $NssmPath remove $ServiceName confirm
    if ($LASTEXITCODE -ne 0) {
        throw "Could not remove the service '$ServiceName'."
    }
    Write-Output "Removed service '$ServiceName'."
    exit 0
}

if (-not (Test-Path $Python)) {
    throw "The project virtual environment was not found at $Python."
}
New-Item -ItemType Directory -Force -Path $LogDirectory | Out-Null
$ServerSettings = @(& $Python -c "from app.config import settings; print(settings.host); print(settings.port)")
if ($LASTEXITCODE -ne 0 -or $ServerSettings.Count -lt 2) {
    throw "Could not read HOST and PORT from .env/application settings."
}
$HostAddress = $ServerSettings[0].Trim()
$Port = $ServerSettings[1].Trim()
$Credentials = Get-Credential -Message "Enter a normal local account for the GateFace service (not SYSTEM)."
if (-not $Credentials) {
    throw "A normal local service account is required."
}
$ServiceUser = $Credentials.UserName.Trim()
$LocalAccountPattern = "^(?:\.\\|$([regex]::Escape($env:COMPUTERNAME))\\)[^\\]+$"
if ($ServiceUser -notmatch $LocalAccountPattern -or $ServiceUser -match '(^|\\)SYSTEM$') {
    throw "Enter a local account as .\username or $env:COMPUTERNAME\username. SYSTEM and domain accounts are not allowed."
}
$ServicePassword = $Credentials.GetNetworkCredential().Password

Invoke-Nssm @("install", $ServiceName, $Python)
Invoke-Nssm @("set", $ServiceName, "AppDirectory", $Root)
Invoke-Nssm @("set", $ServiceName, "AppParameters", "-m uvicorn app.main:app --host $HostAddress --port $Port")
Invoke-Nssm @("set", $ServiceName, "ObjectName", $ServiceUser, $ServicePassword)
Invoke-Nssm @("set", $ServiceName, "Start", "SERVICE_AUTO_START")
Invoke-Nssm @("set", $ServiceName, "AppExit", "Default", "Restart")
Invoke-Nssm @("set", $ServiceName, "AppRestartDelay", "5000")
Invoke-Nssm @("set", $ServiceName, "AppStdout", (Join-Path $LogDirectory "service-out.log"))
Invoke-Nssm @("set", $ServiceName, "AppStderr", (Join-Path $LogDirectory "service-error.log"))
Invoke-Nssm @("set", $ServiceName, "AppRotateFiles", "1")
Invoke-Nssm @("set", $ServiceName, "AppRotateOnline", "1")
Invoke-Nssm @("set", $ServiceName, "AppRotateBytes", "10485760")
Invoke-Nssm @("start", $ServiceName)
Write-Output "Installed and started '$ServiceName'. Logs rotate at 10 MiB under $LogDirectory."
Write-Output "Uninstall with: .\scripts\install_service_windows.ps1 -Uninstall -ServiceName $ServiceName"
