import pytest

from magnetoclip.app.lifecycle import build_context
from magnetoclip.browser.skip import enable_skip_all, skip_all_active
from magnetoclip.core.events.bus import Events
from magnetoclip.ui.dialogs.add_url import AddUrlDialog
from magnetoclip.ui.main_window import MainWindow
from magnetoclip.ui.themes import build_qss


@pytest.fixture
def context(tmp_path):
    ctx = build_context(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
    )
    ctx.settings.set("downloads.default_directory", str(tmp_path / "downloads"))
    return ctx


def test_build_qss_resolves_all_tokens(qapp):
    for theme in ("dark", "light"):
        qss = build_qss(theme)
        assert "$" not in qss, f"unresolved token in {theme} theme"
        assert "QMainWindow" in qss


def test_table_header_sections_have_borders(qapp):
    for theme in ("dark", "light"):
        block = build_qss(theme).split("QHeaderView::section {", 1)[1].split("}", 1)[0]
        assert "border-bottom: 1px solid" in block, theme
        assert "border-right: 1px solid" in block, theme


def test_main_window_constructs(qtbot, context):
    window = MainWindow(context)
    qtbot.addWidget(window)
    assert window.windowTitle() == "MagnetoClip 0.1.2"
    assert window.sidebar is not None
    assert window.stack.count() == 9


def test_navigation_switches_pages(qtbot, context):
    window = MainWindow(context)
    qtbot.addWidget(window)
    window._activate("Settings")
    assert window.stack.currentWidget() is window._pages["Settings"]
    window._activate("Downloads")
    assert window.stack.currentWidget() is window._pages["Downloads"]


def test_downloads_name_column_is_adjustable(qtbot, context):
    from PySide6.QtWidgets import QHeaderView

    window = MainWindow(context)
    qtbot.addWidget(window)
    window._activate("Downloads")
    page = window._pages["Downloads"]
    header = page.table.horizontalHeader()
    assert header.sectionResizeMode(1) == QHeaderView.Interactive


def test_sidebar_finished_unfinished_buttons(qtbot, context):
    window = MainWindow(context)
    qtbot.addWidget(window)
    assert window._finished_button.name_label.text() == "Finished"
    assert window._unfinished_button.name_label.text() == "Unfinished"


def test_sidebar_status_filter_filters_rows(qtbot, context):
    context.manager.add("https://example.com/a.mp4")
    context.manager.add("https://example.com/b.mp4")
    window = MainWindow(context)
    qtbot.addWidget(window)
    window._activate("Downloads")
    page = window._pages["Downloads"]
    assert page.table.rowCount() == 2

    window._finished_button.clicked.emit("finished")
    assert window.stack.currentWidget() is page
    assert page.status_filter == "finished"
    assert page.table.rowCount() == 1  # empty state

    window._unfinished_button.clicked.emit("unfinished")
    assert page.status_filter == "unfinished"
    assert page.table.rowCount() == 2


def test_settings_save_shows_feedback(qtbot, context):
    window = MainWindow(context)
    qtbot.addWidget(window)
    window._activate("Settings")
    page = window._pages["Settings"]
    assert page.save_feedback.isHidden()
    page.save()
    assert not page.save_feedback.isHidden()
    assert page.save_feedback.text() == "Settings saved"


def test_network_event_updates_status_label(qtbot, context):
    window = MainWindow(context)
    qtbot.addWidget(window)
    context.events.post(Events.NETWORK_CHANGED, "Offline")
    assert window.network_label.text() == "● Offline"


def test_network_bandwidth_dict_payload_ignored(qtbot, context):
    window = MainWindow(context)
    qtbot.addWidget(window)
    context.events.post(Events.NETWORK_CHANGED, "Connected")
    context.events.post(Events.NETWORK_CHANGED, {"bandwidth_bytes_per_second": 500000})
    assert window.network_label.text() == "● Connected"


def test_speed_event_updates_status_label(qtbot, context):
    window = MainWindow(context)
    qtbot.addWidget(window)
    context.events.post(Events.SPEED_UPDATED, {"id": 1, "speed": 1024 * 1024})
    assert window.speed_label.text() == "1.0 MB/s"


def test_download_page_renders_rows(qtbot, context):
    manager = context.manager
    manager.add("https://example.com/report.pdf")
    window = MainWindow(context)
    qtbot.addWidget(window)
    window._activate("Downloads")
    page = window._pages["Downloads"]
    assert page.table.rowCount() == 1


