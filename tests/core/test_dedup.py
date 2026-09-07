"""Tests for duplicate detection (hasher, DedupManager, DownloadManager hooks)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from magnetoclip.app.lifecycle import build_context
from magnetoclip.core.dedup import DedupManager
from magnetoclip.core.dedup.hasher import (
    digest_bytes,
    digest_file,
    ensure_supported,
)
from magnetoclip.core.events.bus import Events
from magnetoclip.database.models import DownloadStatus
from tests.support.http_server import PayloadServer

SLEEP_STEP = 0.02


async def wait_for(condition, timeout: float = 15.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if condition():
            return True
        await asyncio.sleep(SLEEP_STEP)
    return False


def make_context(tmp_path):
    context = build_context(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
    )
    context.settings.set("downloads.default_directory", str(tmp_path / "downloads"))
    return context


# ----- hasher -----


def test_ensure_supported_returns_valid_algo():
    assert ensure_supported("xxh3_128") in ("xxh3_128", "blake2b")
    assert ensure_supported("blake2b") == "blake2b"


def test_digest_bytes_and_file_agree(tmp_path):
    data = b"magneto clip dedup test payload" * 10
    p = tmp_path / "sample.bin"
    p.write_bytes(data)
    assert digest_bytes(data, "blake2b") == digest_file(p, "blake2b")


def test_digest_distinguishes_content():
    assert digest_bytes(b"aaaa", "blake2b") != digest_bytes(b"aaab", "blake2b")


# ----- manager -----


def test_check_bytes_detects_exact_duplicate(tmp_path):
    context = make_context(tmp_path)
    dedup: DedupManager = context.dedup
    data = bytes(range(256)) * 64
    context.settings.set("dedup.enabled", True)
    # No index yet -> not a duplicate.
    assert dedup.check_bytes(data, filename="a.bin").is_duplicate is False
    # Index a file with the same content.
    path = tmp_path / "existing.bin"
    path.write_bytes(data)
    assert dedup.index_file(path, filename="existing.bin")
    result = dedup.check_bytes(data, filename="a.bin")
    assert result.is_duplicate is True
    assert result.existing_filename == "existing.bin"
    context.database.close()


def test_index_file_and_count(tmp_path):
    context = make_context(tmp_path)
    dedup: DedupManager = context.dedup
    path = tmp_path / "f.bin"
    path.write_bytes(b"some content here")
    assert dedup.index_file(path, filename="f.bin")
    assert dedup.count() == 1
    # Re-indexing the same file does not duplicate the row.
    assert dedup.index_file(path, filename="f.bin")
    assert dedup.count() == 1
    context.database.close()


def test_check_file_exact_duplicate(tmp_path, monkeypatch):
    context = make_context(tmp_path)
    dedup: DedupManager = context.dedup
    # Use a synchronous digest run (no running loop) so the test is deterministic.
    monkeypatch.setattr(
        "magnetoclip.core.dedup.manager.DedupManager._blocking_digest",
        lambda self, p: digest_file(p, self._algo_name()),
    )
    original = tmp_path / "orig.bin"
    original.write_bytes(b"the same bytes" * 8)
    copy = tmp_path / "copy.bin"
    copy.write_bytes(b"the same bytes" * 8)
    other = tmp_path / "other.bin"
    other.write_bytes(b"totally different" * 8)
    assert dedup.index_file(original, filename="orig.bin")
    assert dedup.check_file(copy, filename="copy.bin").is_duplicate is True
    assert dedup.check_file(other, filename="other.bin").is_duplicate is False
    context.database.close()


def test_check_by_attrs_size_and_name(tmp_path):
    context = make_context(tmp_path)
    dedup: DedupManager = context.dedup
    path = tmp_path / "video.mp4"
    path.write_bytes(b"x" * 1000)
    context.settings.set("dedup.only_same_filename", True)
    dedup.index_file(path, filename="video.mp4")
    assert dedup.check_by_attrs("video.mp4", 1000).is_duplicate is True
    # Same size, different name -> not a duplicate when only_same_filename.
    assert dedup.check_by_attrs("other.mp4", 1000).is_duplicate is False
    context.database.close()


def test_stats(tmp_path):
    context = make_context(tmp_path)
    dedup: DedupManager = context.dedup
    (tmp_path / "a.bin").write_bytes(b"a" * 500)
    (tmp_path / "b.bin").write_bytes(b"b" * 700)
    dedup.index_file(tmp_path / "a.bin", filename="a.bin")
    dedup.index_file(tmp_path / "b.bin", filename="b.bin")
    stats = dedup.stats()
    assert stats.indexed_files == 2
    assert stats.indexed_bytes == 1200
    assert stats.size_groups == 2
    context.database.close()


# ----- DownloadManager integration -----


def test_add_with_data_auto_skip_substitutes_existing(tmp_path):
    context = make_context(tmp_path)
    context.settings.set("dedup.enabled", True)
    context.settings.set("dedup.auto_skip", True)
    manager = context.manager
    dedup: DedupManager = context.dedup
    data = bytes(range(256)) * 64
    # Index a pre-existing file with the same content.
    existing = tmp_path / "existing.bin"
    existing.write_bytes(data)
    dedup.index_file(existing, filename="existing.bin")
    assert dedup.count() == 1

    blob_url = "blob:https://magnetoclip.dev/abcdef"
    download = manager.add(blob_url, filename="new.bin", data=data)
    # auto_skip: no new download row; the substitute references the existing file.
    with context.session_factory() as session:
        from magnetoclip.database.repositories import DownloadRepository

        row = DownloadRepository(session).get(download.id)
        assert row.status == DownloadStatus.completed
        assert Path(row.save_path) == existing
    assert dedup.count() == 1
    context.database.close()


def test_add_with_data_decision_hook_download_anyway(tmp_path):
    context = make_context(tmp_path)
    context.settings.set("dedup.enabled", True)
    context.settings.set("dedup.auto_skip", False)
    context.dedup_decision = lambda payload: "download"
    manager = context.manager
    dedup: DedupManager = context.dedup
    data = bytes(range(256)) * 64
    existing = tmp_path / "existing.bin"
    existing.write_bytes(data)
    dedup.index_file(existing, filename="existing.bin")

    blob_url = "blob:https://magnetoclip.dev/abcdef"
    download = manager.add(blob_url, filename="new.bin", data=data)
    # 'download' decision -> a real new download is created (not the substitute).
    assert download.save_path is not None
    assert Path(download.save_path).name == "new.bin"
    context.database.close()


def test_add_post_duplicate_detected_event(tmp_path):
    context = make_context(tmp_path)
    context.settings.set("dedup.enabled", True)
    context.settings.set("dedup.auto_skip", False)
    context.dedup_decision = lambda payload: "skip"
    manager = context.manager
    dedup: DedupManager = context.dedup
    data = bytes(range(256)) * 64
    existing = tmp_path / "existing.bin"
    existing.write_bytes(data)
    dedup.index_file(existing, filename="existing.bin")

    fired: list[dict] = []
    context.events.connect(Events.DUPLICATE_DETECTED, fired.append)
    manager.add("blob:https://magnetoclip.dev/abcdef", filename="new.bin", data=data)
    assert fired, "DUPLICATE_DETECTED should have fired"
    assert fired[0]["filename"] == "new.bin"
    context.database.close()


def test_add_no_duplicate_when_different_content(tmp_path):
    context = make_context(tmp_path)
    context.settings.set("dedup.enabled", True)
    context.settings.set("dedup.auto_skip", False)
    manager = context.manager
    first = bytes(range(256)) * 64
    second = bytes(reversed(range(256)))  # different content
    manager.add("blob:https://magnetoclip.dev/one", filename="one.bin", data=first)
    dl = manager.add("blob:https://magnetoclip.dev/two", filename="two.bin", data=second)
    # Different content -> a fresh download is created.
    assert Path(dl.save_path).name == "two.bin"
    context.database.close()


def test_url_duplicate_by_existing_file(tmp_path):
    context = make_context(tmp_path)
    context.settings.set("dedup.enabled", True)
    context.settings.set("dedup.auto_skip", True)
    manager = context.manager
    # A file already sits at the destination path.
    dest = tmp_path / "downloads"
    dest.mkdir(exist_ok=True)
    existing_path = dest / "report.pdf"
    existing_path.write_bytes(b"%PDF-1.4 test")
    dl = manager.add("https://example.com/report.pdf", filename="report.pdf",
                     save_dir=dest)
    with context.session_factory() as session:
        from magnetoclip.database.repositories import DownloadRepository

        row = DownloadRepository(session).get(dl.id)
        assert Path(row.save_path) == existing_path
    context.database.close()


async def test_completed_download_is_indexed(tmp_path):
    context = make_context(tmp_path)
    context.settings.set("dedup.enabled", True)
    context.settings.set("dedup.index_after_download", True)
    manager = context.manager
    dedup: DedupManager = context.dedup
    payload = bytes(range(256)) * 128
    with PayloadServer(payload) as server:
        download = manager.add(server.url, filename="indexed.bin")
        assert manager.start(download.id) is True
        assert await wait_for(
            lambda: manager.get_download(download.id)
            and manager.get_download(download.id).status == DownloadStatus.completed
        )
    assert await wait_for(lambda: dedup.count() >= 1, timeout=20.0)
    stats = dedup.stats()
    assert stats.indexed_files >= 1
    # Re-downloading the same content would be flagged by check_bytes.
    assert dedup.check_bytes(payload, filename="again.bin").is_duplicate is True
    context.database.close()
