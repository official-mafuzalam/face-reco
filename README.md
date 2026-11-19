# gate-face

> **বাংলা ডকুমেন্টেশন:** সেটআপ, ক্যামেরা, দৈনন্দিন ব্যবহার, নিয়ম ও রক্ষণাবেক্ষণ গাইড [docs/bn/](docs/bn/README.md) ফোল্ডারে আছে।

A local gate log with FastAPI, SQLite, camera input, face recognition, and
server-rendered Jinja2 pages.

## Requirements

- Python 3.11
- pip

## Setup

Run these commands from the project directory.

### Windows PowerShell

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

### Linux

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Instead of copying `.env.example` by hand, you can generate `.env` with fresh
random `SECRET_KEY` and `FERNET_KEY` values:

```text
python -m tools.generate_env
```

The app **refuses to start** with the sample keys or a `SECRET_KEY` shorter
than 32 characters. For local development only, set
`ALLOW_INSECURE_DEFAULTS=true` to bypass this check.

Back up `FERNET_KEY` somewhere safe. If it is lost or changed after cameras or
faces are saved, the saved camera URLs and face templates can no longer be
decrypted.

## Production checklist

- Generate `.env` with `python -m tools.generate_env`, and keep
  `ALLOW_INSECURE_DEFAULTS=false`.
- Download the models: `python -m tools.download_models`.
- Serve over HTTPS (for example behind Caddy or nginx) and set
  `COOKIE_SECURE=true`. The reverse proxy must pass the original `Host` header,
  because cross-origin form posts are rejected by comparing `Origin` with `Host`.
- Restrict network access with the host firewall to the trusted LAN. For access
  from outside, use Cloudflare Tunnel (see below), never port forwarding.
- Enable 2FA for every admin account before exposing the app remotely.
- Run as a service (see below) and schedule backups (`scripts/backup.*`).
- Failed logins are limited to 5 per 15 minutes per client address and per
  username. The limit is held in memory and resets when the app restarts.
- Get written consent from people before enrolling their faces.

## Create the first admin

```text
python -m app.createadmin
```

Enter an admin username and a password of at least 12 characters when prompted.

## Run and operations

The default bind is `HOST=127.0.0.1` and `PORT=8090`. Start the app from the
project directory:

### Windows PowerShell

```powershell
.\scripts\run.ps1
```

### Linux

```bash
source .venv/bin/activate
set -a
source .env
set +a
python -m uvicorn app.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8090}"
```

The application loads settings from `.env`; `scripts/run.ps1` reads the
effective `HOST` and `PORT` settings before starting Uvicorn. Open
`http://127.0.0.1:8090/login` (or use the configured host and port).
`/health` is unauthenticated and reports only `ok`, camera online/offline
counts, whether Ollama answers (`ai_verifier`) and whether the watchdog is on.

To allow other devices on the LAN to connect, set `HOST=0.0.0.0` in `.env`.
**Warning:** this exposes the service on every network interface. Configure the
host firewall to allow access only from the trusted LAN, and do not expose this
unauthenticated-by-network service directly to the internet.

### Windows service (NSSM)

