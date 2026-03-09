"""
logger.py
---------
Configures logging with rotating file output.

Call `setup_logging()` at startup to redirect all print() and log output
to both the console and a rotating log file under the `logs/` directory.
"""

import os
import sys
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


LOG_DIR = Path(__file__).parent / "logs"
LOG_FILE = LOG_DIR / "post-bot.log"
MAX_BYTES = 5 * 1024 * 1024  # 5 MB per file
BACKUP_COUNT = 5              # keep 5 rotated files (25 MB total max)


class _StreamToLogger:
    """Redirect writes (from print()) to a logger while also writing to the original stream."""

    def __init__(self, logger: logging.Logger, level: int, original_stream):
        self.logger = logger
        self.level = level
        self.original_stream = original_stream
        self._buf = ""

    def write(self, message: str):
        self.original_stream.write(message)
        if message and message.strip():
            self.logger.log(self.level, message.strip())

    def flush(self):
        self.original_stream.flush()


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

    logger = logging.getLogger("post-bot")
    logger.setLevel(logging.INFO)
    logger.addHandler(file_handler)

    # Redirect print() → logger (while keeping console output)
    sys.stdout = _StreamToLogger(logger, logging.INFO, sys.__stdout__)
    sys.stderr = _StreamToLogger(logger, logging.ERROR, sys.__stderr__)
