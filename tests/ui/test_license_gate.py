"""Startup license gate: dialog behavior without touching the network."""

import pytest

from magnetoclip.app.lifecycle import build_context
from magnetoclip.services.licensing.client import UnknownSerial
from magnetoclip.ui.dialogs.activation import ActivationDialog, gate_required

SERIAL = "MGCL-ABCDE-FGHJK-LMNOP-RSTUV"


def test_gate_required_depends_on_endpoint(context, monkeypatch):
    context.settings.set("license.endpoint", "")
    assert gate_required(context.settings) is False
    monkeypatch.setenv("MCLIP_LICENSE_OFF", "1")
    context.settings.set("license.endpoint", "http://x")
    assert gate_required(context.settings) is False
    monkeypatch.delenv("MCLIP_LICENSE_OFF")
    assert gate_required(context.settings) is True


@pytest.fixture
def context(tmp_path):
    ctx = build_context(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
    )
    ctx.settings.set("license.endpoint", "http://testserver")
    return ctx


class FakeClient:
    def __init__(self, endpoint=None, public_key_b64=None, app_version=""):
        self.calls = []

    def activate(self, serial, fingerprint, **kw):
        self.calls.append(("activate", serial))
        return {"status": "ok"}

    def validate(self, serial, fingerprint):
        self.calls.append(("validate", serial))
        return {"action": "validate"}

    def deactivate(self, serial, fingerprint):
        self.calls.append(("deactivate", serial))
        return {"status": "ok"}


class FailingClient(FakeClient):
    def activate(self, serial, fingerprint, **kw):
        raise UnknownSerial()


@pytest.fixture
def licensing_mocks(monkeypatch):
    """Patch the keyring + client seams inside the dialog module."""
    import magnetoclip.ui.dialogs.activation as mod

    state = {"serial": "", "stored": [], "cleared": 0, "validated": 0}
    monkeypatch.setattr(mod, "read_serial", lambda: state["serial"])
    monkeypatch.setattr(mod, "store_serial", lambda s: state["stored"].append(s))
    monkeypatch.setattr(mod, "clear_serial", lambda: state.__setitem__("cleared", state["cleared"] + 1))
    monkeypatch.setattr(
        mod, "mark_validated", lambda s: state.__setitem__("validated", state["validated"] + 1)
    )

    def install(client):
        monkeypatch.setattr(mod, "build_client_from_settings", lambda settings: client)

    return state, install


def test_gate_shows_entry_form_without_serial(qtbot, context, licensing_mocks):
    state, _ = licensing_mocks
    dialog = ActivationDialog(context)
    qtbot.addWidget(dialog)
    assert dialog._primary_btn.text() == "Activate"
    assert dialog._serial_edit.isEnabled()
    assert "serial" in dialog._status_label.text().lower()


def test_gate_activate_success_stores_serial_and_accepts(
    qtbot, context, licensing_mocks
):
    state, install = licensing_mocks
    install(FakeClient())
    dialog = ActivationDialog(context)
    qtbot.addWidget(dialog)

    dialog._serial_edit.setText("mgcl abcde-fghjk-lmnop-rstuv")
    with qtbot.waitSignal(dialog.accepted, timeout=5000):
        dialog._primary_btn.click()

    assert state["stored"] == [SERIAL]
    assert state["validated"] == 1


def test_gate_saved_key_autovalidates_and_accepts(qtbot, context, licensing_mocks):
    state, install = licensing_mocks
    state["serial"] = SERIAL
    fake = FakeClient()
    install(fake)
    dialog = ActivationDialog(context)
    qtbot.addWidget(dialog)

    with qtbot.waitSignal(dialog.accepted, timeout=5000):
        pass

    assert fake.calls and fake.calls[0][0] == "validate"


def test_gate_bad_serial_shows_retry_not_accept(qtbot, context, licensing_mocks):
    state, install = licensing_mocks
    install(FailingClient())
    dialog = ActivationDialog(context)
    qtbot.addWidget(dialog)

    dialog._serial_edit.setText(SERIAL)
    dialog._primary_btn.click()
    qtbot.waitUntil(lambda: dialog._worker is None, timeout=5000)

    assert not dialog.result()  # never accepted
    assert dialog._primary_btn.text() == "Retry"
    assert "not recognized" in dialog._status_label.text().lower()
    assert state["stored"] == []


def test_settings_license_section_reflects_keyring(qtbot, context, monkeypatch):
    import magnetoclip.services.licensing.state as state_mod
    from magnetoclip.ui.main_window import MainWindow

    monkeypatch.setattr(state_mod, "read_serial", lambda: "")
    window = MainWindow(context)
    qtbot.addWidget(window)
    window._activate("Settings")
    page = window._pages["Settings"]

    assert page.license_serial_label.text() == "Not activated"
    assert not page.license_deactivate_button.isEnabled()

    monkeypatch.setattr(state_mod, "read_serial", lambda: SERIAL)
    page._refresh_license_labels()
    assert page.license_serial_label.text() == "MGCL-*****-*****-*****-RSTUV"
    assert page.license_deactivate_button.isEnabled()
