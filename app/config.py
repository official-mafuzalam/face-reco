from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


_DEVELOPMENT_FERNET_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    db_path: Path = Path("data/gate.db")
    # Where scripts/backup.ps1 writes gate-face-*.zip; used to warn when backups stop.
    backup_dir: Path = Path("backups")
    secret_key: str = "change-this-development-secret"
    fernet_key: str = _DEVELOPMENT_FERNET_KEY
    cookie_secure: bool = False
    trust_cloudflare: bool = False
    session_idle_timeout_minutes: int = 60
    # Web Push (VAPID)
    push_enabled: bool = True
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    vapid_claims_email: str = "admin@gateface.local"
    match_threshold: float = 0.363
    match_margin: float = 0.05
    min_face_px: int = 80
    pipeline_fps: float = 5.0
    consensus_n: int = 3
    consensus_window: int = 5
    debounce_s: float = 45.0
    unknown_debounce_s: float = 60.0
    save_match_snapshots: bool = True
    unknown_group_similarity: float = 0.75
    tz: str = "Asia/Dhaka"
    host: str = "127.0.0.1"
    port: int = 8090
    snapshot_retention_days: int = 90
    event_retention_days: int = 365
    allow_insecure_defaults: bool = False
    # Surveillance (object detection, incidents, clips)
    surveillance_enabled: bool = True
    hardware_profile: str = "cpu_small"
    detector_backend: str = "auto"
    surveillance_fps: float = 2.0
    object_confidence: float = 0.45
    motion_threshold: float = 0.003
    incident_idle_seconds: float = 6.0
    # OpenCV's VP8 writer has no bitrate control, so size is managed through width and fps.
    clip_fps: float = 4.0
    clip_pre_seconds: float = 4.0
    clip_max_seconds: float = 60.0
    clip_max_width: int = 640
    incident_media_retention_days: int = 30
    # AI Verification (Local VLM via Ollama)
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5vl:3b"
    ai_verify_enabled: bool = True
    ai_verify_timeout_seconds: float = 15.0
    # 24/7 Watchdog & Disk Guard
    watchdog_enabled: bool = True
    watchdog_interval_seconds: float = 15.0
    min_disk_free_gb: float = 5.0
    min_disk_free_percent: float = 10.0


_DEVELOPMENT_SECRET_KEYS = {"change-this-development-secret", "replace-with-a-long-random-secret"}


def insecure_setting_problems(current: Settings) -> list[str]:
    problems: list[str] = []
    if current.secret_key in _DEVELOPMENT_SECRET_KEYS or len(current.secret_key) < 32:
        problems.append("SECRET_KEY must be a random value of at least 32 characters.")
    if current.fernet_key == _DEVELOPMENT_FERNET_KEY:
        problems.append("FERNET_KEY must be a newly generated Fernet key.")
    if current.trust_cloudflare and not current.cookie_secure:
        problems.append("COOKIE_SECURE must be true when TRUST_CLOUDFLARE=true.")
    return problems


def ensure_secure_settings(current: Settings) -> None:
    problems = insecure_setting_problems(current)
    if problems and not current.allow_insecure_defaults:
        raise RuntimeError(
            "Refusing to start with insecure settings: "
            + " ".join(problems)
            + " Set ALLOW_INSECURE_DEFAULTS=true only for local development."
        )


settings = Settings()
