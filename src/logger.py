"""
    src/logger.py – Structured rotating logger (file + console).
"""

from __future__ import annotations

import os
import sys
import logging
from logging.handlers import RotatingFileHandler

_FMT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FMT = "%Y-%m-%dT%H:%M:%S"

# In-memory ring buffer for the panel's live log viewer
_log_buffer: list[str] = []
_MAX_BUFFER = 500


class _BufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        _log_buffer.append(self.format(record))
        if len(_log_buffer) > _MAX_BUFFER:
            _log_buffer.pop(0)


def get_log_buffer() -> list[str]:
    return list(_log_buffer)


def setup_logger(log_file: str, log_level: str) -> logging.Logger:
    os.makedirs(os.path.dirname(log_file) if os.path.dirname(log_file) else ".", exist_ok=True)
    level = getattr(logging, log_level.upper(), logging.INFO)
    formatter = logging.Formatter(fmt=_FMT, datefmt=_DATE_FMT)

    root = logging.getLogger("netpulse")
    root.setLevel(level)

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(formatter)
    root.addHandler(ch)

    fh = RotatingFileHandler(log_file, maxBytes=10 * 1024 ** 2, backupCount=5, encoding="utf-8")
    fh.setFormatter(formatter)
    root.addHandler(fh)

    bh = _BufferHandler()
    bh.setFormatter(formatter)
    root.addHandler(bh)

    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"netpulse.{name}")
