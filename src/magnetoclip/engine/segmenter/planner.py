from __future__ import annotations

from collections.abc import Sequence


def plan_segments(total_size: int | None, count: int) -> list[tuple[int, int | None]]:
    """Split ``total_size`` bytes into at most ``count`` byte ranges.

    Returns a list of ``(start, end)`` inclusive ranges. When the total size is
    unknown, a single open-ended range ``(0, None)`` is returned.
    """
    if not total_size:
        return [(0, None)]
    if total_size < 0:
        raise ValueError("total_size cannot be negative")
    if total_size == 0:
        return [(0, 0)]

    count = max(1, min(int(count), total_size))
    ranges: list[tuple[int, int | None]] = []
    chunk = total_size // count
    start = 0
    for index in range(count):
        end = start + chunk - 1
        if index == count - 1:
            end = total_size - 1
        ranges.append((start, end))
        start = end + 1
    return ranges


def total_of_ranges(ranges: Sequence[tuple[int, int | None]]) -> int:
    return sum(
        (end - start + 1) if end is not None else 0 for start, end in ranges
    )
