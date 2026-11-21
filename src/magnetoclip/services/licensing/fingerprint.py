"""Stable per-machine fingerprint: SHA-256 of the Windows MachineGuid.

Only the hash is ever sent to the license server — the raw GUID stays on
the machine. The value survives app reinstalls; an OS reinstall rotates it
(the user then reactivates, or the vendor unbinds the old slot).
"""

from __future__ import annotations

import hashlib
import platform
import subprocess
import uuid


def _windows_machine_guid() -> str | None:
    if platform.system() != "Windows":
        return None
    try:
        import winreg

        for view in (winreg.HKEY_LOCAL_MACHINE,):
            with winreg.OpenKey(
                view,
                r"SOFTWARE\Microsoft\Cryptography",
                0,
                winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
            ) as key:
                value, _ = winreg.QueryValueEx(key, "MachineGuid")
                return str(value)
    except OSError:
        return None
    return None


def _macos_io_platform_uuid() -> str | None:
    if platform.system() != "Darwin":
        return None
    try:
        out = subprocess.run(
            ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        if '"IOPlatformUUID"' in line:
            return line.split('"')[-2]
    return None


def _linux_machine_id() -> str | None:
    for candidate in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            text = open(candidate).read().strip()
        except OSError:
            continue
        if text:
            return text
    return None


def raw_fingerprint() -> str:
    """Best available stable hardware string (never leaves the machine)."""
    source = (
        _windows_machine_guid()
        or _macos_io_platform_uuid()
        or _linux_machine_id()
    )
    if not source:
        # last resort: MAC address of any interface (stable enough on desktops)
        source = str(uuid.getnode())
    return source


def machine_id() -> str:
    """64-hex-char fingerprint sent to the license server."""
    return hashlib.sha256(raw_fingerprint().encode("utf-8")).hexdigest()
