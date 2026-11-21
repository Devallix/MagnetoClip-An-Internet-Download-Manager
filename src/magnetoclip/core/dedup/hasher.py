"""Hashing helpers for the duplicate-detection feature.

Uses xxHash (xxh3_128) when available for speed, falling back to ``blake2b``
from the stdlib. All hashing runs in a worker thread via :func:`run_in_thread`
so it never blocks the Qt event loop.
"""

from __future__ import annotations

import asyncio
import hashlib
from functools import partial
from pathlib import Path

_SUPPORTED_ALGOS = ("xxh3_128", "blake2b", "sha256")


def has_xxhash() -> bool:
    try:
        import xxhash  # noqa: F401

        return True
    except Exception:  # noqa: BLE001 - best-effort feature detection
        return False


def ensure_supported(algo: str) -> str:
    """Return an algorithm that definitely works for the current environment.

    Maps the configured algorithm to a concrete one, falling back if the
    preferred implementation is not importable (e.g. xxhash not installed).
    """
    if algo not in _SUPPORTED_ALGOS:
        algo = "xxh3_128" if has_xxhash() else "blake2b"
    if algo == "xxh3_128" and not has_xxhash():
        return "blake2b"
    return algo


def _digest_file(path: Path, algo: str) -> str:
    """Hash the full contents of *path* with *algo* (blocking)."""
    algo = ensure_supported(algo)
    if algo == "xxh3_128":
        import xxhash

        hasher = xxhash.xxh3_128()
    else:
        hasher = hashlib.new(algo)
    with path.open("rb") as handle:
        for chunk in iter(partial(handle.read, 1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def digest_file(path: Path, algo: str) -> str:
    """Return the hex digest of *path* (blocking; call off the event loop)."""
    return _digest_file(path, algo)


async def digest_file_async(path: Path, algo: str) -> str:
    """Hash *path* in a worker thread."""
    return await asyncio.to_thread(_digest_file, path, algo)


def _digest_bytes(data: bytes, algo: str) -> str:
    algo = ensure_supported(algo)
    if algo == "xxh3_128":
        import xxhash

        return xxhash.xxh3_128_hexdigest(data)
    return hashlib.new(algo, data).hexdigest()


def digest_bytes(data: bytes, algo: str) -> str:
    return _digest_bytes(data, algo)
