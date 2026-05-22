"""Centralised ``logging`` configuration (SPEC §23.2: print 禁止、logging 使用)."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

import config


_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(threadName)-15s %(name)-30s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def configure_logging(
    level: str = config.LOG_LEVEL,
    log_file: Path = config.LOG_FILE,
    max_bytes: int = config.LOG_FILE_MAX_BYTES,
    backup_count: int = config.LOG_FILE_BACKUP_COUNT,
) -> None:
    """Install console + rotating-file handlers on the root logger.

    Idempotent: calling it twice will not duplicate handlers.
    """
    root = logging.getLogger()
    root.setLevel(level)

    # Remove any previously-installed handlers (notably the bare basicConfig
    # handler the smoke scripts may have set up).
    for h in list(root.handlers):
        root.removeHandler(h)

    log_file.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    console.setLevel(level)
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    root.addHandler(file_handler)

    # Quiet down libraries that flood at INFO without losing actual errors.
    for noisy in ("werkzeug", "flask_sock", "engineio", "socketio"):
        logging.getLogger(noisy).setLevel("WARNING")
