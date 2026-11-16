# Gate Face installer / updater for Windows.
# Run through Install.bat (double-click). Safe to run again: it never replaces .env or data\.
param(
    [switch]$Unattended,  # accept defaults for yes/no questions (no admin account is created)
    [switch]$SkipSystem   # only set up files and database: no firewall, auto-start, backup task or shortcuts
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$Port = 8090

function Step([string]$Text) { Write-Host ""; Write-Host "==> $Text" -ForegroundColor Cyan }
function Ok([string]$Text) { Write-Host "    OK  $Text" -ForegroundColor Green }
function Warn([string]$Text) { Write-Host "    !!  $Text" -ForegroundColor Yellow }
function Ask([string]$Question, [bool]$Default = $true) {
    if ($Unattended) { return $Default }
    $hint = if ($Default) { "[Y/n]" } else { "[y/N]" }
    $answer = Read-Host "    $Question $hint"
    if ([string]::IsNullOrWhiteSpace($answer)) { return $Default }
    return $answer.Trim().ToLower().StartsWith("y")
}
function Test-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal $identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
# Windows PowerShell 5.1 turns a native program's stderr into a terminating error when
# $ErrorActionPreference is Stop, so native programs run with a local "Continue" and are
# judged by their exit code only.
function Invoke-Checked([string]$Exe, [string[]]$Arguments, [string]$What) {
    $ErrorActionPreference = "Continue"
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE)." }
}
function Get-Output([string]$Exe, [string[]]$Arguments) {
    # Probe a program; returns its trimmed stdout, or $null if it failed.
    $ErrorActionPreference = "Continue"
    $text = & $Exe @Arguments 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $text) { return $null }
    return ([string]($text | Select-Object -Last 1)).Trim()
}

Write-Host "Gate Face installer" -ForegroundColor White
Write-Host "Folder: $Root"
$IsAdmin = Test-Admin
if (-not $IsAdmin) {
    Warn "Not running as administrator: firewall, auto-start service and scheduled backup steps will be skipped."
    Warn "Right-click Install.bat > Run as administrator to include them."
}

# ------------------------------------------------------------------ 1. Python
Step "Checking Python 3.11 or 3.12"
function Find-Python {
    $candidates = @()
    foreach ($version in "3.12", "3.11") {
        $py = Get-Command "py.exe" -ErrorAction SilentlyContinue
        if ($py) {
            $path = Get-Output $py.Source @("-$version", "-c", "import sys; print(sys.executable)")
            if ($path) { $candidates += $path }
        }
    }
    $candidates += @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:ProgramFiles\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:ProgramFiles\Python311\python.exe"
    )
    $onPath = Get-Command "python.exe" -ErrorAction SilentlyContinue
    if ($onPath) { $candidates += $onPath.Source }
    foreach ($candidate in $candidates) {
        if (-not $candidate -or -not (Test-Path $candidate)) { continue }
        $version = Get-Output $candidate @("-c", "import sys; print('%d.%d' % sys.version_info[:2])")
        if ($version -in @("3.11", "3.12")) { return $candidate }
    }
    return $null
}
$SystemPython = Find-Python
if (-not $SystemPython) {
    $winget = Get-Command "winget.exe" -ErrorAction SilentlyContinue
    if ($winget -and (Ask "Python 3.12 is not installed. Install it now with winget (needs internet)?")) {
        Invoke-Checked $winget.Source @("install", "-e", "--id", "Python.Python.3.12", "--accept-package-agreements", "--accept-source-agreements") "Python install"
        $SystemPython = Find-Python
    }
    if (-not $SystemPython) {
        throw "Python 3.11 or 3.12 is required. Install it from https://www.python.org/downloads/ (tick 'Add Python to PATH'), then run Install.bat again."
    }
}
Ok "Python: $SystemPython"

# ------------------------------------------------------------------ 2. Virtual environment and packages
Step "Preparing the private Python environment (.venv)"
$venvHealthy = $false
if (Test-Path $VenvPython) {
    $venvHealthy = [bool](Get-Output $VenvPython @("-c", "print('ok')"))
    if (-not $venvHealthy) { Warn "Existing .venv is broken (copied from another PC?); recreating it." }
}
if (-not $venvHealthy) {
    if (Test-Path ".venv") { Remove-Item -Recurse -Force ".venv" }
    Invoke-Checked $SystemPython @("-m", "venv", ".venv") "Creating .venv"
}
Ok ".venv ready"

