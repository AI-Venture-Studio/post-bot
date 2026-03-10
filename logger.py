"""
logger.py
---------
Configures logging with rotating file output.

Call `setup_logging()` at startup to redirect all print() and log output
to both the console and a rotating log file under the `logs/` directory.
"""

import os
import re
import sys
import logging
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path


LOG_DIR = Path(__file__).parent / "logs"
LOG_FILE = LOG_DIR / "post-bot.log"
MAX_BYTES = 5 * 1024 * 1024  # 5 MB per file
BACKUP_COUNT = 5              # keep 5 rotated files (25 MB total max)


class _AccessLogFilter(logging.Filter):
    """Downgrade HTTP access log lines (200/3xx) from WARNING/ERROR to DEBUG."""
    _ACCESS_LOG_RE = re.compile(r'HTTP/1\.[01]"\s+[23]\d{2}\s')

    def filter(self, record):
        if self._ACCESS_LOG_RE.search(record.getMessage()):
            record.levelno = logging.DEBUG
            record.levelname = "DEBUG"
        return True


class _StreamToLogger:
    """Redirect writes (from print()) to a logger while also writing to the original stream."""

    def __init__(self, logger: logging.Logger, level: int, original_stream):
        self.logger = logger
        self.level = level
        self.original_stream = original_stream
        self._thread_local = threading.local()

    def _is_logging(self) -> bool:
        return getattr(self._thread_local, 'logging', False)

    def _set_logging(self, value: bool):
        self._thread_local.logging = value

    def write(self, message: str):
        if self.original_stream is not None:
            self.original_stream.write(message)
        if message and message.strip() and not self._is_logging():
            self._set_logging(True)
            try:
                self.logger.log(self.level, message.strip())
            finally:
                self._set_logging(False)

    def flush(self):
        if self.original_stream is not None:
            self.original_stream.flush()

    @property
    def encoding(self):
        if self.original_stream is not None and hasattr(self.original_stream, 'encoding'):
            return self.original_stream.encoding
        return 'utf-8'

    @property
    def name(self):
        if self.original_stream is not None and hasattr(self.original_stream, 'name'):
            return self.original_stream.name
        return '<logger>'

    def fileno(self):
        if self.original_stream is not None and hasattr(self.original_stream, 'fileno'):
            return self.original_stream.fileno()
        raise OSError("no underlying file descriptor")

    def isatty(self):
        if self.original_stream is not None and hasattr(self.original_stream, 'isatty'):
            return self.original_stream.isatty()
        return False


def setup_logging() -> None:
    """Set up rotating file logging and redirect stdout/stderr."""
    LOG_DIR.mkdir(exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(_AccessLogFilter())

    logger = logging.getLogger("post-bot")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)

    # Redirect print() → logger (while keeping console output)
    sys.stdout = _StreamToLogger(logger, logging.INFO, sys.__stdout__)
    sys.stderr = _StreamToLogger(logger, logging.WARNING, sys.__stderr__)
