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
    store_machine_usage,
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
        self.setObjectName("activation_dialog")
        self.setWindowTitle("Activate MagnetoClip")
        self.setModal(True)
        self.setFixedWidth(460)

        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setContentsMargins(32, 32, 32, 32)

        title = QLabel("MagnetoClip Licensing")
        title.setObjectName("activation_title")
        title.setAlignment(Qt.AlignHCenter)
        layout.addWidget(title)

        self._status_label = QLabel()
        self._status_label.setObjectName("activation_subtitle")
        self._status_label.setWordWrap(True)
        self._status_label.setAlignment(Qt.AlignHCenter)
        layout.addWidget(self._status_label)

        self._serial_edit = QLineEdit()
        self._serial_edit.setObjectName("serial_input")
        self._serial_edit.setPlaceholderText("MGCL-XXXXX-XXXXX-XXXXX-XXXXX")
        self._serial_edit.setAlignment(Qt.AlignHCenter)
        self._serial_edit.setMinimumHeight(40)
        from PySide6.QtGui import QFontDatabase

        mono = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        self._serial_edit.setFont(mono)
        layout.addWidget(self._serial_edit)

        buttons_row = QHBoxLayout()
        buttons_row.setSpacing(12)
        self._quit_btn = QPushButton("Quit")
        self._quit_btn.setProperty("role", "activation_secondary")
        self._quit_btn.setMinimumHeight(40)
        buttons_row.addWidget(self._quit_btn)
        self._primary_btn = QPushButton("Activate")
        self._primary_btn.setObjectName("activation_primary_btn")
        self._primary_btn.setProperty("role", "activation_primary")
        self._primary_btn.setDefault(True)
        self._primary_btn.setMinimumHeight(40)
        buttons_row.addWidget(self._primary_btn)
        layout.addLayout(buttons_row)

        self._primary_btn.clicked.connect(self._on_primary)
        self._quit_btn.clicked.connect(self.reject)
        self._serial_edit.returnPressed.connect(self._on_primary)

        self._fade_in()

        stored = read_serial()
        if stored:
            self._begin(stored, auto=True)
        else:
            self._mode_enter()

    # ---- state transitions -------------------------------------------------

    def _fade_in(self) -> None:
        """Animate the dialog fading in."""
        from PySide6.QtCore import QPropertyAnimation, QEasingCurve

        self.setWindowOpacity(0.0)
        anim = QPropertyAnimation(self, b"windowOpacity")
        anim.setDuration(300)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start()
        self._fade_anim = anim

    def _mode_enter(self) -> None:
        self._primary_btn.setText("Activate")
        self._primary_btn.setEnabled(True)
        self._serial_edit.setEnabled(True)
        self._status_label.setText(
            "Enter the serial key you received after purchase.\n"
            f"This PC will be bound to that key."
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
            if isinstance(data, dict):
                max_m = data.get("max_machines", 1)
                used = data.get("machines_used", 1)
                store_machine_usage(
                    self._context.settings, max_m, used,
                    getattr(self._context, "session_factory", None),
                )
                if max_m > 1:
                    self._status_label.setText(
                        f"Activated — this key: {used} of {max_m} PCs in use"
                    )
                    from PySide6.QtCore import QTimer
                    QTimer.singleShot(1500, self.accept)
                    return
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
    if os.environ.get("MCLIP_LICENSE_OFF") == "1":
        return True
    endpoint = str(context.settings.get("license.endpoint", "") or "").strip()
    if not endpoint:
        log.info("license_gate_skipped_no_endpoint")
        return True

    from magnetoclip.services.licensing.trial import (
        is_trial_active,
        trial_days_remaining,
    )

    stored = read_serial()
    if stored:
        log.info("license_gate_found_stored_serial")
        dialog = ActivationDialog(context, parent=parent)
        result = dialog.exec()
        if result != QDialog.Accepted:
            log.info("license_gate_rejected_by_user")
        return result == QDialog.Accepted

    if is_trial_active(context.settings):
        days = trial_days_remaining(context.settings)
        _show_trial_info(context, parent, days)
        return True

    dialog = ActivationDialog(context, parent=parent)
    result = dialog.exec()
    if result != QDialog.Accepted:
        log.info("license_gate_rejected_by_user")
    return result == QDialog.Accepted


def _show_trial_info(context, parent, days: int) -> None:
    """Trial dialog — shows remaining days and allows entering a serial key."""
    from PySide6.QtCore import QEasingCurve, QPropertyAnimation

    dlg = QDialog(parent)
    dlg.setObjectName("activation_dialog")
    dlg.setWindowTitle("MagnetoClip")
    dlg.setModal(True)
    dlg.setFixedWidth(460)

    layout = QVBoxLayout(dlg)
    layout.setSpacing(16)
    layout.setContentsMargins(32, 32, 32, 32)

    title = QLabel("MagnetoClip Licensing")
    title.setObjectName("activation_title")
    title.setAlignment(Qt.AlignHCenter)
    layout.addWidget(title)

    status = QLabel(
        f"Your 7-day trial is active.\n"
        f"{days} day{'s' if days != 1 else ''} remaining"
    )
    status.setObjectName("activation_subtitle")
    status.setWordWrap(True)
    status.setAlignment(Qt.AlignHCenter)
    layout.addWidget(status)

    serial_edit = QLineEdit()
    serial_edit.setObjectName("serial_input")
    serial_edit.setPlaceholderText("MGCL-XXXXX-XXXXX-XXXXX-XXXXX")
    serial_edit.setAlignment(Qt.AlignHCenter)
    serial_edit.setMinimumHeight(40)
    from PySide6.QtGui import QFontDatabase

    mono = QFontDatabase.systemFont(QFontDatabase.FixedFont)
    serial_edit.setFont(mono)
    layout.addWidget(serial_edit)

    worker_holder: list = [None]

    def on_activate():
        from magnetoclip.services.licensing.client import canonical_serial_input

        raw = canonical_serial_input(serial_edit.text())
        if len(raw.replace("-", "")) < 10:
            status.setText("Please enter a complete serial key.")
            return
        activate_btn.setEnabled(False)
        serial_edit.setEnabled(False)
        status.setText("Activating\u2026")
        client = build_client_from_settings(context.settings)
        worker = _LicenseWorker(client, "activate", raw)
        worker_holder[0] = worker

        def on_done(data, exc):
            worker_holder[0] = None
            if exc is None:
                from magnetoclip.services.licensing.state import (
                    mark_validated,
                    store_serial,
                )

                store_serial(raw)
                mark_validated(context.settings)
                status.setText("Activated! Opening MagnetoClip\u2026")
                from PySide6.QtCore import QTimer

                QTimer.singleShot(800, dlg.accept)
                return
            activate_btn.setEnabled(True)
            serial_edit.setEnabled(True)
            status.setText(_friendly_error(exc))

        worker.done.connect(on_done)
        worker.start()

    buttons_row = QHBoxLayout()
    buttons_row.setSpacing(12)
    quit_btn = QPushButton("Continue Trial")
    quit_btn.setProperty("role", "activation_secondary")
    quit_btn.setMinimumHeight(40)
    quit_btn.clicked.connect(dlg.reject)
    buttons_row.addWidget(quit_btn)
    activate_btn = QPushButton("Activate")
    activate_btn.setProperty("role", "activation_primary")
    activate_btn.setDefault(True)
    activate_btn.setMinimumHeight(40)
    activate_btn.clicked.connect(on_activate)
    buttons_row.addWidget(activate_btn)
    layout.addLayout(buttons_row)

    dlg.setWindowOpacity(0.0)
    anim = QPropertyAnimation(dlg, b"windowOpacity")
    anim.setDuration(300)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(QEasingCurve.OutCubic)
    anim.start()
    dlg._fade_anim = anim

    dlg.exec()


__all__ = ["ActivationDialog", "run_activation_gate", "_friendly_error"]
