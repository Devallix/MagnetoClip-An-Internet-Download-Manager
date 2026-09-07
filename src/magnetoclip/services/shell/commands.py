"""Parsing and dispatch for ``magneto://`` command URIs used by toast actions."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

_MAGNETO_SCHEME = "magneto"


def parse_command_uri(uri: str) -> dict | None:
    """Parse a ``magneto://`` command URI into an action dict, or ``None``.

    Supported commands:

    - ``magneto://open``                -> show the main window
    - ``magneto://open-downloads``      -> show the Downloads page
    - ``magneto://open-detected``       -> show the Detected page
    - ``magneto://reveal?download=<id>`` -> show Downloads and reveal the file
    """
    if not uri or not uri.lower().startswith(_MAGNETO_SCHEME + ":"):
        return None
    try:
        parsed = urlparse(uri)
    except Exception:  # noqa: BLE001 - malformed URIs are ignored
        return None
    if parsed.scheme.lower() != _MAGNETO_SCHEME:
        return None
    host = (parsed.netloc or "").lower()
    if host == "open-detected":
        return {"action": "open-detected"}
    if host == "open-downloads":
        return {"action": "open-downloads"}
    if host == "open":
        return {"action": "open"}
    if host == "reveal":
        download_id = None
        query = parse_qs(parsed.query)
        raw = query.get("download", [""])[0]
        if isinstance(raw, str) and raw.isdigit():
            download_id = int(raw)
        elif isinstance(raw, str) and raw:
            try:
                download_id = int(raw)
            except (TypeError, ValueError):
                download_id = None
        return {"action": "reveal", "download_id": download_id}
    return None


def _show_window(window) -> None:
    for method in ("show", "raise_", "activateWindow"):
        handler = getattr(window, method, None)
        if handler is not None:
            try:
                handler()
            except Exception:  # noqa: BLE001 - best-effort window activation
                pass


def _activate_nav(window, key: str) -> None:
    handler = getattr(window, "_activate_nav", None)
    if handler is not None:
        handler(key)


def apply_command(window, uri: str) -> bool:
    """Apply a ``magneto://`` command URI against *window*.

    Returns ``True`` when the URI was recognised and handled.
    """
    command = parse_command_uri(uri)
    if command is None:
        return False
    action = command["action"]
    if action == "open-detected":
        open_detected = getattr(window, "_open_detected", None)
        if open_detected is not None:
            open_detected()
        else:
            _show_window(window)
            _activate_nav(window, "detected")
        return True
    _show_window(window)
    if action == "open":
        _activate_nav(window, "overview")
    elif action == "open-downloads":
        _activate_nav(window, "downloads")
    elif action == "reveal":
        _activate_nav(window, "downloads")
        manager = getattr(getattr(window, "context", None), "manager", None)
        download_id = command.get("download_id")
        if manager is not None and download_id:
            path = manager.path_of(download_id)
            if path is not None:
                from magnetoclip.ui.util import reveal_path

                reveal_path(path)
    return True


def build_launch_uri(kind: str, download_id=None, action: str | None = None):
    """Map a notification payload to the ``magneto://`` URI its toast should open."""
    if action == "detected":
        return "magneto://open-detected"
    if kind == "completed" and download_id:
        return f"magneto://reveal?download={int(download_id)}"
    if kind in ("failed", "verification_failed"):
        return "magneto://open-downloads"
    return None