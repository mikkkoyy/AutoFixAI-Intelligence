import logging
import os
from pathlib import Path
from logging.handlers import RotatingFileHandler
from datetime import datetime

LOGS_DIR = Path(__file__).parent.parent / "logs"
LOGS_DIR.mkdir(exist_ok=True)

_loggers: dict[str, logging.Logger] = {}


def setup_logging(level: str = "INFO"):
    log_level = getattr(logging, level.upper(), logging.INFO)

    root_logger = logging.getLogger("aira")
    root_logger.setLevel(log_level)

    if not root_logger.handlers:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(log_level)
        fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        console_handler.setFormatter(fmt)
        root_logger.addHandler(console_handler)

    categories = [
        "application", "ai", "memory", "tools", "git", "errors", "improvements"
    ]
    for cat in categories:
        get_logger(cat)


def get_logger(category: str = "application") -> logging.Logger:
    if category in _loggers:
        return _loggers[category]

    logger = logging.getLogger(f"aira.{category}")
    logger.setLevel(logging.DEBUG)

    log_file = LOGS_DIR / f"{category}.log"
    handler = RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    handler.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(fmt)
    logger.addHandler(handler)

    _loggers[category] = logger
    return logger


def log_error(category: str, error: Exception, context: str = ""):
    logger = get_logger("errors")
    msg = f"[{category}] {type(error).__name__}: {error}"
    if context:
        msg = f"{context} | {msg}"
    logger.error(msg, exc_info=True)


def log_improvement(action: str, details: str, result: str):
    logger = get_logger("improvements")
    logger.info(f"Action: {action} | Details: {details} | Result: {result}")
