"""Browser integration page: enable capture, install the host and extension."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QVBoxLayout,
)

from magnetoclip.browser.service import _POLICY_KEY, SUPPORTED_BROWSERS
from magnetoclip.core.events.bus import Events

from ..components.buttons import AccentButton, DangerButton, GhostButton
from .base import Page, make_scrollable

_BROWSER_LABELS = {
    "chrome": "Google Chrome",
    "edge": "Microsoft Edge",
    "firefox": "Firefox",
    "brave": "Brave",
    "vivaldi": "Vivaldi",
    "chromium": "Chromium",
}

_MANUAL_STEPS = {
    "chrome": (
        "Open Google Chrome and go to chrome://extensions.",
        "Turn on Developer mode (toggle in the top-right corner).",
        "Click Load unpacked.",
        "Select the extension folder shown above.",
        "MagnetoClip Companion appears; captures are automatic.",
    ),
    "edge": (
        "Open Microsoft Edge and go to edge://extensions.",
        "Turn on Developer mode (toggle in the bottom-left).",
        "Click Load unpacked.",
        "Select the extension folder shown above.",
        "MagnetoClip Companion appears; captures are automatic.",
    ),
    "firefox": (
        "Open Firefox and go to about:debugging#/runtime/this-firefox.",
        "Click Load Temporary Add-on.",
        "Select the manifest.json file inside the extension folder above.",
        "The add-on appears; captures are automatic.",
        "Note: temporary add-ons reset when Firefox restarts, so re-load it after restarting.",
    ),
    "brave": (
        "Open Brave and go to brave://extensions.",
        "Turn on Developer mode (toggle in the top-right corner).",
        "Click Load unpacked.",
        "Select the extension folder shown above.",
        "MagnetoClip Companion appears; captures are automatic.",
    ),
    "vivaldi": (
        "Open Vivaldi and go to vivaldi://extensions.",
        "Turn on Developer mode (toggle in the top-right corner).",
        "Click Load unpacked.",
        "Select the extension folder shown above.",
        "MagnetoClip Companion appears; captures are automatic.",
    ),
    "chromium": (
        "Open Chromium and go to chrome://extensions.",
        "Turn on Developer mode (toggle in the top-right corner).",
        "Click Load unpacked.",
        "Select the extension folder shown above.",
        "MagnetoClip Companion appears; captures are automatic.",
    ),
}


class BrowserPage(Page):
    def __init__(self, context, parent=None) -> None:
        super().__init__(context, parent)
        self._browser_controls: dict[str, dict] = {}

        layout = make_scrollable(self, margins=(24, 16, 24, 16), spacing=12)

        header = QVBoxLayout()
        header.setSpacing(2)
        title = QLabel("Browser")
        title.setObjectName("page_title")
        subtitle = QLabel("Capture downloads from your browser")
        subtitle.setObjectName("page_subtitle")
        header.addWidget(title)
        header.addWidget(subtitle)
        layout.addLayout(header)

        self.integration_check = QCheckBox("Enable browser integration")
        self.integration_check.toggled.connect(self._on_integration_toggled)
        layout.addWidget(self.integration_check)

        self.capture_check = QCheckBox("Capture media streams")
        self.capture_check.toggled.connect(self._on_capture_toggled)
        layout.addWidget(self.capture_check)

        self.default_downloader_check = QCheckBox(
            "Make MagnetoClip the default downloader "
            "(intercept every browser download)"
        )
        self.default_downloader_check.toggled.connect(self._on_default_downloader_toggled)
        layout.addWidget(self.default_downloader_check)

        layout.addWidget(self._build_browsers_card())
        layout.addWidget(self._build_extension_card())
        layout.addWidget(self._build_setup_card())

        status_row = QHBoxLayout()
        self.status_dot = QLabel("●")
        self.status_dot.setObjectName("status_network")
        self.status_label = QLabel("")
        self.status_label.setObjectName("card_caption")
        status_row.addWidget(self.status_dot)
        status_row.addWidget(self.status_label, 1)
        layout.addLayout(status_row)

        layout.addStretch(1)

        context.events.connect(Events.BROWSER_STATUS_CHANGED, lambda _: self.refresh())
        self.refresh()

    # ----- construction -----

    def _build_browsers_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 12, 16, 12)
        card_layout.setSpacing(8)

        row = QHBoxLayout()
        card_title = QLabel("Installed browsers")
        card_title.setObjectName("card_title")
        row.addWidget(card_title)
        row.addStretch(1)
        self.redetect_button = GhostButton("Re-detect browsers")
        self.redetect_button.clicked.connect(self.refresh)
        row.addWidget(self.redetect_button)
        card_layout.addLayout(row)

        self.browser_list = QVBoxLayout()
        self.browser_list.setSpacing(6)
        card_layout.addLayout(self.browser_list)
        return card

    def _build_extension_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 12, 16, 12)
        card_layout.setSpacing(8)

        title = QLabel("Browser extension")
        title.setObjectName("card_title")
        card_layout.addWidget(title)

        self.extension_path_label = QLabel("")
        self.extension_path_label.setObjectName("card_caption")
        self.extension_path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        card_layout.addWidget(self.extension_path_label)

        self.extension_id_label = QLabel("")
        self.extension_id_label.setObjectName("card_caption")
        self.extension_id_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        card_layout.addWidget(self.extension_id_label)

        self.instructions_label = QLabel(
            "Load unpacked from chrome://extensions (Developer mode) or "
            "about:debugging in Firefox."
        )
        self.instructions_label.setObjectName("card_caption")
        self.instructions_label.setWordWrap(True)
        card_layout.addWidget(self.instructions_label)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.prepare_button = AccentButton("Prepare extension")
        self.prepare_button.clicked.connect(self._prepare_extension)
        self.copy_button = GhostButton("Copy path")
        self.copy_button.clicked.connect(self._copy_extension_path)
        buttons.addWidget(self.prepare_button)
        buttons.addWidget(self.copy_button)
        buttons.addStretch(1)
        card_layout.addLayout(buttons)
        return card

    def _build_setup_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 12, 16, 12)
        card_layout.setSpacing(8)

        row = QHBoxLayout()
        title = QLabel("Manual setup")
        title.setObjectName("card_title")
        row.addWidget(title)
        row.addStretch(1)
        row.addWidget(QLabel("Browser:"))
        self.setup_combo = QComboBox()
        for name in SUPPORTED_BROWSERS:
            self.setup_combo.addItem(_BROWSER_LABELS.get(name, name), name)
        self.setup_combo.currentIndexChanged.connect(lambda _: self._update_steps())
        row.addWidget(self.setup_combo)
        card_layout.addLayout(row)

        self.steps_label = QLabel("")
        self.steps_label.setObjectName("card_caption")
        self.steps_label.setWordWrap(True)
        card_layout.addWidget(self.steps_label)

        self.auto_hint = QLabel(
            "Tip: the Auto-install button next to a browser can push the "
            "extension automatically via browser policy, but it only works "
            "after the extension is published to the Chrome Web Store. "
            "Until then, follow the steps above once per browser."
        )
        self.auto_hint.setObjectName("card_caption")
        self.auto_hint.setWordWrap(True)
        card_layout.addWidget(self.auto_hint)
        return card

    # ----- handlers -----

    def _on_integration_toggled(self, checked: bool) -> None:
        self.context.settings.set("browser.integration_enabled", checked)
        if checked:
            self._activate()
        self._persist()
        self.refresh()

    def _on_capture_toggled(self, checked: bool) -> None:
        self.context.settings.set("browser.capture_enabled", checked)
        self._persist()
        self.refresh()

    def _on_default_downloader_toggled(self, checked: bool) -> None:
        if checked:
            # The extension only runs while the integration is enabled, so
            # turning MagnetoClip into the default downloader also activates it.
            self.context.settings.set("browser.integration_enabled", True)
            self._activate()
        self.context.settings.set("browser.default_downloader", checked)
        self._persist()
        self.refresh()

    def _activate(self) -> None:
        try:
            results = self.context.browser.ensure_installed()
            failed = [browser for browser, result in results.items() if "failed" in result]
            if failed:
                QMessageBox.warning(
                    self,
                    "Browser integration",
                    f"Host install failed for: {', '.join(failed)}",
                )
        except Exception as exc:  # noqa: BLE001 - surface activation errors to the user
            QMessageBox.warning(self, "Browser integration", str(exc))

    def _prepare_extension(self) -> None:
        try:
            self.context.browser.install_extension()
        except Exception as exc:  # noqa: BLE001 - surface copy errors to the user
            QMessageBox.warning(self, "Browser extension", str(exc))
        self.refresh()

    def _copy_extension_path(self) -> None:
        from PySide6.QtWidgets import QApplication

        path = self.context.browser.extension_dir()
        QApplication.clipboard().setText(str(path))

    def _install_one(self, browser: str) -> None:
        self.context.browser.install([browser])
        self.refresh()

    def _remove_one(self, browser: str) -> None:
        self.context.browser.uninstall([browser])
        self.refresh()

    def _auto_install_one(self, browser: str) -> None:
        result = self.context.browser.force_install(browser)
        box = QMessageBox.information if result["ok"] else QMessageBox.warning
        box(self, "Auto-install", result["message"])
        self.refresh()

    def _update_steps(self) -> None:
        name = self.setup_combo.currentData()
        steps = _MANUAL_STEPS.get(name, ())
        text = "\n".join(f"{index}. {step}" for index, step in enumerate(steps, 1))
        self.steps_label.setText(text)

    def _persist(self) -> None:
        from magnetoclip.database.repositories import SettingsStore

        store = SettingsStore(self.context.session_factory)
        store.save_many(self.context.settings.to_store_dict())
        self.context.events.post(Events.SETTINGS_CHANGED, {"browser": True})

    # ----- rendering -----

    def refresh(self) -> None:
        service = getattr(self.context, "browser", None)
        status = service.status() if service is not None else None

        self.integration_check.blockSignals(True)
        self.integration_check.setChecked(
            bool(self.context.settings.get("browser.integration_enabled", False))
        )
        self.integration_check.blockSignals(False)
        self.capture_check.blockSignals(True)
        self.capture_check.setChecked(
            bool(self.context.settings.get("browser.capture_enabled", True))
        )
        self.capture_check.blockSignals(False)
        self.default_downloader_check.blockSignals(True)
        self.default_downloader_check.setChecked(
            bool(self.context.settings.get("browser.default_downloader", False))
        )
        self.default_downloader_check.blockSignals(False)

        if status is not None:
            self.extension_path_label.setText(f"Folder: {status['extension_dir']}")
            self.extension_id_label.setText(f"Extension ID: {status['extension_id']}")
            enabled = status["enabled"]
            self.status_dot.setStyleSheet("color: #34D399;" if enabled else "color: #F87171;")
            launcher = "host launcher ready" if status["launcher_exists"] else "host launcher missing"
            extension = "extension ready" if status["extension_ready"] else "extension not prepared"
            state = "enabled" if enabled else "disabled"
            self.status_label.setText(f"Integration {state} · {launcher} · {extension}")

        self._render_browsers(status)
        self._update_steps()

    def _render_browsers(self, status) -> None:
        for controls in self._browser_controls.values():
            self.browser_list.removeWidget(controls["row"])
            controls["row"].deleteLater()
        self._browser_controls.clear()

        detected = set()
        if status is not None:
            detected = {
                name for name, info in status["browsers"].items() if info["installed"]
            }
        if not detected:
            caption = QLabel("No supported browsers detected on this system.")
            caption.setObjectName("card_caption")
            self.browser_list.addWidget(caption)
            return

        for name in SUPPORTED_BROWSERS:
            if name not in detected:
                continue
            info = status["browsers"][name]
            row = QFrame()
            row.setObjectName("stat_card")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(12, 8, 12, 8)

            label = QLabel(_BROWSER_LABELS.get(name, name))
            label.setObjectName("card_title")
            row_layout.addWidget(label)

            state = QLabel("Host installed" if info["manifest"] else "Host not installed")
            state.setObjectName("card_caption")
            row_layout.addWidget(state)

            row_layout.addStretch(1)

            if name in _POLICY_KEY:
                auto = GhostButton("Auto-install")
                auto.setToolTip(
                    "Push the extension via browser policy (requires the "
                    "extension to be published to the Chrome Web Store)."
                )
                auto.setEnabled(not info.get("force_installed", False))
                auto.setText(
                    "Auto-installed" if info.get("force_installed", False) else "Auto-install"
                )
                auto.clicked.connect(lambda _=False, b=name: self._auto_install_one(b))
                row_layout.addWidget(auto)
            install = AccentButton("Install host")
            remove = DangerButton("Remove host")
            install.clicked.connect(lambda _=False, b=name: self._install_one(b))
            remove.clicked.connect(lambda _=False, b=name: self._remove_one(b))
            install.setEnabled(not info["manifest"])
            remove.setEnabled(info["manifest"])
            row_layout.addWidget(install)
            row_layout.addWidget(remove)

            self.browser_list.addWidget(row)
            controls: dict = {
                "row": row,
                "state": state,
                "install": install,
                "remove": remove,
            }
            if name in _POLICY_KEY:
                controls["auto"] = auto
            self._browser_controls[name] = controls
