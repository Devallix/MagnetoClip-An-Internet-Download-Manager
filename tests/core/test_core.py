"""Tests for the Phase 3 orchestration layer (manager, torrent queue)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from magnetoclip.app.lifecycle import build_context
from magnetoclip.core.categories.manager import CategoryManager
from magnetoclip.core.downloads.manager import DownloadManager
from magnetoclip.core.events.bus import Events
from magnetoclip.database.models import Category, DownloadStatus
from magnetoclip.database.repositories import DownloadRepository

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


class StateTracker:
    """Records the maximum number of simultaneously-active downloads."""

    ACTIVE = {"connecting", "downloading", "retrying", "verifying"}
    IDLE = {"paused", "completed", "failed", "verification_failed", "stopped"}

    def __init__(self) -> None:
        self.active: set[int] = set()
        self.max_active = 0

    def __call__(self, payload: dict) -> None:
        state = payload.get("state")
        if state in self.ACTIVE:
            self.active.add(payload["id"])
        elif state in self.IDLE:
            self.active.discard(payload["id"])
        self.max_active = max(self.max_active, len(self.active))


# ----- categories -----


def test_category_defaults_created(tmp_path):
    context = make_context(tmp_path)
    manager: CategoryManager = context.categories
    names = [c.name for c in manager.list()]
    for default in ("Documents", "Videos", "Music", "Images", "Software", "Archives", "Other"):
        assert default in names


def test_category_crud(tmp_path):
    context = make_context(tmp_path)
    manager: CategoryManager = context.categories
    category = manager.add("Books", folder="~/Books", icon="book", color="#123456")
    assert isinstance(category, Category)
    assert manager.get_by_name("Books").folder == "~/Books"
    assert manager.get(category.id).name == "Books"
    with pytest.raises(ValueError):
        manager.add("Books")
    manager.remove("Books")
    assert manager.get_by_name("Books") is None
    with pytest.raises(ValueError):
        manager.remove("Other")


def test_category_classify_by_extension(tmp_path):
    context = make_context(tmp_path)
    manager: CategoryManager = context.categories
    assert manager.classify("report.pdf").name == "Documents"
    assert manager.classify("movie.mkv").name == "Videos"
    assert manager.classify("song.flac").name == "Music"
    assert manager.classify("photo.png").name == "Images"
    assert manager.classify("setup.exe").name == "Software"
    assert manager.classify("bundle.zip").name == "Archives"
    assert manager.classify("mystery.xyz").name == "Other"


# ----- priorities -----


def test_set_priority_persists(tmp_path):
    context = make_context(tmp_path)
    manager: DownloadManager = context.manager
    dl1 = manager.add("https://example.com/p1.zip")
    dl2 = manager.add("https://example.com/p2.zip")

    events = []
    context.events.connect(Events.DOWNLOAD_UPDATED, lambda p: events.append(p))
    manager.set_priority(dl2.id, 9)

    assert manager.get_download(dl2.id).priority == 9
    assert any(p.get("id") == dl2.id and p.get("priority") == 9 for p in events)


def test_set_priority_clamps_to_range(tmp_path):
    context = make_context(tmp_path)
    manager: DownloadManager = context.manager
    download = manager.add("https://example.com/c.zip")
    manager.set_priority(download.id, 999)
    assert manager.get_download(download.id).priority == 10
    manager.set_priority(download.id, -999)
    assert manager.get_download(download.id).priority == -10


# ----- download manager -----


def test_add_validates_url(tmp_path):
    context = make_context(tmp_path)
    manager: DownloadManager = context.manager
    with pytest.raises(ValueError):
        manager.add("ftp://example.com/file.zip")
    with pytest.raises(ValueError):
        manager.add("not-a-url")


def test_add_auto_categorizes_and_sanitizes(tmp_path):
    context = make_context(tmp_path)
    manager: DownloadManager = context.manager
    download = manager.add("https://example.com/../../etc/passwd.pdf")
    assert download.filename == "passwd.pdf"
    assert download.category_id is not None
    category = context.categories.get(download.category_id)
    assert category.name == "Documents"


def test_add_strips_query_strings_from_derived_names(tmp_path):
    context = make_context(tmp_path)
    manager: DownloadManager = context.manager
    # Facebook-style CDN URLs carry a huge query string that used to become part
    # of the filename and pushed Windows paths past MAX_PATH (bug report).
    facebook = (
        "https://scontent.facc6-1.fna.fbcdn.net/v/t39.99422-6/"
        "772840093_2002848522768189_5870094313626133451_n.png"
        "?stp=dst-jpg_p526x296&_nc_cat=110&ccb=1-7&_nc_sid=5f2048"
        "&_nc_ohc=abc&_nc_ht=scontent&oh=00_AFx&oe=6A83A0ED"
    )
    download = manager.add(facebook)
    assert download.filename == "772840093_2002848522768189_5870094313626133451_n.png"
    assert "?" not in download.filename


def test_add_applies_format_extension_to_extensionless_cdns(tmp_path):
    context = make_context(tmp_path)
    manager: DownloadManager = context.manager
    # Twitter images are extensionless and declare their format in the query.
    twitter = "https://pbs.twimg.com/media/HPlReWoWQAAmMqB?format=jpg&name=900x900"
    download = manager.add(twitter)
    assert download.filename == "HPlReWoWQAAmMqB.jpg"


def test_add_data_bytes_completes_immediately(tmp_path):
    context = make_context(tmp_path)
    manager: DownloadManager = context.manager
    blob_url = "blob:https://web.telegram.org/55d2a84a-91c1-4b2e-8a13-0f0e0c2e8f1c"
    payload = b"\x89PNG\r\n\x1a\n" + bytes(1024)
    download = manager.add(blob_url, filename="photo.png", data=payload)
    assert download.status == DownloadStatus.completed
    assert Path(download.save_path).read_bytes() == payload
    # Completed records must not be re-scheduled by a stray start() call.
    assert manager.start(download.id) is False


async def test_download_completes(tmp_path):
    context = make_context(tmp_path)
    manager: DownloadManager = context.manager
    payload = bytes(range(256)) * 1024  # 256 KiB
    with PayloadServer(payload) as server:
        download = manager.add(server.url)
        assert manager.start(download.id) is True
        assert await wait_for(
            lambda: _status(manager, download.id) == DownloadStatus.completed
        )
    snapshot = manager.snapshot_item(_get(manager, download.id))
    assert snapshot["status"] == "completed"
    assert snapshot["size_downloaded"] == len(payload)
    final = _path(manager, download.id)
    assert final.exists()
    assert final.read_bytes() == payload
    assert not final.with_suffix(final.suffix + ".mclip").exists()


async def test_download_fails_on_404(tmp_path):
    context = make_context(tmp_path)
    manager: DownloadManager = context.manager
    with PayloadServer(b"x") as server:
        download = manager.add(f"{server.base}/missing.bin")
        assert manager.start(download.id) is True
        assert await wait_for(
            lambda: _status(manager, download.id) == DownloadStatus.failed
        )
    snapshot = manager.snapshot_item(_get(manager, download.id))
    assert snapshot["error"]


async def test_concurrency_limit(tmp_path):
    context = make_context(tmp_path)
    context.settings.set("downloads.simultaneous", 1)
    manager = DownloadManager(context)
    tracker = StateTracker()
    context.events.connect(Events.DOWNLOAD_STATE_CHANGED, tracker)
    payload = bytes(range(256)) * 256  # 64 KiB
    with PayloadServer(payload, chunk_size=4096, chunk_delay=0.01) as server:
        dl1 = manager.add(f"{server.base}/file.bin", filename="one.bin")
        dl2 = manager.add(f"{server.base}/file.bin", filename="two.bin")
        assert manager.start(dl1.id)
        assert manager.start(dl2.id)
        assert await wait_for(
            lambda: (
                _status(manager, dl1.id) == DownloadStatus.completed
                and _status(manager, dl2.id) == DownloadStatus.completed
            ),
            timeout=30,
        )
    assert tracker.max_active <= 1


async def test_pause_resume(tmp_path):
    context = make_context(tmp_path)
    manager: DownloadManager = context.manager
    payload = bytes(range(256)) * 512  # 128 KiB
    with PayloadServer(payload, chunk_size=4096, chunk_delay=0.01) as server:
        download = manager.add(server.url)
        assert manager.start(download.id)
        assert await wait_for(
            lambda: _status(manager, download.id) == DownloadStatus.downloading
        )
        manager.pause(download.id)
        assert await wait_for(
            lambda: _status(manager, download.id) == DownloadStatus.paused
        )
        manager.resume(download.id)
        assert await wait_for(
            lambda: _status(manager, download.id) == DownloadStatus.completed,
            timeout=30,
        )
    assert _path(manager, download.id).read_bytes() == payload


async def test_cancel_mid_download(tmp_path):
    context = make_context(tmp_path)
    manager: DownloadManager = context.manager
    payload = bytes(range(256)) * 512
    with PayloadServer(payload, chunk_size=4096, chunk_delay=0.02) as server:
        download = manager.add(server.url)
        assert manager.start(download.id)
        assert await wait_for(
            lambda: _status(manager, download.id) == DownloadStatus.downloading
        )
        manager.cancel(download.id)
        assert await wait_for(
            lambda: _status(manager, download.id) == DownloadStatus.stopped
        )
    assert not _path(manager, download.id).exists()


async def test_restart(tmp_path):
    context = make_context(tmp_path)
    manager: DownloadManager = context.manager
    payload = bytes(range(256)) * 512
    with PayloadServer(payload) as server:
        download = manager.add(server.url)
        assert manager.start(download.id)
        assert await wait_for(
            lambda: _status(manager, download.id) == DownloadStatus.completed
        )
        assert await manager.restart(download.id)
        assert await wait_for(
            lambda: _status(manager, download.id) == DownloadStatus.completed
        )
    assert _path(manager, download.id).read_bytes() == payload


async def test_resume_picks_up_sidecar(tmp_path):
    context = make_context(tmp_path)
    manager: DownloadManager = context.manager
    payload = bytes(range(256)) * 1024  # 256 KiB
    with PayloadServer(payload, chunk_size=4096, chunk_delay=0.01) as server:
        download = manager.add(server.url)
        assert manager.start(download.id)
        assert await wait_for(
            lambda: _status(manager, download.id) == DownloadStatus.downloading
        )
        manager.pause(download.id)
        assert await wait_for(
            lambda: _status(manager, download.id) == DownloadStatus.paused
        )
        final = _path(manager, download.id)
        assert final.with_suffix(final.suffix + ".mclip").exists()
        manager.resume(download.id)
        assert await wait_for(
            lambda: _status(manager, download.id) == DownloadStatus.completed,
            timeout=30,
        )
    assert _path(manager, download.id).read_bytes() == payload


# ----- torrent queue -----


def _add_magnet(manager: DownloadManager, name: str):
    import hashlib

    infohash = hashlib.sha1(name.encode()).hexdigest()
    return manager.add(f"magnet:?xt=urn:btih:{infohash}&dn={name}")


def _fake_start(manager: DownloadManager, started: list[int]):
    def fake(did):
        with manager.session_factory() as session:
            DownloadRepository(session).update_status(did, DownloadStatus.downloading)
        started.append(did)
        return True

    return fake


def test_torrent_admission_caps_queue_slots(tmp_path, monkeypatch):
    context = make_context(tmp_path)
    context.settings.set("torrent.max_active_torrents", 2)
    context.settings.set("torrent.max_active_downloads", 2)
    manager = DownloadManager(context)
    monkeypatch.setattr(manager, "_start_torrent", _fake_start(manager, []))
    downloads = [_add_magnet(manager, f"t{i}") for i in range(4)]

    manager.torrent_queue.reconcile()

    statuses = [_status(manager, dl.id) for dl in downloads]
    assert statuses == [
        DownloadStatus.queued,
        DownloadStatus.queued,
        DownloadStatus.scheduled,
        DownloadStatus.scheduled,
    ]


def test_torrent_download_limit_and_promotion(tmp_path, monkeypatch):
    context = make_context(tmp_path)
    context.settings.set("torrent.max_active_torrents", 4)
    context.settings.set("torrent.max_active_downloads", 2)
    manager = DownloadManager(context)
    started: list[int] = []
    monkeypatch.setattr(manager, "_start_torrent", _fake_start(manager, started))
    downloads = [_add_magnet(manager, f"p{i}") for i in range(4)]

    manager.torrent_queue.admit_and_advance()
    assert sorted(started) == sorted(dl.id for dl in downloads[:2])

    # Finishing one transfer frees a slot and promotes the next waiting torrent.
    with manager.session_factory() as session:
        DownloadRepository(session).update_status(
            downloads[0].id, DownloadStatus.completed
        )
    manager.torrent_queue.admit_and_advance()

    assert downloads[2].id in started
    assert downloads[3].id not in started
    assert len(started) == 3
    assert _status(manager, downloads[3].id) == DownloadStatus.queued


def test_manual_start_respects_limits(tmp_path, monkeypatch):
    context = make_context(tmp_path)
    context.settings.set("torrent.max_active_torrents", 5)
    context.settings.set("torrent.max_active_downloads", 1)
    manager = DownloadManager(context)
    started: list[int] = []
    monkeypatch.setattr(manager, "_start_torrent", _fake_start(manager, started))
    first = _add_magnet(manager, "m1")
    second = _add_magnet(manager, "m2")

    manager.torrent_queue.admit_and_advance()
    assert started == [first.id]

    assert manager.start(second.id) is False
    assert _status(manager, second.id) == DownloadStatus.queued


def test_seeding_torrents_do_not_occupy_slots(tmp_path):
    context = make_context(tmp_path)
    manager: DownloadManager = context.manager
    seed = _add_magnet(manager, "seedme")
    with manager.session_factory() as session:
        repo = DownloadRepository(session)
        repo.update_status(seed.id, DownloadStatus.completed)
        record = repo.get(seed.id)
        record.torrent_seeding = True
        session.commit()

    assert manager.torrent_queue.count_unfinished() == 0
    assert manager.torrent_queue.count_transfers() == 0
    assert manager.torrent_queue.slots_full() is False


class _StubHandler:
    """Mimics TorrentDownloadHandler's pause/resume surface."""

    def __init__(self) -> None:
        self.paused = False
        self.resumed = False

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.resumed = True

    def is_paused(self) -> bool:
        return self.paused


