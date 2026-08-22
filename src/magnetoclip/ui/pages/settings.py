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


class _UpdateCheckWorker(QThread):
    """Background worker for update checks (avoids qasync task conflicts)."""

    finished = Signal(object)

    def __init__(self, endpoint: str, current_version: str, parent=None) -> None:
        super().__init__(parent)
        self._endpoint = endpoint
        self._current_version = current_version

    def run(self) -> None:
        from magnetoclip.services.updates import UpdateChecker

        checker = UpdateChecker(self._endpoint)
        result = checker.check_sync(self._current_version)
        self.finished.emit(result)


class _UpdateDownloadWorker(QThread):
    """Background worker for update downloads (avoids qasync task conflicts)."""

    progress = Signal(object)
    finished = Signal(object)

    def __init__(self, update_info, parent=None) -> None:
        super().__init__(parent)
        self._update_info = update_info
        self._downloader = None

    def run(self) -> None:
        from magnetoclip.services.updates import UpdateDownloader

        self._downloader = UpdateDownloader()
        result = self._downloader.download_sync(
            self._update_info,
            on_progress=lambda p: self.progress.emit(p),
        )
        self.finished.emit(result)


class SettingsPage(Page):
    def __init__(self, context, parent=None) -> None:
        super().__init__(context, parent)

        layout = make_scrollable(self)

        header = QVBoxLayout()
        title = QLabel("Settings")
        title.setObjectName("page_title")
        subtitle = QLabel("Tune MagnetoClip to your workflow")
        subtitle.setObjectName("page_subtitle")
        header.addWidget(title)
        header.addWidget(subtitle)
        layout.addLayout(header)

        form = QFormLayout()
        form.setSpacing(12)

        self.simultaneous_spin = QSpinBox()
        self.simultaneous_spin.setRange(1, 32)
        form.addRow("Simultaneous downloads", self.simultaneous_spin)

        self.connections_spin = QSpinBox()
        self.connections_spin.setRange(1, 64)
        form.addRow("Connections per download", self.connections_spin)

        self.bandwidth_spin = QDoubleSpinBox()
        self.bandwidth_spin.setRange(0, 100000)
        self.bandwidth_spin.setSuffix(" MB/s")
        form.addRow("Max bandwidth (0 = unlimited)", self.bandwidth_spin)

        self.user_agent_edit = QLineEdit()
        form.addRow("User agent", self.user_agent_edit)

        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(5, 300)
        self.timeout_spin.setSuffix(" s")
        form.addRow("Timeout", self.timeout_spin)

        self.retry_spin = QSpinBox()
        self.retry_spin.setRange(0, 20)
        form.addRow("Max retries", self.retry_spin)

        self.directory_edit = QLineEdit()
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_directory)
        directory_row = QHBoxLayout()
        directory_row.addWidget(self.directory_edit, 1)
        directory_row.addWidget(browse)
        form.addRow("Default download folder", directory_row)

        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["dark", "light"])
        form.addRow("Theme", self.theme_combo)

        self.startup_check = QCheckBox("Start with system")
        form.addRow("", self.startup_check)

        self.auto_categorize_check = QCheckBox("Auto-categorize by file type")
        form.addRow("", self.auto_categorize_check)

        self.confirm_capture_check = QCheckBox(
            "Show a confirmation dialog before downloading browser captures"
        )
        form.addRow("", self.confirm_capture_check)

        self.skip_all_check = QCheckBox(
            "Skip all detected files without asking (turns the confirmation dialog back on when unchecked)"
        )
        form.addRow("", self.skip_all_check)

        self.notify_downloadable_check = QCheckBox(
            "Notify me when downloadable files are found on a page"
        )
        form.addRow("", self.notify_downloadable_check)

        self.streaming_quality_combo = QComboBox()
        self.streaming_quality_combo.addItem("Best", "best")
        self.streaming_quality_combo.addItem("1080p", "1080")
        self.streaming_quality_combo.addItem("720p", "720")
        self.streaming_quality_combo.addItem("Audio only", "audio")
        form.addRow("Streaming media quality", self.streaming_quality_combo)

        form.addRow("", QLabel(""))

        torrent_label = QLabel("Torrent")
        torrent_label.setObjectName("page_subtitle")
        form.addRow(torrent_label)

        self.torrent_enable_dht_check = QCheckBox("Enable DHT (Distributed Hash Table)")
        form.addRow("", self.torrent_enable_dht_check)

        self.torrent_enable_pex_check = QCheckBox("Enable PEX (Peer Exchange)")
        form.addRow("", self.torrent_enable_pex_check)

        self.torrent_enable_encryption_check = QCheckBox("Enable encryption")
        form.addRow("", self.torrent_enable_encryption_check)

        self.torrent_listen_port_spin = QSpinBox()
        self.torrent_listen_port_spin.setRange(1024, 65535)
        form.addRow("Listen port", self.torrent_listen_port_spin)

        self.torrent_max_connections_spin = QSpinBox()
        self.torrent_max_connections_spin.setRange(10, 1000)
        form.addRow("Max connections", self.torrent_max_connections_spin)

        self.torrent_max_uploads_spin = QSpinBox()
        self.torrent_max_uploads_spin.setRange(1, 100)
        form.addRow("Max upload slots", self.torrent_max_uploads_spin)

        self.torrent_max_active_torrents_spin = QSpinBox()
        self.torrent_max_active_torrents_spin.setRange(1, 100)
        self.torrent_max_active_torrents_spin.setToolTip(
            "How many unfinished torrents may hold a place in the torrent queue."
        )
        form.addRow("Maximum number of active torrents", self.torrent_max_active_torrents_spin)

        self.torrent_max_active_downloads_spin = QSpinBox()
        self.torrent_max_active_downloads_spin.setRange(1, 100)
        self.torrent_max_active_downloads_spin.setToolTip(
            "How many torrents may download at the same time; the rest wait in queue."
        )
        form.addRow("Maximum number of active downloads", self.torrent_max_active_downloads_spin)

        self.torrent_default_sequential_check = QCheckBox("Download sequentially by default")
        form.addRow("", self.torrent_default_sequential_check)

        self.torrent_auto_seed_check = QCheckBox("Auto-seed after download completes")
        form.addRow("", self.torrent_auto_seed_check)

        self.torrent_file_association_check = QCheckBox(
            "Register MagnetoClip to open .torrent files on Windows"
        )
        form.addRow("", self.torrent_file_association_check)

        self.torrent_magnet_protocol_check = QCheckBox(
            "Register MagnetoClip to handle magnet: links on Windows"
        )
        form.addRow("", self.torrent_magnet_protocol_check)

        torrent_dir_row = QHBoxLayout()
        self.torrent_save_dir_edit = QLineEdit()
        torrent_dir_row.addWidget(self.torrent_save_dir_edit, 1)
        torrent_browse = QPushButton("Browse\u2026")
        torrent_browse.clicked.connect(self._browse_torrent_dir)
        torrent_dir_row.addWidget(torrent_browse)
        form.addRow("Default torrent save folder", torrent_dir_row)

        proxy_label = QLabel("Proxy")
        proxy_label.setObjectName("page_subtitle")
        layout.addWidget(proxy_label)

        proxy_form = QFormLayout()
        proxy_form.setSpacing(12)
        self.proxy_combo = QComboBox()
        proxy_form.addRow("Default proxy for new downloads", self.proxy_combo)
        manage_proxy_button = QPushButton("Manage proxy profiles…")
        manage_proxy_button.clicked.connect(self._manage_proxies)
        proxy_form.addRow("", manage_proxy_button)
        layout.addLayout(proxy_form)

        updates_label = QLabel("Updates")
        updates_label.setObjectName("page_subtitle")
        layout.addWidget(updates_label)

        updates_form = QFormLayout()
        updates_form.setSpacing(12)

        self.updates_check_enabled = QCheckBox("Check for updates automatically")
        updates_form.addRow("", self.updates_check_enabled)

        self.updates_endpoint_edit = QLineEdit()
        updates_form.addRow("Update endpoint", self.updates_endpoint_edit)

        self.updates_status_label = QLabel("")
        self.updates_status_label.setObjectName("updates_status")
        updates_form.addRow("Last checked", self.updates_status_label)

        self.updates_version_label = QLabel("")
        self.updates_version_label.setObjectName("updates_version")
        updates_form.addRow("Available version", self.updates_version_label)

        self.updates_progress = QProgressBar()
        self.updates_progress.setVisible(False)
        updates_form.addRow("Download progress", self.updates_progress)

        self.updates_progress_label = QLabel("")
        self.updates_progress_label.setVisible(False)
        updates_form.addRow("", self.updates_progress_label)

        buttons_row = QHBoxLayout()
        self.check_updates_button = QPushButton("Check Now")
        self.check_updates_button.clicked.connect(self._check_for_updates)
        buttons_row.addWidget(self.check_updates_button)

        self.download_updates_button = QPushButton("Download")
        self.download_updates_button.setVisible(False)
        self.download_updates_button.clicked.connect(self._download_update)
        buttons_row.addWidget(self.download_updates_button)

        self.install_updates_button = QPushButton("Install")
        self.install_updates_button.setVisible(False)
        self.install_updates_button.clicked.connect(self._install_update)
        buttons_row.addWidget(self.install_updates_button)

        buttons_row.addStretch(1)
        updates_form.addRow("", buttons_row)
        layout.addLayout(updates_form)

        self._pending_update_info = None
        self._downloaded_installer = None
        layout.addLayout(form)

        save_row = QHBoxLayout()
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

        self._feedback_timer = QTimer(self)
        self._feedback_timer.setSingleShot(True)
        self._feedback_timer.setInterval(2500)
        self._feedback_timer.timeout.connect(self.save_feedback.hide)

        self._load_values()

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
        if last_checked:
            self.updates_status_label.setText(last_checked)
        else:
            self.updates_status_label.setText("Never")

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

    def _check_for_updates(self) -> None:
        """Check for updates using a background thread."""
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
        """Handle the result from the update check worker thread."""
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
        """Download the update using a background thread."""
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
        """Handle progress updates from the download worker."""
        self.updates_progress.setValue(int(progress.percent))
        if progress.total_bytes > 0:
            downloaded_mb = progress.bytes_downloaded / (1024 * 1024)
            total_mb = progress.total_bytes / (1024 * 1024)
            self.updates_progress_label.setText(
                f"{downloaded_mb:.1f} MB / {total_mb:.1f} MB"
            )

    def _on_download_done(self, result) -> None:
        """Handle the result from the download worker thread."""
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
        """Launch the update installer."""
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
            from magnetoclip.services.updates import UpdateDownloader

            downloader = UpdateDownloader()
            if downloader.install(installer_path):
                self.context.app.quit()
            else:
                QMessageBox.warning(
                    self,
                    "Install Update",
                    "Failed to launch the installer. Please run it manually:\n"
                    f"{installer_path}",
                )

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

        store = SettingsStore(self.context.session_factory)
        store.save_many(s.to_store_dict())
        self.context.events.post(Events.SETTINGS_CHANGED, s.as_dict())

        window = self.window()
        apply_theme(window, theme=s.get("appearance.theme", "dark"))

        self.save_feedback.show()
        self._feedback_timer.start()
