"""Tests for the Phase 3 orchestration layer (manager, queues, scheduler)."""

from __future__ import annotations

import asyncio

import pytest

from magnetoclip.app.lifecycle import build_context
from magnetoclip.core.categories.manager import CategoryManager
from magnetoclip.core.downloads.manager import DownloadManager
from magnetoclip.core.events.bus import Events
from magnetoclip.core.queues.manager import QueueManager
from magnetoclip.core.scheduler.scheduler import Scheduler
from magnetoclip.database.models import Category, DownloadStatus

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


# ----- scheduler -----


def test_scheduler_time_window(tmp_path):
    context = make_context(tmp_path)
    scheduler: Scheduler = context.scheduler
    from datetime import datetime

    with context.session_factory() as session:
        from magnetoclip.database.repositories import ScheduleRepository

    schedule = ScheduleRepository(session).add(
        "Work hours", start_time="09:00", end_time="17:00",
        days_mask=0b0011111, enabled=True,
    )
    assert scheduler.is_active(schedule, datetime(2026, 1, 5, 10, 0))  # Monday 10:00
    assert not scheduler.is_active(schedule, datetime(2026, 1, 5, 20, 0))
    assert not scheduler.is_active(schedule, datetime(2026, 1, 4, 10, 0))  # Sunday


def test_scheduler_wrap_midnight(tmp_path):
    context = make_context(tmp_path)
    scheduler: Scheduler = context.scheduler
    with context.session_factory() as session:
        from magnetoclip.database.repositories import ScheduleRepository

        schedule = ScheduleRepository(session).add(
            "Night", start_time="22:00", end_time="06:00", enabled=True
        )
    from datetime import datetime

    assert scheduler.is_active(schedule, datetime(2026, 1, 5, 23, 30))
    assert scheduler.is_active(schedule, datetime(2026, 1, 6, 2, 0))
    assert not scheduler.is_active(schedule, datetime(2026, 1, 5, 12, 0))


def test_scheduler_days_mask(tmp_path):
    context = make_context(tmp_path)
    scheduler: Scheduler = context.scheduler
    with context.session_factory() as session:
        from magnetoclip.database.repositories import ScheduleRepository

        schedule = ScheduleRepository(session).add(
            "Weekdays", start_time="08:00", end_time="18:00",
            days_mask=0b0011111, enabled=True,
        )
    from datetime import datetime

    assert scheduler.is_active(schedule, datetime(2026, 1, 5, 9, 0))   # Monday
    assert scheduler.is_active(schedule, datetime(2026, 1, 9, 9, 0))   # Friday
    assert not scheduler.is_active(schedule, datetime(2026, 1, 10, 9, 0))  # Saturday


def test_scheduler_bandwidth_day_night(tmp_path):
    context = make_context(tmp_path)
    scheduler: Scheduler = context.scheduler
    with context.session_factory() as session:
        from magnetoclip.database.repositories import ScheduleRepository

        schedule = ScheduleRepository(session).add(
            "Speed", start_time=None, end_time=None, enabled=True,
            speed_day=1.0, speed_night=2.0,
        )
    from datetime import datetime

    day = scheduler.bandwidth_for(schedule, datetime(2026, 1, 5, 12, 0))
    night = scheduler.bandwidth_for(schedule, datetime(2026, 1, 5, 23, 0))
    assert day == 1.0 * 1024 * 1024
    assert night == 2.0 * 1024 * 1024
    assert scheduler.effective_bandwidth(datetime(2026, 1, 5, 12, 0)) == day


def test_scheduler_request_toggle_without_loop_is_noop(tmp_path):
    context = make_context(tmp_path)
    scheduler: Scheduler = context.scheduler
    assert scheduler._tick_task is None
    scheduler.request_toggle(True)
    assert scheduler._tick_task is None


async def test_scheduler_request_toggle_starts_and_stops(tmp_path):
    context = make_context(tmp_path)
    scheduler: Scheduler = context.scheduler
    assert scheduler._tick_task is None
    scheduler.request_toggle(True)
    await asyncio.sleep(0.02)
    assert scheduler._tick_task is not None
    scheduler.request_toggle(False)
    await asyncio.sleep(0.02)
    assert scheduler._tick_task is None


# ----- queues -----


async def test_queue_management(tmp_path):
    context = make_context(tmp_path)
    manager: DownloadManager = context.manager
    queues: QueueManager = context.queues
    queue = queues.add("My Queue", max_concurrent=2)
    dl1 = manager.add("https://example.com/a.zip")
    dl2 = manager.add("https://example.com/b.zip")
    queues.add_download(queue.id, dl1.id, auto_start=False)
    queues.add_download(queue.id, dl2.id, auto_start=False)
    items = queues.items(queue.id)
    assert {i.download_id for i in items} == {dl1.id, dl2.id}
    assert len(queues.pending_downloads(queue.id)) == 2
    queues.remove_download(queue.id, dl1.id)
    assert [i.download_id for i in queues.items(queue.id)] == [dl2.id]
    queues.reorder(queue.id, [dl2.id])
    queues.remove(queue.id)
    assert queues.get(queue.id) is None


async def test_set_priority_persists_and_reorders(tmp_path):
    context = make_context(tmp_path)
    manager: DownloadManager = context.manager
    queues: QueueManager = context.queues
    queue = queues.add("Priority", max_concurrent=1)
    dl1 = manager.add("https://example.com/p1.zip")
    dl2 = manager.add("https://example.com/p2.zip")
    queues.add_download(queue.id, dl1.id, auto_start=False)
    queues.add_download(queue.id, dl2.id, auto_start=False)

    events = []
    context.events.connect(Events.DOWNLOAD_UPDATED, lambda p: events.append(p))
    manager.set_priority(dl2.id, 9)

    assert manager.get_download(dl2.id).priority == 9
    pending = queues.pending_downloads(queue.id)
    assert pending[0].id == dl2.id  # higher priority advances first
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


async def test_queue_auto_advance(tmp_path):
    context = make_context(tmp_path)
    context.settings.set("downloads.simultaneous", 4)
    manager = DownloadManager(context)
    queues = context.queues
    queues.attach(manager)
    queue = queues.add("Auto", max_concurrent=1)
    payload = bytes(range(256)) * 512
    with PayloadServer(payload, chunk_size=8192, chunk_delay=0.005) as server:
        dl1 = manager.add(f"{server.base}/file.bin", filename="first.bin")
        dl2 = manager.add(f"{server.base}/file.bin", filename="second.bin")
        queues.add_download(queue.id, dl1.id)
        queues.add_download(queue.id, dl2.id)
        assert await wait_for(
            lambda: (
                _status(manager, dl1.id) == DownloadStatus.completed
                and _status(manager, dl2.id) == DownloadStatus.completed
            ),
            timeout=30,
        )
    assert _path(manager, dl1.id).exists()
    assert _path(manager, dl2.id).exists()


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
