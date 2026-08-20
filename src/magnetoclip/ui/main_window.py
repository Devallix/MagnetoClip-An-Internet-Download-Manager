"""MagnetoClip main window: top icon toolbar, category sidebar, page stack."""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QSize, Qt
from PySide6.QtGui import QPixmap, QResizeEvent
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QGraphicsOpacityEffect,
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

from .categories import CATEGORY_LABELS, CATEGORY_ORDER, snapshot_category
from .components.buttons import CategoryButton, VerticalIconButton
from .components.capture_watcher import CaptureWatcher
from .components.icons import category_icon, nav_icon
from .components.tray import SystemTray
from .dialogs.about import show_about
from .pages import (
    AnalyticsPage,
    BrowserPage,
    DetectedPage,
    DownloadsPage,
    OverviewPage,
    QueuePage,
    SchedulerPage,
    SettingsPage,
    TorrentsPage,
)
from .themes import apply_theme
from .util import format_speed

log = get_logger(__name__)

TERMINAL_STATUSES = {"completed", "failed", "verification_failed", "stopped"}

NAV_ITEMS = [
    ("overview", "Overview"),
    ("downloads", "Downloads"),
    ("detected", "Detected"),
    ("queue", "Queue"),
    ("completed", "Completed"),
    ("torrents", "Torrents"),
    ("scheduler", "Scheduler"),
    ("analytics", "Analytics"),
    ("browser", "Browser"),
    ("settings", "Settings"),
]


class MainWindow(QMainWindow):
    """Main application window shell."""

    def __init__(self, context) -> None:
        super().__init__()
        self.context = context
        self.setWindowTitle(f"MagnetoClip {__version__}")
        self.setWindowIcon(app_icon())
        self._fitting = True
        self.resize(1100, 640)
        self.setMinimumSize(720, 580)
        self._fit_to_work_area()
        self._speeds: dict[int, float] = {}
        self._active = 0

        theme = context.settings.get("appearance.theme", "dark")
        apply_theme(self, theme=theme)
        log.info("theme_applied", theme=theme)

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

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
        self._pages: dict[str, QWidget] = {}
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

        self.tray = SystemTray(context, parent=self)
        self.tray.set_open_callback(self.show)
        self.tray.register_action("detected", self._open_detected)
        self.tray.show()
        notifier = getattr(context, "notifier", None)
        if notifier is not None:
            notifier.attach_tray(self.tray)

        self._capture_watcher = CaptureWatcher(context, parent=self)
        self._capture_watcher.start()

        self._activate("Downloads")
        self._refresh_stats()

    # ----- construction -----

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
            "Queue": lambda: QueuePage(context),
            "Completed": lambda: DownloadsPage(context, completed_only=True),
            "Torrents": lambda: TorrentsPage(context),
            "Scheduler": lambda: SchedulerPage(context),
            "Analytics": lambda: AnalyticsPage(context),
            "Browser": lambda: BrowserPage(context),
            "Settings": lambda: SettingsPage(context),
        }
        for key, label in NAV_ITEMS:
            page = builders[label]()
            self.stack.addWidget(page)
            self._pages[label] = page

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

    # ----- page transitions -----

    def _fade_in_page(self) -> None:
        widget = self.stack.currentWidget()
        if widget is None:
            return
        effect = QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(effect)
        animation = QPropertyAnimation(effect, b"opacity", self)
        animation.setDuration(180)
        animation.setStartValue(0.0)
        animation.setEndValue(1.0)
        animation.setEasingCurve(QEasingCurve.OutCubic)
        animation.finished.connect(lambda w=widget: w.setGraphicsEffect(None))
        animation.start(QPropertyAnimation.DeleteWhenStopped)
        self._fade_animation = animation

    # ----- navigation -----

    def _open_detected(self) -> None:
        """Handle a tray notification click for detected files."""
        self.show()
        self.raise_()
        self.activateWindow()
        self._activate_nav("detected")
        watcher = getattr(self, "_capture_watcher", None)
        if watcher is not None:
            watcher.poll()

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

        self.stack.setCurrentWidget(self._pages[self._label_for_key(key)])
        page = self._pages[self._label_for_key(key)]
        refresh = getattr(page, "refresh", None)
        if refresh is not None:
            refresh()
        self._fade_in_page()

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

    # ----- status -----

    def _on_network_changed(self, payload) -> None:
        if not isinstance(payload, str):
            return  # bandwidth override payloads (e.g. from the scheduler)
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
        self._refresh_stats()
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

    def _refresh_stats(self) -> None:
        manager = getattr(self.context, "manager", None)
        if manager is None:
            return
        snapshots = manager.list_snapshots(limit=2000)
        counts = {category: 0 for category in CATEGORY_ORDER}
        active = 0
        finished = 0
        for snapshot in snapshots:
            counts[snapshot_category(snapshot)] += 1
            if snapshot.get("status") in (
                "connecting", "downloading", "retrying", "verifying",
            ):
                active += 1
            if snapshot.get("status") in TERMINAL_STATUSES:
                finished += 1
        self.all_counter.setText(str(len(snapshots)))
        self.finished_counter.setText(str(finished))
        self.unfinished_counter.setText(str(len(snapshots) - finished))
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
