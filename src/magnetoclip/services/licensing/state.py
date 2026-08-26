"""Where the serial lives: OS keychain via keyring, plus settings glue."""

from __future__ import annotations

from magnetoclip.services.logging.setup import get_logger

log = get_logger(__name__)

KEYRING_SERVICE = "MagnetoClip"
KEYRING_ACCOUNT = "license.serial"


def read_serial() -> str:
    try:
        import keyring

        value = keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
        return str(value or "")
    except Exception as exc:  # noqa: BLE001 - missing backend must not crash
        log.warning("license_keyring_read_failed", error=str(exc))
        return ""


def store_serial(serial: str) -> None:
    try:
        import keyring

        keyring.set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, serial)
    except Exception as exc:  # noqa: BLE001
        log.warning("license_keyring_write_failed", error=str(exc))


def clear_serial() -> None:
    try:
        import keyring

        keyring.delete_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
    except Exception as exc:  # noqa: BLE001
        log.info("license_keyring_clear_skipped", error=str(exc))


def build_client_from_settings(settings):
    from ...version import __version__
    from .client import LicenseClient

    endpoint = str(settings.get("license.endpoint", "") or "").strip()
    public_key = str(settings.get("license.public_key", "") or "").strip()
    return LicenseClient(endpoint, public_key or None, app_version=__version__)


def format_masked_serial(serial: str) -> str:
    parts = [p for p in (serial or "").upper().split("-") if p]
    if len(parts) == 5:
        return f"{parts[0]}-*****-*****-*****-{parts[-1]}"
    return (serial or "").strip() or "—"


def mark_validated(settings) -> None:
    from datetime import UTC, datetime

    settings.set(
        "license.last_validated",
        datetime.now(UTC).isoformat(timespec="seconds"),
    )


def last_validated_text(settings) -> str:
    raw = str(settings.get("license.last_validated", "") or "")
    return raw.replace("T", " ") if raw else "Never"


def store_machine_usage(settings, max_machines: int, machines_used: int, session_factory=None) -> None:
    settings.set("license.max_machines", max_machines)
    settings.set("license.machines_used", machines_used)
    if session_factory is not None:
        from magnetoclip.database.repositories import SettingsStore

        store = SettingsStore(session_factory)
        store.save("license.max_machines", max_machines)
        store.save("license.machines_used", machines_used)


def read_machine_usage(settings) -> tuple[int, int]:
    max_m = int(settings.get("license.max_machines", 1) or 1)
    used = int(settings.get("license.machines_used", 1) or 1)
    return max_m, used
