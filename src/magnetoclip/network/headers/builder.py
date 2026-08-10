from __future__ import annotations

from typing import Mapping

DEFAULT_ACCEPT = "*/*"


def build_headers(
    user_agent: str,
    extra: Mapping[str, str] | None = None,
    *,
    accept: str = DEFAULT_ACCEPT,
) -> dict[str, str]:
    """Build request headers for download operations.

    ``Accept-Encoding: identity`` is forced so that Range requests operate on
    uncompressed byte offsets.
    """
    headers = {
        "User-Agent": user_agent,
        "Accept": accept,
        "Accept-Encoding": "identity",
    }
    if extra:
        headers.update(extra)
    return headers
