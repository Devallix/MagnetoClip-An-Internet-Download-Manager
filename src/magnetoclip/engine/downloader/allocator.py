"""Adaptive connection allocation: ramp up connections by measured throughput."""

from __future__ import annotations


class AdaptiveAllocator:
    """Grows the active connection count based on measured throughput.

    Starts at ``initial`` connections and doubles (up to ``maximum``) while the
    measured throughput keeps improving by at least ``minimum_gain`` per step.
    Once gains fall below the threshold, growth stops permanently.
    """

    def __init__(
        self,
        maximum: int,
        *,
        initial: int = 2,
        minimum_gain: float = 0.1,
        ramp_interval: float = 2.0,
    ) -> None:
        self.maximum = max(1, int(maximum))
        self.initial = min(int(initial), self.maximum)
        self.minimum_gain = minimum_gain
        self.ramp_interval = ramp_interval
        self.active = self.initial
        self._baseline: float | None = None
        self._stopped = False

    def evaluate(self, throughput: float) -> int:
        """Feed a throughput sample (bytes/s); returns the new target count."""
        if self._stopped or self.active >= self.maximum:
            return self.active
        if self._baseline is None:
            self._baseline = max(throughput, 0.0)
            return self.active
        baseline = self._baseline
        if baseline <= 0 or throughput <= 0:
            self._stopped = True
            return self.active
        gain = (throughput - baseline) / baseline
        if gain >= self.minimum_gain:
            self._baseline = throughput
            self.active = min(self.maximum, self.active * 2)
            return self.active
        self._stopped = True
        return self.active
