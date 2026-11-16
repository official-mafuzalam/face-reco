"""Application logging: a rotating file plus an in-memory tail for the log viewer."""

import logging
import threading
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.camera.reader import mask_credentials

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = PROJECT_ROOT / "data" / "logs"
LOG_FILE = LOG_DIR / "app.log"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 5
BUFFER_SIZE = 3000
_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# Logger name prefix -> area shown in the viewer filter.
AREAS = {
    "app.camera": "camera",
    "app.pipeline": "face",
    "app.vision": "face",
    "app.services.enroll": "face",
    "app.surveillance": "surveillance",
    "app.jobs": "jobs",
    "app.web": "web",
    "uvicorn": "web",
}


def area_of(logger_name: str) -> str:
    for prefix, area in AREAS.items():
        if logger_name == prefix or logger_name.startswith(prefix + "."):
            return area
    return "app"


@dataclass(frozen=True)
class LogRecordView:
    seq: int
    at: datetime
    level: str
    area: str
    logger: str
    message: str


class MaskingFilter(logging.Filter):
    """Never let a camera password reach a log file or the viewer."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = mask_credentials(record.getMessage())
        record.args = ()
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = mask_credentials(record.exc_text)
        return True


class RingBufferHandler(logging.Handler):
    def __init__(self, capacity: int = BUFFER_SIZE) -> None:
        super().__init__()
        self._records: deque[LogRecordView] = deque(maxlen=capacity)
        self._seq = 0
        self._lock_buffer = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if record.exc_text:
            message = f"{message}\n{record.exc_text}"
        with self._lock_buffer:
            self._seq += 1
            self._records.append(
                LogRecordView(
                    self._seq,
                    datetime.fromtimestamp(record.created, UTC),
                    record.levelname,
                    area_of(record.name),
                    record.name,
                    message,
                )
            )

    def records(
        self,
        *,
        min_level: int = logging.DEBUG,
        area: str | None = None,
        search: str | None = None,
        after: int = 0,
        limit: int = 500,
    ) -> list[LogRecordView]:
        needle = search.casefold() if search else None
        with self._lock_buffer:
            items = list(self._records)
        selected = [
            item
            for item in items
            if item.seq > after
            and logging.getLevelName(item.level) >= min_level
            and (area is None or item.area == area)
            and (needle is None or needle in item.message.casefold() or needle in item.logger.casefold())
        ]
        return selected[-limit:]


log_buffer = RingBufferHandler()
_configured = False


def setup_logging(log_file: Path = LOG_FILE, level: int = logging.INFO) -> None:
    """Idempotent; called at app startup so tests importing the app write no files."""
    global _configured
    if _configured:
        return
    _configured = True
    root = logging.getLogger()
    root.setLevel(level)
    masking = MaskingFilter()
    formatter = logging.Formatter(_FORMAT)
    handlers: list[logging.Handler] = [log_buffer]
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
        )
        handlers.append(file_handler)
    except OSError:
        logging.getLogger(__name__).exception("Unable to open log file %s.", log_file)
    if not any(isinstance(handler, logging.StreamHandler) and not isinstance(handler, RotatingFileHandler) for handler in root.handlers):
        handlers.append(logging.StreamHandler())
    for handler in handlers:
        handler.addFilter(masking)
        handler.setFormatter(formatter)
        root.addHandler(handler)
    # uvicorn's access log is noise in the viewer (every page refresh); keep it on stdout only.
    logging.getLogger("uvicorn.access").propagate = False
    logging.getLogger(__name__).info("Logging to %s.", log_file)
