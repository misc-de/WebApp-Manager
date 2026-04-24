import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

APP_NAME = 'webapp'
LOG_DIR = Path.home() / '.local/state' / APP_NAME
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / 'app.log'

LOG_LEVEL_ENV_VAR = 'WEBAPP_MANAGER_LOG_LEVEL'
LOG_MAX_BYTES_ENV_VAR = 'WEBAPP_MANAGER_LOG_MAX_BYTES'
LOG_BACKUP_COUNT_ENV_VAR = 'WEBAPP_MANAGER_LOG_BACKUP_COUNT'
DEFAULT_LOG_MAX_BYTES = 1_000_000
DEFAULT_LOG_BACKUP_COUNT = 3


def _resolve_log_level():
    raw = (os.environ.get(LOG_LEVEL_ENV_VAR) or '').strip().upper()
    if not raw:
        return logging.INFO
    try:
        return int(raw)
    except ValueError:
        pass
    return logging._nameToLevel.get(raw, logging.INFO)


def _resolve_int_env(name, default, minimum=0):
    raw = (os.environ.get(name) or '').strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(value, minimum)


def get_log_file_path() -> Path:
    return LOG_FILE


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(_resolve_log_level())
    formatter = logging.Formatter(
        '%(asctime)s %(levelname)s [%(name)s] %(message)s'
    )

    max_bytes = _resolve_int_env(LOG_MAX_BYTES_ENV_VAR, DEFAULT_LOG_MAX_BYTES, minimum=1024)
    backup_count = _resolve_int_env(LOG_BACKUP_COUNT_ENV_VAR, DEFAULT_LOG_BACKUP_COUNT, minimum=0)

    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding='utf-8',
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    logger.propagate = False
    return logger
