"""Pure display filter chain: pixel clamp, equalization and blur.

The module is deliberately free of Qt and of file IO. It works on a
uint8 RGB array and is reused by the canvas paint hook and by the
preview pane of the filter window. Every function returns a new array;
the input is never modified in place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

import cv2
import numpy as np

__all__ = [
    "FilterParamError",
    "FilterSpec",
    "KIND_EQUALIZE",
    "KIND_GAUSSIAN",
    "KIND_LABELS",
    "KIND_PIXEL",
    "apply_chain",
    "apply_gaussian_blur",
    "apply_histogram_equalization",
    "apply_pixel_filter",
    "default_spec",
    "normalize_specs",
]

KIND_PIXEL = "pixel"
KIND_EQUALIZE = "equalize"
KIND_GAUSSIAN = "gaussian"

KIND_LABELS = {
    KIND_PIXEL: "像素值过滤",
    KIND_EQUALIZE: "直方图均衡化",
    KIND_GAUSSIAN: "高斯滤波",
}

DEFAULT_PARAMS = {
    KIND_PIXEL: {"low": 0, "high": 255, "fill": 0},
    KIND_EQUALIZE: {"exclude": None},
    KIND_GAUSSIAN: {"kernel": 5, "sigma": 0.0},
}

MAX_CHANNEL = 255


class FilterParamError(ValueError):
    """Raised when a filter specification cannot be applied."""


@dataclass
class FilterSpec:
    """One entry of the display filter chain.

    The params dict always carries the normalized parameter values of
    its kind; the UI edits a clone of the active chain, never the chain
    the canvas is using.
    """

    kind: str
    enabled: bool = True
    params: Dict = field(default_factory=dict)

    def clone(self):
        """Return a deep enough copy for the dialog to edit."""

        return FilterSpec(self.kind, self.enabled, dict(self.params))

    def to_dict(self):
        """Return a plain JSON friendly representation."""

        return {
            "kind": self.kind,
            "enabled": bool(self.enabled),
            "params": dict(self.params),
        }

    @classmethod
    def from_dict(cls, value):
        """Build a spec from to_dict output or a plain mapping."""

        if isinstance(value, FilterSpec):
            return value.clone()
        if not isinstance(value, dict):
            raise FilterParamError("filter spec must be a mapping")
        return cls(
            str(value.get("kind", "")),
            bool(value.get("enabled", True)),
            dict(value.get("params") or {}),
        )

    def summary(self):
        """Return the one line description shown in the filter list."""

        if self.kind == KIND_PIXEL:
            return "像素值过滤：<{low} 或 >{high} → {fill}".format(
                **self.params
            )
        if self.kind == KIND_EQUALIZE:
            exclude = self.params.get("exclude")
            if exclude is None:
                return "直方图均衡化"
            return "直方图均衡化（排除 {}）".format(int(exclude))
        if self.kind == KIND_GAUSSIAN:
            return "高斯滤波：{kernel}×{kernel}，σ={sigma:g}".format(
                **self.params
            )
        return str(self.kind)


def default_spec(kind: str) -> FilterSpec:
    """Return one neutral spec of the requested kind."""

    if kind not in DEFAULT_PARAMS:
        raise FilterParamError("unknown filter kind: %r" % (kind,))
    return FilterSpec(kind, True, dict(DEFAULT_PARAMS[kind]))


def _uint8_channel(value, name: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise FilterParamError("%s must be an integer" % name) from None
    if number < 0 or number > MAX_CHANNEL:
        raise FilterParamError(
            "%s must be in 0..%d" % (name, MAX_CHANNEL)
        )
    return number


def _positive_int(value, name: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise FilterParamError("%s must be an integer" % name) from None
    if number < 1:
        raise FilterParamError("%s must be positive" % name)
    return number


def _non_negative_float(value, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise FilterParamError("%s must be a number" % name) from None
    if number < 0:
        raise FilterParamError("%s must not be negative" % name)
    return number


def _normalize_params(kind: str, params) -> Dict:
    params = dict(params or {})
    if kind == KIND_PIXEL:
        low = _uint8_channel(params.get("low", 0), "low")
        high = _uint8_channel(params.get("high", MAX_CHANNEL), "high")
        fill = _uint8_channel(params.get("fill", 0), "fill")
        if low > high:
            raise FilterParamError("low must not exceed high")
        return {"low": low, "high": high, "fill": fill}
    if kind == KIND_EQUALIZE:
        exclude = params.get("exclude")
        if exclude is None or exclude == "":
            return {"exclude": None}
        return {"exclude": _uint8_channel(exclude, "exclude")}
    if kind == KIND_GAUSSIAN:
        kernel = _positive_int(params.get("kernel", 5), "kernel")
        if kernel % 2 == 0:
            kernel += 1
        sigma = _non_negative_float(params.get("sigma", 0.0), "sigma")
        return {"kernel": kernel, "sigma": sigma}
    raise FilterParamError("unknown filter kind: %r" % (kind,))


def normalize_specs(specs: Iterable) -> List[FilterSpec]:
    """Normalize and validate a whole chain, returning fresh specs."""

    normalized = []
    for entry in specs or ():
        spec = FilterSpec.from_dict(entry)
        if spec.kind not in KIND_LABELS:
            raise FilterParamError(
                "unknown filter kind: %r" % (spec.kind,)
            )
        normalized.append(
            FilterSpec(
                spec.kind,
                bool(spec.enabled),
                _normalize_params(spec.kind, spec.params),
            )
        )
    return normalized


def _rgb_array(rgb) -> np.ndarray:
    """Validate one RGB uint8 array and return a contiguous copy."""

    array = np.asarray(rgb)
    if array.dtype != np.uint8:
        raise FilterParamError("rgb must be uint8")
    if array.ndim != 3 or array.shape[2] != 3:
        raise FilterParamError("rgb must have shape (h, w, 3)")
    return np.ascontiguousarray(array)


def apply_pixel_filter(rgb, low, high, fill=0):
    """Set every channel value outside the inclusive range to fill.

    The comparison is per channel: a pixel with one channel outside the
    range is changed in that channel only.
    """

    array = _rgb_array(rgb)
    low = _uint8_channel(low, "low")
    high = _uint8_channel(high, "high")
    fill = _uint8_channel(fill, "fill")
    if low > high:
        raise FilterParamError("low must not exceed high")
    result = array.copy()
    result[(result < low) | (result > high)] = fill
    return result


def apply_histogram_equalization(rgb, exclude: Optional[int] = None):
    """Equalize the luminance of rgb, optionally excluding a value.

    The histogram is built from the luminance channel only; when
    exclude is not None that one luminance level does not take part in
    the statistics. All pixels are then mapped through the resulting
    lookup table, including the excluded level (an excluded level gets
    the value of its predecessor in the LUT). Chroma is left untouched,
    so a grayscale image stays exactly grayscale.
    """

    array = _rgb_array(rgb)
    exclude_value = None
    if exclude is not None:
        exclude_value = _uint8_channel(exclude, "exclude")

    ycbcr = cv2.cvtColor(array, cv2.COLOR_RGB2YCrCb)
    luminance = ycbcr[:, :, 0]
    histogram = np.bincount(
        luminance.ravel(), minlength=MAX_CHANNEL + 1
    ).astype(np.int64)
    if exclude_value is not None:
        histogram[exclude_value] = 0

    cdf = np.cumsum(histogram)
    total = int(cdf[-1])
    if total == 0:
        return array.copy()
    nonzero = np.flatnonzero(cdf)
    cdf_min = int(cdf[nonzero[0]])
    denominator = total - cdf_min
    if denominator <= 0:
        return array.copy()

    scale = float(MAX_CHANNEL) / float(denominator)
    lut = np.rint((cdf - cdf_min) * scale)
    lut = np.clip(lut, 0, MAX_CHANNEL).astype(np.uint8)
    ycbcr[:, :, 0] = lut[luminance]
    return cv2.cvtColor(ycbcr, cv2.COLOR_YCrCb2RGB)


def apply_gaussian_blur(rgb, kernel=5, sigma=0.0):
    """Blur rgb with an odd Gaussian kernel.

    The kernel is coerced to the next odd integer; a sigma of zero lets
    OpenCV derive the standard deviation from the kernel size.
    """

    array = _rgb_array(rgb)
    size = _positive_int(kernel, "kernel")
    if size % 2 == 0:
        size += 1
    sigma_value = _non_negative_float(sigma, "sigma")
    if size == 1 and sigma_value <= 0:
        return array.copy()
    return cv2.GaussianBlur(
        array, (size, size), sigmaX=sigma_value, sigmaY=sigma_value
    )


def apply_chain(rgb, specs: Iterable):
    """Apply every enabled spec in list order and return a new array."""

    result = _rgb_array(rgb)
    for spec in normalize_specs(specs):
        if not spec.enabled:
            continue
        if spec.kind == KIND_PIXEL:
            result = apply_pixel_filter(result, **spec.params)
        elif spec.kind == KIND_EQUALIZE:
            result = apply_histogram_equalization(
                result, spec.params.get("exclude")
            )
        elif spec.kind == KIND_GAUSSIAN:
            result = apply_gaussian_blur(result, **spec.params)
        else:  # pragma: no cover - normalize_specs already guards this
            raise FilterParamError(
                "unknown filter kind: %r" % (spec.kind,)
            )
    if result is rgb:
        return result.copy()
    return result
