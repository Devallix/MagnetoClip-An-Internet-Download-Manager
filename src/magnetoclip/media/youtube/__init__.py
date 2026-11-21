"""YouTube playlist expansion features.

A pasted playlist URL resolves to several videos. This package adds a
*detect → expand → preview → queue* pipeline on top of the existing
single-URL download engine:

* :mod:`~magnetoclip.media.youtube.types` — enums + data models.
* :mod:`~magnetoclip.media.youtube.detector` — cheap, network-free URL
  classification.
* :mod:`~magnetoclip.media.youtube.engine` — flat (header-only) yt-dlp
  extraction that never downloads.
* :mod:`~magnetoclip.media.youtube.worker` — threaded extraction worker.
* :mod:`~magnetoclip.media.youtube.controller` — orchestration glue.

Downloading itself always flows through the existing per-video pipeline
(`manager.add`), exactly as if each video had been pasted on its own.
"""

from __future__ import annotations

__all__ = [
    "detector",
    "engine",
    "types",
    "worker",
    "controller",
]