"""Tests for logging shutdown behavior (Windows log-file lock release)."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from magnetoclip.app.lifecycle import build_context
from magnetoclip.services.logging.setup import get_logger, shutdown_logging

log = get_logger(__name__)


def _file_handlers() -> list[logging.FileHandler]:
    root = logging.getLogger()
    return [h for h in root.handlers if isinstance(h, logging.FileHandler)]


def test_shutdown_logging_closes_and_removes_file_handlers(tmp_path: Path) -> None:
    log_file = tmp_path / "app.log"
    handler = logging.FileHandler(log_file, encoding="utf-8")
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        assert handler.stream is not None
        assert _file_handlers()
        shutdown_logging()
        assert handler.stream is None
        assert not _file_handlers()
        log_file.unlink()
    finally:
        if handler in root.handlers:
            root.removeHandler(handler)
        if handler.stream is not None:
            handler.close()


def test_context_shutdown_releases_log_file(tmp_path: Path) -> None:
    context = build_context(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
    )
    try:
        assert _file_handlers(), "build_context should install a file handler"
        log.info("shutdown_release_probe")
    finally:
        asyncio.run(context.shutdown())

    assert not _file_handlers(), "shutdown must close the file handler"
    log_file = tmp_path / "logs" / "magnetoclip.log"
    assert log_file.exists()
    renamed = log_file.with_name("magnetoclip.log.released")
    log_file.rename(renamed)
    renamed.unlink()
