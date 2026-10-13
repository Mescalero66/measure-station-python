"""Logging configuration."""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging(
    level: str,
    log_file: Path | None = None,
    *,
    max_bytes: int = 1_000_000,
    backup_count: int = 3,
) -> None:
    """Log to stdout (captured by journald under systemd) and optionally a rotating file."""
    # journald timestamps every line itself; avoid duplicating it.
    under_journald = "JOURNAL_STREAM" in os.environ
    stream_format = "%(levelname)s [%(name)s] %(message)s"
    file_format = "%(asctime)s %(levelname)s [%(name)s] %(message)s"

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(logging.Formatter(stream_format if under_journald else file_format))
    handlers: list[logging.Handler] = [stream_handler]

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter(file_format))
        handlers.append(file_handler)

    logging.basicConfig(level=level.upper(), handlers=handlers, force=True)
