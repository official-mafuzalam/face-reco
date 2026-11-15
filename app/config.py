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
    allow_insecure_defaults: bool = False


_DEVELOPMENT_SECRET_KEYS = {"change-this-development-secret", "replace-with-a-long-random-secret"}


def insecure_setting_problems(current: Settings) -> list[str]:
    problems: list[str] = []
    if current.secret_key in _DEVELOPMENT_SECRET_KEYS or len(current.secret_key) < 32:
        problems.append("SECRET_KEY must be a random value of at least 32 characters.")
    if current.fernet_key == _DEVELOPMENT_FERNET_KEY:
        problems.append("FERNET_KEY must be a newly generated Fernet key.")
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
