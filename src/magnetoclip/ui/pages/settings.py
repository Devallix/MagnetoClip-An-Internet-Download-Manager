"""Settings page: edit and persist application settings."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from magnetoclip.core.events.bus import Events
from magnetoclip.version import __version__

from ..components.buttons import AccentButton
from ..dialogs.proxy import ProxyDialog
from .base import Page, make_scrollable


# ── helpers ──────────────────────────────────────────────────────────────────


def _card(title: str) -> tuple[QFrame, QVBoxLayout]:
    """Return (frame, inner layout) styled as a section card."""
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(16, 14, 16, 14)
    layout.setSpacing(10)
    lbl = QLabel(title)
    lbl.setObjectName("card_title")
    layout.addWidget(lbl)
    return frame, layout


def _check(text: str, tooltip: str = "") -> QCheckBox:
    cb = QCheckBox(text)
    if tooltip:
        cb.setToolTip(tooltip)
    return cb


def _spin(lo: int, hi: int, suffix: str = "", tooltip: str = "") -> QSpinBox:
    s = QSpinBox()
    s.setRange(lo, hi)
    if suffix:
        s.setSuffix(" " + suffix)
    if tooltip:
        s.setToolTip(tooltip)
    return s


def _dspin(lo: float, hi: float, suffix: str = "") -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    if suffix:
        s.setSuffix(" " + suffix)
    return s


def _browse_btn(slot, tooltip: str = "Choose folder") -> QPushButton:
    b = QPushButton("Browse…")
    b.setToolTip(tooltip)
    b.setFixedWidth(90)
    b.clicked.connect(slot)
    return b


# ── background workers ───────────────────────────────────────────────────────


class _UpdateCheckWorker(QThread):
    finished = Signal(object)

    def __init__(self, endpoint: str, current_version: str, parent=None) -> None:
        super().__init__(parent)
        self._endpoint = endpoint
        self._current_version = current_version

    def run(self) -> None:
        from magnetoclip.services.updates import UpdateChecker

        self.finished.emit(
            UpdateChecker(self._endpoint).check_sync(self._current_version)
        )


class _UpdateDownloadWorker(QThread):
    progress = Signal(object)
    finished = Signal(object)

    def __init__(self, update_info, parent=None) -> None:
        super().__init__(parent)
        self._update_info = update_info
        self._downloader = None

    def run(self) -> None:
        from magnetoclip.services.updates import UpdateDownloader

        self._downloader = UpdateDownloader()
        self.finished.emit(
            self._downloader.download_sync(
                self._update_info, on_progress=lambda p: self.progress.emit(p)
            )
        )


# ── settings page ────────────────────────────────────────────────────────────


class SettingsPage(Page):
    def __init__(self, context, parent=None) -> None:
        super().__init__(context, parent)

        layout = make_scrollable(self)

        # ── page header ──────────────────────────────────────────────────────
        header = QVBoxLayout()
        title = QLabel("Settings")
        title.setObjectName("page_title")
        subtitle = QLabel("Tune MagnetoClip to your workflow")
        subtitle.setObjectName("page_subtitle")
        header.addWidget(title)
        header.addWidget(subtitle)
        layout.addLayout(header)

        # ── section: General ─────────────────────────────────────────────────
        card, cl = _card("General")
        grid = QGridLayout()
        grid.setSpacing(12)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)

        grid.addWidget(QLabel("Theme"), 0, 0)
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["dark", "light"])
        self.theme_combo.setFixedWidth(120)
        grid.addWidget(self.theme_combo, 0, 1)

        grid.addWidget(QLabel("Default download folder"), 0, 2)
        self.directory_edit = QLineEdit()
        grid.addWidget(self.directory_edit, 0, 3)
        grid.addWidget(_browse_btn(self._browse_directory), 0, 4)

        self.startup_check = _check("Start with system")
        self.auto_categorize_check = _check("Auto-categorize by file type")
        grid.addWidget(self.startup_check, 1, 0, 1, 2)
        grid.addWidget(self.auto_categorize_check, 1, 2, 1, 3)

        cl.addLayout(grid)
        layout.addWidget(card)

        # ── section: Downloads ───────────────────────────────────────────────
        card, cl = _card("Downloads")
        grid = QGridLayout()
        grid.setSpacing(12)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)

        grid.addWidget(QLabel("Simultaneous downloads"), 0, 0)
        self.simultaneous_spin = _spin(1, 32)
        grid.addWidget(self.simultaneous_spin, 0, 1)

        grid.addWidget(QLabel("Connections per download"), 0, 2)
        self.connections_spin = _spin(1, 64)
        grid.addWidget(self.connections_spin, 0, 3)

        grid.addWidget(QLabel("Max bandwidth (0 = unlimited)"), 1, 0)
        self.bandwidth_spin = _dspin(0, 100000, "MB/s")
        grid.addWidget(self.bandwidth_spin, 1, 1)

        grid.addWidget(QLabel("Timeout"), 1, 2)
        self.timeout_spin = _spin(5, 300, "s")
        grid.addWidget(self.timeout_spin, 1, 3)

        grid.addWidget(QLabel("Max retries"), 2, 0)
        self.retry_spin = _spin(0, 20)
        grid.addWidget(self.retry_spin, 2, 1)

        cl.addLayout(grid)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(6)
        self.user_agent_edit = QLineEdit()
        self.user_agent_edit.setPlaceholderText("Keep default if unsure")
        form.addRow("User agent", self.user_agent_edit)
        cl.addLayout(form)

        layout.addWidget(card)

        # ── section: Browser & Capture ───────────────────────────────────────
        card, cl = _card("Browser & Capture")
        self.confirm_capture_check = _check(
            "Show a confirmation dialog before downloading browser captures"
        )
        self.skip_all_check = _check(
            "Skip all detected files without asking",
            "Turns the confirmation dialog back on when unchecked",
        )
        self.notify_downloadable_check = _check(
            "Notify me when downloadable files are found on a page"
        )
        cl.addWidget(self.confirm_capture_check)
        cl.addWidget(self.skip_all_check)
        cl.addWidget(self.notify_downloadable_check)

        quality_row = QHBoxLayout()
        quality_row.setSpacing(8)
        quality_row.addWidget(QLabel("Streaming quality"))
        self.streaming_quality_combo = QComboBox()
        self.streaming_quality_combo.setFixedWidth(120)
        self.streaming_quality_combo.addItem("Best", "best")
        self.streaming_quality_combo.addItem("1080p", "1080")
        self.streaming_quality_combo.addItem("720p", "720")
        self.streaming_quality_combo.addItem("Audio only", "audio")
        quality_row.addWidget(self.streaming_quality_combo)
        quality_row.addStretch(1)
        cl.addLayout(quality_row)

        layout.addWidget(card)

        # ── section: Torrents ────────────────────────────────────────────────
        card, cl = _card("Torrents")
        grid = QGridLayout()
        grid.setSpacing(10)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)

        row = 0
        grid.addWidget(QLabel("Protocol"), row, 0, 1, 2)
        grid.addWidget(QLabel("Performance"), row, 2, 1, 2)
        row += 1

        self.torrent_enable_dht_check = _check("Enable DHT (Distributed Hash Table)")
        self.torrent_enable_pex_check = _check("Enable PEX (Peer Exchange)")
        self.torrent_enable_encryption_check = _check("Enable encryption")
        grid.addWidget(self.torrent_enable_dht_check, row, 0, 1, 2)
        grid.addWidget(QLabel("Listen port"), row, 2)
        self.torrent_listen_port_spin = _spin(1024, 65535)
        grid.addWidget(self.torrent_listen_port_spin, row, 3)
        row += 1

        grid.addWidget(self.torrent_enable_pex_check, row, 0, 1, 2)
        grid.addWidget(QLabel("Max connections"), row, 2)
        self.torrent_max_connections_spin = _spin(10, 1000)
        grid.addWidget(self.torrent_max_connections_spin, row, 3)
        row += 1

        grid.addWidget(self.torrent_enable_encryption_check, row, 0, 1, 2)
        grid.addWidget(QLabel("Max upload slots"), row, 2)
        self.torrent_max_uploads_spin = _spin(1, 100)
        grid.addWidget(self.torrent_max_uploads_spin, row, 3)
        row += 1

        grid.addWidget(QLabel("Queue"), row, 0, 1, 2)
        grid.addWidget(QLabel("Active torrents"), row, 2)
        self.torrent_max_active_torrents_spin = _spin(1, 100)
        self.torrent_max_active_torrents_spin.setToolTip(
            "How many unfinished torrents may hold a place in the queue"
        )
        grid.addWidget(self.torrent_max_active_torrents_spin, row, 3)
        row += 1

        self.torrent_default_sequential_check = _check("Sequential download")
        self.torrent_auto_seed_check = _check("Auto-seed after completion")
        grid.addWidget(self.torrent_default_sequential_check, row, 0, 1, 2)
        grid.addWidget(QLabel("Active downloads"), row, 2)
        self.torrent_max_active_downloads_spin = _spin(1, 100)
        self.torrent_max_active_downloads_spin.setToolTip(
            "How many torrents may download at the same time; the rest wait in queue"
        )
        grid.addWidget(self.torrent_max_active_downloads_spin, row, 3)
        row += 1

        grid.addWidget(self.torrent_auto_seed_check, row, 0, 1, 2)
        row += 1

        # ── torrent integration sub-row ──────────────────────────────────────
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFrameShadow(QFrame.Sunken)
        cl.addLayout(grid)
        cl.addWidget(sep)

        self.torrent_file_association_check = _check(
            "Register to open .torrent files on Windows"
        )
        self.torrent_magnet_protocol_check = _check(
            "Register to handle magnet: links on Windows"
        )

        self.torrent_save_dir_edit = QLineEdit()
        self.torrent_save_dir_edit.setPlaceholderText("Default torrent save folder")
        dir_row = QHBoxLayout()
        dir_row.setSpacing(6)
        dir_row.addWidget(QLabel("Save folder"))
        dir_row.addWidget(self.torrent_save_dir_edit, 1)
        dir_row.addWidget(_browse_btn(self._browse_torrent_dir))
        assoc_row = QHBoxLayout()
        assoc_row.setSpacing(20)
        assoc_row.addWidget(self.torrent_file_association_check)
        assoc_row.addWidget(self.torrent_magnet_protocol_check)
        assoc_row.addStretch(1)
        cl.addLayout(assoc_row)
        cl.addLayout(dir_row)

        layout.addWidget(card)

        # ── section: Network (Proxy) ─────────────────────────────────────────
        card, cl = _card("Network")
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(QLabel("Default proxy"))
        self.proxy_combo = QComboBox()
        self.proxy_combo.setMinimumWidth(160)
        row.addWidget(self.proxy_combo, 1)
        manage_proxy_button = QPushButton("Manage profiles…")
        manage_proxy_button.clicked.connect(self._manage_proxies)
        row.addWidget(manage_proxy_button)
        cl.addLayout(row)
        layout.addWidget(card)

        # ── section: Updates ─────────────────────────────────────────────────
        card, cl = _card("Updates")
        self.updates_check_enabled = _check("Check for updates automatically")
        cl.addWidget(self.updates_check_enabled)

        endpoint_row = QHBoxLayout()
        endpoint_row.setSpacing(8)
        endpoint_row.addWidget(QLabel("Endpoint"))
        self.updates_endpoint_edit = QLineEdit()
        endpoint_row.addWidget(self.updates_endpoint_edit, 1)
        cl.addLayout(endpoint_row)

        status_grid = QGridLayout()
        status_grid.setSpacing(12)
        status_grid.addWidget(QLabel("Last checked"), 0, 0)
        self.updates_status_label = QLabel("Never")
        self.updates_status_label.setObjectName("card_caption")
        status_grid.addWidget(self.updates_status_label, 0, 1)
        status_grid.addWidget(QLabel("Available version"), 0, 2)
        self.updates_version_label = QLabel("")
        self.updates_version_label.setObjectName("card_caption")
        status_grid.addWidget(self.updates_version_label, 0, 3)
        cl.addLayout(status_grid)

        self.updates_progress = QProgressBar()
        self.updates_progress.setVisible(False)
        cl.addWidget(self.updates_progress)
        self.updates_progress_label = QLabel("")
        self.updates_progress_label.setObjectName("card_caption")
        self.updates_progress_label.setVisible(False)
        cl.addWidget(self.updates_progress_label)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self.check_updates_button = QPushButton("Check Now")
        self.check_updates_button.clicked.connect(self._check_for_updates)
        btn_row.addWidget(self.check_updates_button)
        self.download_updates_button = QPushButton("Download")
        self.download_updates_button.setVisible(False)
        self.download_updates_button.clicked.connect(self._download_update)
        btn_row.addWidget(self.download_updates_button)
        self.install_updates_button = QPushButton("Install")
        self.install_updates_button.setVisible(False)
        self.install_updates_button.clicked.connect(self._install_update)
        btn_row.addWidget(self.install_updates_button)
        btn_row.addStretch(1)
        cl.addLayout(btn_row)
        layout.addWidget(card)

        # ── section: Remote Control ──────────────────────────────────────────
        card, cl = _card("Remote Control")
        self.remote_enabled_check = _check(
            "Enable remote dashboard (local network only)",
            "Control MagnetoClip from your phone or another browser on the "
            "same Wi-Fi/LAN. The dashboard never exposes your filesystem.",
        )
        cl.addWidget(self.remote_enabled_check)

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(QLabel("Dashboard port"))
        self.remote_port_spin = _spin(1024, 65535)
        self.remote_port_spin.setFixedWidth(90)
        row.addWidget(self.remote_port_spin)
        row.addSpacing(16)
        self.remote_status_label = QLabel("Off")
        self.remote_status_label.setObjectName("card_caption")
        row.addWidget(self.remote_status_label)
        row.addStretch(1)
        cl.addLayout(row)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self.remote_qr_button = QPushButton("Show QR…")
        self.remote_qr_button.clicked.connect(self._show_remote_pairing)
        btn_row.addWidget(self.remote_qr_button)
        self.remote_regen_button = QPushButton("Regenerate Token")
        self.remote_regen_button.clicked.connect(self._regenerate_remote_token)
        btn_row.addWidget(self.remote_regen_button)
        btn_row.addStretch(1)
        cl.addLayout(btn_row)
        layout.addWidget(card)

        # ── section: License ─────────────────────────────────────────────────
        card, cl = _card("License")
        row = QHBoxLayout()
        row.setSpacing(24)
        lbl_serial = QLabel("Serial")
        lbl_serial.setObjectName("card_caption")
        self.license_serial_label = QLabel("Not activated")
        self.license_serial_label.setObjectName("card_caption")
        lbl_last = QLabel("Last verified")
        lbl_last.setObjectName("card_caption")
        self.license_validated_label = QLabel("Never")
        self.license_validated_label.setObjectName("card_caption")
        pair1 = QVBoxLayout()
        pair1.addWidget(lbl_serial)
        pair1.addWidget(self.license_serial_label)
        pair2 = QVBoxLayout()
        pair2.addWidget(lbl_last)
        pair2.addWidget(self.license_validated_label)
        row.addLayout(pair1, 2)
        row.addLayout(pair2, 2)
        row.addStretch(1)
        self.license_deactivate_button = QPushButton("Deactivate this PC…")
        self.license_deactivate_button.clicked.connect(self._deactivate_license)
        row.addWidget(self.license_deactivate_button)
        cl.addLayout(row)
        layout.addWidget(card)

        # ── save row ─────────────────────────────────────────────────────────
        save_row = QHBoxLayout()
        save_row.setContentsMargins(0, 8, 0, 0)
        self.save_button = AccentButton("Save Settings")
        self.save_button.clicked.connect(self.save)
        save_row.addWidget(self.save_button)
        self.save_feedback = QLabel("Settings saved")
        self.save_feedback.setObjectName("save_feedback")
        self.save_feedback.hide()
        save_row.addWidget(self.save_feedback)
        save_row.addStretch(1)
        layout.addLayout(save_row)
        layout.addStretch(1)

        # ── timers / state ───────────────────────────────────────────────────
        self._feedback_timer = QTimer(self)
        self._feedback_timer.setSingleShot(True)
        self._feedback_timer.setInterval(2500)
        self._feedback_timer.timeout.connect(self.save_feedback.hide)
        self._pending_update_info = None
        self._downloaded_installer = None

        self._load_values()

    # ── value load / persist ─────────────────────────────────────────────────

    def _load_values(self) -> None:
        s = self.context.settings
        self.simultaneous_spin.setValue(int(s.get("downloads.simultaneous", 3)))
        self.connections_spin.setValue(int(s.get("downloads.connections_per_download", 8)))
        self.bandwidth_spin.setValue(float(s.get("network.max_bandwidth_mbps", 0.0) or 0.0))
        self.user_agent_edit.setText(str(s.get("network.user_agent", "")))
        self.timeout_spin.setValue(int(s.get("network.timeout_seconds", 30)))
        self.retry_spin.setValue(int(s.get("network.retry_max", 5)))
        self.directory_edit.setText(str(s.get("downloads.default_directory", "")))
        self.theme_combo.setCurrentText(str(s.get("appearance.theme", "dark")))
        self.startup_check.setChecked(bool(s.get("general.startup", True)))
        self.auto_categorize_check.setChecked(bool(s.get("downloads.auto_categorize", True)))
        self.confirm_capture_check.setChecked(bool(s.get("browser.confirm_capture", True)))

        from magnetoclip.browser.skip import skip_all_active

        self.skip_all_check.setChecked(skip_all_active(self.context))
        self.notify_downloadable_check.setChecked(
            bool(s.get("browser.notify_downloadable", True))
        )
        index = self.streaming_quality_combo.findData(str(s.get("streaming.quality", "best")))
        self.streaming_quality_combo.setCurrentIndex(max(0, index))

        self.torrent_enable_dht_check.setChecked(bool(s.get("torrent.enable_dht", True)))
        self.torrent_enable_pex_check.setChecked(bool(s.get("torrent.enable_pex", True)))
        self.torrent_enable_encryption_check.setChecked(bool(s.get("torrent.enable_encryption", True)))
        self.torrent_listen_port_spin.setValue(int(s.get("torrent.listen_port", 6881)))
        self.torrent_max_connections_spin.setValue(int(s.get("torrent.max_connections", 200)))
        self.torrent_max_uploads_spin.setValue(int(s.get("torrent.max_uploads", 4)))
        self.torrent_max_active_torrents_spin.setValue(
            int(s.get("torrent.max_active_torrents", 5))
        )
        self.torrent_max_active_downloads_spin.setValue(
            int(s.get("torrent.max_active_downloads", 3))
        )
        self.torrent_default_sequential_check.setChecked(bool(s.get("torrent.default_sequential", False)))
        self.torrent_auto_seed_check.setChecked(bool(s.get("torrent.auto_seed", False)))
        self.torrent_save_dir_edit.setText(str(s.get("torrent.default_save_dir", "")))
        self.torrent_file_association_check.setChecked(bool(s.get("torrent.file_association", False)))
        self.torrent_magnet_protocol_check.setChecked(bool(s.get("torrent.magnet_protocol", False)))

        self._load_proxy_combo()

        self.updates_check_enabled.setChecked(bool(s.get("updates.check_enabled", True)))
        self.updates_endpoint_edit.setText(str(s.get("updates.endpoint", "")))
        last_checked = str(s.get("updates.last_checked", ""))
        self.updates_status_label.setText(last_checked or "Never")

        self.remote_enabled_check.setChecked(bool(s.get("remote.enabled", False)))
        self.remote_port_spin.setValue(int(s.get("remote.port", 8477)))
        self._refresh_remote_status()
        self._refresh_license_labels()

    def _refresh_license_labels(self) -> None:
        from magnetoclip.services.licensing.state import (
            format_masked_serial,
            last_validated_text,
            read_serial,
        )

        serial = read_serial()
        self.license_serial_label.setText(
            format_masked_serial(serial) if serial else "Not activated"
        )
        self.license_validated_label.setText(
            last_validated_text(self.context.settings)
        )
        self.license_deactivate_button.setEnabled(bool(serial))

    # ── actions ──────────────────────────────────────────────────────────────

    def _browse_directory(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Default download folder")
        if directory:
            self.directory_edit.setText(directory)

    def _browse_torrent_dir(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Default torrent save folder")
        if directory:
            self.torrent_save_dir_edit.setText(directory)

    def _load_proxy_combo(self) -> None:
        self.proxy_combo.clear()
        self.proxy_combo.addItem("Direct (no proxy)", 0)
        proxies = getattr(self.context, "proxies", None)
        if proxies is not None:
            for profile in proxies.list():
                self.proxy_combo.addItem(profile.name, profile.id)
        default_id = int(self.context.settings.get("network.default_proxy_id", 0) or 0)
        index = self.proxy_combo.findData(default_id)
        if index >= 0:
            self.proxy_combo.setCurrentIndex(index)

    def _manage_proxies(self) -> None:
        dialog = ProxyDialog(self.context, parent=self)
        dialog.exec()
        self._load_proxy_combo()

    # ── updates ──────────────────────────────────────────────────────────────

    def _check_for_updates(self) -> None:
        endpoint = self.updates_endpoint_edit.text().strip()
        if not endpoint:
            QMessageBox.warning(self, "Check for Updates", "Please enter an update endpoint URL.")
            return

        self.check_updates_button.setEnabled(False)
        self.check_updates_button.setText("Checking…")
        self.download_updates_button.setVisible(False)
        self.install_updates_button.setVisible(False)
        self._pending_update_info = None
        self._downloaded_installer = None

        self._update_worker = _UpdateCheckWorker(endpoint, __version__, self)
        self._update_worker.finished.connect(self._on_update_check_done)
        self._update_worker.start()

    def _on_update_check_done(self, result) -> None:
        from datetime import UTC, datetime

        try:
            now = datetime.now(UTC).isoformat(timespec="seconds")
            self.context.settings.set("updates.last_checked", now)
            self.updates_status_label.setText(now)

            if result.update_available:
                info = result.update_info
                self._pending_update_info = info
                self.updates_version_label.setText(result.latest_version)
                self.download_updates_button.setVisible(True)
                self.download_updates_button.setEnabled(True)
                message = f"A new version {result.latest_version} is available!"
                if info and info.release_notes:
                    message += f"\n\nRelease notes:\n{info.release_notes}"
                QMessageBox.information(self, "Update Available", message)
            elif result.error:
                self.updates_version_label.setText("")
                QMessageBox.warning(
                    self, "Check for Updates", f"Error: {result.error}"
                )
            else:
                self.updates_version_label.setText("")
                QMessageBox.information(
                    self,
                    "Check for Updates",
                    f"You are running the latest version ({__version__}).",
                )
        except Exception as exc:
            QMessageBox.warning(
                self, "Check for Updates", f"Failed to check for updates: {exc}"
            )
        finally:
            self.check_updates_button.setEnabled(True)
            self.check_updates_button.setText("Check Now")

    def _download_update(self) -> None:
        if not self._pending_update_info:
            return

        self.download_updates_button.setEnabled(False)
        self.download_updates_button.setText("Downloading…")
        self.check_updates_button.setEnabled(False)
        self.updates_progress.setVisible(True)
        self.updates_progress_label.setVisible(True)
        self.updates_progress.setValue(0)
        self.updates_progress_label.setText("Starting download…")

        self._download_worker = _UpdateDownloadWorker(self._pending_update_info, self)
        self._download_worker.progress.connect(self._on_download_progress)
        self._download_worker.finished.connect(self._on_download_done)
        self._download_worker.start()

    def _on_download_progress(self, progress) -> None:
        self.updates_progress.setValue(int(progress.percent))
        if progress.total_bytes > 0:
            downloaded_mb = progress.bytes_downloaded / (1024 * 1024)
            total_mb = progress.total_bytes / (1024 * 1024)
            self.updates_progress_label.setText(
                f"{downloaded_mb:.1f} MB / {total_mb:.1f} MB"
            )

    def _on_download_done(self, result) -> None:
        try:
            if result:
                self._downloaded_installer = str(result)
                self.install_updates_button.setVisible(True)
                self.install_updates_button.setEnabled(True)
                self.updates_progress_label.setText("Download complete!")
                QMessageBox.information(
                    self,
                    "Download Complete",
                    "Update downloaded successfully. Click 'Install' to proceed.",
                )
            else:
                self.updates_progress_label.setText("Download cancelled or failed.")
        except Exception as exc:
            self.updates_progress_label.setText(f"Error: {exc}")
        finally:
            self.download_updates_button.setEnabled(True)
            self.download_updates_button.setText("Download")
            self.check_updates_button.setEnabled(True)
            self.updates_progress.setVisible(False)

    def _install_update(self) -> None:
        if not self._downloaded_installer:
            return

        installer_path = Path(self._downloaded_installer)
        if not installer_path.is_file():
            QMessageBox.warning(
                self,
                "Install Update",
                "The installer file was not found. Please download again.",
            )
            self.install_updates_button.setVisible(False)
            self._downloaded_installer = None
            return

        reply = QMessageBox.question(
            self,
            "Install Update",
            "MagnetoClip will close, the update will be applied,\n"
            "and MagnetoClip will restart automatically.\n\n"
            "Do you want to proceed?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )

        if reply == QMessageBox.Yes:
            from PySide6.QtCore import QTimer

            from magnetoclip.services.updates import UpdateDownloader

            downloader = UpdateDownloader()
            if downloader.install(installer_path):
                # Closing the main window (not QApplication.quit()) drives the
                # graceful shutdown path in app.main: the run loop exits,
                # context.shutdown() runs, the single-instance lock is
                # released, and only then does the swap batch proceed.
                # NOTE: AppContext has no `app` attribute — calling
                # self.context.app.quit() used to raise AttributeError after
                # the batch was launched, leaving it waiting forever.
                window = self.window()
                QTimer.singleShot(300, window.close)
            else:
                QMessageBox.warning(
                    self,
                    "Install Update",
                    "Failed to launch the installer. Please run it manually:\n"
                    f"{installer_path}",
                )

    # ── remote control ───────────────────────────────────────────────────────

    def _refresh_remote_status(self) -> None:
        server = getattr(self.context, "remote", None)
        if server is not None and getattr(server, "running", False):
            self.remote_status_label.setText(f"Running on port {server.port}")
        elif bool(self.context.settings.get("remote.enabled", False)):
            self.remote_status_label.setText("Enabled (not running)")
        else:
            self.remote_status_label.setText("Off")

    def _show_remote_pairing(self) -> None:
        from ..dialogs.remote_pair import RemotePairDialog

        server = getattr(self.context, "remote", None)
        if server is None or not getattr(server, "running", False):
            QMessageBox.information(
                self,
                "Remote Control",
                "The remote dashboard is not running. Enable it and press "
                "Save Settings first.",
            )
            return
        RemotePairDialog(server, parent=self).exec()

    def _regenerate_remote_token(self) -> None:
        import secrets as _secrets

        from magnetoclip.database.repositories import SettingsStore

        token = _secrets.token_urlsafe(24)
        s = self.context.settings
        s.set("remote.token", token)
        SettingsStore(self.context.session_factory).save_many(s.to_store_dict())
        QMessageBox.information(
            self,
            "Remote Control",
            "A new pairing token was generated. Previously paired devices "
            "must pair again.",
        )

    def _schedule_remote(self, coro, done=None) -> None:
        import asyncio

        try:
            task = asyncio.ensure_future(coro)
        except RuntimeError:
            coro.close()
            return
        if done is not None:
            task.add_done_callback(lambda _: done())

    def _apply_remote_changes(self) -> None:
        import asyncio

        from ...services.remote.server import RemoteServer

        enabled = bool(self.context.settings.get("remote.enabled", False))
        port = int(self.context.settings.get("remote.port", 8477))
        server = getattr(self.context, "remote", None)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if not enabled:
            if server is not None and server.running:
                if loop is None:
                    return
                self._schedule_remote(
                    server.stop(), done=self._refresh_remote_status
                )
            self._refresh_remote_status()
            return

        if server is None:
            server = RemoteServer(self.context)
            self.context.remote = server
        server.port = port
        if server.running:
            if server.port != port:

                async def _chain() -> None:
                    await server.stop()
                    server.port = int(
                        server.context.settings.get("remote.port", server.port)
                    )
                    await server.start()
                    self._refresh_remote_status()

                self._schedule_remote(_chain())
                return
        else:
            if loop is None:
                return
            self._schedule_remote(
                server.start(), done=self._refresh_remote_status
            )
        self._refresh_remote_status()

    # ── license ──────────────────────────────────────────────────────────────

    def _deactivate_license(self) -> None:
        from magnetoclip.services.licensing.state import (
            build_client_from_settings,
            read_serial,
        )
        from magnetoclip.ui.dialogs.activation import _LicenseWorker

        serial = read_serial()
        if not serial:
            return
        answer = QMessageBox.question(
            self,
            "Deactivate License",
            "Deactivate MagnetoClip on this PC?\n\nThe serial key will be freed "
            "so it can be used on another computer.",
        )
        if answer != QMessageBox.Yes:
            return
        client = build_client_from_settings(self.context.settings)
        self._license_worker = _LicenseWorker(client, "deactivate", serial)
        self._license_worker.done.connect(
            lambda data, exc: self._on_license_deactivated(serial, exc)
        )
        self._license_worker.start()

    def _on_license_deactivated(self, serial: str, exc: Exception | None) -> None:
        from magnetoclip.services.licensing.state import clear_serial

        if isinstance(exc, Exception):
            QMessageBox.warning(
                self,
                "Deactivate License",
                f"Could not deactivate:\n{exc}\n\nThe key was NOT removed from this PC.",
            )
            return
        clear_serial()
        self._refresh_license_labels()
        QMessageBox.information(
            self,
            "Deactivate License",
            "This PC has been deactivated.\nMagnetoClip will ask for a serial key on next launch.",
        )

    # ── save ─────────────────────────────────────────────────────────────────

    def save(self) -> None:
        from magnetoclip.database.repositories import SettingsStore
        from magnetoclip.ui.themes import apply_theme

        s = self.context.settings
        s.set("downloads.simultaneous", self.simultaneous_spin.value())
        s.set("downloads.connections_per_download", self.connections_spin.value())
        s.set("network.max_bandwidth_mbps", self.bandwidth_spin.value())
        s.set("network.user_agent", self.user_agent_edit.text())
        s.set("network.timeout_seconds", self.timeout_spin.value())
        s.set("network.retry_max", self.retry_spin.value())
        s.set("downloads.default_directory", self.directory_edit.text())
        s.set("appearance.theme", self.theme_combo.currentText())
        s.set("general.startup", self.startup_check.isChecked())
        s.set("downloads.auto_categorize", self.auto_categorize_check.isChecked())
        s.set("browser.confirm_capture", self.confirm_capture_check.isChecked())
        from magnetoclip.browser.skip import disable_skip_all, enable_skip_all

        if self.skip_all_check.isChecked():
            enable_skip_all(self.context, duration=None)
        else:
            disable_skip_all(self.context)
        s.set("browser.notify_downloadable", self.notify_downloadable_check.isChecked())
        s.set(
            "streaming.quality",
            self.streaming_quality_combo.currentData() or "best",
        )

        s.set("torrent.enable_dht", self.torrent_enable_dht_check.isChecked())
        s.set("torrent.enable_pex", self.torrent_enable_pex_check.isChecked())
        s.set("torrent.enable_encryption", self.torrent_enable_encryption_check.isChecked())
        s.set("torrent.listen_port", self.torrent_listen_port_spin.value())
        s.set("torrent.max_connections", self.torrent_max_connections_spin.value())
        s.set("torrent.max_uploads", self.torrent_max_uploads_spin.value())
        s.set(
            "torrent.max_active_torrents",
            self.torrent_max_active_torrents_spin.value(),
        )
        s.set(
            "torrent.max_active_downloads",
            self.torrent_max_active_downloads_spin.value(),
        )
        s.set("torrent.default_sequential", self.torrent_default_sequential_check.isChecked())
        s.set("torrent.auto_seed", self.torrent_auto_seed_check.isChecked())
        s.set("torrent.default_save_dir", self.torrent_save_dir_edit.text())
        s.set("torrent.file_association", self.torrent_file_association_check.isChecked())

        if self.torrent_file_association_check.isChecked():
            from magnetoclip.torrent.file_association import register
            register()
        else:
            from magnetoclip.torrent.file_association import unregister
            unregister()

        s.set("torrent.magnet_protocol", self.torrent_magnet_protocol_check.isChecked())

        if self.torrent_magnet_protocol_check.isChecked():
            from magnetoclip.torrent.file_association import register_magnet
            register_magnet()
        else:
            from magnetoclip.torrent.file_association import unregister_magnet
            unregister_magnet()

        s.set("network.default_proxy_id", self.proxy_combo.currentData() or 0)

        s.set("updates.check_enabled", self.updates_check_enabled.isChecked())
        s.set("updates.endpoint", self.updates_endpoint_edit.text())

        if (
            self.remote_enabled_check.isChecked()
            and not str(s.get("remote.token", ""))
        ):
            import secrets as _secrets

            s.set("remote.token", _secrets.token_urlsafe(24))
        s.set("remote.enabled", self.remote_enabled_check.isChecked())
        s.set("remote.port", self.remote_port_spin.value())

        store = SettingsStore(self.context.session_factory)
        store.save_many(s.to_store_dict())
        self.context.events.post(Events.SETTINGS_CHANGED, s.as_dict())

        self._apply_remote_changes()

        window = self.window()
        apply_theme(window, theme=s.get("appearance.theme", "dark"))

        self.save_feedback.show()
        self._feedback_timer.start()
