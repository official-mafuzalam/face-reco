# Gate Face — System Architecture

This document describes the high-level architecture, module boundaries, concurrency model, and data flow for **Gate Face & Smart AI Surveillance**.

---

## 1. High-Level Architecture

The system operates as a single-process, local-first application running on a local host (Windows Service or Linux daemon). Video frames stay strictly within the local network; no frames, snapshots, or face templates are sent to third-party cloud services. Remote access is achieved securely via an outbound-only Cloudflare Tunnel.

```text
 IP Cameras (RTSP main + sub stream)
        │
 ┌──────▼──────────────── Local Host (Windows Service / Linux Daemon) ────────┐
 │ CameraReader ──► Motion gate ──► Detector backend ──► Tracker ──► Rules   │
 │                    (Profiles: cpu / openvino / cuda)             │        │
 │ Face Pipeline (YuNet + SFace) ──► Track Identity Association ────┤        │
 │ Tamper Detection (blur / occlusion / camera shift) ──────────────┤        │
 │                                                                  ▼        │
 │                                    Incident + Snapshot + Clip (SQLite)    │
 │                                                                  │        │
 │                     AI Verify Agent (Local VLM via Ollama) ◄─────┘        │
 │                     Verdict: confirmed / false_alarm + summary (bn/en)    │
 │                                                                  │        │
 │              NotificationBus ──► SSE (Browser) + Web Push (PWA)           │
 │ FastAPI + Jinja/htmx UI · 2FA · Audit · Backup · Retention · Watchdog     │
 └──────────────────────────────┬────────────────────────────────────────────┘
                                │ cloudflared (Outbound tunnel, zero open ports)
                         Cloudflare Tunnel + Cloudflare Access (Email OTP)
                                │
                  Phone (PWA) / Remote Browser (Anywhere)
```

---

## 2. Core Principles

1. **Local-First & Privacy-First**:
   - Camera feeds, face templates, snapshots, and incident clips are processed and stored locally.
   - AI verification runs locally (via Ollama VLM) on CPU or local GPU; no telemetry or media leaves the premises.
2. **Single Process, Single SQLite Database**:
   - SQLite in WAL (Write-Ahead Logging) mode handles all transactional metadata, logs, attendance, and incidents.
   - Eliminates distributed database complexity and runs reliably on modest hardware.
3. **Hardware-Adaptive Inference**:
   - Abstracted detector backend (`ObjectDetector` interface).
   - CPU-only execution with OpenCV DNN / ONNX Runtime for budget setups (3–4 cameras).
   - Hardware acceleration with Intel OpenVINO or NVIDIA CUDA via ONNX Runtime for larger deployments (10+ cameras).
4. **Asynchronous Verification**:
   - Real-time rule triggers are never blocked by heavy AI verification.
   - Incidents are logged and critical alerts dispatched immediately; local VLM verifies context asynchronously.
5. **Secure Remote Access**:
   - No inbound firewall ports or port forwarding. Outbound tunnel via `cloudflared`.
   - Admin MFA (TOTP 2FA), strict origin verification, and rate limiting with real client IP resolution.

---

## 3. Module Map