def test_pause_frees_slot_and_advances_queue(tmp_path, monkeypatch):
    context = make_context(tmp_path)
    context.settings.set("torrent.max_active_torrents", 5)
    context.settings.set("torrent.max_active_downloads", 1)
    manager = DownloadManager(context)
    started: list[int] = []
    monkeypatch.setattr(manager, "_start_torrent", _fake_start(manager, started))
    first = _add_magnet(manager, "q1")
    second = _add_magnet(manager, "q2")

    manager.torrent_queue.admit_and_advance()
    assert started == [first.id]

    stub = _StubHandler()
    manager._torrent_handlers[first.id] = stub
    manager.pause(first.id)

    assert stub.paused is True
    assert _status(manager, first.id) == DownloadStatus.paused
    # The freed slot is used by the waiting torrent right away.
    assert second.id in started
    assert _status(manager, second.id) == DownloadStatus.downloading


def test_resume_parked_handler_in_place(tmp_path, monkeypatch):
    context = make_context(tmp_path)
    context.settings.set("torrent.max_active_downloads", 1)
    manager = DownloadManager(context)
    started: list[int] = []
    monkeypatch.setattr(manager, "_start_torrent", _fake_start(manager, started))
    first = _add_magnet(manager, "r1")

    manager.torrent_queue.admit_and_advance()
    stub = _StubHandler()
    manager._torrent_handlers[first.id] = stub

    manager.pause(first.id)
    assert _status(manager, first.id) == DownloadStatus.paused

    manager.resume(first.id)
    assert stub.resumed is True
    assert _status(manager, first.id) == DownloadStatus.downloading
    # The parked polling task is reused; no fresh start was spawned.
    assert started.count(first.id) == 1


