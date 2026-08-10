from __future__ import annotations

import os
from pathlib import Path


def default_config_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
        return Path(base) / "MagnetoClip"
    return Path.home() / ".config" / "magnetoclip"


def default_data_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / "MagnetoClip"
    return Path.home() / ".local" / "share" / "magnetoclip"


def default_log_dir() -> Path:
    return default_data_dir() / "logs"


def ensure_dirs(*paths: Path) -> list[Path]:
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)
    return list(paths)