1. Download NSSM yourself from [nssm.cc](https://nssm.cc/download); this project
   does not include `nssm.exe`.
2. Run PowerShell as an administrator, then install from the project directory:

   ```powershell
   .\scripts\install_service_windows.ps1 -NssmPath "C:\Tools\nssm\win64\nssm.exe"
   ```

3. Enter a local Windows account as `.\username` or `COMPUTERNAME\username`.
   Do not use `SYSTEM` or a domain account.
   Ensure that account can read the project and `.env`, and write to `data\`
   (including `data\logs`). The service is configured to start automatically,
   restart after failure, and rotate its stdout/stderr logs under `data\logs`.
4. To remove the service:

   ```powershell
   .\scripts\install_service_windows.ps1 -Uninstall -NssmPath "C:\Tools\nssm\win64\nssm.exe"
   ```

### Linux systemd

Edit `scripts/gate-face.service` first: replace `REPLACE_WITH_LOCAL_SERVICE_USER`
and `/opt/gate-face` with the dedicated, non-root service account and project
installation path. Ensure the account can read `.env` and write to `data/`.
Then install and enable it:

```bash
sudo cp scripts/gate-face.service /etc/systemd/system/gate-face.service
sudo systemctl daemon-reload
sudo systemctl enable --now gate-face
sudo systemctl status gate-face
```

### Backups and retention

Backups use Python's SQLite online backup API (safe while the app is running),
include `data/faces/`, create a dated ZIP in `backups/`, and retain the newest
14 archives by default.

```powershell
# Windows: optional arguments are keep-count and destination directory
.\scripts\backup.ps1
.\scripts\backup.ps1 -Keep 30 -Destination "D:\gate-face-backups"
```

```bash
# Linux: optional arguments are keep-count and destination directory
bash scripts/backup.sh
bash scripts/backup.sh 30 /srv/backups/gate-face
```

Daily retention removes snapshots older than `SNAPSHOT_RETENTION_DAYS` (default
90) and events older than `EVENT_RETENTION_DAYS` (default 365). Each run writes
an audit entry. To preview candidates without deleting anything:

```text
python -m app.jobs.retention --dry-run
```

The database is created at `DB_PATH` with SQLite WAL enabled.

## Surveillance (incidents)

Every enabled camera also runs a surveillance pipeline: a motion check, then the
YOLOX-S object detector (OpenCV model zoo, Apache-2.0) on the CPU, then a
tracker. Continuous activity by people or vehicles becomes one **incident** with
a snapshot and a WebM clip (a few seconds of pre-roll, up to `CLIP_MAX_SECONDS`).
Objects that stay still for 10 seconds (parked cars, a seated guard) stop
counting as activity, so they do not keep an incident open.

- **Per-camera use:** each camera can run face recognition (gate log),
  surveillance, or both (camera form). Turn face recognition off for cameras that
  are not at a gate, otherwise they log IN/OUT events.
- **Rules** (Cameras → Rules): draw on the camera picture. Every rule has an
  optional active-hours window (for example 22:00–06:00) and a severity.
  - Restricted area: someone enters the area (after-hours presence = whole
    picture + schedule).
  - Line crossing: someone crosses line A→B, B→A, or either way.
  - Loitering: someone stays in the area longer than N seconds.
  - Left-behind bag: a bag/suitcase stays still with nobody near it.
  - Item moved or taken: the area looks different for N seconds after people
    leave (draw tightly around the shelf or item; lighting changes are ignored).
  - Running / person fell: heuristics from movement speed and body shape.
  Fights are not detected: that needs a pose model and a GPU to be reliable.
- Repeated hits of one rule during one clip are grouped into one incident with a
  count. Warnings and critical incidents appear on the dashboard.
- Clips are H.264 MP4 through `imageio-ffmpeg` (about 0.04 GB per camera per hour
  of activity); without it, larger WebM clips are written instead.
- Download the detector with `python -m tools.download_models`. If the model is
  missing, surveillance is disabled and the rest of the app keeps working.
- Incidents are listed under **Incidents** with filters, a clip player and review.
- Media lives in `data/incidents/` and is deleted after
  `INCIDENT_MEDIA_RETENTION_DAYS` (default 30). Incident rows follow
  `EVENT_RETENTION_DAYS`.
- Sizing on an i5-10400 (no GPU): about 100 ms per detection, so 1–4 cameras at
  `SURVEILLANCE_FPS=2`.
- More rule kinds: after hours, cash drawer, tailgating (two people cross a line
  within 3 s), count limit (more than 2 people), and camera tamper (covered,
  blinded or blurred; add it per camera). Site templates create a preset set of
  rules; redraw their zones afterwards (rules can be edited). The authorized-zone
  rule takes the identity from face recognition on the same camera plus an
  "allowed people" list; its template copy starts disabled. Tailgating keeps its
  line direction and the count limit has its own maximum/minimum fields.
  Details and limits: `docs/bn/04-rules.md`.

## Remote access, 2FA and mobile push

- **Cloudflare Tunnel** instead of port forwarding. Set `TRUST_CLOUDFLARE=true`
  (the login limiter then uses `CF-Connecting-IP`, trusted only from loopback)
  and `COOKIE_SECURE=true` (required; the app refuses to start otherwise).
  `scripts/install.ps1` can install the `cloudflared` service from a tunnel token.
  Remote live view is reduced to 640 px / 4 fps automatically.
- **TOTP 2FA** for every account (admin and security): click your name in the
  sidebar → My account → Set up 2FA (QR code). An admin can disable it if a phone
  is lost. "Log out from all devices" revokes every session of a user. A session
  ends after `SESSION_IDLE_TIMEOUT_MINUTES` (60) without activity; an open page
  renews it every 2 minutes.
- **PWA + Web Push** (HTTPS only): install the app from the browser, then press
  "Enable push alerts". Needs `VAPID_PUBLIC_KEY` / `VAPID_PRIVATE_KEY`
  (`tools.generate_env` creates them for a new `.env`). Critical alerts are pushed
  at any time; warnings are not pushed in the quiet hours (default 22:00-06:00,
  changeable per user on the My account page, which also has a test push).
  With AI verification on, a warning is pushed once, after the AI verdict (or as
  "not checked" if the AI is unreachable); a false alarm is not pushed.

Bengali guide: `docs/bn/08-remote-access.md`.

## AI verification, hardware profile, watchdog

- **AI verification** (optional): when an incident ends, 2–3 frames go to a local
  VLM through Ollama (`OLLAMA_URL`, `OLLAMA_MODEL=qwen2.5vl:3b`). The verdict
  (confirmed / false alarm / inconclusive) is shown on the incident; nothing
  leaves the PC and alerts are never held back. Install Ollama, run
  `ollama pull qwen2.5vl:3b`, and on CPU-only PCs set
  `AI_VERIFY_TIMEOUT_SECONDS=60`. Set `AI_VERIFY_ENABLED=false` without Ollama.
- **Review feedback**: confirmed / false alarm / missed buttons on each incident;
  each verdict is also written to `data/labels/incident_<id>.json`.
- **`HARDWARE_PROFILE`** (`cpu_small`, `cpu_large`, `openvino`, `cuda`) selects the
  onnxruntime execution provider and the analysis frame rate (2/3/4/5 fps unless
  `SURVEILLANCE_FPS` is set); `openvino` and `cuda` need the matching onnxruntime
  package installed by hand. The System page warns when more cameras run than the
  profile suits. `scripts/install.ps1` asks how many cameras you have, recommends a
  profile (`python -m tools.choose_profile`) and can install Ollama.
- **Tuning**: the System page lists, per camera, the guards' review results and
  suggests a detection confidence; it is applied to that camera only when an admin
  presses Apply.
- **Daily summary**: shown on the dashboard and pushed at `DAILY_SUMMARY_TIME`
  (default 20:00, empty = off).
- **Watchdog** (every `WATCHDOG_INTERVAL_SECONDS`, default 15): restarts dead
  camera readers (with a critical alert), face and surveillance workers, and the
  AI verifier.
- **Disk guard**: below `MIN_DISK_FREE_GB` (5) or `MIN_DISK_FREE_PERCENT` (10) it
  deletes the oldest files in `data/incidents/` and `data/snapshots/` and raises a
  critical alert.
- **Release gate**: `python -m tools.benchmark --gate` fails when recall or
  precision drops by more than 0.05 or false alarms rise by more than 5.

Implementation status of every roadmap item: `docs/bn/ROADMAP.md`.

## Tests

```text
pytest
```

Accuracy and performance benchmark (tests, detector speed, detection counts against
hand-labelled frames, full pipeline, face accuracy when `data/eval` exists). Each run is
appended to `docs/benchmarks/history.csv` so changes can be compared:

```text
python -m tools.benchmark --note "what changed"
```

## Easy install (Windows)

Build a release on the development PC with `.\scripts\build_release.ps1` (add `-Offline` to bundle the Python packages).
On the target PC unzip it (e.g. to `C:\GateFace`) and right-click `Install.bat` > Run as administrator.
The installer is safe to rerun for updates: it never replaces `.env` or `data\`. Bengali guide: `docs/bn/01-setup.md`.
