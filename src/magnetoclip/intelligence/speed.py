"""Speed smoothing, ETA estimation, and download ranking."""

from __future__ import annotations


class SpeedPredictor:
    """Exponentially-weighted moving average of measured throughput."""

    def __init__(self, alpha: float = 0.35) -> None:
        self.alpha = alpha
        self._ema: float | None = None

    @property
    def value(self) -> float:
        return self._ema or 0.0

    def update(self, bytes_per_second: float) -> float:
        value = max(0.0, float(bytes_per_second or 0.0))
        if self._ema is None:
            if value > 0:
                self._ema = value
        else:
            self._ema = self.alpha * value + (1 - self.alpha) * self._ema
        return self._ema or 0.0

    def reset(self) -> None:
        self._ema = None

    def eta(self, remaining_bytes: float) -> float | None:
        """Estimated seconds to download ``remaining_bytes``, or None."""
        if self._ema and self._ema > 0 and remaining_bytes and remaining_bytes > 0:
            return remaining_bytes / self._ema
        return None


class PriorityRanker:
    """Orders queued downloads so higher-priority work starts first."""

    @staticmethod
    def key(priority: int, position: int = 0) -> tuple[int, int]:
        """Sort key: higher priority first, then earlier position."""
        return (-int(priority), position)

    @classmethod
    def rank(cls, items: list) -> list:
        """Stable-sort items by ``(priority, position)`` attributes."""
        return sorted(
            items,
            key=lambda item: cls.key(
                getattr(item, "priority", 0) or 0,
                getattr(item, "position", 0) or 0,
            ),
        )
