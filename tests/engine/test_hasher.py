import hashlib

import pytest

from magnetoclip.engine.verification.hasher import (
    hash_file,
    hash_file_async,
    verify,
)


@pytest.mark.asyncio
async def test_hash_file_matches_hashing(tmp_path):
    payload = b"magneto-clip-hash-test" * 1000
    path = tmp_path / "data.bin"
    path.write_bytes(payload)
    assert hash_file(path, "sha256") == hashlib.sha256(payload).hexdigest()
    assert hash_file(path, "blake2b") == hashlib.blake2b(payload).hexdigest()


@pytest.mark.asyncio
async def test_hash_file_async(tmp_path):
    payload = b"x" * 3000
    path = tmp_path / "data.bin"
    path.write_bytes(payload)
    assert await hash_file_async(path, "sha1") == hashlib.sha1(payload).hexdigest()


def test_verify_case_insensitive():
    assert verify("ABCDEF", "abcdef")
    assert not verify("ABCDEF", "abcdef1")


def test_unsupported_algorithm_raises(tmp_path):
    path = tmp_path / "a.bin"
    path.write_bytes(b"x")
    with pytest.raises(ValueError):
        hash_file(path, "not-a-hash")
