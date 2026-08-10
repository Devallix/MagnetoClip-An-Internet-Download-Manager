"""End-to-end test downloads for every default category.

Verifies the download engine (analyze -> segment -> merge) works for each
available category and that files land in the category's folder.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from magnetoclip.app.lifecycle import build_context
from magnetoclip.database.models import DownloadStatus
from tests.support.http_server import PayloadServer

PAYLOAD = bytes(range(256)) * 512  # 128 KiB, deterministic

# One representative extension per default category.
CATEGORY_SAMPLES = [
    ("Documents", "report.pdf"),
    ("Videos", "movie.mp4"),
    ("Music", "song.mp3"),
    ("Images", "photo.png"),
    ("Software", "setup.exe"),
    ("Archives", "bundle.zip"),
    ("Other", "mystery.xyz"),
]


def make_context(tmp_path):
    context = build_context(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
    )
    context.settings.set("downloads.default_directory", str(tmp_path / "downloads"))
    return context


async def wait_for_status(manager, download_id, status, timeout=30.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        d = manager.get_download(download_id)
        if d is not None and d.status == status:
            return d
        await asyncio.sleep(0.02)
    raise AssertionError(f"download {download_id} never reached {status.value}")


@pytest.mark.asyncio
@pytest.mark.parametrize("category,filename", CATEGORY_SAMPLES)
async def test_download_engine_completes_for_each_category(tmp_path, category, filename):
    context = make_context(tmp_path)
    manager = context.manager
    try:
        with PayloadServer(PAYLOAD) as server:
            download = manager.add(server.url, filename=filename)
            assert download.filename == filename

            category_record = context.categories.get(download.category_id)
            assert category_record is not None, "download was not categorized"
            assert category_record.name == category

            assert manager.start(download.id) is True
            await wait_for_status(manager, download.id, DownloadStatus.completed)

            snapshot = manager.snapshot_item(manager.get_download(download.id))
            assert snapshot["status"] == "completed"
            assert snapshot["size_downloaded"] == len(PAYLOAD)

            final = Path(snapshot["save_path"])
            assert final.exists(), "final file missing"
            assert final.read_bytes() == PAYLOAD, "file content mismatch"

            expected_dir = tmp_path / "downloads" / category_record.folder
            assert final.parent == expected_dir, (
                f"file saved to {final.parent}, expected {expected_dir}"
            )
    finally:
        await context.shutdown()


@pytest.mark.asyncio
async def test_download_engine_uses_multiple_connections(tmp_path):
    context = make_context(tmp_path)
    manager = context.manager
    try:
        with PayloadServer(PAYLOAD) as server:
            download = manager.add(server.url, filename="parallel.bin")
            manager.start(download.id)
            await wait_for_status(manager, download.id, DownloadStatus.completed)
            assert server.range_requests > 0, "server never received Range requests"
    finally:
        await context.shutdown()


@pytest.mark.asyncio
async def test_download_engine_verifies_integrity(tmp_path):
    import hashlib

    context = make_context(tmp_path)
    manager = context.manager
    try:
        expected = hashlib.sha256(PAYLOAD).hexdigest()
        with PayloadServer(PAYLOAD) as server:
            download = manager.add(
                server.url,
                filename="verified.bin",
                hash_algo="sha256",
                hash_expected=expected,
            )
            manager.start(download.id)
            record = await wait_for_status(manager, download.id, DownloadStatus.completed)
            assert record.hash_calculated == expected
    finally:
        await context.shutdown()
