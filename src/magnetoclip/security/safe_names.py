from __future__ import annotations

import re
from pathlib import Path

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# Cap the filename so it always fits Windows' MAX_PATH together with the
# download directory. CDN URLs (Facebook, Telegram, ...) can carry enormous
# query strings that otherwise turn into absurdly long, unopenable filenames.
_MAX_NAME_LENGTH = 180
_MAX_EXTENSION_LENGTH = 10


class UnsafePathError(ValueError):
    pass


def _cap_length(name: str) -> str:
    if len(name) <= _MAX_NAME_LENGTH:
        return name
    dot = name.rfind(".")
    if dot > 0 and len(name) - dot - 1 <= _MAX_EXTENSION_LENGTH:
        ext = name[dot:]
        return name[:dot][: _MAX_NAME_LENGTH - len(ext)] + ext
    return name[: _MAX_NAME_LENGTH]


def sanitize_filename(name: str) -> str:
    """Sanitize a filename: strip paths, invalid chars, reserved names."""
    name = name.replace("\\", "/").split("/")[-1].split("\x00")[-1]
    name = _INVALID_CHARS.sub("_", name)
    name = name.strip(" .")
    if not name:
        name = "download"
    name = _cap_length(name)
    stem = name.split(".")[0].upper()
    if stem in _RESERVED_NAMES:
        name = "_" + name
    return name


def safe_join(directory: Path, filename: str) -> Path:
    """Join ``directory`` + ``filename`` preventing path traversal."""
    base = Path(directory).expanduser().resolve()
    target = (base / sanitize_filename(filename)).resolve()
    if target != base and base not in target.parents:
        raise UnsafePathError(f"path escapes target directory: {filename!r}")
    return target