def test_download_page_shows_table_when_empty(qtbot, context):
    window = MainWindow(context)
    qtbot.addWidget(window)
    window._activate("Downloads")
    page = window._pages["Downloads"]
    assert not page.table.isHidden()
    assert page.table.horizontalHeader().count() == 7
    labels = [
        page.table.horizontalHeaderItem(i).text()
        for i in range(page.table.horizontalHeader().count())
    ]
    assert labels == ["", "Name", "Size", "Status", "Speed", "Time left", "Time added"]
    assert page.table.rowCount() == 1
    assert page.table.item(0, 1).text() == "No downloads yet."


def test_categories_page_removed(qtbot, context):
    window = MainWindow(context)
    qtbot.addWidget(window)
    assert "categories" not in window._nav_buttons
    assert "Categories" not in window._pages
    assert window.stack.count() == 9


def test_sidebar_toggle_collapses(qtbot, context):
    window = MainWindow(context)
    window.show()
    qtbot.addWidget(window)
    qtbot.wait(20)
    assert window.sidebar.width() == 280
    assert window._all_button.name_label.isVisibleTo(window)
    window._toggle_sidebar()
    assert window._sidebar_expanded is False
    assert not window._all_button.name_label.isVisibleTo(window)
    assert not window._sidebar_caption.isVisibleTo(window)
    window._toggle_sidebar()
    assert window._sidebar_expanded is True
    assert window._all_button.name_label.isVisibleTo(window)


def test_sidebar_buttons_keep_height_at_minimum_window(qtbot, context):
    window = MainWindow(context)
    window.show()
    qtbot.addWidget(window)
    qtbot.wait(20)
    window.resize(1000, 580)
    qtbot.wait(50)
    button = window._all_button
    assert button.height() >= button.sizeHint().height()
    assert button.height() >= 38
    scrollbar = window._nav_scroll.verticalScrollBar()
    assert scrollbar.maximum() > 0


def test_sidebar_buttons_keep_height_at_collapsed_minimum(qtbot, context):
    window = MainWindow(context)
    window.show()
    qtbot.addWidget(window)
    qtbot.wait(20)
    window.resize(window.minimumWidth(), window.minimumHeight())
    qtbot.wait(50)
    button = window._all_button
    assert button.height() >= button.sizeHint().height()
    assert button.height() >= 38


def test_fit_to_work_area_clamps_window(qtbot, context):
    window = MainWindow(context)
    window.resize(5000, 5000)
    qtbot.addWidget(window)
    window.show()
    qtbot.wait(20)
    area = window.screen().availableGeometry()
    assert window.width() <= area.width()
    assert window.height() <= area.height()
    assert window.x() >= area.x()
    assert window.y() >= area.y()
    assert window.x() + window.width() <= area.x() + area.width()
    assert window.y() + window.height() <= area.y() + area.height()


def test_page_switch_applies_fade(qtbot, context):
    window = MainWindow(context)
    qtbot.addWidget(window)
    window._activate("Settings")
    assert window.stack.currentWidget() is window._pages["Settings"]
    assert window.stack.currentWidget().graphicsEffect() is not None


def test_dark_palette_defaults(qtbot, context):
    from PySide6.QtGui import QPalette
    from PySide6.QtWidgets import QApplication

    window = MainWindow(context)
    qtbot.addWidget(window)
    palette = QApplication.instance().palette()
    assert palette.color(QPalette.Window).name() == "#0b0d14"
    assert palette.color(QPalette.Base).name() == "#131624"


def test_icons_load_from_bundled_assets(qtbot):
    from magnetoclip.ui.components.icons import (
        category_icon,
        nav_icon,
        tool_icon,
        type_icon,
    )

    for name in ("overview", "downloads", "queue", "completed", "scheduler",
                 "analytics", "browser", "settings", "about", "all", "menu"):
        assert not nav_icon(name).isNull(), name
    for name in ("add", "start", "pause", "remove"):
        assert not tool_icon(name).isNull(), name
    for name in ("image", "video", "audio", "document", "archive", "software", "other"):
        assert not category_icon(name).isNull(), name
    assert not type_icon("image").isNull()


def test_add_url_dialog_validates(qtbot, context):
    dialog = AddUrlDialog(context)
    qtbot.addWidget(dialog)
    assert dialog.url() == ""
    assert dialog.category() in {"Other", "Archives", "Documents"}
    assert dialog.connections() >= 1


