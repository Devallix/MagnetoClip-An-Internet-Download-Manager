#!/usr/bin/env python
"""Fetch the bundled ffmpeg used for preview proxies.

The preview falls back to transcoding when the host cannot decode a codec (AV1
on machines with no hardware AV1 support, for example). That fallback needs
ffmpeg, and the app must not depend on the user having it installed, so the
build vendors a known-good binary here:

    python scripts/fetch_ffmpeg.py

The download is pinned to a fixed URL and verified against a SHA-256 digest, so
a swapped or truncated artifact fails loudly instead of shipping. Only
``ffmpeg.exe`` is taken: ``ffprobe`` is optional because the preview's
no-frames watchdog covers codec detection when it is absent, and dropping it
saves roughly 128 MB.

The build is the LGPL variant on purpose. The GPL builds (and the ``libx264``
they carry) cannot be redistributed inside this proprietary application; see
``docs/FFMPEG_BUNDLE.md`` for the reasoning and obligations.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIN_DIR = ROOT / "src" / "magnetoclip" / "resources" / "bin"

# BtbN/FFmpeg-Builds "latest" rolling release, LGPL variant, static win64.
# LGPL, so redistributable alongside a proprietary app; contains libdav1d (AV1
# decode) and libopenh264 (BSD H.264 encode) but no libx264.
FFMPEG_URL = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
    "ffmpeg-n8.1-latest-win64-lgpl-8.1.zip"
)
FFMPEG_SHA256 = "fe8b08ff9ceb4e982a310450dd63c0d1ea7e3bc76d4c52e5b257ceb3e96871a3"
WANTED = "bin/ffmpeg.exe"

# The LGPL build must travel with its license text, and the spec only packages
# src/magnetoclip/resources/docs, so the license has to land there.
LICENSE_URL = "https://raw.githubusercontent.com/FFmpeg/FFmpeg/n8.1/LICENSE.md"
DOCS_DIR = ROOT / "src" / "magnetoclip" / "resources" / "docs"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, dest: Path) -> None:
    print(f"downloading {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "magnetoclip-build"})
    with urllib.request.urlopen(request, timeout=600) as response, dest.open("wb") as out:
        shutil.copyfileobj(response, out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="re-download even if present")
    args = parser.parse_args()

    target = BIN_DIR / "ffmpeg.exe"
    if target.exists() and not args.force:
        print(f"already present: {target} ({target.stat().st_size // (1024 * 1024)} MB)")
        return 0

    BIN_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "ffmpeg.zip"
        _download(FFMPEG_URL, archive)

        print("verifying checksum")
        actual = _sha256(archive)
        if actual.lower() != FFMPEG_SHA256.lower():
            print(f"checksum mismatch\n  expected {FFMPEG_SHA256}\n  actual   {actual}")
            return 1

        print("extracting ffmpeg.exe")
        with zipfile.ZipFile(archive) as zf:
            member = next(
                (n for n in zf.namelist() if n.endswith(WANTED)), None
            )
            if member is None:
                print(f"{WANTED} not found in archive")
                return 1
            with zf.open(member) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)

    # The upstream license travels with the binary.
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _download(LICENSE_URL, DOCS_DIR / "FFMPEG_LICENSE.md")
    except OSError as exc:
        print(f"warning: could not fetch ffmpeg license: {exc}")

    print(f"OK: {target} ({target.stat().st_size // (1024 * 1024)} MB)")
    print("This file is intentionally not committed; run this script before building.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
