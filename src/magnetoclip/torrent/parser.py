"""Parse .torrent files and magnet URIs to extract metadata.

Uses a pure-Python bencoder — no external dependencies required.
"""

from __future__ import annotations

import hashlib
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Minimal bencode decoder
# ---------------------------------------------------------------------------

def _bdecode(data: bytes) -> Any:
    """Decode a bencoded byte string into Python objects."""
    idx = [0]

    def _parse() -> Any:
        if idx[0] >= len(data):
            raise ValueError("unexpected end of data")
        ch = data[idx[0]]
        if ch == ord("d"):
            idx[0] += 1
            d: dict[bytes, Any] = {}
            while data[idx[0]] != ord("e"):
                key = _parse()
                val = _parse()
                d[key] = val
            idx[0] += 1
            return d
        if ch == ord("l"):
            idx[0] += 1
            lst: list[Any] = []
            while data[idx[0]] != ord("e"):
                lst.append(_parse())
            idx[0] += 1
            return lst
        if ch == ord("i"):
            idx[0] += 1
            end = data.index(ord("e"), idx[0])
            val = int(data[idx[0]:end])
            idx[0] = end + 1
            return val
        # byte-string: length : data
        end = data.index(ord(":"), idx[0])
        length = int(data[idx[0]:end])
        idx[0] = end + 1
        val = data[idx[0]:idx[0] + length]
        idx[0] += length
        return val

    return _parse()


def _get(d: dict, *keys: str | bytes) -> Any:
    """Safely get a nested key from a decoded dict."""
    for key in keys:
        if isinstance(key, str):
            key = key.encode()
        if not isinstance(d, dict):
            return None
        d = d.get(key)
    return d


# ---------------------------------------------------------------------------
# Torrent metadata
# ---------------------------------------------------------------------------

@dataclass
class TorrentFileInfo:
    """A single file inside a multi-file torrent."""
    path: str
    size: int


@dataclass
class TorrentMeta:
    """Parsed metadata from a .torrent file or magnet URI."""
    name: str = ""
    total_size: int = 0
    comment: str = ""
    created_by: str = ""
    info_hash: str = ""
    tracker_url: str = ""
    files: list[TorrentFileInfo] = field(default_factory=list)
    source: str = ""  # "file" or "magnet"

    @property
    def file_count(self) -> int:
        return len(self.files) if self.files else 1

    @property
    def size_text(self) -> str:
        return _human_size(self.total_size)


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} PB"


def parse_torrent_file(path: str | Path) -> TorrentMeta:
    """Parse a .torrent file and return its metadata."""
    raw = Path(path).read_bytes()
    return _parse_torrent_bytes(raw)


def _parse_torrent_bytes(raw: bytes) -> TorrentMeta:
    """Parse raw .torrent bytes and return metadata."""
    meta = TorrentMeta(source="file")
    try:
        decoded = _bdecode(raw)
    except Exception:
        return meta

    if not isinstance(decoded, dict):
        return meta

    info = decoded.get(b"info")
    if not isinstance(info, dict):
        return meta

    # Name
    name_raw = info.get(b"name")
    if isinstance(name_raw, bytes):
        meta.name = name_raw.decode("utf-8", errors="replace")

    # Info hash
    info_encoded = _bencode_dict(info)
    meta.info_hash = hashlib.sha1(info_encoded).hexdigest()

    # Single file
    length = info.get(b"length")
    if isinstance(length, int):
        meta.total_size = length
        meta.files = []

    # Multi-file
    file_list = info.get(b"files")
    if isinstance(file_list, list):
        meta.files = []
        meta.total_size = 0
        for fentry in file_list:
            if not isinstance(fentry, dict):
                continue
            path_parts = fentry.get(b"path")
            if isinstance(path_parts, list):
                parts = []
                for p in path_parts:
                    if isinstance(p, bytes):
                        parts.append(p.decode("utf-8", errors="replace"))
                file_path = "/".join(parts)
            elif isinstance(path_parts, bytes):
                file_path = path_parts.decode("utf-8", errors="replace")
            else:
                file_path = "unknown"
            file_size = fentry.get(b"length", 0) if isinstance(fentry.get(b"length"), int) else 0
            meta.files.append(TorrentFileInfo(path=file_path, size=file_size))
            meta.total_size += file_size

    # Top-level fields
    meta.comment = _bytes_to_str(decoded.get(b"comment"))
    meta.created_by = _bytes_to_str(decoded.get(b"created by"))

    # Tracker
    announce = decoded.get(b"announce")
    if isinstance(announce, bytes):
        meta.tracker_url = announce.decode("utf-8", errors="replace")
    elif not meta.tracker_url:
        announce_list = decoded.get(b"announce-list")
        if isinstance(announce_list, list):
            for tier in announce_list:
                if isinstance(tier, list) and tier:
                    first = tier[0]
                    if isinstance(first, bytes):
                        meta.tracker_url = first.decode("utf-8", errors="replace")
                        break

    return meta


def parse_magnet_uri(magnet: str) -> TorrentMeta:
    """Extract what we can from a magnet URI (info hash only — no name until metadata arrives)."""
    meta = TorrentMeta(source="magnet")

    # Extract info hash
    match = re.search(r"btih:([A-Fa-f0-9]{40}|[A-Za-z0-9]{32})", magnet, re.IGNORECASE)
    if match:
        h = match.group(1).lower()
        if len(h) == 32:
            h = h.hex() if hasattr(h, "hex") else bytes.fromhex(h.replace("-", "")).hex()
        meta.info_hash = h

    # Extract display name if present
    dn_match = re.search(r"[&?]dn=([^&]+)", magnet, re.IGNORECASE)
    if dn_match:
        import urllib.parse
        meta.name = urllib.parse.unquote(dn_match.group(1)).replace("+", " ")

    # Extract tracker if present
    tr_match = re.search(r"[&?]tr=([^&]+)", magnet, re.IGNORECASE)
    if tr_match:
        import urllib.parse
        meta.tracker_url = urllib.parse.unquote(tr_match.group(1))

    return meta


def _bytes_to_str(val: Any) -> str:
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    return ""


# Minimal bencode encoder (for info-hash computation)
def _bencode_value(val: Any) -> bytes:
    if isinstance(val, int):
        return f"i{val}e".encode()
    if isinstance(val, bytes):
        return f"{len(val)}:".encode() + val
    if isinstance(val, str):
        return _bencode_value(val.encode())
    if isinstance(val, list):
        return b"l" + b"".join(_bencode_value(v) for v in val) + b"e"
    if isinstance(val, dict):
        return b"d" + b"".join(
            _bencode_value(k) + _bencode_value(v) for k, v in sorted(val.items())
        ) + b"e"
    raise TypeError(f"cannot bencode {type(val)}")


def _bencode_dict(d: dict) -> bytes:
    return _bencode_value(d)