def test_add_url_dialog_rejects_unsupported_schemes(qtbot, context):
    dialog = AddUrlDialog(context)
    qtbot.addWidget(dialog)
    assert AddUrlDialog._url_error("https://example.com/file.zip") is None
    assert AddUrlDialog._url_error("http://example.com/a.bin") is None
    # blob: URLs are accepted (content is fetched via the browser extension).
    assert AddUrlDialog._url_error("blob:https://web.telegram.org/55d2a84a-91c1-4b2e") is None
    assert AddUrlDialog._url_error("ftp://example.com/x.zip")
    assert AddUrlDialog._url_error("not-a-url")
    # The dialog stays open and shows the message instead of crashing the app.
    dialog.url_edit.setText("ftp://example.com/x.zip")
    dialog._accept()
    assert not dialog.result()
    assert dialog.error_label.text() and "http://" in dialog.error_label.text().lower()
    assert not dialog.error_label.isHidden()


def test_add_url_dialog_shows_blob_hint(qtbot, context):
    dialog = AddUrlDialog(context)
    qtbot.addWidget(dialog)
    assert dialog.blob_hint.isHidden()
    dialog.url_edit.setText("blob:https://web.telegram.org/55d2a84a-91c1-4b2e")
    assert not dialog.blob_hint.isHidden()
    dialog.url_edit.setText("https://example.com/file.zip")
    assert dialog.blob_hint.isHidden()


def test_about_dialog_constructs(qtbot):
    from magnetoclip.ui.dialogs.about import AboutDialog

    dialog = AboutDialog()
    qtbot.addWidget(dialog)
    assert "About" in dialog.windowTitle()


def test_queue_page_shows_empty_state(qtbot, context):
    window = MainWindow(context)
    qtbot.addWidget(window)
    window._activate("Queue")
    page = window._pages["Queue"]
    assert not page.table.isHidden()
    assert page.table.rowCount() == 1
    assert page.table.item(0, 1).text() == (
        "No queues yet. Add a queue to organize your downloads."
    )
    assert not page.start_button.isEnabled()


def test_queue_page_renders_queues(qtbot, context):
    context.queues.add("Work", max_concurrent=2)
    context.queues.add("Media", max_concurrent=5)
    window = MainWindow(context)
    qtbot.addWidget(window)
    window._activate("Queue")
    page = window._pages["Queue"]
    assert page.table.rowCount() == 2
    names = {
        page.table.item(row, 1).text(): row for row in range(page.table.rowCount())
    }
    assert set(names) == {"Work", "Media"}
    assert page.table.item(names["Work"], 2).text() == "Idle"
    assert page.table.item(names["Work"], 3).text() == "2"
    assert page.table.item(names["Media"], 3).text() == "5"


def test_queue_page_counts_downloads(qtbot, context):
    download = context.manager.add("https://example.com/report.pdf")
    queue = context.queues.add("Work")
    context.queues.add_download(queue.id, download.id, auto_start=False)
    window = MainWindow(context)
    qtbot.addWidget(window)
    window._activate("Queue")
    page = window._pages["Queue"]
    assert page.table.rowCount() == 1
    assert page.table.item(0, 2).text() == "Queued"
    assert page.table.item(0, 4).text() == "0"
    assert page.table.item(0, 5).text() == "1"
    assert page.table.item(0, 6).text() == "1"


def test_queue_manager_update_persists(qtbot, context):
    queue = context.queues.add("Old")
    updated = context.queues.update(queue.id, name="New", max_concurrent=7)
    assert updated.name == "New"
    assert updated.max_concurrent == 7
    assert context.queues.get(queue.id).name == "New"


def test_queue_dialog_defaults(qtbot, context):
    from magnetoclip.ui.dialogs.queue import QueueDialog

    dialog = QueueDialog()
    qtbot.addWidget(dialog)
    assert dialog.name() == ""
    assert dialog.max_concurrent() == 3
    dialog.name_edit.setText("  ")
    dialog._validate_and_accept()
    assert dialog.result() == 0


def test_browser_page_constructs(qtbot, context):
    window = MainWindow(context)
    qtbot.addWidget(window)
    window._activate("Browser")
    page = window._pages["Browser"]
    assert page.integration_check.isChecked() is False
    assert page.capture_check.isChecked() is True
    assert page.prepare_button is not None
    assert page.redetect_button is not None
    assert page.status_label.text() != ""


