from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


_DEVELOPMENT_FERNET_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    db_path: Path = Path("data/gate.db")
    secret_key: str = "change-this-development-secret"
    fernet_key: str = _DEVELOPMENT_FERNET_KEY
    cookie_secure: bool = False
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


settings = Settings()
