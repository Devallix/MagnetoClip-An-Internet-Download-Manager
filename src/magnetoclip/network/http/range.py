from __future__ import annotations


def range_header(start: int, end: int | None = None) -> dict[str, str]:
    """Build an HTTP ``Range`` header for the given byte range."""
    if end is None:
        return {"Range": f"bytes={start}-"}
    return {"Range": f"bytes={start}-{end}"}


def parse_content_range(value: str | None) -> tuple[int | None, int | None, int | None]:
    """Parse a ``Content-Range: bytes start-end/total`` header.

    Returns ``(start, end, total)``; any unknown part is ``None``.
    """
    if not value:
        return None, None, None
    try:
        spec = value.strip().split(" ", 1)[1]
        ranges, _, total = spec.partition("/")
        start_s, _, end_s = ranges.partition("-")
        start = int(start_s) if start_s else None
        end = int(end_s) if end_s else None
        total = int(total) if total not in ("", "*") else None
        return start, end, total
    except (ValueError, IndexError):
        return None, None, None
