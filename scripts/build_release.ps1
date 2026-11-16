# Builds a ZIP that can be copied to another PC and installed by double-clicking Install.bat.
#
#   .\scripts\build_release.ps1              -> dist\GateFace-<date>-<commit>.zip (code + AI models)
#   .\scripts\build_release.ps1 -Offline     -> also bundles Python packages, so the target PC
#                                               needs no internet (this PC needs it once to download them)
#
# Only files committed to git are included, so .env, data\, backups\ and .venv\ can never leak.
param(
    [switch]$Offline,
    [switch]$AllowUncommitted
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

$changes = git status --porcelain
if ($changes -and -not $AllowUncommitted) {
    throw "There are uncommitted changes; the ZIP is built from the last commit. Commit first, or pass -AllowUncommitted to build the last commit anyway."
}
$commit = (git rev-parse --short HEAD).Trim()
$name = "GateFace-$(Get-Date -Format yyyyMMdd)-$commit"
$dist = Join-Path $Root "dist"
$stage = Join-Path $dist $name
if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
New-Item -ItemType Directory -Force -Path $stage | Out-Null

Write-Host "==> Exporting commit $commit"
$archive = Join-Path $dist "$name-src.zip"
git archive --format=zip -o $archive HEAD
if ($LASTEXITCODE -ne 0) { throw "git archive failed." }
Expand-Archive -Path $archive -DestinationPath $stage -Force
Remove-Item $archive

Write-Host "==> Adding AI models"
$models = Get-ChildItem (Join-Path $Root "models") -Filter *.onnx -ErrorAction SilentlyContinue
if ($models) {
    New-Item -ItemType Directory -Force -Path (Join-Path $stage "models") | Out-Null
    $models | Copy-Item -Destination (Join-Path $stage "models")
    Write-Host "    $($models.Count) model(s), $([math]::Round(($models | Measure-Object Length -Sum).Sum / 1MB)) MB"
} else {
    Write-Host "    No local models; Install.bat will download them (needs internet)." -ForegroundColor Yellow
}

if ($Offline) {
    Write-Host "==> Downloading Python packages for Windows / Python 3.12 (one-time internet use)"
    $python = Join-Path $Root ".venv\Scripts\python.exe"
    & $python -m pip download -r requirements.txt -d (Join-Path $stage "wheelhouse") `
        --only-binary=:all: --platform win_amd64 --python-version 3.12 --implementation cp
    if ($LASTEXITCODE -ne 0) { throw "Package download failed." }
}

# Safety net: never ship secrets or personal data, even if someone committed them by mistake.
$forbidden = Get-ChildItem $stage -Recurse -Force | Where-Object {
    $_.Name -eq ".env" -or $_.FullName -match '\\(data|backups|\.venv)\\' -or $_.Extension -in ".db", ".sqlite"
}
if ($forbidden) {
    throw "Refusing to build: secret or personal files found in the release: $($forbidden.FullName -join ', ')"
}

Write-Host "==> Compressing"
$zip = Join-Path $dist "$name.zip"
if (Test-Path $zip) { Remove-Item $zip }
# Windows PowerShell's Compress-Archive and .NET Framework's ZipFile both write "\" in entry
# names, which some unzip tools turn into odd file names instead of folders. Python's zipfile
# always writes standard "/" paths.
$zipCode = @'
import sys, zipfile
from pathlib import Path
stage, target = Path(sys.argv[1]), sys.argv[2]
with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
    for path in sorted(stage.rglob("*")):
        if path.is_file():
            archive.write(path, path.relative_to(stage).as_posix())
'@
$zipCode | & (Join-Path $Root ".venv\Scripts\python.exe") - $stage $zip
if ($LASTEXITCODE -ne 0) { throw "Creating the ZIP failed." }
Remove-Item -Recurse -Force $stage
Write-Host ""
Write-Host "Built $zip ($([math]::Round((Get-Item $zip).Length / 1MB)) MB)" -ForegroundColor Green
Write-Host "On the target PC: unzip to a folder such as C:\GateFace, then double-click Install.bat."