Step "Installing packages"
$Wheelhouse = Join-Path $Root "wheelhouse"
if (Test-Path $Wheelhouse) {
    Ok "Using bundled packages (offline install)"
    Invoke-Checked $VenvPython @("-m", "pip", "install", "--no-index", "--find-links", $Wheelhouse, "-r", "requirements.txt") "Package install"
} else {
    Invoke-Checked $VenvPython @("-m", "pip", "install", "--disable-pip-version-check", "-r", "requirements.txt") "Package install (needs internet)"
}
Ok "Packages installed"

# ------------------------------------------------------------------ 3. AI models
Step "Checking AI models"
Invoke-Checked $VenvPython @("-m", "tools.download_models") "Model download (needs internet if models are missing)"
Ok "Models ready"

# ------------------------------------------------------------------ 4. Secret keys (.env)
Step "Security keys (.env)"
$EnvFile = Join-Path $Root ".env"
if (Test-Path $EnvFile) {
    Ok ".env already exists - kept unchanged (it holds the keys for saved camera passwords and faces)"
} else {
    Invoke-Checked $VenvPython @("-m", "tools.generate_env") "Creating .env"
    Ok ".env created with new random keys"
    Warn "Copy .env to a USB stick or other safe place. Without it, backups cannot be restored."
}

# ------------------------------------------------------------------ 5. Database and first admin
Step "Database and admin account"
Invoke-Checked $VenvPython @("-c", "from app.db import migrate; migrate()") "Database setup"
$admins = Get-Output $VenvPython @("-c", "from sqlalchemy import func, select; from app.db import SessionLocal; from app.models import User; db = SessionLocal(); print(db.scalar(select(func.count(User.id)).where(User.role == 'admin', User.active.is_(True))))")
if ($null -eq $admins) { throw "Could not read the database." }
if ([int]$admins -gt 0) {
    Ok "An admin account already exists"
} elseif ($Unattended) {
    Warn "No admin account yet. Create one later with: .venv\Scripts\python.exe -m app.createadmin"
} else {
    Write-Host "    Create the first admin account (password at least 12 characters):"
    Invoke-Checked $VenvPython @("-m", "app.createadmin") "Creating admin account"
    Ok "Admin account created"
}

# ------------------------------------------------------------------ 6. Access from other computers
if ($SkipSystem) {
    Step "Done (files and database only; -SkipSystem)"
    exit 0
}
Step "Network access"
$hostLine = (Select-String -Path $EnvFile -Pattern '^HOST=' | Select-Object -First 1).Line
$listensOnNetwork = $hostLine -eq "HOST=0.0.0.0"
if (-not $listensOnNetwork -and (Ask "Open Gate Face from other computers on this office network (guard room, office)?" $false)) {
    # Write without a BOM: Windows PowerShell's "utf8" adds one, which would corrupt the first setting.
    $updated = (Get-Content $EnvFile) -replace '^HOST=.*$', 'HOST=0.0.0.0'
    [IO.File]::WriteAllLines($EnvFile, [string[]]$updated, (New-Object Text.UTF8Encoding $false))
    $listensOnNetwork = $true
    Ok "HOST set to 0.0.0.0 in .env"
}
$LanAddress = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.PrefixOrigin -ne "WellKnown" -and $_.IPAddress -notlike "169.254.*" -and $_.IPAddress -ne "127.0.0.1" } |
    Select-Object -First 1)
if ($listensOnNetwork -and $LanAddress) {
    $parts = $LanAddress.IPAddress.Split(".")
    $subnet = if ($LanAddress.PrefixLength -eq 24) { "$($parts[0]).$($parts[1]).$($parts[2]).0/24" } else { "LocalSubnet" }
    if ($IsAdmin) {
        if (-not (Get-NetFirewallRule -DisplayName "Gate Face" -ErrorAction SilentlyContinue)) {
            New-NetFirewallRule -DisplayName "Gate Face" -Direction Inbound -Protocol TCP -LocalPort $Port -RemoteAddress $subnet -Action Allow | Out-Null
        }
        Ok "Firewall allows port $Port from $subnet only"
    } else {
        Warn "Firewall not changed (not administrator). Other computers may be blocked until you rerun as administrator."
    }
    Ok "Other computers open: http://$($LanAddress.IPAddress):$Port"
    Warn "Never forward this port on the internet router."
}

