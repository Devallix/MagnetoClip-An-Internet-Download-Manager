"""MagnetoClip main window: top icon toolbar, category sidebar, page stack."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QSize, Qt, QTimer
from PySide6.QtGui import QPixmap, QResizeEvent
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QScrollArea,
    QStackedWidget,
    QStatusBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from magnetoclip.core.events.bus import Events
from magnetoclip.resources import app_icon, resource_path
from magnetoclip.services.logging.setup import get_logger
from magnetoclip.version import __version__

from .categories import CATEGORY_LABELS, CATEGORY_ORDER, detected_type_category
from .components.buttons import CategoryButton, VerticalIconButton
from .components.capture_watcher import CaptureWatcher
from .components.completion_notifier import CompletionNotifier
from .components.icons import category_icon, nav_icon, text_icon
from .components.tray import SystemTray
from .dialogs.about import show_about
from .pages import (
    AnalyticsPage,
    BrowserPage,
    DetectedPage,
    DownloadSitesPage,
    DownloadsPage,
    OverviewPage,
    PausePage,
    SettingsPage,
    SpeedTestPage,
    TorrentsPage,
)
from .themes import apply_theme
from .util import format_speed

log = get_logger(__name__)

ACTIVE_STATUSES = {"connecting", "downloading", "retrying", "verifying"}
TERMINAL_STATUSES = {"completed", "failed", "verification_failed", "stopped"}

NAV_ITEMS = [
    ("overview", "Overview"),
    ("downloads", "Downloads"),
    ("detected", "Detected"),
    ("completed", "Completed"),
    ("torrents", "Torrents"),
    ("analytics", "Analytics"),
    ("pause", "Pause"),
    ("speedtest", "Speed"),
    ("browser", "Browser"),
    ("sites", "Sites"),
    ("settings", "Settings"),
]


class _LazyPageRegistry(dict):
    """Maps a nav label to its page widget, constructing it on first access.

    Behaves like the plain ``dict`` it replaces, so ``window._pages["Downloads"]``
    keeps working whether or not that page has been visited yet.
    """

    def __init__(self, window) -> None:
        super().__init__()
        self._window = window

    def _build(self, label: str) -> None:
        builder = self._window._page_builders.get(label)
        if builder is None:
            return
        page = builder()
        self._window.stack.addWidget(page)
        dict.__setitem__(self, label, page)

    def __getitem__(self, label):
        # Must consult the raw dict: __contains__ also reports buildable
        # labels, so `label not in self` would never be true here.
        if not dict.__contains__(self, label):
            self._build(label)
        return dict.__getitem__(self, label)

    def get(self, label, default=None):
        if not dict.__contains__(self, label):
            self._build(label)
        return dict.get(self, label, default)

    def __contains__(self, label) -> bool:
        return dict.__contains__(self, label) or label in self._window._page_builders


class MainWindow(QMainWindow):
    """Main application window shell."""

    def __init__(self, context) -> None:
        super().__init__()
        self.context = context
        self.setWindowTitle(f"MagnetoClip {__version__}")
        self.setWindowIcon(app_icon())
        self._fitting = True
        self.resize(1100, 740)
        self.setMinimumSize(720, 640)
        self._fit_to_work_area()
        self._speeds: dict[int, float] = {}
        self._active = 0
        # Sidebar counters re-read all snapshots; coalesce bursts of download
        # events (e.g. "Start all") so a bulk action doesn't re-scan the table
        # once per download.
        self._stats_timer = QTimer(self)
        self._stats_timer.setSingleShot(True)
        self._stats_timer.setInterval(400)
        self._stats_timer.timeout.connect(self._refresh_stats)

        theme = context.settings.get("appearance.theme", "dark")
        apply_theme(self, theme=theme)
        log.info("theme_applied", theme=theme)

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._build_menu_bar()
        root.addWidget(self._build_toolbar())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        self.sidebar = self._build_sidebar()
        body.addWidget(self.sidebar)

        content = QFrame()
        content.setObjectName("content")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        self.stack = QStackedWidget()
        self._build_pages(context)
        content_layout.addWidget(self.stack, 1)
        body.addWidget(content, 1)

        root.addLayout(body, 1)
        self.setCentralWidget(central)

        status = QStatusBar()
        self.setStatusBar(status)
        self.network_label = QLabel("● Connected")
        self.network_label.setObjectName("status_network")
        status.addWidget(self.network_label)

        powered = QLabel("powered by devallix")
        powered.setObjectName("muted")
        status.addPermanentWidget(powered)
        self.count_label = QLabel("")
        status.addPermanentWidget(self.count_label)
        self.speed_label = QLabel("0 B/s")
        status.addPermanentWidget(self.speed_label)

        self.trial_label = QLabel("")
        self.trial_label.setObjectName("muted")
        self.trial_label.hide()
        status.addPermanentWidget(self.trial_label)
        self._refresh_trial_label()

        self.version_label = QLabel(f"v{__version__}")
        self.version_label.setObjectName("muted")
        status.addPermanentWidget(self.version_label)

        events = context.events
        events.connect(Events.NETWORK_CHANGED, self._on_network_changed)
        events.connect(Events.SPEED_UPDATED, self._on_speed)
        events.connect(Events.DOWNLOAD_ADDED, self._on_download_event)
        events.connect(Events.DOWNLOAD_REMOVED, self._on_download_event)
        events.connect(Events.DOWNLOAD_UPDATED, self._on_download_event)
        events.connect(Events.UPDATE_AVAILABLE, self._on_update_available)
        events.connect(Events.NAVIGATE, self._on_navigate)

        self.tray = SystemTray(context, parent=self)
        self.tray.set_open_callback(self.show)
        self.tray.register_action("detected", self._open_detected)
        self.tray.set_remote_callback(self._open_remote_pairing)
        self.tray.set_license_callback(self._show_license_info)
        self.tray.show()
        notifier = getattr(context, "notifier", None)
        if notifier is not None:
            notifier.attach_tray(self.tray)
            notifier.enable_toasts()

        self._capture_watcher = CaptureWatcher(context, parent=self)
        self._capture_watcher.start()

        # In-window completion feedback: a desktop-level slide-in toast that is
        # independent of the main window (it shows even when minimized or
        # unfocused). Keep a reference so it is not garbage-collected.
        self._completion_notifier = CompletionNotifier(context, parent=self)
        if notifier is not None:
            notifier.set_app_toast_handler(
                self._completion_notifier.is_active
            )

        self._activate("Downloads")
        self._refresh_stats()

    # ----- construction -----

    def _build_menu_bar(self) -> None:
        menu_bar = self.menuBar()
        menu_bar.setNativeMenuBar(False)

        help_menu = menu_bar.addMenu("&Help")

        doc_action = help_menu.addAction("Documentation")
        doc_action.triggered.connect(
            lambda: self._show_markdown("USER_GUIDE.md", "MagnetoClip — User Guide")
        )

        eula_action = help_menu.addAction("License Agreement")
        eula_action.triggered.connect(
            lambda: self._show_markdown("EULA.md", "MagnetoClip — License Agreement")
        )

        privacy_action = help_menu.addAction("Privacy Policy")
        privacy_action.triggered.connect(
            lambda: self._show_markdown(
                "PRIVACY_POLICY.md", "MagnetoClip — Privacy Policy"
            )
        )

        tos_action = help_menu.addAction("Terms of Service")
        tos_action.triggered.connect(
            lambda: self._show_markdown(
                "TERMS_OF_SERVICE.md", "MagnetoClip — Terms of Service"
            )
        )

    def _show_markdown(self, filename: str, title: str) -> None:
        from .dialogs.markdown_viewer import MarkdownViewerDialog

        MarkdownViewerDialog(filename, title, parent=self).exec()

    def _build_toolbar(self) -> QFrame:
        toolbar = QFrame()
        toolbar.setObjectName("toolbar")
        layout = QHBoxLayout(toolbar)
        layout.setContentsMargins(16, 8, 16, 8)
        layout.setSpacing(4)

        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        self._nav_buttons: dict[str, QToolButton] = {}
        for key, label in NAV_ITEMS:
            button = VerticalIconButton(nav_icon(key), label)
            button.setObjectName("nav_button")
            button.setProperty("tint", key)
            button.setIconSize(QSize(22, 22))
            button.setCheckable(True)
            button.clicked.connect(
                lambda checked=False, k=key: self._activate_nav(k)
            )
            layout.addWidget(button)
            self._nav_group.addButton(button)
            self._nav_buttons[key] = button

        # Override the torrents button icon with the "T" glyph
        if "torrents" in self._nav_buttons:
            self._nav_buttons["torrents"].setIcon(text_icon("T"))

        if "sites" in self._nav_buttons:
            self._nav_buttons["sites"].setToolTip("Download Sites")

        layout.addStretch(1)
        about = VerticalIconButton(nav_icon("about"), "About")
        about.setObjectName("nav_button")
        about.setProperty("tint", "about")
        about.setIconSize(QSize(22, 22))
        about.clicked.connect(lambda: show_about(self))
        layout.addWidget(about)
        return toolbar

    def _build_sidebar(self) -> QFrame:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        self._sidebar_width = 280
        self._sidebar_collapsed_width = 64
        self._sidebar_expanded = True
        sidebar.setFixedWidth(self._sidebar_width)

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(6)

        self.toggle_button = QToolButton()
        self.toggle_button.setObjectName("sidebar_toggle")
        self.toggle_button.setIcon(nav_icon("menu"))
        self.toggle_button.setIconSize(QSize(20, 20))
        self.toggle_button.setToolTip("Toggle sidebar")
        self.toggle_button.setCursor(Qt.PointingHandCursor)
        self.toggle_button.clicked.connect(self._toggle_sidebar)
        layout.addWidget(self.toggle_button)
        layout.addSpacing(8)

        nav_container = QWidget()
        nav_container.setObjectName("sidebar_nav")
        nav_layout = QVBoxLayout(nav_container)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(6)

        self._all_button = CategoryButton(
            "all", "All Downloads", nav_icon("all")
        )
        self.all_counter = self._all_button.count_label
        self.all_counter.setObjectName("counter_badge")
        self.all_counter.setFixedSize(28, 28)
        self._all_button.clicked.connect(self._set_category)
        nav_layout.addWidget(self._all_button)
        nav_layout.addSpacing(8)

        self._finished_button = CategoryButton(
            "finished", "Finished", nav_icon("completed")
        )
        self.finished_counter = self._finished_button.count_label
        self.finished_counter.setObjectName("counter_badge")
        self.finished_counter.setFixedSize(28, 28)
        self._finished_button.clicked.connect(self._set_status_filter)
        nav_layout.addWidget(self._finished_button)

        self._unfinished_button = CategoryButton(
            "unfinished", "Unfinished", nav_icon("downloads")
        )
        self.unfinished_counter = self._unfinished_button.count_label
        self.unfinished_counter.setObjectName("counter_badge")
        self.unfinished_counter.setFixedSize(28, 28)
        self._unfinished_button.clicked.connect(self._set_status_filter)
        nav_layout.addWidget(self._unfinished_button)
        nav_layout.addSpacing(8)

        self._sidebar_caption = QLabel("Categories")
        self._sidebar_caption.setObjectName("page_subtitle")
        nav_layout.addWidget(self._sidebar_caption)

        self._category_buttons: dict[str, CategoryButton] = {}
        for key in CATEGORY_ORDER:
            button = CategoryButton(key, CATEGORY_LABELS[key], category_icon(key))
            button.clicked.connect(self._set_category)
            nav_layout.addWidget(button)
            self._category_buttons[key] = button

        # Override the torrent category icon with the "T" glyph
        if "torrent" in self._category_buttons:
            btn = self._category_buttons["torrent"]
            btn.icon_label.setPixmap(text_icon("T").pixmap(QSize(18, 18)))

        nav_layout.addStretch(1)

        self._nav_scroll = QScrollArea()
        self._nav_scroll.setObjectName("sidebar_nav_scroll")
        self._nav_scroll.setWidgetResizable(True)
        self._nav_scroll.setFrameShape(QFrame.NoFrame)
        self._nav_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._nav_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._nav_scroll.setWidget(nav_container)
        layout.addWidget(self._nav_scroll, 1)

        self.logo_label = QLabel()
        self.logo_label.setObjectName("sidebar_logo")
        self.logo_label.setAlignment(Qt.AlignCenter)
        img_path = resource_path("icons", "magnetoclip.png")
        pixmap = QPixmap(str(img_path))
        if not pixmap.isNull():
            self.logo_label.setPixmap(pixmap.scaled(160, 160, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        layout.addWidget(self.logo_label)

        return sidebar

    def _build_pages(self, context) -> None:
        builders = {
            "Overview": lambda: OverviewPage(context),
            "Downloads": lambda: DownloadsPage(context),
            "Detected": lambda: DetectedPage(context),
            "Completed": lambda: DownloadsPage(context, completed_only=True),
            "Torrents": lambda: TorrentsPage(context),
            "Analytics": lambda: AnalyticsPage(context),
            "Pause": lambda: PausePage(context),
            "Speed": lambda: SpeedTestPage(context),
            "Browser": lambda: BrowserPage(context),
            "Sites": lambda: DownloadSitesPage(context),
            "Settings": lambda: SettingsPage(context),
        }
        # Pages are built on first use, not up front. Each constructor loads
        # its tables synchronously, and building all eleven cost seconds of
        # frozen UI during startup. ``_pages`` keeps building on access so
        # existing ``window._pages["Downloads"]`` call sites still work.
        self._page_builders = builders
        self._pages = _LazyPageRegistry(self)

    # ----- sidebar collapse -----

    def _toggle_sidebar(self) -> None:
        self._set_sidebar_expanded(not self._sidebar_expanded)

    def _set_sidebar_expanded(self, expanded: bool) -> None:
        self._sidebar_expanded = expanded
        self.logo_label.setVisible(expanded)
        if expanded:
            self.sidebar.setMaximumWidth(self._sidebar_width)
            prop = b"minimumWidth"
            start = self.sidebar.minimumWidth()
            end = self._sidebar_width
        else:
            self.sidebar.setMinimumWidth(self._sidebar_collapsed_width)
            prop = b"maximumWidth"
            start = self.sidebar.maximumWidth()
            end = self._sidebar_collapsed_width
        animation = QPropertyAnimation(self.sidebar, prop, self)
        animation.setDuration(220)
        animation.setStartValue(start)
        animation.setEndValue(end)
        animation.setEasingCurve(QEasingCurve.OutCubic)
        animation.start(QPropertyAnimation.DeleteWhenStopped)
        self._sidebar_animation = animation
        self._set_sidebar_compact(not expanded)

    def _set_sidebar_compact(self, compact: bool) -> None:
        self._all_button.set_compact(compact)
        self._finished_button.set_compact(compact)
        self._unfinished_button.set_compact(compact)
        for button in self._category_buttons.values():
            button.set_compact(compact)
        self._sidebar_caption.setVisible(not compact)

    # ----- navigation -----

    def _open_detected(self, category: str | None = None) -> None:
        """Handle a tray notification, shell URI or extension request.

        *category* is one of the extension's six detection categories; ``None``
        (or an empty value) shows every category.
        """
        self.show()
        self.raise_()
        self.activateWindow()
        self._activate_nav("detected")
        page = self._pages.get("Detected")
        setter = getattr(page, "set_filter", None)
        if setter is not None:
            setter(category)
        watcher = getattr(self, "_capture_watcher", None)
        if watcher is not None:
            watcher.poll()

    def _on_navigate(self, payload: Any) -> None:
        """Navigate in response to a ``NAVIGATE`` event (extension, shell, tray)."""
        if not isinstance(payload, dict):
            return
        page = str(payload.get("page") or "")
        if not page:
            return
        key = self._key_for_label(page)
        if key == "overview" and page.lower() != "overview":
            key = page.lower()
        self.show()
        self.raise_()
        self.activateWindow()
        self._activate_nav(key)
        category = payload.get("category")
        if category is not None:
            widget = self._pages.get(self._label_for_key(key))
            setter = getattr(widget, "set_filter", None)
            if setter is not None:
                setter(category)


    def _activate(self, name: str) -> None:
        """Activate a page by nav name (public surface used by tests)."""
        if name not in self._pages:
            return
        self._activate_nav(self._key_for_label(name))

    def _key_for_label(self, label: str) -> str:
        for key, item in NAV_ITEMS:
            if item == label:
                return key
        return "overview"

    def _activate_nav(self, key: str) -> None:
        button = self._nav_buttons.get(key)
        if button is not None:
            button.setChecked(True)
        self._clear_sidebar_filters()
        if key == "downloads":
            self._all_button.set_active(True)
            page = self._pages["Downloads"]
            if isinstance(page, DownloadsPage):
                page.set_filter("all")

        page = self._pages[self._label_for_key(key)]
        self.stack.setCurrentWidget(page)
        refresh = getattr(page, "refresh", None)
        if refresh is not None:
            needs_refresh = getattr(page, "needs_refresh", None)
            if needs_refresh is None or needs_refresh():
                refresh()

    def _label_for_key(self, key: str) -> str:
        for item_key, label in NAV_ITEMS:
            if item_key == key:
                return label
        return "Overview"

    def _clear_sidebar_filters(self) -> None:
        self._all_button.set_active(False)
        self._finished_button.set_active(False)
        self._unfinished_button.set_active(False)
        for button in self._category_buttons.values():
            button.set_active(False)

    def _set_category(self, category: str) -> None:
        page = self._pages["Downloads"]
        if isinstance(page, DownloadsPage):
            page.set_filter(category)
        self._clear_sidebar_filters()
        if category == "all":
            self._all_button.set_active(True)
        else:
            button = self._category_buttons.get(category)
            if button is not None:
                button.set_active(True)
        self._nav_buttons["downloads"].setChecked(True)
        self.stack.setCurrentWidget(page)

    def _set_status_filter(self, kind: str) -> None:
        page = self._pages["Downloads"]
        if isinstance(page, DownloadsPage):
            page.set_status_filter(kind)
        self._clear_sidebar_filters()
        button = (
            self._finished_button if kind == "finished" else self._unfinished_button
        )
        button.set_active(True)
        self._nav_buttons["downloads"].setChecked(True)
        self.stack.setCurrentWidget(page)

    # ----- remote control -----

    def _open_remote_pairing(self) -> None:
        server = getattr(self.context, "remote", None)
        if server is None or not getattr(server, "running", False):
            return
        from .dialogs.remote_pair import RemotePairDialog

        RemotePairDialog(server, parent=self).exec()

    # ----- license -----

    def _show_license_info(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        from magnetoclip.services.licensing.state import (
            format_masked_serial,
            last_validated_text,
            read_machine_usage,
            read_serial,
        )

        serial = read_serial()
        if not serial:
            QMessageBox.information(
                self,
                "License",
                "MagnetoClip is not activated on this PC.\n"
                "Manage your license in Settings after activation.",
            )
            return
        msg = (
            f"Serial: {format_masked_serial(serial)}\n"
            f"Last verified online: {last_validated_text(self.context.settings)}\n"
        )
        max_m, used = read_machine_usage(self.context.settings)
        if max_m > 1:
            msg += f"\nMachines: {used} of {max_m} activated\n"
        msg += (
            "\nMagnetoClip verifies this license with the vendor server at every "
            "launch. Manage devices under Settings → License."
        )
        QMessageBox.information(self, "License", msg)

    # ----- status -----

    def _on_network_changed(self, payload) -> None:
        if not isinstance(payload, str):
            return  # dict payloads carry bandwidth overrides
        self.network_label.setText(f"● {payload}")

    def _on_speed(self, payload) -> None:
        if not isinstance(payload, dict):
            return
        download_id = payload.get("id")
        speed = float(payload.get("speed") or 0.0)
        if download_id is not None:
            self._speeds[download_id] = speed
            if speed <= 0:
                self._speeds.pop(download_id, None)
        self._update_speed_label()

    def _on_download_event(self, payload) -> None:
        if isinstance(payload, dict) and payload.get("id") is not None:
            self._speeds.pop(payload["id"], None)
        self._stats_timer.start()
        self._update_speed_label()

    def _on_update_available(self, payload) -> None:
        """Show notification when an update is available."""
        from PySide6.QtWidgets import QMessageBox

        if not isinstance(payload, dict):
            return

        latest_version = payload.get("latest_version", "")
        update_info = payload.get("update_info")

        message = f"A new version {latest_version} is available!"
        if update_info and update_info.release_notes:
            message += f"\n\nRelease notes:\n{update_info.release_notes}"

        reply = QMessageBox.information(
            self,
            "Update Available",
            message + "\n\nWould you like to download it now?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )

        if reply == QMessageBox.Yes:
            self._activate_nav("settings")

    def _refresh_trial_label(self) -> None:
        from magnetoclip.services.licensing.trial import (
            is_trial_active,
            trial_days_remaining,
        )

        if is_trial_active(self.context.settings):
            days = trial_days_remaining(self.context.settings)
            self.trial_label.setText(
                f"Trial: {days} day{'s' if days != 1 else ''} left"
            )
            self.trial_label.show()
        else:
            self.trial_label.hide()

    def _refresh_stats(self) -> None:
        manager = getattr(self.context, "manager", None)
        if manager is None:
            return
        stats = manager.stats()
        rows = stats["rows"]
        counts = {category: 0 for category in CATEGORY_ORDER}
        active = 0
        finished = 0
        total = 0
        for status, detected_type, count in rows:
            total += count
            if status in ACTIVE_STATUSES:
                active += count
            if status in TERMINAL_STATUSES:
                finished += count
            counts[detected_type_category(detected_type)] += count
        self.all_counter.setText(str(total))
        self.finished_counter.setText(str(finished))
        self.unfinished_counter.setText(str(total - finished))
        for category, count in counts.items():
            button = self._category_buttons.get(category)
            if button is not None:
                button.set_count(count)
        self._active = active
        self.count_label.setText(f"{active} active")
        tray = getattr(self, "tray", None)
        if tray is not None:
            tray.set_status(active, format_speed(sum(self._speeds.values())))

    def _update_speed_label(self) -> None:
        total = sum(self._speeds.values())
        self.speed_label.setText(format_speed(total))
        tray = getattr(self, "tray", None)
        if tray is not None:
            tray.set_status(self._active, format_speed(total))

    def resizeEvent(self, event: QResizeEvent) -> None:
        if not self.isVisible():
            return super().resizeEvent(event)
        width = event.size().width()
        if self._fitting:
            self._fitting = False
        else:
            if width < 880 and self._sidebar_expanded:
                self._set_sidebar_expanded(False)
            elif width > 960 and not self._sidebar_expanded:
                self._set_sidebar_expanded(True)
        super().resizeEvent(event)

    def closeEvent(self, event) -> None:
        watcher = getattr(self, "_capture_watcher", None)
        if watcher is not None:
            watcher.stop()
        server = getattr(self.context, "remote", None)
        if server is not None and getattr(server, "running", False):
            import asyncio

            try:
                asyncio.ensure_future(server.stop())
            except RuntimeError:
                pass
        super().closeEvent(event)

    def showEvent(self, event) -> None:
        self._fitting = True
        self._fit_to_work_area()
        super().showEvent(event)

    def _fit_to_work_area(self) -> None:
        """Clamp the window size and position to the screen work area so the
        status bar never hides behind the taskbar."""
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        size = self.size()
        width = max(self.minimumWidth(), min(size.width(), area.width()))
        height = max(self.minimumHeight(), min(size.height(), area.height()))
        if width != size.width() or height != size.height():
            self._fitting = True
            self.resize(width, height)
            self._fitting = False
        position = self.pos()
        x = max(area.x(), min(position.x(), area.x() + area.width() - width))
        y = max(area.y(), min(position.y(), area.y() + area.height() - height))
        if x != position.x() or y != position.y():
            self.move(x, y)