def test_browser_capture_toggle_persists(qtbot, context):
    window = MainWindow(context)
    qtbot.addWidget(window)
    window._activate("Browser")
    page = window._pages["Browser"]
    page.capture_check.setChecked(False)
    assert context.settings.get("browser.capture_enabled") is False
    assert page.capture_check.isChecked() is False


def test_capture_watcher_skip_all_click_activates_suppression(qtbot, context):
    from magnetoclip.database.repositories import PendingCaptureRepository
    from magnetoclip.ui.components.capture_watcher import CaptureWatcher
    from magnetoclip.ui.dialogs.capture import RESULT_SKIP_ALL

    watcher = CaptureWatcher(context)
    with context.session_factory() as session:
        repo = PendingCaptureRepository(session)
        first = repo.add("https://example.com/a.zip", filename="a.zip")
        second = repo.add("https://example.com/b.zip", filename="b.zip")

    watcher._apply_decision(first, RESULT_SKIP_ALL, None)

    assert skip_all_active(context) is True
    with context.session_factory() as session:
        repo = PendingCaptureRepository(session)
        assert repo.pending() == []
        assert repo.get(first.id).status == "rejected"
        assert repo.get(second.id).status == "rejected"


def test_capture_watcher_suppresses_dialog_while_skip_all_active(qtbot, context):
    from magnetoclip.database.repositories import PendingCaptureRepository
    from magnetoclip.ui.components.capture_watcher import CaptureWatcher

    enable_skip_all(context, duration=None)
    with context.session_factory() as session:
        PendingCaptureRepository(session).add("https://example.com/a.zip")

    watcher = CaptureWatcher(context)
    with context.session_factory() as session:
        assert len(PendingCaptureRepository(session).pending()) == 1

    handled = watcher._handle_captures()

    assert handled == 0
    with context.session_factory() as session:
        assert PendingCaptureRepository(session).pending() == []


def test_capture_watcher_skip_all_re_enabled(qtbot, context):
    from magnetoclip.browser.skip import disable_skip_all
    from magnetoclip.database.repositories import PendingCaptureRepository
    from magnetoclip.ui.components.capture_watcher import CaptureWatcher
    from magnetoclip.ui.dialogs.capture import RESULT_SKIP_ALL

    watcher = CaptureWatcher(context)
    with context.session_factory() as session:
        capture = PendingCaptureRepository(session).add("https://example.com/a.zip")

    watcher._apply_decision(capture, RESULT_SKIP_ALL, None)
    assert skip_all_active(context) is True

    disable_skip_all(context)
    assert skip_all_active(context) is False

    with context.session_factory() as session:
        PendingCaptureRepository(session).add("https://example.com/b.zip")
    with context.session_factory() as session:
        assert len(PendingCaptureRepository(session).pending()) == 1


def test_browser_page_manual_setup_steps(qtbot, context):
    window = MainWindow(context)
    qtbot.addWidget(window)
    window._activate("Browser")
    page = window._pages["Browser"]
    assert page.setup_combo.count() == 6
    assert page.steps_label.text().startswith("1. Open ")
    page.setup_combo.setCurrentIndex(page.setup_combo.findData("firefox"))
    assert "about:debugging" in page.steps_label.text()


def test_browser_row_has_auto_install_button(qtbot, context):
    window = MainWindow(context)
    qtbot.addWidget(window)
    window._activate("Browser")
    page = window._pages["Browser"]
    base = context.browser.status()
    base["browsers"]["chrome"]["installed"] = True
    base["browsers"]["chrome"]["manifest"] = True
    base["browsers"]["chrome"]["force_installed"] = False
    context.browser.status = lambda: base
    page.refresh()
    controls = page._browser_controls["chrome"]
    assert controls["auto"].text() == "Auto-install"
    assert controls["auto"].isEnabled()
    base["browsers"]["chrome"]["force_installed"] = True
    page.refresh()
    assert page._browser_controls["chrome"]["auto"].text() == "Auto-installed"
    assert not page._browser_controls["chrome"]["auto"].isEnabled()


def test_scheduler_page_shows_empty_state(qtbot, context):
    window = MainWindow(context)
    qtbot.addWidget(window)
    window._activate("Scheduler")
    page = window._pages["Scheduler"]
    assert not page.table.isHidden()
    assert page.table.rowCount() == 1
    assert page.table.item(0, 1).text() == (
        "No schedules yet. Add a schedule to control bandwidth by time of day."
    )
    assert page.add_button.isEnabled()
    assert not page.enable_button.isEnabled()
    assert not page.enable_check.isChecked()