| Directory / Module | Purpose | Key Components |
|---|---|---|
| `app/main.py` | App lifecycle, routers, startup/shutdown | FastAPI app factory, background worker startup |
| `app/camera/` | Camera ingest and connection pooling | `reader.py` (RTSP reader loop), `manager.py`, `probe.py` |
| `app/vision/` | Face recognition primitives | `yunet.py` (detection), `sface.py` (embeddings), `quality.py`, `matcher.py` |
| `app/pipeline/` | Gate entrance / exit processing | `worker.py` (face detection/matching loop, event logging) |
| `app/surveillance/` | Vision surveillance pipeline | `motion.py`, `detector.py`, `tracker.py`, `rules.py`, `manager.py`, `clips.py` |
| `app/agent/` | AI secondary verification | `verify.py` (Ollama VLM query worker) |
| `app/notify/` | Real-time event dispatching | `bus.py` (SSE async bus), `sources.py` (event emitters), `push.py` (Web Push) |
| `app/security.py` | Security & crypto utilities | Fernet encryption (secrets, templates, RTSP passwords), password hashing, rate limiting, IP resolution |
| `app/auth.py` | User authentication & sessions | Session verification, role-based access control, TOTP 2FA verification |
| `app/models.py`, `app/db.py` | Database schema & migrations | SQLAlchemy ORM models, SQLite connection, `migrate()` with `_ADDED_COLUMNS` |
| `app/jobs/` | Periodic background maintenance | Log retention cleanup, camera status watcher, disk space guard |
| `app/web/` | Web presentation layer | `routes/` (FastAPI controllers), `templates/` (Jinja2), `static/` (CSS/JS, htmx) |
| `app/i18n.py` | Internationalization | Bilingual dictionaries (`TRANSLATIONS["en"]` and `TRANSLATIONS["bn"]`) |
| `tools/` | Operations & evaluation CLI | `benchmark.py`, `eval_accuracy.py`, `download_models.py`, `generate_env.py` |

---

## 4. End-to-End Data Flow

### 4.1 Surveillance & Incident Pipeline
1. **Frame Ingestion**: `CameraReader` pulls RTSP frames in an isolated thread, managing reconnects and dropped frames.
2. **Motion Gating**: `MotionDetector` analyzes frame differences. If motion is below threshold, heavyweight object detection is skipped to save CPU cycles.
3. **Object Detection**: `ObjectDetector` runs YOLOX on detected motion ROIs or full frames, returning bounding boxes and class probabilities (person, vehicle, bag, etc.).
4. **Tracking**: `ObjectTracker` tracks objects across consecutive frames, maintaining unique track IDs and spatial trajectories.
5. **Rule Evaluation**: `RuleEngine` evaluates active camera rules against tracks (e.g., intrusion into polygon zones, tripwire crossing, loitering, unattended objects, running, falls, unauthorized zone access, drawer tampering).
6. **Incident Recording**: When a rule triggers, an `Incident` is committed to SQLite. A pre/post-roll H.264 video clip and snapshot are generated by `ClipRecorder`.
7. **AI Verification**: The incident is queued for `AIVerifyWorker`, which queries local Ollama VLM with key incident frames to confirm whether it is a genuine event or a false alarm.
8. **Notification**: `NotificationBus` dispatches real-time SSE messages to open browser sessions and Web Push notifications to subscribed mobile PWA devices.

### 4.2 Gate Face Pipeline
1. RTSP sub-stream is processed by `GateWorker`.
2. YuNet locates faces in the frame; `FaceQuality` verifies face size, blur, and frontal angle.
3. SFace extracts a 128-dimensional embedding vector.
4. Cosine similarity matcher compares against enrolled encrypted gallery templates in SQLite.
5. If match exceeds threshold, an IN/OUT attendance event is logged, and SSE notifies the gate dashboard.

---

## 5. Security & Threat Model

- **Data at Rest**:
  - Passwords hashed with bcrypt.
  - Camera RTSP URLs and passwords are encrypted using Fernet keys (`SECRET_KEY`).
  - Face biometric embeddings are Fernet-encrypted before database storage.
  - TOTP secrets are Fernet-encrypted.
- **Data in Transit**:
  - Remote access requires HTTPS via Cloudflare Tunnel (`COOKIE_SECURE=true`).
  - State-altering requests require valid session cookies and strict `Origin` header matching.
  - Rate limiting protects against brute force attacks, utilizing `CF-Connecting-IP` when behind a trusted Cloudflare proxy.
- **Audit Trails**:
  - Administrative changes (user edits, rule modifications, camera changes, enrollment deletions) write immutable entries to `AuditLog`.
