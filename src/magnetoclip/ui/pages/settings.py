"""Settings page: edit and persist application settings."""

from __future__ import annotations

import asyncio
from pathlib import Path

from PySide6.QtCore import QTimer
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
        """Check for updates asynchronously."""
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

        from magnetoclip.services.updates import UpdateChecker

        checker = UpdateChecker(endpoint)

        async def _do_check() -> None:
            try:
                result = await checker.check(__version__)
                from datetime import UTC, datetime

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

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_do_check())
        except RuntimeError:
            asyncio.run(_do_check())

    def _download_update(self) -> None:
        """Download the update asynchronously."""
        if not self._pending_update_info:
            return

        self.download_updates_button.setEnabled(False)
        self.download_updates_button.setText("Downloading…")
        self.check_updates_button.setEnabled(False)
        self.updates_progress.setVisible(True)
        self.updates_progress_label.setVisible(True)
        self.updates_progress.setValue(0)
        self.updates_progress_label.setText("Starting download…")

        from magnetoclip.services.updates import UpdateDownloader

        downloader = UpdateDownloader()

        def _on_progress(progress) -> None:
            self.updates_progress.setValue(int(progress.percent))
            if progress.total_bytes > 0:
                downloaded_mb = progress.bytes_downloaded / (1024 * 1024)
                total_mb = progress.total_bytes / (1024 * 1024)
                self.updates_progress_label.setText(
                    f"{downloaded_mb:.1f} MB / {total_mb:.1f} MB"
                )

        async def _do_download() -> None:
            try:
                result = await downloader.download(
                    self._pending_update_info,
                    on_progress=_on_progress,
                )
                if result:
                    self._downloaded_installer = result
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

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_do_download())
        except RuntimeError:
            asyncio.run(_do_download())

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
            "MagnetoClip will close and the installer will launch.\n\n"
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
