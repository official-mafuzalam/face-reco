# AGENTS.md — Gate Face

Single source of truth for every AI coding agent (Claude Code, Codex, Antigravity/Gemini,
Cursor, Copilot). `CLAUDE.md` and `GEMINI.md` only import this file. Edit rules **here only**.

## Project

Local gate log + CCTV surveillance for a LAN, heading to production.
FastAPI + SQLAlchemy 2 (SQLite, WAL) + Jinja2 server-rendered pages + htmx (vendored).
OpenCV ONNX models (face detect/embed, YOLOX-S) on CPU. Windows-first; Linux supported.

| Path | What lives there |
|---|---|
| `app/main.py` | App factory, router wiring, startup jobs |
| `app/web/routes/` | One router per page area; HTML responses via `templates.TemplateResponse` |
| `app/web/templates/` | Jinja2 pages, extend `base.html` |
| `app/web/static/` | Plain CSS/JS, no build step; vendor libs pinned in `vendor/` |
| `app/services/` | Business logic (no FastAPI imports where avoidable) |
| `app/vision/`, `app/pipeline/` | Face detection, embedding, matching, gate pipeline |
| `app/surveillance/` | Motion → YOLOX → tracker → rules → incidents/clips |
| `app/camera/` | Camera readers and manager |
| `app/jobs/` | Background jobs (retention, monitor, incidents) |
| `app/models.py`, `app/db.py` | ORM models; `migrate()` adds missing columns |
| `app/i18n.py` | `TRANSLATIONS["en"]` and `TRANSLATIONS["bn"]` |
| `tests/` | pytest; `conftest.py` sets insecure dev keys and disables surveillance workers |
| `tools/`, `scripts/` | CLI tools (`python -m tools.x`) and PowerShell/bash ops scripts |
| `docs/bn/` | Bangla end-user guides |

## Commands

Run from the repo root. Use the venv interpreter.

```powershell
.\.venv\Scripts\python.exe -m pytest -q            # all tests (must pass before done)
.\.venv\Scripts\python.exe -m pytest tests/test_x.py -q   # one file
.\.venv\Scripts\python.exe -m pytest -m "not models" -q   # skip tests needing ONNX models
.\scripts\run.ps1                                  # run app at http://127.0.0.1:8090
```

Linux: replace `.\.venv\Scripts\python.exe` with `.venv/bin/python`.
Never run `scripts/install*.ps1`, `install_service_windows.ps1`, `build_release.ps1` or
backup/retention jobs without `--dry-run` unless the user asks.

## Rules (all agents)

### Workflow
1. Read the relevant files before editing. Follow the existing pattern of the nearest similar code.
2. Smallest change that solves the task. No drive-by refactors, renames or reformatting.
3. Add or update a test in `tests/` for every behavior change or bug fix.
4. Run the tests for what you touched, then the full suite. Report the real result; never claim
   "passing" without running it.
5. Do not add a dependency to `requirements.txt` without asking. Pin vendored JS by version.

### Code style
- Python 3.11+ syntax, type hints on new functions, 4-space indent, UTF-8.
- SQLAlchemy 2.0 style (`select(...)`, `Session`), no legacy `query()` in new code.
- Imports: stdlib, third-party, `app.*` — grouped, absolute imports only.
- Comments explain *why*, not what. No commented-out code.
- No new frontend framework or build step. Use htmx + small plain JS in `app/web/static/`.

### UI text (bilingual — mandatory)
- Never hard-code user-visible text in templates or routes. Add the key to **both**
  `TRANSLATIONS["en"]` and `TRANSLATIONS["bn"]` in `app/i18n.py` and use `t.<key>`.
- Bangla text must be natural Bangla, not transliteration.

### Database
- New column on an existing table: add to the model **and** to `_ADDED_COLUMNS` in
  `app/db.py` so existing production databases upgrade. Never drop or rename columns that hold user data.
- Never delete or edit files under `data/` or `backups/`.

### Security (production system with face data)
- Every new router/page must require auth (`get_current_user` / `require_role(...)`).
  Only `/login` and `/health` are public.
- State-changing actions are POST/PUT/DELETE forms; the Origin check in `app/security.py`
  must keep working. Never add GET endpoints that change state.
- Camera URLs and face templates stay Fernet-encrypted. Never log secrets, passwords,
  camera credentials or embeddings.
- Write an audit row for admin actions, e.g.
  `db.add(AuditLog(user_id=user.id, action="rule.delete", detail=f"rule_id={rule_id}"))`.
- Never commit or print `.env`, `data/`, `models/*.onnx`, `dist/`.

### Git
- Commit only when the user asks. Conventional commits: `feat:`, `fix:`, `docs:`, `test:`,
  `refactor:`, `chore:` + short imperative English summary.
- Never force-push, rewrite history, or skip hooks.

### Docs
- User-facing behavior change → update `README.md` and the matching `docs/bn/*.md` guide.

## Communication with the user
- Reply in **Bangla** (Bengali script or Banglish as the user writes). Code, identifiers,
  commit messages and code comments stay in English.
- Be concise. Final message: what changed (file links), test result, anything left undone.
