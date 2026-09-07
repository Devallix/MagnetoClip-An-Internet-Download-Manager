"""Native Windows toast notifications via the WinRT API (PowerShell bridge).

Windows 10/11 often suppress QSystemTrayIcon balloons, so this module is the
primary channel for user-facing notifications. It shells out to a small
PowerShell bridge that drives the ``Windows.UI.Notifications`` WinRT API and
returns success/failure. Clicks on the toast's "Open" action re-launch the app
through the ``magneto://`` protocol handler, letting the GUI jump to the
relevant page.
"""

from __future__ import annotations

import base64
import subprocess
import sys

from magnetoclip.resources import resource_path
from magnetoclip.services.logging import get_logger

log = get_logger(__name__)

AUMID = "MagnetoClip"
_POWERSHELL = "powershell"


def _encode_command(script: str) -> str:
    """Base64 UTF-16LE so ``-EncodedCommand`` survives every quoting edge case."""
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _escape_html(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _logo_image_element() -> str:
    path = resource_path("icons", "logo.png")
    if not path.exists():
        return ""
    return f'<image placement="appLogoOverride" src="{path.as_uri()}"/>'


def _build_xml(title: str, body: str, launch: str | None) -> str:
    title = _escape_html(title)
    body = _escape_html(body)
    launch_attr = f' launch="{_escape_html(launch)}"' if launch else ""
    actions = ""
    if launch:
        label = "Open"
        actions = (
            "<actions>"
            f'<action activationType="protocol" protocol="{_escape_html(launch)}" '
            f'arguments="" content="{label}"/>'
            "</actions>"
        )
    return (
        f'<toast duration="short"{launch_attr}>'
        "<visual><binding template=\"ToastGeneric\">"
        f"<text>{title}</text>"
        f"<text>{body}</text>"
        f"{_logo_image_element()}"
        "</binding></visual>"
        f"{actions}"
        "</toast>"
    )


def _powershell_script(xml: str, app_id: str) -> str:
    return (
        "$ErrorActionPreference = 'Stop'\n"
        "[Windows.UI.Notifications.ToastNotificationManager, "
        "Windows.UI.Notifications, ContentType = WindowsRuntime] "
        "| Out-Null\n"
        "[Windows.Data.Xml.Dom.XmlDocument, "
        "Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] "
        "| Out-Null\n"
        "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument\n"
        f"$xml.LoadXml(@'\n{xml}\n'@)\n"
        "$toast = New-Object Windows.UI.Notifications.ToastNotification $xml\n"
        f"$notifier = [Windows.UI.Notifications.ToastNotificationManager]"
        f"::CreateToastNotifier('{app_id}')\n"
        "$null = $notifier.Show($toast)\n"
    )


def show_toast(
    title: str,
    body: str,
    *,
    launch: str | None = None,
    app_id: str = AUMID,
    timeout: int = 8,
) -> bool:
    """Display a Windows toast and return True when accepted for display."""
    if sys.platform != "win32":
        return False
    xml = _build_xml(title, body, launch)
    script = _powershell_script(xml, app_id)
    try:
        completed = subprocess.run(
            [
                _POWERSHELL,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-WindowStyle",
                "Hidden",
                "-EncodedCommand",
                _encode_command(script),
            ],
            capture_output=True,
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception as exc:  # noqa: BLE001 - notifications are best-effort
        log.warning("toast_failed", error=str(exc))
        return False
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace")[:500]
        log.warning("toast_failed", error=stderr)
        return False
    return True