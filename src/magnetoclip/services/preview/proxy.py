"""Cached, low-resolution proxy files used when a codec cannot be previewed.

Qt's bundled FFmpeg cannot decode every modern codec. AV1, for example, is
shipped without a software decoder, so playback produces audio but no video
frames. Rather than showing a black rectangle the preview transcodes the file
once into a small H.264 proxy and replays that.

The proxy is cached on disk keyed by the source's identity, so reopening the
same download does not pay the transcode cost again.
"""

from __future__ import annotations

import hashlib
import tempfile
import time
from pathlib import Path

# A preview only needs to be recognisable, so keep proxies small. This also
# bounds how long a long source takes to transcode.
PROXY_MAX_WIDTH = 640
PROXY_MAX_SECONDS = 15 * 60
PROXY_DIR_NAME = "preview_cache"

# Keep the cache from growing without bound; entries are cheap to rebuild.
_PROXY_KEEP = 8
_PROXY_MAX_AGE_SECONDS = 7 * 24 * 3600

# Video codecs Qt's bundled FFmpeg has no usable decoder for. Its avcodec ships
# the native ``av1`` decoder behind a d3d11 hardware path only, so without a
# supported GPU the decode fails and the file yields audio with no frames.
# Handing such a file to QMediaPlayer only produces a wall of decoder errors
# before any frames appear, so the preview goes straight to a proxy instead.
UNSUPPORTED_CODECS = frozenset({"av1"})


def needs_proxy(codec: str | None) -> bool:
    """Whether *codec* must be transcoded before Qt can preview it."""
    if not codec:
        return False
    return codec.strip().lower() in UNSUPPORTED_CODECS


def cache_dir(context=None) -> Path:
    """Directory holding preview proxies, created on demand."""
    base: Path | None = None
    data_dir = getattr(context, "data_dir", None)
    if data_dir:
        base = Path(data_dir) / PROXY_DIR_NAME
    if base is None:
        base = Path(tempfile.gettempdir()) / f"{PROXY_DIR_NAME}-{_user_scope()}"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        return Path(tempfile.gettempdir())
    return base


def _user_scope() -> str:
    import getpass

    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - a missing username is not fatal
        return "default"


def _signature(source: str | Path) -> str:
    """Identity of *source*: path, size and mtime, hashed.

    Size and mtime are included so that re-downloading a file under the same
    name does not serve a stale proxy.
    """
    path = Path(source)
    try:
        stat = path.stat()
        size, mtime = stat.st_size, int(stat.st_mtime_ns)
    except OSError:
        size, mtime = -1, 0
    key = f"{path.resolve()}|{size}|{mtime}".encode(errors="replace")
    return hashlib.sha256(key).hexdigest()[:32]


def proxy_path(source: str | Path, context=None) -> Path:
    """Where the cached proxy for *source* lives."""
    digest = _signature(source)
    return cache_dir(context) / f"{Path(source).stem[:48]}-{digest}.mkv"


def cached_proxy(source: str | Path, context=None) -> Path | None:
    """Return an existing, non-empty proxy for *source*, if there is one."""
    path = proxy_path(source, context)
    try:
        if path.is_file() and path.stat().st_size > 0:
            return path
    except OSError:
        return None
    return None


def prune(directory: Path | None = None, *, keep: int = _PROXY_KEEP) -> int:
    """Trim the proxy cache; returns how many files were removed.

    Entries past the ``keep`` cap are removed regardless of age, and anything
    older than the maximum age is removed even inside the cap, so a long-lived
    install cannot accumulate proxies indefinitely.
    """
    if directory is None:
        directory = cache_dir()
    try:
        entries = sorted(
            directory.glob("*.mkv"), key=lambda p: p.stat().st_mtime
        )
    except OSError:
        return 0

    now = time.time()
    candidates = list(entries[:-keep]) if len(entries) > keep else []
    candidates += [
        entry
        for entry in (entries[-keep:] if keep else entries)
        if now - entry.stat().st_mtime > _PROXY_MAX_AGE_SECONDS
    ]

    removed = 0
    for entry in dict.fromkeys(candidates):
        try:
            entry.unlink()
            removed += 1
        except OSError:
            continue
    return removed
