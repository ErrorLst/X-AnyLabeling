"""Unit tests of the pure filter chain.

No Qt is needed here: the core works on uint8 RGB arrays only.
"""

from __future__ import annotations

import numpy as np
import pytest

from anylabeling.custom.filter_tool import core


def _gray(values):
    """Build a small grayscale RGB image from a flat list of values."""

    array = np.asarray(values, dtype=np.uint8).reshape(-1, 1)
    return np.repeat(array, 3, axis=1)[:, None, :]


def _rgb(values):
    """Build one RGB image from a list of (r, g, b) tuples."""

    array = np.asarray(values, dtype=np.uint8)
    return array.reshape(1, -1, 3)


class TestPixelFilter:
    def test_replaces_values_outside_inclusive_bounds(self):
        rgb = _rgb([(0, 10, 100), (8, 9, 101), (7, 11, 99)])
        result = core.apply_pixel_filter(rgb, 8, 100, 0)
        assert result.tolist() == [
            [[0, 10, 100], [8, 9, 0], [0, 11, 99]]
        ]

    def test_fill_value_is_used(self):
        rgb = _rgb([(0, 255, 128)])
        result = core.apply_pixel_filter(rgb, 10, 200, 7)
        assert result.tolist() == [[[7, 7, 128]]]

    def test_input_is_not_modified(self):
        rgb = _rgb([(0, 255, 128)])
        core.apply_pixel_filter(rgb, 10, 200, 7)
        assert rgb.tolist() == [[[0, 255, 128]]]

    def test_inverted_bounds_are_rejected(self):
        with pytest.raises(core.FilterParamError):
            core.apply_pixel_filter(_rgb([(1, 2, 3)]), 200, 10, 0)


class TestHistogramEqualization:
    def test_stretches_a_low_contrast_image(self):
        values = list(range(100, 111)) * 4
        rgb = _gray(values)
        result = core.apply_histogram_equalization(rgb)
        assert int(result.min()) == 0
        assert int(result.max()) == 255
        assert np.array_equal(
            result[:, :, 0], result[:, :, 1]
        )
        assert np.array_equal(
            result[:, :, 1], result[:, :, 2]
        )

    def test_excluded_values_do_not_change_the_included_lut(self):
        base = _gray([1, 2, 3] * 10)
        with_zeros = _gray([0] * 100 + [1, 2, 3] * 10)
        base_out = core.apply_histogram_equalization(base, 0)
        zero_out = core.apply_histogram_equalization(with_zeros, 0)
        assert np.array_equal(base_out, zero_out[100:])

    def test_without_exclusion_the_dominant_value_changes_the_lut(self):
        base = _gray([1, 2, 3] * 10)
        with_zeros = _gray([0] * 100 + [1, 2, 3] * 10)
        base_out = core.apply_histogram_equalization(base)
        zero_out = core.apply_histogram_equalization(with_zeros)
        assert not np.array_equal(base_out, zero_out[100:])

    def test_all_pixels_excluded_returns_the_input(self):
        rgb = _gray([0, 0, 0, 0])
        result = core.apply_histogram_equalization(rgb, 0)
        assert result.tolist() == rgb.tolist()

    def test_single_level_returns_the_input(self):
        rgb = _gray([128, 128, 128, 128])
        result = core.apply_histogram_equalization(rgb)
        assert result.tolist() == rgb.tolist()

    def test_color_image_keeps_more_than_one_channel(self):
        rgb = np.zeros((4, 4, 3), dtype=np.uint8)
        rgb[:, :, 0] = 40
        rgb[:, :, 1] = 80
        rgb[:, :, 2] = 120
        result = core.apply_histogram_equalization(rgb)
        assert result.shape == rgb.shape
        assert np.array_equal(
            result[:, :, 0], result[:, :, 1]
        ) is False


class TestGaussianBlur:
    def test_constant_image_stays_constant(self):
        rgb = np.full((8, 8, 3), 42, dtype=np.uint8)
        result = core.apply_gaussian_blur(rgb, 5, 0)
        assert np.array_equal(result, rgb)

    def test_impulse_spreads_to_neighbours(self):
        rgb = np.zeros((9, 9, 3), dtype=np.uint8)
        rgb[4, 4] = 255
        result = core.apply_gaussian_blur(rgb, 3, 0)
        assert int(result[4, 4, 0]) < 255
        assert int(result[4, 3, 0]) > 0

    def test_even_kernel_is_normalized_to_odd(self):
        rgb = np.arange(9 * 9 * 3, dtype=np.uint8).reshape(9, 9, 3)
        even = core.apply_gaussian_blur(rgb, 4, 1.0)
        odd = core.apply_gaussian_blur(rgb, 5, 1.0)
        assert np.array_equal(even, odd)


class TestChain:
    def test_order_changes_the_result(self):
        rgb = np.zeros((9, 9, 3), dtype=np.uint8)
        rgb[4, 4] = 255
        pixel = core.FilterSpec(
            core.KIND_PIXEL, True, {"low": 0, "high": 50, "fill": 0}
        )
        blur = core.FilterSpec(
            core.KIND_GAUSSIAN, True, {"kernel": 3, "sigma": 0.0}
        )
        first = core.apply_chain(rgb, [pixel, blur])
        second = core.apply_chain(rgb, [blur, pixel])
        assert not np.array_equal(first, second)

    def test_disabled_filter_is_skipped(self):
        rgb = _rgb([(1, 2, 3)])
        spec = core.FilterSpec(
            core.KIND_PIXEL,
            False,
            {"low": 0, "high": 0, "fill": 9},
        )
        result = core.apply_chain(rgb, [spec])
        assert result.tolist() == rgb.tolist()

    def test_empty_chain_returns_a_copy(self):
        rgb = _rgb([(1, 2, 3)])
        result = core.apply_chain(rgb, [])
        assert result is not rgb
        result[0, 0, 0] = 99
        assert rgb[0, 0, 0] == 1

    def test_accepts_plain_dicts_and_normalizes_kernel(self):
        rgb = np.zeros((5, 5, 3), dtype=np.uint8)
        specs = [
            {"kind": "gaussian", "params": {"kernel": 4, "sigma": 0}},
            {"kind": "pixel", "params": {"low": 0, "high": 255}},
        ]
        normalized = core.normalize_specs(specs)
        assert normalized[0].params["kernel"] == 5
        assert normalized[1].params["fill"] == 0
        assert core.apply_chain(rgb, specs).shape == rgb.shape

    def test_rejects_unknown_kind(self):
        with pytest.raises(core.FilterParamError):
            core.normalize_specs([{"kind": "unknown", "params": {}}])

    def test_rejects_non_rgb_input(self):
        with pytest.raises(core.FilterParamError):
            core.apply_chain(np.zeros((4, 4), dtype=np.uint8), [])


def test_default_specs_are_neutral():
    assert core.default_spec(core.KIND_PIXEL).params == {
        "low": 0,
        "high": 255,
        "fill": 0,
    }
    assert core.default_spec(core.KIND_EQUALIZE).params == {"exclude": None}
    assert core.default_spec(core.KIND_GAUSSIAN).params == {
        "kernel": 5,
        "sigma": 0.0,
    }