# ------------------------------------------------------------------ 7. Start automatically
Step "Automatic start"
$Nssm = @((Join-Path $PSScriptRoot "nssm.exe"), (Get-Command "nssm.exe" -ErrorAction SilentlyContinue).Source) |
    Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
$serviceInstalled = [bool](Get-Service "GateFace" -ErrorAction SilentlyContinue)
$logonTask = [bool](Get-ScheduledTask -TaskName "GateFace" -ErrorAction SilentlyContinue)
if ($serviceInstalled) {
    Ok "Windows service GateFace is installed; restarting it to load the new version"
    if ($IsAdmin) { Restart-Service "GateFace" } else { Warn "Restart it from an administrator PowerShell: Restart-Service GateFace" }
} elseif ($logonTask) {
    Ok "Gate Face already starts when this user signs in"
} elseif (Ask "Start Gate Face automatically whenever this PC starts?") {
    if ($Nssm -and $IsAdmin) {
        Write-Host "    Installing as a Windows service. Enter this PC's user account as .\username and its password."
        & (Join-Path $PSScriptRoot "install_service_windows.ps1") -NssmPath $Nssm
        $serviceInstalled = $true
    } else {
        if (-not $Nssm) { Warn "NSSM not found, so using a sign-in task instead of a service. For a true service see docs\bn\01-setup.md." }
        $action = New-ScheduledTaskAction -Execute "powershell.exe" `
            -Argument "-NoProfile -WindowStyle Minimized -ExecutionPolicy Bypass -File `"$Root\scripts\run.ps1`"" -WorkingDirectory $Root
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
        Register-ScheduledTask -TaskName "GateFace" -Action $action -Trigger $trigger -Settings $settings `
            -Description "Starts Gate Face when $env:USERNAME signs in" | Out-Null
        $logonTask = $true
        Ok "Gate Face will start when $env:USERNAME signs in (turn on automatic sign-in for an unattended PC)"
    }
}

# ------------------------------------------------------------------ 8. Daily backup
Step "Daily backup"
if (Get-ScheduledTask -TaskName "GateFace Backup" -ErrorAction SilentlyContinue) {
    Ok "Daily backup task already exists"
} elseif (-not $IsAdmin) {
    Warn "Skipped (needs administrator). Backups can still be made from the System page."
} elseif (Ask "Back up the database every night at 2:00?") {
    $backupAction = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Root\scripts\backup.ps1`"" -WorkingDirectory $Root
    $backupTrigger = New-ScheduledTaskTrigger -Daily -At 2am
    Register-ScheduledTask -TaskName "GateFace Backup" -Action $backupAction -Trigger $backupTrigger -User "SYSTEM" -RunLevel Highest | Out-Null
    Ok "Nightly backup into $Root\backups (copy them to another drive regularly)"
}

# ------------------------------------------------------------------ 9. Shortcuts
Step "Desktop shortcuts"
$Desktop = [Environment]::GetFolderPath("Desktop")
Set-Content -Encoding ascii -Path (Join-Path $Desktop "Gate Face.url") -Value "[InternetShortcut]`r`nURL=http://127.0.0.1:$Port/`r`n"
if (-not $serviceInstalled) {
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut((Join-Path $Desktop "Start Gate Face.lnk"))
    $link.TargetPath = Join-Path $Root "Start Gate Face.bat"
    $link.WorkingDirectory = $Root
    $link.Save()
}
Ok "Desktop: 'Gate Face' (opens the app)$(if (-not $serviceInstalled) { " and 'Start Gate Face'" })"

# ------------------------------------------------------------------ 10. Start now
Step "Done"
if (-not $serviceInstalled -and (Ask "Start Gate Face now and open it in the browser?")) {
    Start-Process -FilePath (Join-Path $Root "Start Gate Face.bat") -WorkingDirectory $Root
} elseif ($serviceInstalled) {
    Start-Process "http://127.0.0.1:$Port/"
}
Write-Host ""
Write-Host "Gate Face is installed. Open http://127.0.0.1:$Port in a browser on this PC." -ForegroundColor Green
Write-Host "Next: add cameras, enrol faces - see docs\bn\01-setup.md, step 8."
