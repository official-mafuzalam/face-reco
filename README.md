# gate-face

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

Edit `.env` before use. Set `SECRET_KEY` to a long random value and set
`FERNET_KEY` to a newly generated Fernet key. To generate one, run:

```text
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

The sample Fernet key is for local development only. If it is changed after
camera URLs are saved, those saved URLs can no longer be decrypted.

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
`/health` is unauthenticated and reports only `ok` and camera online/offline
counts.

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

## Tests

```text
pytest
```
