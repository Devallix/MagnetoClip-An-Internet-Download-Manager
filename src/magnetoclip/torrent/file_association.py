"""Windows file association: register MagnetoClip as a .torrent / magnet handler."""

from __future__ import annotations

import sys
from pathlib import Path

from magnetoclip.services.logging.setup import get_logger

log = get_logger(__name__)

_TORRENT_PROG_ID = "MagnetoClip.Torrent"
_TORRENT_EXT = ".torrent"
_MAGNET_PROTOCOL = "magnet"
_COMPANY = "MagnetoClip"
_APP_NAME = "MagnetoClip"


def _get_exe_path() -> Path | None:
    """Return the path to the running executable (works in both dev and frozen)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable)
    return None


def is_registered() -> bool:
    """Check if MagnetoClip is already registered as the .torrent handler."""
    if sys.platform != "win32":
        return False
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            f"Software\\Classes\\{_TORRENT_EXT}",
            0,
            winreg.KEY_READ,
        )
        value, _ = winreg.QueryValueEx(key, "")
        winreg.CloseKey(key)
        return value == _TORRENT_PROG_ID
    except OSError:
        return False


def register() -> bool:
    """Register MagnetoClip as a .torrent file handler in the Windows registry.

    Uses HKEY_CURRENT_USER so no admin rights are required.
    Returns True on success, False on failure.
    """
    if sys.platform != "win32":
        log.info("torrent_association_skipped", reason="not_windows")
        return False

    exe_path = _get_exe_path()
    if exe_path is None:
        log.warning("torrent_association_failed", reason="exe_path_not_found")
        return False

    try:
        import winreg

        # 1. Set the ProgID shell\open\command
        cmd_key = winreg.CreateKey(
            winreg.HKEY_CURRENT_USER,
            f"Software\\Classes\\{_TORRENT_PROG_ID}\\shell\\open\\command",
        )
        winreg.SetValueEx(cmd_key, "", 0, winreg.REG_SZ, f'"{exe_path}" "%1"')
        winreg.CloseKey(cmd_key)

        # 2. Set the ProgID DefaultIcon
        icon_key = winreg.CreateKey(
            winreg.HKEY_CURRENT_USER,
            f"Software\\Classes\\{_TORRENT_PROG_ID}\\DefaultIcon",
        )
        winreg.SetValueEx(icon_key, "", 0, winreg.REG_SZ, f'"{exe_path}",0')
        winreg.CloseKey(icon_key)

        # 3. Associate .torrent extension with our ProgID
        ext_key = winreg.CreateKey(
            winreg.HKEY_CURRENT_USER,
            f"Software\\Classes\\{_TORRENT_EXT}",
        )
        winreg.SetValueEx(ext_key, "", 0, winreg.REG_SZ, _TORRENT_PROG_ID)
        winreg.CloseKey(ext_key)

        # 4. Register in OpenWithProgids so we appear in "Open with" menu
        openwith_key = winreg.CreateKey(
            winreg.HKEY_CURRENT_USER,
            f"Software\\Classes\\{_TORRENT_EXT}\\OpenWithProgids",
        )
        winreg.SetValueEx(openwith_key, _TORRENT_PROG_ID, 0, winreg.REG_NONE, b"")
        winreg.CloseKey(openwith_key)

        log.info("torrent_association_registered", exe=str(exe_path))
        return True
    except OSError as exc:
        log.warning("torrent_association_failed", error=str(exc))
        return False


def unregister() -> bool:
    """Remove MagnetoClip as the .torrent file handler."""
    if sys.platform != "win32":
        return False
    try:
        import winreg

        # Remove the extension association
        try:
            winreg.DeleteKey(
                winreg.HKEY_CURRENT_USER,
                f"Software\\Classes\\{_TORRENT_EXT}\\OpenWithProgids",
            )
        except OSError:
            pass
        try:
            winreg.DeleteValue(
                winreg.HKEY_CURRENT_USER,
                f"Software\\Classes\\{_TORRENT_EXT}",
            )
        except OSError:
            pass

        # Remove the ProgID
        for sub in ["shell\\open\\command", "DefaultIcon", "shell\\open", "shell", ""]:
            try:
                key_path = f"Software\\Classes\\{_TORRENT_PROG_ID}"
                if sub:
                    key_path += f"\\{sub}"
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
            except OSError:
                pass

        log.info("torrent_association_unregistered")
        return True
    except OSError as exc:
        log.warning("torrent_association_unregister_failed", error=str(exc))
        return False


# ---------------------------------------------------------------------------
# magnet: protocol handler registration
# ---------------------------------------------------------------------------

def is_magnet_registered() -> bool:
    """Check if MagnetoClip is registered as the magnet: protocol handler."""
    if sys.platform != "win32":
        return False
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            f"Software\\Classes\\{_MAGNET_PROTOCOL}\\shell\\open\\command",
            0,
            winreg.KEY_READ,
        )
        value, _ = winreg.QueryValueEx(key, "")
        winreg.CloseKey(key)
        exe = _get_exe_path()
        return exe is not None and str(exe) in value
    except OSError:
        return False


def register_magnet() -> bool:
    """Register MagnetoClip as a magnet: protocol handler in the Windows registry.

    Uses HKEY_CURRENT_USER so no admin rights are required.
    Returns True on success, False on failure.
    """
    if sys.platform != "win32":
        log.info("magnet_protocol_skipped", reason="not_windows")
        return False

    exe_path = _get_exe_path()
    if exe_path is None:
        log.warning("magnet_protocol_failed", reason="exe_path_not_found")
        return False

    try:
        import winreg

        # 1. Set the protocol description and mark as URL Protocol
        desc_key = winreg.CreateKey(
            winreg.HKEY_CURRENT_USER,
            f"Software\\Classes\\{_MAGNET_PROTOCOL}",
        )
        winreg.SetValueEx(desc_key, "", 0, winreg.REG_SZ, "URL:Magnet Protocol")
        winreg.SetValueEx(desc_key, "URL Protocol", 0, winreg.REG_SZ, "")
        winreg.CloseKey(desc_key)

        # 2. Set the DefaultIcon
        icon_key = winreg.CreateKey(
            winreg.HKEY_CURRENT_USER,
            f"Software\\Classes\\{_MAGNET_PROTOCOL}\\DefaultIcon",
        )
        winreg.SetValueEx(icon_key, "", 0, winreg.REG_SZ, f'"{exe_path}",0')
        winreg.CloseKey(icon_key)

        # 3. Set shell\open\command — Windows passes the magnet URI as %1
        cmd_key = winreg.CreateKey(
            winreg.HKEY_CURRENT_USER,
            f"Software\\Classes\\{_MAGNET_PROTOCOL}\\shell\\open\\command",
        )
        winreg.SetValueEx(cmd_key, "", 0, winreg.REG_SZ, f'"{exe_path}" "%1"')
        winreg.CloseKey(cmd_key)

        log.info("magnet_protocol_registered", exe=str(exe_path))
        return True
    except OSError as exc:
        log.warning("magnet_protocol_failed", error=str(exc))
        return False


def unregister_magnet() -> bool:
    """Remove MagnetoClip as the magnet: protocol handler."""
    if sys.platform != "win32":
        return False
    try:
        import winreg

        for sub in ["shell\\open\\command", "DefaultIcon", "shell\\open", "shell", ""]:
            try:
                key_path = f"Software\\Classes\\{_MAGNET_PROTOCOL}"
                if sub:
                    key_path += f"\\{sub}"
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
            except OSError:
                pass

        log.info("magnet_protocol_unregistered")
        return True
    except OSError as exc:
        log.warning("magnet_protocol_unregister_failed", error=str(exc))
        return False
