from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

ALGORITHMS = ("md5", "sha1", "sha256", "sha512", "blake2b")

CHUNK_SIZE = 1 << 20  # 1 MiB


def _hasher(algo: str) -> hashlib._Hash:
    if algo == "blake2b":
        return hashlib.blake2b()
    return hashlib.new(algo)


def hash_file(path: Path, algo: str, chunk_size: int = CHUNK_SIZE) -> str:
    """Compute the hex digest of a file using the given algorithm."""
    if algo not in ALGORITHMS:
        raise ValueError(f"unsupported algorithm: {algo}")
    hasher = _hasher(algo)
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk_size)
            if not block:
                break
            hasher.update(block)
    return hasher.hexdigest()


async def hash_file_async(path: Path, algo: str) -> str:
    return await asyncio.to_thread(hash_file, path, algo)


def verify(expected: str, calculated: str) -> bool:
    """Compare an expected digest with a calculated digest (case-insensitive)."""
    return expected.strip().lower() == calculated.strip().lower()
