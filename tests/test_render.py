"""Tests for the terminal-graphics helpers."""

from trikiscope.render import downsample_peaks, sparkline


def test_downsample_keeps_short_spikes():
    values = [0.0] * 1000
    values[503] = -250.0  # a single-sample flick
    out = downsample_peaks(values, 48)
    assert len(out) == 48
    assert min(out) == -250.0


def test_downsample_passes_short_series_through():
    assert downsample_peaks([1.0, 2.0], 48) == [1.0, 2.0]


def test_sparkline_width():
    assert len(sparkline(downsample_peaks(range(900), 48), 48)) == 48
