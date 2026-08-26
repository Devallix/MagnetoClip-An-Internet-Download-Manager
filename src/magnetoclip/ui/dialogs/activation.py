"""License activation dialog — the strict startup gate.

Every launch verifies the stored serial against the license server before
the main window appears. No network + no valid response = no app.
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from magnetoclip.services.licensing.client import (
    BoundToOtherPC,
    Expired,
    LicenseClient,
    LicenseError,
    MachineLimitReached,
    NetworkUnavailable,
    RateLimited,
    Revoked,
    UnknownSerial,
)
from magnetoclip.services.licensing.fingerprint import machine_id
from magnetoclip.services.licensing.state import (
    build_client_from_settings,
    clear_serial,
    format_masked_serial,
    mark_validated,
    read_serial,
    store_serial,
)
from magnetoclip.services.logging.setup import get_logger

log = get_logger(__name__)


class _LicenseWorker(QThread):
    """Runs one license API call off the UI thread."""

    done = Signal(object, object)  # (data, exception)

    def __init__(self, client: LicenseClient, action: str, serial: str) -> None:
        super().__init__()
        self._client = client
        self._action = action
        self._serial = serial
        self._fingerprint = machine_id()

    def run(self) -> None:  # noqa: D102 - thread body
        call = getattr(self._client, self._action)
        try:
            data = call(self._serial, self._fingerprint)
            self.done.emit(data, None)
        except Exception as exc:  # noqa: BLE001 - surfaced in dialog
            log.warning("license_call_failed", action=self._action, error=str(exc))
            self.done.emit(None, exc)


def _friendly_error(exc: Exception) -> str:
    if isinstance(exc, NetworkUnavailable):
        return "Could not reach the license server.\nCheck your internet connection and try again."
    if isinstance(exc, Revoked):
        return "This serial key has been revoked by the vendor.\nContact support if you believe this is a mistake."
    if isinstance(exc, Expired):
        return "This serial key has expired.\nRenew your license to keep using MagnetoClip."
    if isinstance(exc, (MachineLimitReached, BoundToOtherPC)):
        where = getattr(exc, "extra", {}).get("bound_hostname", "")
        suffix = f" ({where})" if where else ""
        return (
            "This serial is already active on another PC"
            + suffix
            + ".\nDeactivate it there first, or contact support to reset it."
        )
    if isinstance(exc, UnknownSerial):
        return "That serial was not recognized.\nDouble-check the key and try again."
    if isinstance(exc, RateLimited):
        return "Too many attempts — please wait a minute and retry."
    detail = getattr(exc, "code", "") or str(exc)
    return f"Activation failed ({detail}).\nPlease try again."


class ActivationDialog(QDialog):
    """Blocks startup until a valid license check passes."""

    def __init__(self, context, parent=None) -> None:
        super().__init__(parent)
        self._context = context
        self._worker: _LicenseWorker | None = None
        self.setWindowTitle("Activate MagnetoClip")
        self.setModal(True)
        self.setFixedWidth(440)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        title = QLabel("MagnetoClip Licensing")
        title.setObjectName("about_title")
        title.setAlignment(Qt.AlignHCenter)
        layout.addWidget(title)

        self._status_label = QLabel()
        self._status_label.setObjectName("about_description")
        self._status_label.setWordWrap(True)
        self._status_label.setAlignment(Qt.AlignHCenter)
        layout.addWidget(self._status_label)

        self._serial_edit = QLineEdit()
        self._serial_edit.setPlaceholderText("MGCL-XXXXX-XXXXX-XXXXX-XXXXX")
        self._serial_edit.setAlignment(Qt.AlignHCenter)
        self._serial_edit.setMinimumHeight(34)
        font = self._serial_edit.font()
        from PySide6.QtGui import QFontDatabase

        mono = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        self._serial_edit.setFont(mono)
        layout.addWidget(self._serial_edit)

        buttons_row = QHBoxLayout()
        self._primary_btn = QPushButton("Activate")
        self._primary_btn.setDefault(True)
        self._quit_btn = QPushButton("Quit")
        buttons_row.addWidget(self._quit_btn)
        buttons_row.addWidget(self._primary_btn)
        layout.addLayout(buttons_row)

        self._primary_btn.clicked.connect(self._on_primary)
        self._quit_btn.clicked.connect(self.reject)
        self._serial_edit.returnPressed.connect(self._on_primary)

        stored = read_serial()
        if stored:
            self._begin(stored, auto=True)
        else:
            self._mode_enter()

    # ---- state transitions -------------------------------------------------

    def _mode_enter(self) -> None:
        self._primary_btn.setText("Activate")
        self._primary_btn.setEnabled(True)
        self._serial_edit.setEnabled(True)
        self._status_label.setText(
            "Enter the serial key you received after purchase.\n"
            f"This PC will be bound to that key.\n\nServer: {self._endpoint_host()}"
        )

    def _begin(self, serial: str, auto: bool) -> None:
        self._worker_active_serial = serial
        self._serial_edit.setText(serial)
        self._serial_edit.setEnabled(False)
        self._primary_btn.setText("Verifying…")
        self._primary_btn.setEnabled(False)
        verb = "Verifying saved license…" if auto else "Activating…"
        self._status_label.setText(verb)

        client = build_client_from_settings(self._context.settings)
        action = "validate" if auto else "activate"
        self._worker = _LicenseWorker(client, action, serial)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _endpoint_host(self) -> str:
        try:
            return str(
                self._context.settings.get("license.endpoint", "") or ""
            ).rstrip("/")
        except Exception:  # noqa: BLE001 - settings never blocks the gate UI
            return ""

    # ---- results -----------------------------------------------------------

    def _on_primary(self) -> None:
        raw = canonical_input(self._serial_edit.text())
        if len(raw.replace("-", "")) < 10:
            self._status_label.setText("Please enter a complete serial key.")
            return
        self._begin(raw, auto=False)

    def _on_done(self, data, exc) -> None:
        self._worker = None
        if exc is None:
            store_serial(self._worker_active_serial)
            mark_validated(self._context.settings)
            log.info("license_gate_passed")
            self.accept()
            return

        if isinstance(exc, (NetworkUnavailable,)) and not self._serial_edit.isEnabled():
            # Saved-key validation failed due to connectivity: allow retry only.
            self._primary_btn.setText("Retry")
            self._primary_btn.setEnabled(True)
            self._status_label.setText(_friendly_error(exc))
            return

        self._primary_btn.setText("Retry")
        self._primary_btn.setEnabled(True)
        self._serial_edit.setEnabled(True)
        self._status_label.setText(_friendly_error(exc))


def canonical_input(text: str) -> str:
    from magnetoclip.services.licensing.client import canonical_serial_input

    return canonical_serial_input(text)


def gate_required(settings) -> bool:
    """Licensing is enforced only when a server endpoint is configured
    AND the 7-day trial has expired."""
    if os.environ.get("MCLIP_LICENSE_OFF") == "1":
        return False
    endpoint = str(settings.get("license.endpoint", "") or "").strip()
    if not endpoint:
        return False
    from magnetoclip.services.licensing.trial import is_trial_active

    if is_trial_active(settings):
        return False
    return True


def run_activation_gate(context, parent=None) -> bool:
    """Open the modal gate; True means the user may proceed."""
    if not gate_required(context.settings):
        from magnetoclip.services.licensing.trial import is_trial_active

        if is_trial_active(context.settings):
            log.info("license_gate_skipped_trial_active")
        else:
            log.info("license_gate_skipped_no_endpoint")
        return True
    dialog = ActivationDialog(context, parent=parent)
    result = dialog.exec()
    if result != QDialog.Accepted:
        log.info("license_gate_rejected_by_user")
    return result == QDialog.Accepted


__all__ = ["ActivationDialog", "run_activation_gate", "_friendly_error"]
