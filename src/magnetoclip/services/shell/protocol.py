"""Register the ``magneto://`` URL scheme used for toast action activation."""

from __future__ import annotations

import sys
from pathlib import Path

from magnetoclip.services.logging import get_logger

log = get_logger(__name__)

_MAGNETO_PROTOCOL = "magneto"


def _command_line() -> str | None:
    """shell\\open\\command value that launches the app with the URI as %1."""
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable)}" "%1"'
    try:
        import magnetoclip

        main_py = Path(magnetoclip.__file__).resolve().parent / "app" / "main.py"
    except Exception:  # noqa: BLE001 - registration is best-effort
        main_py = None
    if main_py is None or not main_py.exists():
        return None
    return f'"{Path(sys.executable)}" "{main_py}" "%1"'


def is_magneto_registered() -> bool:
    """Check whether the ``magneto://`` scheme currently points at this app."""
    if sys.platform != "win32":
        return False
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            f"Software\\Classes\\{_MAGNETO_PROTOCOL}\\shell\\open\\command",
            0,
            winreg.KEY_READ,
        )
        value, _ = winreg.QueryValueEx(key, "")
        winreg.CloseKey(key)
        return "%1" in value and _MAGNETO_PROTOCOL in value.lower()
    except OSError:
        return False


def register_magneto_protocol() -> bool:
    """Register MagnetoClip as the ``magneto://`` protocol handler.

    Uses HKEY_CURRENT_USER so no admin rights are required, mirroring the
    torrent/magnet registration in ``torrent.file_association``.
    Returns True on success, False on failure.
    """
    if sys.platform != "win32":
        log.info("magneto_protocol_skipped", reason="not_windows")
        return False

    command = _command_line()
    if command is None:
        log.warning("magneto_protocol_failed", reason="command_line_unavailable")
        return False

    try:
        import winreg

        desc_key = winreg.CreateKey(
            winreg.HKEY_CURRENT_USER,
            f"Software\\Classes\\{_MAGNETO_PROTOCOL}",
        )
        winreg.SetValueEx(desc_key, "", 0, winreg.REG_SZ, "URL:MagnetoClip Command")
        winreg.SetValueEx(desc_key, "URL Protocol", 0, winreg.REG_SZ, "")
        winreg.CloseKey(desc_key)

        icon_key = winreg.CreateKey(
            winreg.HKEY_CURRENT_USER,
            f"Software\\Classes\\{_MAGNETO_PROTOCOL}\\DefaultIcon",
        )
        winreg.SetValueEx(
            icon_key,
            "",
            0,
            winreg.REG_SZ,
            f'"{Path(sys.executable)}",0',
        )
        winreg.CloseKey(icon_key)

        cmd_key = winreg.CreateKey(
            winreg.HKEY_CURRENT_USER,
            f"Software\\Classes\\{_MAGNETO_PROTOCOL}\\shell\\open\\command",
        )
        winreg.SetValueEx(cmd_key, "", 0, winreg.REG_SZ, command)
        winreg.CloseKey(cmd_key)

        log.info("magneto_protocol_registered", command=command)
        return True
    except OSError as exc:
        log.warning("magneto_protocol_failed", error=str(exc))
        return False