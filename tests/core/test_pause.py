"""Tests for the pause switch + auto-resume feature."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from magnetoclip.app.lifecycle import build_context
from magnetoclip.core.downloads.manager import DownloadManager
from magnetoclip.core.events.bus import Events
from magnetoclip.database.models import DownloadStatus
from magnetoclip.database.repositories import DownloadRepository, SettingsStore


def make_context(tmp_path):
    context = build_context(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
    )
    context.settings.set("downloads.default_directory", str(tmp_path / "downloads"))
    return context


def _insert(manager: DownloadManager, url: str) -> int:
    with manager.session_factory() as session:
        download = DownloadRepository(session).add(
            url, filename=url.rsplit("/", 1)[-1]
        )
        return download.id


def _status(manager: DownloadManager, download_id: int):
    with manager.session_factory() as session:
        download = DownloadRepository(session).get(download_id)
        return download.status if download else None


def test_pause_controller_toggle_persists(tmp_path):
    context = make_context(tmp_path)
    controller = context.pauses
    assert controller.is_paused() is False

    events: list[dict] = []
    context.events.connect(
        Events.PAUSE_STATE_CHANGED, lambda payload: events.append(payload)
    )

    controller.pause()
    assert controller.is_paused() is True
    assert context.settings.get("pause.enabled") is True
    assert context.settings.get("pause.resume_at") == ""
    assert events[-1]["paused"] is True
    assert SettingsStore(context.session_factory).load_all()["pause.enabled"] is True

    controller.resume()
    assert controller.is_paused() is False
    assert events[-1]["paused"] is False
    assert SettingsStore(context.session_factory).load_all()["pause.enabled"] is False


def test_pause_controller_auto_resume_schedules_and_fires(tmp_path):
    context = make_context(tmp_path)
    controller = context.pauses

    controller.set_auto_resume_hours(2)
    assert controller.auto_resume_hours() == 2

    controller.pause()
    resume_at = controller.resume_at()
    assert resume_at is not None
    delta = resume_at - datetime.now(UTC)
    assert timedelta(minutes=119) <= delta <= timedelta(minutes=121)

    # Rewind the deadline to the past; the tick should fire the resume.
    context.settings.set(
        "pause.resume_at",
        (datetime.now(UTC) - timedelta(seconds=5)).isoformat(),
    )
    controller._check_resume()
    assert controller.is_paused() is False
    assert context.settings.get("pause.resume_at") == ""


def test_pause_all_resume_all_tracks_toggled_downloads(tmp_path):
    context = make_context(tmp_path)
    manager: DownloadManager = context.manager

    active_id = _insert(manager, "https://example.com/a.bin")
    queued_id = _insert(manager, "https://example.com/b.bin")
    waiting_id = _insert(manager, "https://example.com/c.bin")
    manual_pause_id = _insert(manager, "https://example.com/d.bin")
    done_id = _insert(manager, "https://example.com/e.bin")

    with manager.session_factory() as session:
        repo = DownloadRepository(session)
        repo.update_status(active_id, DownloadStatus.downloading)
        repo.update_status(queued_id, DownloadStatus.queued)
        repo.update_status(waiting_id, DownloadStatus.scheduled)
        repo.update_status(manual_pause_id, DownloadStatus.paused)
        repo.update_status(done_id, DownloadStatus.completed)

    paused_calls: list[int] = []
    resumed_calls: list[int] = []
    manager.pause = lambda download_id: paused_calls.append(download_id)
    manager.resume = lambda download_id: resumed_calls.append(download_id)

    manager.pause_all()
    assert sorted(paused_calls) == sorted([active_id, queued_id, waiting_id])

    manager.resume_all()
    assert sorted(resumed_calls) == sorted([active_id, queued_id, waiting_id])
    assert manual_pause_id not in resumed_calls
    assert done_id not in resumed_calls


def test_pause_state_event_drives_manager(tmp_path):
    context = make_context(tmp_path)
    manager: DownloadManager = context.manager
    did = _insert(manager, "https://example.com/f.bin")
    with manager.session_factory() as session:
        DownloadRepository(session).update_status(did, DownloadStatus.downloading)

    resumed: list[int] = []
    manager.resume = lambda download_id: resumed.append(download_id)

    manager.events.post(Events.PAUSE_STATE_CHANGED, {"paused": True})
    assert did in manager._paused_by_switch
    assert _status(manager, did) == DownloadStatus.paused

    manager.events.post(Events.PAUSE_STATE_CHANGED, {"paused": False})
    assert did not in manager._paused_by_switch
    assert resumed == [did]