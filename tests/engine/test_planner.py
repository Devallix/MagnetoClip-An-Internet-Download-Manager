from magnetoclip.engine.segmenter.planner import plan_segments, total_of_ranges


def test_unknown_size_plans_single_open_range():
    ranges = plan_segments(None, 8)
    assert ranges == [(0, None)]


def test_small_file_capped_by_size():
    ranges = plan_segments(3, 8)
    assert total_of_ranges(ranges) == 3
    assert ranges == [(0, 0), (1, 1), (2, 2)]


def test_even_split():
    ranges = plan_segments(100, 4)
    assert total_of_ranges(ranges) == 100
    assert ranges == [(0, 24), (25, 49), (50, 74), (75, 99)]


def test_contiguous_and_inclusive():
    ranges = plan_segments(1000, 3)
    assert total_of_ranges(ranges) == 1000
    assert ranges[0][1] + 1 == ranges[1][0]
    assert ranges[1][1] + 1 == ranges[2][0]
    assert ranges[-1][1] == 999


def test_count_less_than_one_clamped():
    assert plan_segments(100, 0) == [(0, 99)]
