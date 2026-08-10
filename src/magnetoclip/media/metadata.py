"""Filename-based media metadata extraction."""

from __future__ import annotations

import re
from pathlib import Path

_ARTIST_TITLE = re.compile(
    r"^(?P<artist>.+?)\s*[-_–]\s*(?P<title>.+?)(?:\s*\[(?P<extra>[^\]]+)\])?$"
)
_YEAR = re.compile(r"\((?:19|20)\d{2}\)|(?:19|20)\d{2}")
_EPISODE = re.compile(r"\b[Ss](?P<season>\d{1,2})[xXeE](?P<episode>\d{1,2})\b")
_RESOLUTION = re.compile(r"\b(720p|1080p|2160p|4k|8k|480p|360p)\b", re.IGNORECASE)
_SOURCE = re.compile(r"\b(BRRip|BluRay|WEB-DL|WEBRip|HDRip|DVDRip|HDTV|Remux)\b", re.IGNORECASE)


def extract_from_filename(filename: str | None) -> dict:
    """Best-effort metadata from a filename: title, artist, year, episode, etc."""
    if not filename:
        return {}
    name = Path(filename).stem.strip()
    metadata: dict = {}

    resolution = _RESOLUTION.search(name)
    if resolution:
        metadata["resolution"] = resolution.group(1).lower()

    source = _SOURCE.search(name)
    if source:
        metadata["source"] = source.group(1).lower()

    episode = _EPISODE.search(name)
    if episode:
        metadata["season"] = int(episode.group("season"))
        metadata["episode"] = int(episode.group("episode"))

    year = _YEAR.search(name)
    if year:
        match = re.search(r"(19|20)\d{2}", year.group(0))
        if match:
            metadata["year"] = int(match.group(0))

    artist_title = _ARTIST_TITLE.match(name)
    if artist_title:
        metadata["artist"] = artist_title.group("artist").strip()
        metadata["title"] = artist_title.group("title").strip()
    else:
        metadata["title"] = name
    return metadata


def clean_title(metadata: dict) -> str:
    """Human-friendly title from extracted metadata."""
    title = metadata.get("title") or "unknown"
    parts = [title]
    if "artist" in metadata:
        parts = [f"{metadata['artist']} - {title}"]
    if "season" in metadata and "episode" in metadata:
        parts.append(f"S{metadata['season']:02d}E{metadata['episode']:02d}")
    if "resolution" in metadata:
        parts.append(metadata["resolution"])
    return " ".join(parts)
