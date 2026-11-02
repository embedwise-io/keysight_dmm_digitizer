"""Numerical helpers: sample ranges, selection-box statistics, and overload gaps."""

import math

import numpy as np
import pytest

from keysight_daq.instrument import box_stats, find_gaps, samples_between


def test_samples_between_includes_both_edges():
    assert samples_between(2.0, 3.999, 1e-3, 10_000) == (2000, 4000)
    assert samples_between(2.0005, 2.0015, 1e-3, 10_000) == (2001, 2002)


def test_samples_between_clips_to_the_data():
    assert samples_between(-1.0, 0.0025, 1e-3, 10_000) == (0, 3)
    assert samples_between(9.5, 20.0, 1e-3, 10_000) == (9500, 10_000)
    assert samples_between(20.0, 30.0, 1e-3, 10_000) == (10_000, 10_000)


def test_box_stats_counts_and_moments():
    x = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    s = box_stats(x, 0, 5, 2.0, 4.0)
    assert (s.span, s.missing, s.count) == (5, 0, 3)
    assert (s.min, s.mean, s.max) == (2.0, 3.0, 4.0)
    assert s.std == pytest.approx(np.std([2.0, 3.0, 4.0]))


def test_box_stats_leaves_out_overloads():
    x = np.array([1.0, np.nan, 3.0, np.nan, 5.0])
    s = box_stats(x, 0, 5, 0.0, 10.0)
    assert (s.span, s.missing, s.count) == (3, 2, 3)
    assert s.mean == pytest.approx(3.0)


def test_box_stats_with_nothing_inside():
    s = box_stats(np.array([1.0, 2.0]), 0, 2, 5.0, 6.0)
    assert (s.span, s.count) == (2, 0)
    assert all(math.isnan(v) for v in (s.mean, s.std, s.min, s.max))


def test_find_gaps():
    v = np.arange(12.0)
    v[[0, 3, 4, 5, 9, 11]] = np.nan
    assert find_gaps(v).tolist() == [[0, 1], [3, 6], [9, 10], [11, 12]]
    assert find_gaps(np.ones(5)).tolist() == []
    assert find_gaps(np.full(3, np.nan)).tolist() == [[0, 3]]
