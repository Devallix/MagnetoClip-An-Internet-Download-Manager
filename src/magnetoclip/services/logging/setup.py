from __future__ import annotations

import logging
import sys
from pathlib import Path

import structlog

_PRE_PROCESSORS = [
    structlog.contextvars.merge_contextvars,
    structlog.processors.add_log_level,
    structlog.processors.TimeStamper(fmt="iso", utc=True),
    structlog.processors.StackInfoRenderer(),
]


def configure_logging(log_dir: Path, *, level: str = "INFO") -> None:
    """Configure structlog (JSON on stderr and to a file).

    The application log file lives at ``<log_dir>/magnetoclip.log``.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    numeric = getattr(logging, str(level).upper(), logging.INFO)

    formatter = logging.Formatter("%(message)s")
    handlers: list[logging.Handler] = []

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(numeric)
    console.setFormatter(formatter)
    handlers.append(console)

    file_handler = logging.FileHandler(log_dir / "magnetoclip.log", encoding="utf-8")
    file_handler.setLevel(numeric)
    file_handler.setFormatter(formatter)
    handlers.append(file_handler)

    logging.basicConfig(level=numeric, handlers=handlers, force=True)

    structlog.configure(
        processors=_PRE_PROCESSORS + [structlog.processors.JSONRenderer()],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "magnetoclip") -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


def shutdown_logging() -> None:
    """Close and remove file handlers so the log file is not left locked.

    On Windows an open ``magnetoclip.log`` file handle survives until the
    process exits, which blocks clean shutdowns, test temp-dir cleanup and
    app restarts. Call this during shutdown after all modules have logged.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, logging.FileHandler):
            try:
                handler.flush()
            except Exception:
                pass
            try:
                handler.close()
            except Exception:
                pass
            root.removeHandler(handler)