def test_scheduler_page_renders_schedules(qtbot, context):
    from magnetoclip.database.repositories import ScheduleRepository

    with context.session_factory() as session:
        repo = ScheduleRepository(session)
        repo.add(
            "Night cap",
            start_time="22:00",
            end_time="06:00",
            days_mask=0b1111111,
            speed_day=2.5,
            speed_night=1.0,
            enabled=True,
        )
        repo.add(
            "Weekend boost",
            start_time=None,
            end_time=None,
            days_mask=0b1100000,
            speed_day=0.0,
            speed_night=0.0,
            enabled=False,
        )
    window = MainWindow(context)
    qtbot.addWidget(window)
    window._activate("Scheduler")
    page = window._pages["Scheduler"]

    class _FakeScheduler:
        def is_active(self, schedule):
            return schedule.name == "Night cap"

    page.scheduler = _FakeScheduler()
    page._refresh_statuses()

    assert page.table.rowCount() == 2
    names = {
        page.table.item(row, 1).text(): row for row in range(page.table.rowCount())
    }
    assert set(names) == {"Night cap", "Weekend boost"}
    row = names["Night cap"]
    assert page.table.item(row, 2).text() == "22:00 - 06:00"
    assert page.table.item(row, 3).text() == "Every day"
    assert page.table.item(row, 4).text() == "2.5 MB/s"
    assert page.table.item(row, 5).text() == "1 MB/s"
    assert page.table.item(row, 6).text() == "Active"
    row = names["Weekend boost"]
    assert page.table.item(row, 2).text() == "All day"
    assert page.table.item(row, 3).text() == "Weekends"
    assert page.table.item(row, 4).text() == "Unlimited"
    assert page.table.item(row, 6).text() == "Off"


def test_scheduler_status_column_reports_idle(qtbot, context):
    from magnetoclip.database.repositories import ScheduleRepository

    with context.session_factory() as session:
        ScheduleRepository(session).add(
            "Weekday morning",
            start_time="08:00",
            end_time="10:00",
            days_mask=0b0011111,
            enabled=True,
        )
    window = MainWindow(context)
    qtbot.addWidget(window)
    window._activate("Scheduler")
    page = window._pages["Scheduler"]

    class _FakeScheduler:
        def is_active(self, schedule):
            return False

    page.scheduler = _FakeScheduler()
    page._refresh_statuses()

    assert page.table.item(0, 3).text() == "Weekdays"
    assert page.table.item(0, 6).text() == "Idle"


def test_scheduler_master_toggle_persists(qtbot, context):
    window = MainWindow(context)
    qtbot.addWidget(window)
    window._activate("Scheduler")
    page = window._pages["Scheduler"]
    page.enable_check.setChecked(True)
    assert context.settings.get("scheduler.enabled") is True
    page.enable_check.setChecked(False)
    assert context.settings.get("scheduler.enabled") is False


def test_scheduler_dialog_defaults(qtbot, context):
    from magnetoclip.ui.dialogs.schedule import ScheduleDialog

    dialog = ScheduleDialog()
    qtbot.addWidget(dialog)
    assert dialog.name() == ""
    assert dialog.all_day_check.isChecked()
    assert dialog.start_time() is None
    assert dialog.end_time() is None
    assert dialog.days_mask() == 0b1111111
    assert dialog.speed_day() is None
    assert dialog.enabled() is False
    dialog.name_edit.setText("  ")
    dialog._validate_and_accept()
    assert dialog.result() == 0
    dialog.name_edit.setText("Evening")
    dialog._validate_and_accept()
    assert dialog.result() == 1


def test_scheduler_dialog_prefills_for_edit(qtbot, context):
    from magnetoclip.database.repositories import ScheduleRepository
    from magnetoclip.ui.dialogs.schedule import ScheduleDialog

    with context.session_factory() as session:
        schedule = ScheduleRepository(session).add(
            "Night",
            start_time="22:00",
            end_time="06:00",
            days_mask=0b0011111,
            speed_day=2.0,
            speed_night=1.0,
            enabled=True,
        )
    dialog = ScheduleDialog(schedule=schedule)
    qtbot.addWidget(dialog)
    assert dialog.name() == "Night"
    assert dialog.all_day_check.isChecked() is False
    assert dialog.start_time() == "22:00"
    assert dialog.end_time() == "06:00"
    assert dialog.days_mask() == 0b0011111
    assert dialog.speed_day() == 2.0
    assert dialog.speed_night() == 1.0
    assert dialog.enabled() is True
    assert dialog.start_edit.isEnabled()
    assert dialog.end_edit.isEnabled()