def test_resume_requeues_when_slots_full(tmp_path, monkeypatch):
    context = make_context(tmp_path)
    context.settings.set("torrent.max_active_torrents", 5)
    context.settings.set("torrent.max_active_downloads", 1)
    manager = DownloadManager(context)
    started: list[int] = []
    monkeypatch.setattr(manager, "_start_torrent", _fake_start(manager, started))
    first = _add_magnet(manager, "w1")
    busy = _add_magnet(manager, "w2")
    third = _add_magnet(manager, "w3")

    manager.torrent_queue.admit_and_advance()
    assert started == [first.id]

    first_stub = _StubHandler()
    manager._torrent_handlers[first.id] = first_stub
    manager.pause(first.id)
    assert _status(manager, busy.id) == DownloadStatus.downloading
    # Simulate the handler registering once the transfer begins.
    busy_stub = _StubHandler()
    manager._torrent_handlers[busy.id] = busy_stub

    manager.resume(first.id)
    # The slot belongs to `busy`: the resumed torrent goes back to waiting
    # with its libtorrent handler still parked.
    assert first_stub.resumed is False
    assert _status(manager, first.id) == DownloadStatus.queued
    assert manager.torrent_queue.count_transfers() == 1

    manager.pause(busy.id)
    # Freed slot revives the parked handler instead of spawning a task.
    assert first_stub.resumed is True
    assert _status(manager, first.id) == DownloadStatus.downloading
    assert started.count(busy.id) == 1
    assert _status(manager, third.id) == DownloadStatus.queued


# ----- helpers -----


def _get(manager: DownloadManager, download_id: int):
    with manager.session_factory() as session:
        from magnetoclip.database.repositories import DownloadRepository

        return DownloadRepository(session).get(download_id)


def _status(manager: DownloadManager, download_id: int):
    download = _get(manager, download_id)
    return download.status if download else None


def _path(manager: DownloadManager, download_id: int):
    return manager.path_of(download_id)
