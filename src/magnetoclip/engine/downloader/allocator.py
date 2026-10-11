"""Adaptive connection allocation: ramp up connections by measured throughput."""

from __future__ import annotations


class AdaptiveAllocator:
    """Grows the active connection count based on measured throughput.

    Starts at ``initial`` connections and doubles (up to ``maximum``) while the
    measured throughput keeps improving by at least ``minimum_gain`` per step
    evaluated every ``ramp_interval``.

    Growth is conservative so extra connections never hurt total throughput:

    - Adding connections that drag per-connection throughput below
      ``minimum_efficiency`` of the best-observed value means the server caps
      per-connection bandwidth (doubling will not pay): growth stops
      permanently.
    - A single below-threshold sample does not stop growth. The baseline
      re-arms on every weak sample so a momentary dip or a server warm-up can
      recover; only ``max_weak_steps`` consecutive weak samples (a sustained
      plateau) stop growth permanently.
    """

    def __init__(
        self,
        maximum: int,
        *,
        initial: int = 2,
        minimum_gain: float = 0.1,
        ramp_interval: float = 2.0,
        minimum_efficiency: float = 0.5,
        max_weak_steps: int = 2,
    ) -> None:
        self.maximum = max(1, int(maximum))
        self.initial = min(int(initial), self.maximum)
        self.minimum_gain = minimum_gain
        self.ramp_interval = ramp_interval
        self.minimum_efficiency = minimum_efficiency
        self.max_weak_steps = max(1, int(max_weak_steps))
        self.active = self.initial
        self._baseline: float | None = None
        self._best_per_connection: float | None = None
        self._weak_steps = 0
        self._stopped = False

    def evaluate(self, throughput: float) -> int:
        """Feed a throughput sample (bytes/s); returns the new target count."""
        if self._stopped or self.active >= self.maximum:
            return self.active
        throughput = max(throughput, 0.0)
        if self._baseline is None:
            self._baseline = throughput
            self._best_per_connection = throughput / self.active
            return self.active
        if throughput <= 0:
            self._weak_steps += 1
            if self._weak_steps >= self.max_weak_steps:
                self._stopped = True
            return self.active
        per_connection = throughput / self.active
        if (
            self._best_per_connection is None
            or per_connection > self._best_per_connection
        ):
            self._best_per_connection = per_connection
        if per_connection < self._best_per_connection * self.minimum_efficiency:
            self._stopped = True
            return self.active
        baseline = self._baseline
        if baseline <= 0:
            self._baseline = throughput
            return self.active
        gain = (throughput - baseline) / baseline
        if gain >= self.minimum_gain:
            self._baseline = throughput
            self.active = min(self.maximum, self.active * 2)
            self._weak_steps = 0
            return self.active
        self._baseline = throughput
        self._weak_steps += 1
        if self._weak_steps >= self.max_weak_steps:
            self._stopped = True
        return self.active