def test_about_dialog_has_developer_info(qtbot):
    from PySide6.QtWidgets import QLabel

    from magnetoclip.ui.dialogs import about

    dialog = about.AboutDialog()
    qtbot.addWidget(dialog)
    texts = [label.text() for label in dialog.findChildren(QLabel)]
    assert "Powered by Devallix" in texts
    assert any(text.startswith("MagnetoClip is an advanced download manager") for text in texts)
    dev_path = about._developer_image()
    assert dev_path.exists()
    assert dev_path.name == "developer.png"
    assert any(
        label.pixmap() is not None and not label.pixmap().isNull()
        for label in dialog.findChildren(QLabel)
    )


def test_add_url_dialog_auto_selects_category(qtbot, context):
    dialog = AddUrlDialog(context)
    qtbot.addWidget(dialog)
    dialog.url_edit.setText("https://example.com/clip.mp4")
    assert dialog.category() == "Videos"
    dialog.url_edit.setText("https://example.com/pack.zip")
    assert dialog.category() == "Archives"
    dialog.url_edit.setText("https://example.com/photo.png")
    assert dialog.category() == "Images"
    dialog.url_edit.setText("https://www.youtube.com/watch?v=abc")
    assert dialog.category() == "Videos"
    dialog.url_edit.setText("https://soundcloud.com/artist/track")
    assert dialog.category() == "Music"
    dialog.url_edit.setText("https://pbs.twimg.com/media/XYZ?format=jpg&name=large")
    assert dialog.category() == "Images"


def test_capture_dialog_preselects_category(qtbot, context):
    from magnetoclip.ui.dialogs.capture import CaptureDialog

    dialog = CaptureDialog(
        context,
        url="https://pbs.twimg.com/media/XYZ?format=jpg&name=large",
        filename="XYZ",
        detected_type="image",
    )
    qtbot.addWidget(dialog)
    assert dialog.category() == "Images"

    dialog = CaptureDialog(
        context,
        url="https://www.youtube.com/watch?v=abc",
        filename="",
        detected_type="file",
    )
    qtbot.addWidget(dialog)
    assert dialog.category() == "Videos"


def test_download_context_menu_queued(qtbot, context):
    download = context.manager.add("https://example.com/a.mp4")
    window = MainWindow(context)
    qtbot.addWidget(window)
    window._activate("Downloads")
    page = window._pages["Downloads"]
    menu = page._build_context_menu(download.id)
    assert [action.text() for action in menu.actions() if action.text()] == [
        "Start", "Copy URL", "Remove from List",
    ]


def test_download_context_menu_completed(qtbot, context, tmp_path):
    from magnetoclip.database.models import DownloadStatus
    from magnetoclip.database.repositories import DownloadRepository

    target = tmp_path / "report.pdf"
    target.write_bytes(b"pdf-data")

    download = context.manager.add("https://example.com/report.pdf")
    with context.session_factory() as session:
        record = DownloadRepository(session).get(download.id)
        record.status = DownloadStatus.completed
        record.save_path = str(target)
        record.size_total = target.stat().st_size
        record.size_downloaded = target.stat().st_size
        session.commit()

    window = MainWindow(context)
    qtbot.addWidget(window)
    window._activate("Downloads")
    page = window._pages["Downloads"]
    assert page.table.rowCount() == 1

    menu = page._build_context_menu(download.id)
    actions = menu.actions()
    assert [a.text() for a in actions if a.text()] == [
        "Open File", "Open File Location", "Restart Download",
        "Copy URL", "Remove from List",
    ]
    assert actions[0].isEnabled()
    assert actions[1].isEnabled()


def test_download_context_menu_failed(qtbot, context):
    from magnetoclip.database.models import DownloadStatus
    from magnetoclip.database.repositories import DownloadRepository

    download = context.manager.add("https://example.com/broken.zip")
    with context.session_factory() as session:
        record = DownloadRepository(session).get(download.id)
        record.status = DownloadStatus.failed
        record.error = "404 Not Found"
        session.commit()

    window = MainWindow(context)
    qtbot.addWidget(window)
    window._activate("Downloads")
    page = window._pages["Downloads"]
    menu = page._build_context_menu(download.id)
    assert [a.text() for a in menu.actions() if a.text()] == [
        "Retry Download", "Copy URL", "Remove from List",
    ]


