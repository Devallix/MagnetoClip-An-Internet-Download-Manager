"""Fair bandwidth allocation across active downloads."""

from __future__ import annotations


class BandwidthAllocator:
    """Splits a global bandwidth budget across downloads by weight.

    A weight is derived from a download's priority, so higher-priority
    downloads receive a larger share of the available bandwidth. A total of
    ``0`` means no cap (all downloads run unlimited).
    """

    def __init__(self, total_bytes_per_second: float = 0.0) -> None:
        self.set_total(total_bytes_per_second)

    @property
    def total(self) -> float:
        return self._total

    def set_total(self, bytes_per_second: float) -> None:
        self._total = max(0.0, float(bytes_per_second or 0.0))

    def allocate(
        self, weights: dict[int, float]
    ) -> dict[int, float]:
        """Return ``download_id -> bytes_per_second`` under the budget.

        Uncapable or weightless inputs produce all-``0.0`` rates, meaning
        "unlimited".
        """
        positive = {k: w for k, w in weights.items() if w is not None and w > 0}
        if self._total <= 0 or not positive:
            return {k: 0.0 for k in weights}
        total_weight = sum(positive.values())
        return {
            k: (self._total * w) / total_weight for k, w in positive.items()
        }

    @staticmethod
    def weight_for(priority: int) -> float:
        """Priority-based share weight; default priority maps to 1.0."""
        return float((priority or 0) + 1)
