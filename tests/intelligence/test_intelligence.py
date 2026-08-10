"""Unit tests for speed prediction, ranking, and bandwidth allocation."""

from __future__ import annotations

import pytest

from magnetoclip.intelligence.allocation import BandwidthAllocator
from magnetoclip.intelligence.speed import PriorityRanker, SpeedPredictor


class TestSpeedPredictor:
    def test_ema_converges_toward_steady_speed(self) -> None:
        predictor = SpeedPredictor(alpha=0.5)
        for _ in range(10):
            predictor.update(1_000_000)
        assert predictor.value == 1_000_000

    def test_ema_ignores_zero_before_first_reading(self) -> None:
        predictor = SpeedPredictor()
        assert predictor.update(0.0) == 0.0
        predictor.update(500.0)
        assert predictor.value == 500.0

    def test_eta_from_ema(self) -> None:
        predictor = SpeedPredictor(alpha=1.0)
        predictor.update(100.0)
        assert predictor.eta(1000.0) == pytest.approx(10.0)
        assert predictor.eta(0) is None

    def test_eta_without_reading_is_none(self) -> None:
        assert SpeedPredictor().eta(1000.0) is None

    def test_reset_clears_state(self) -> None:
        predictor = SpeedPredictor(alpha=1.0)
        predictor.update(42.0)
        predictor.reset()
        assert predictor.value == 0.0
        assert predictor.eta(100.0) is None


class TestPriorityRanker:
    def test_higher_priority_first(self) -> None:
        class Item:
            def __init__(self, priority: int, position: int) -> None:
                self.priority = priority
                self.position = position

        items = [
            Item(0, 1),
            Item(5, 0),
            Item(2, 2),
            Item(5, 1),
        ]
        ordered = PriorityRanker.rank(items)
        assert [i.priority for i in ordered] == [5, 5, 2, 0]
        assert ordered[0].position == 0
        assert ordered[1].position == 1

    def test_key_order(self) -> None:
        assert PriorityRanker.key(3, 1) < PriorityRanker.key(2, 0)
        assert PriorityRanker.key(3, 1) < PriorityRanker.key(3, 2)
        assert PriorityRanker.key(1, 0) < PriorityRanker.key(0, 0)


class TestBandwidthAllocator:
    def test_zero_budget_means_unlimited(self) -> None:
        allocator = BandwidthAllocator(0.0)
        assert allocator.allocate({1: 1.0, 2: 1.0}) == {1: 0.0, 2: 0.0}

    def test_equal_weights_split_evenly(self) -> None:
        allocator = BandwidthAllocator(1_000_000.0)
        rates = allocator.allocate({1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0})
        for rate in rates.values():
            assert rate == pytest.approx(250_000.0)

    def test_priority_weights_split_proportionally(self) -> None:
        allocator = BandwidthAllocator(900.0)
        rates = allocator.allocate(
            {1: allocator.weight_for(0), 2: allocator.weight_for(2)}
        )
        assert rates[2] / rates[1] == pytest.approx(
            allocator.weight_for(2) / allocator.weight_for(0)
        )

    def test_dynamic_budget_reallocation(self) -> None:
        allocator = BandwidthAllocator(1_000.0)
        rates = allocator.allocate({1: 1.0, 2: 1.0})
        assert rates[1] == pytest.approx(500.0)
        allocator.set_total(400.0)
        rates = allocator.allocate({1: 1.0, 2: 1.0})
        assert rates[1] == pytest.approx(200.0)

    def test_all_rates_sum_to_budget(self) -> None:
        allocator = BandwidthAllocator(777.0)
        weights = {i: allocator.weight_for(i) for i in range(1, 6)}
        rates = allocator.allocate(weights)
        assert sum(rates.values()) == pytest.approx(777.0)