def test_download_context_menu_remove(qtbot, context):
    download = context.manager.add("https://example.com/a.mp4")
    context.manager.add("https://example.com/b.mp4")
    window = MainWindow(context)
    qtbot.addWidget(window)
    window._activate("Downloads")
    page = window._pages["Downloads"]
    assert page.table.rowCount() == 2

    menu = page._build_context_menu(download.id)
    remove = next(a for a in menu.actions() if a.text() == "Remove from List")
    remove.trigger()
    assert page.table.rowCount() == 1


def test_download_context_menu_copy_url(qtbot, context):
    from PySide6.QtWidgets import QApplication

    download = context.manager.add("https://example.com/a.mp4")
    window = MainWindow(context)
    qtbot.addWidget(window)
    window._activate("Downloads")
    page = window._pages["Downloads"]

    menu = page._build_context_menu(download.id)
    copy = next(a for a in menu.actions() if a.text() == "Copy URL")
    copy.trigger()
    assert QApplication.clipboard().text() == "https://example.com/a.mp4"


def test_tray_message_click_opens_file_location(qtbot, context, tmp_path, monkeypatch):
    import magnetoclip.ui.components.tray as tray_module

    calls = []
    monkeypatch.setattr(tray_module, "reveal_path", lambda p: calls.append(p))

    tray = tray_module.SystemTray(context)
    target = tmp_path / "clip.mp4"
    target.write_bytes(b"data")
    tray._message_download_id = 5
    monkeypatch.setattr(context.manager, "path_of", lambda download_id: target)

    tray._on_message_clicked()
    assert calls == [target]
    assert tray._message_download_id is None


def test_tray_message_click_ignored_without_download(qtbot, context, monkeypatch):
    import magnetoclip.ui.components.tray as tray_module

    calls = []
    monkeypatch.setattr(tray_module, "reveal_path", lambda p: calls.append(p))

    tray = tray_module.SystemTray(context)
    tray._message_download_id = None
    tray._on_message_clicked()
    assert calls == []


def test_tray_message_click_routes_registered_action(qtbot, context, monkeypatch):
    import magnetoclip.ui.components.tray as tray_module

    monkeypatch.setattr(tray_module, "reveal_path", lambda p: None)
    tray = tray_module.SystemTray(context)
    opened = []
    tray.register_action("detected", lambda: opened.append("detected"))
    tray.show_message("Title", "Body", download_id=None, action="detected")
    tray._message_action = "detected"
    tray._on_message_clicked()
    assert opened == ["detected"]
    assert tray._message_action is None


def test_tray_message_click_unregistered_action_opens_window(qtbot, context, monkeypatch):
    import magnetoclip.ui.components.tray as tray_module

    monkeypatch.setattr(tray_module, "reveal_path", lambda p: None)
    tray = tray_module.SystemTray(context)
    opened = []
    tray.register_action("detected", lambda: opened.append("detected"))
    tray.set_open_callback(lambda: opened.append("window"))
    tray._message_action = "other"
    tray._on_message_clicked()
    assert opened == ["window"]


def test_notifier_plays_sound_and_passes_download_id(qtbot, context, monkeypatch):
    import sys
    import types

    from magnetoclip.services.notification.notifier import Notifier

    class FakeTray:
        def __init__(self):
            self.shown = []

        def is_available(self):
            return True

        def show_message(self, title, body, download_id=None, action=None):
            self.shown.append((title, body, download_id, action))

    fake_winsound = types.ModuleType("winsound")
    beeps = []
    fake_winsound.MB_ICONASTERISK = 0x40
    fake_winsound.MessageBeep = lambda icon: beeps.append(icon)
    monkeypatch.setitem(sys.modules, "winsound", fake_winsound)

    tray = FakeTray()
    notifier = Notifier(context, tray=tray)
    notifier._on_notification(
        {
            "kind": "completed",
            "title": "clip.mp4",
            "body": "Download complete",
            "download_id": 7,
        }
    )
    assert beeps == [0x40]
    assert tray.shown == [("clip.mp4", "Download complete", 7, None)]
    notifier.close()


def test_notifier_failed_kind_no_sound(qtbot, context, monkeypatch):
    import sys
    import types

    from magnetoclip.services.notification.notifier import Notifier

    class FakeTray:
        def is_available(self):
            return True

        def show_message(self, title, body, download_id=None, action=None):
            pass

    fake_winsound = types.ModuleType("winsound")
    beeps = []
    fake_winsound.MessageBeep = lambda icon: beeps.append(icon)
    monkeypatch.setitem(sys.modules, "winsound", fake_winsound)

    notifier = Notifier(context, tray=FakeTray())
    notifier._on_notification(
        {"kind": "failed", "title": "x", "body": "Download failed"}
    )
    assert beeps == []
    notifier.close()


