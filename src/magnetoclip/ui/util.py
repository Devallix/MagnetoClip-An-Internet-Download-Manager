"""UI helper functions: human-readable formatting and file actions."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def reveal_path(path: Path | str) -> bool:
    """Open the file manager with *path* selected. Returns success."""
    target = Path(path)
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(target)])
            return True
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(target)])
            return True
        parent = target.parent if target.is_file() else target
        subprocess.Popen(["xdg-open", str(parent)])
        return True
    except OSError:
        return False


def open_path(path: Path | str) -> bool:
    """Open *path* with the operating system's default application."""
    target = Path(path)
    try:
        if sys.platform == "win32":
            os.startfile(str(target))
            return True
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
            return True
        subprocess.Popen(["xdg-open", str(target)])
        return True
    except OSError:
        return False


def format_bytes(value: float | None) -> str:
    if not value:
        return "0 B"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return "0 B"


def format_speed(bytes_per_second: float | None) -> str:
    if not bytes_per_second:
        return "0 B/s"
    return f"{format_bytes(bytes_per_second)}/s"


def format_eta(seconds: float | None) -> str:
    if not seconds or seconds <= 0:
        return "--"
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def fraction(downloaded: int | None, total: int | None) -> float:
    if not total:
        return 0.0
    return max(0.0, min(1.0, (downloaded or 0) / total))