def test_detected_page_renders_detections(qtbot, context):
    from PySide6.QtCore import Qt

    from magnetoclip.database.repositories import BrowserDetectionRepository

    with context.session_factory() as session:
        BrowserDetectionRepository(session).add(
            "https://example.com/page",
            count=2,
            files=[
                {
                    "url": "https://example.com/a.zip",
                    "filename": "a.zip",
                    "detected_type": "archive",
                },
                {
                    "url": "https://example.com/b.pdf",
                    "filename": "b.pdf",
                    "detected_type": "document",
                },
            ],
        )

    window = MainWindow(context)
    qtbot.addWidget(window)
    window._activate("Detected")
    page = window._pages["Detected"]
    assert len(page._files) == 2
    assert page.table.rowCount() == 2
    assert page.table.item(0, 1).text() in ("a.zip", "b.pdf")

    page.table.item(0, 0).setCheckState(Qt.Checked)
    page._download_selected()
    download = next(
        (d for d in context.manager.list_snapshots() if d["filename"] in ("a.zip", "b.pdf")),
        None,
    )
    assert download is not None
    assert len(page._files) == 1
    with context.session_factory() as session:
        assert len(BrowserDetectionRepository(session).list_detections()) == 1


def test_detected_page_empty_state(qtbot, context):
    window = MainWindow(context)
    qtbot.addWidget(window)
    window._activate("Detected")
    page = window._pages["Detected"]
    assert page.table.rowCount() == 1
    assert page._empty_row == 0
    assert not page.select_all_button.isEnabled()


def test_detected_page_shows_source_page(qtbot, context):
    from magnetoclip.database.repositories import BrowserDetectionRepository

    with context.session_factory() as session:
        BrowserDetectionRepository(session).add(
            "https://www.web.telegram.org/a/b?hl=en",
            count=1,
            files=[
                {
                    "url": "blob:https://www.web.telegram.org/abc",
                    "filename": "clip.jpg",
                    "detected_type": "image",
                }
            ],
        )

    window = MainWindow(context)
    qtbot.addWidget(window)
    window._activate("Detected")
    page = window._pages["Detected"]
    assert page.table.rowCount() == 1
    page_item = page.table.item(0, 4)
    assert page_item.text() == "web.telegram.org"
    assert page_item.toolTip() == "https://www.web.telegram.org/a/b?hl=en"


def test_page_label_fallbacks():
    from magnetoclip.ui.pages.detected import _page_label

    assert _page_label("https://www.web.telegram.org/a") == "web.telegram.org"
    assert _page_label("https://web.telegram.org/a") == "web.telegram.org"
    assert _page_label("") == "—"
    assert _page_label("not a url") == "—"


def test_tray_notification_click_opens_detected_page(qtbot, context):
    window = MainWindow(context)
    qtbot.addWidget(window)
    window._activate("Downloads")
    assert window.stack.currentWidget() is window._pages["Downloads"]

    window.tray._message_action = "detected"
    window.tray._on_message_clicked()
    assert window.stack.currentWidget() is window._pages["Detected"]
    assert window.tray._message_action is None


def test_detected_page_select_all(qtbot, context):
    from PySide6.QtCore import Qt

    from magnetoclip.database.repositories import BrowserDetectionRepository

    with context.session_factory() as session:
        BrowserDetectionRepository(session).add(
            "https://example.com/page",
            count=3,
            files=[
                {"url": "https://example.com/a.zip", "filename": "a.zip", "detected_type": "archive"},
                {"url": "https://example.com/b.pdf", "filename": "b.pdf", "detected_type": "document"},
                {"url": "https://example.com/c.jpg", "filename": "c.jpg", "detected_type": "image"},
            ],
        )

    window = MainWindow(context)
    qtbot.addWidget(window)
    window._activate("Detected")
    page = window._pages["Detected"]

    assert page.select_all_button.isEnabled()
    page._toggle_select_all()
    assert page._selected_indexes() == [0, 1, 2]
    assert page.download_button.isEnabled()

    page._toggle_select_all()
    assert page._selected_indexes() == []
    assert not page.download_button.isEnabled()
