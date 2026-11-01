"""Display-only canvas hook and QImage <-> RGB conversion helpers.

The canvas keeps the real pixels in Canvas.pixmap. This module never
writes a filtered image back there: it temporarily swaps the attribute
for the duration of one paintEvent and restores it in a finally block,
so editing, brushing, auto labeling and visualization exports keep
reading the raw image.
"""

from __future__ import annotations

import logging
from collections import OrderedDict

import numpy as np
from PyQt6 import QtGui

from . import core

__all__ = [
    "FilterDisplayController",
    "VIEW_HINT",
    "filtered_qimage",
    "qimage_to_rgba",
    "rgba_to_qimage",
]

LOGGER = logging.getLogger(__name__)

#: Hint appended to the filter tooltip and the dialog status.
VIEW_HINT = "点击「显示原图」切换"

#: Number of filtered pixmaps kept for the canvas and its compare image.
CACHE_LIMIT = 2


def qimage_to_rgba(image: QtGui.QImage):
    """Return (rgb, alpha, device_pixel_ratio) of a QImage.

    The conversion goes through Format_RGBA8888 so that a premultiplied
    ARGB source is un-premultiplied first; semi transparent pixels keep
    their real colour. The returned arrays are fresh copies and own
    their memory.
    """

    if image is None or image.isNull():
        raise ValueError("null image")
    converted = image.convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
    width = int(converted.width())
    height = int(converted.height())
    if width <= 0 or height <= 0:
        raise ValueError("empty image")
    stride = int(converted.bytesPerLine())
    payload = converted.constBits().asstring(converted.sizeInBytes())
    buffer = np.frombuffer(payload, dtype=np.uint8)
    if buffer.size < stride * height:
        raise ValueError("truncated image buffer")
    buffer = buffer[: stride * height]
    rgba = (
        buffer.reshape(height, stride)[:, : width * 4]
        .reshape(height, width, 4)
        .copy()
    )
    ratio = float(converted.devicePixelRatio())
    if ratio <= 0:
        ratio = 1.0
    return rgba[:, :, :3].copy(), rgba[:, :, 3].copy(), ratio


def rgba_to_qimage(rgb, alpha=None, device_pixel_ratio=1.0) -> QtGui.QImage:
    """Build an RGBA8888 QImage from an RGB array and an alpha array."""

    rgb = np.ascontiguousarray(rgb, dtype=np.uint8)
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError("rgb must have shape (h, w, 3)")
    height, width = rgb.shape[:2]
    if height <= 0 or width <= 0:
        raise ValueError("empty image")
    if alpha is None:
        alpha = np.full((height, width), 255, dtype=np.uint8)
    alpha = np.ascontiguousarray(alpha, dtype=np.uint8)
    if alpha.shape != (height, width):
        raise ValueError("alpha shape does not match rgb")
    rgba = np.dstack((rgb, alpha))
    data = np.ascontiguousarray(rgba, dtype=np.uint8)
    image = QtGui.QImage(
        data.data,
        width,
        height,
        width * 4,
        QtGui.QImage.Format.Format_RGBA8888,
    ).copy()
    ratio = float(device_pixel_ratio)
    if ratio <= 0:
        ratio = 1.0
    image.setDevicePixelRatio(ratio)
    return image


def filtered_qimage(image, specs):
    """Return a filtered QImage, preserving alpha and device ratio."""

    rgb, alpha, ratio = qimage_to_rgba(image)
    filtered = core.apply_chain(rgb, specs)
    return rgba_to_qimage(filtered, alpha, ratio)


class FilterDisplayController:
    """Owns the display chain of one labeling widget.

    The chain starts empty and lives for the session. The controller
    installs one instance level paintEvent wrapper on the canvas; the
    wrapper is honest about its scope: it only changes what is painted,
    never Canvas.pixmap itself.
    """

    def __init__(self, widget=None):
        self.widget = widget
        self.canvas = None
        self.last_error = ""
        self._chain = []
        self._version = 0
        self._cache = OrderedDict()
        self._action = None
        self._base_tip = ""
        self._bypass_action = None
        self._bypass_action_tip = ""
        self._painting = False
        self._bypass = False
        self.attach()

    # ------------------------------------------------------------ chain

    def chain(self):
        """Return a copy of the active chain."""

        return [spec.clone() for spec in self._chain]

    def active(self):
        """Return True when at least one filter is enabled."""

        return any(spec.enabled for spec in self._chain)

    def set_chain(self, specs):
        """Replace the chain; raises FilterParamError on bad input."""

        normalized = core.normalize_specs(specs)
        self._chain = normalized
        self._version += 1
        self._cache.clear()
        self._bypass = False
        self.last_error = ""
        self._refresh_action_tip()
        self._refresh_bypass_action()
        canvas = self.attach()
        if canvas is not None:
            canvas.update()
        return normalized

    def clear(self):
        """Turn every filter off (equivalent to an empty chain)."""

        return self.set_chain([])

    def summary(self):
        """Return a short Chinese description of the active chain."""

        enabled = [spec for spec in self._chain if spec.enabled]
        if not enabled:
            return "无滤镜"
        return "、".join(spec.summary() for spec in enabled)

    # ----------------------------------------------------------- bypass

    def bypassed(self):
        """Return True while the canvas paints the raw image."""

        return self._bypass

    def set_bypass(self, bypass):
        """Toggle between the raw image and the filtered image."""

        bypass = bool(bypass) and self.active()
        if self._bypass == bypass:
            self._refresh_bypass_action()
            return self._bypass
        self._bypass = bypass
        self._refresh_bypass_action()
        canvas = self.attach()
        if canvas is not None:
            canvas.update()
        return self._bypass

    def toggle_bypass(self):
        """Flip the raw/filtered display and return the new state."""

        return self.set_bypass(not self._bypass)

    # ----------------------------------------------------------- action

    def bind_action(self, action, tip=""):
        """Remember the Tool menu entry and refresh its tooltip."""

        self._action = action
        self._base_tip = str(tip or "")
        self._refresh_action_tip()

    def _refresh_action_tip(self):
        if self._action is None:
            return
        enabled = sum(1 for spec in self._chain if spec.enabled)
        if enabled:
            self._action.setToolTip(
                "%s（生效中：%d 个，仅显示；%s）"
                % (self._base_tip, enabled, VIEW_HINT)
            )
        else:
            self._action.setToolTip(self._base_tip)

    def bind_bypass_action(self, action, tip=""):
        """Remember the checkable raw-view action and sync its state."""

        self._bypass_action = action
        self._bypass_action_tip = str(tip or "")
        self._refresh_bypass_action()

    def _refresh_bypass_action(self):
        action = self._bypass_action
        if action is None:
            return
        active = self.active()
        blocked = action.blockSignals(True)
        try:
            action.setEnabled(active)
            action.setChecked(bool(self._bypass and active))
            tip = self._bypass_action_tip
            if active and self._bypass:
                tip = "当前显示原图，点击恢复滤镜"
            action.setToolTip(tip)
            action.setStatusTip(tip)
        finally:
            action.blockSignals(blocked)

    # ---------------------------------------------------------- attach

    def attach(self):
        """Attach to the canvas of the widget, installing the hook once."""

        canvas = getattr(self.widget, "canvas", None)
        if canvas is None:
            return None
        if canvas is not self.canvas:
            self.canvas = canvas
            self._cache.clear()
            self._install_paint(canvas)
        return canvas

    def _install_paint(self, canvas):
        if getattr(canvas, "_filter_tool_paint_wrapped", False):
            return
        original = getattr(canvas, "paintEvent", None)
        if not callable(original):
            return

        def paint_event(event, _original=original, _canvas=canvas):
            return self._paint(_canvas, event, _original)

        canvas.paintEvent = paint_event
        canvas._filter_tool_paint_wrapped = True

    def _paint(self, canvas, event, original):
        self.attach()
        if self._painting or self._bypass or not self.active():
            return original(event)
        source = getattr(canvas, "pixmap", None)
        if source is None or source.isNull():
            return original(event)
        filtered = self.filtered_pixmap(source)
        if filtered is None:
            return original(event)

        has_compare = hasattr(canvas, "compare_pixmap")
        saved_compare = getattr(canvas, "compare_pixmap", None)
        canvas.pixmap = filtered
        if (
            has_compare
            and saved_compare is not None
            and not saved_compare.isNull()
        ):
            filtered_compare = self.filtered_pixmap(saved_compare)
            if filtered_compare is not None:
                canvas.compare_pixmap = filtered_compare

        self._painting = True
        try:
            return original(event)
        finally:
            self._painting = False
            canvas.pixmap = source
            if has_compare:
                canvas.compare_pixmap = saved_compare

    # ----------------------------------------------------------- cache

    def filtered_pixmap(self, source):
        """Return the filtered pixmap of a source, or None on failure."""

        if not self.active():
            return None
        if source is None or source.isNull():
            return None
        ratio = float(source.devicePixelRatio())
        if ratio <= 0:
            ratio = 1.0
        key = (
            int(source.cacheKey()),
            int(source.width()),
            int(source.height()),
            self._version,
            int(round(ratio * 1000.0)),
        )
        if key in self._cache:
            return self._cache[key]
        try:
            image = source.toImage()
            rgb, alpha, ratio = qimage_to_rgba(image)
            filtered = core.apply_chain(rgb, self._chain)
            result = QtGui.QPixmap.fromImage(
                rgba_to_qimage(filtered, alpha, ratio)
            )
            if result.isNull():
                raise ValueError("empty filtered pixmap")
            self.last_error = ""
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            LOGGER.warning(
                "filter_tool: cannot filter display: %s", exc,
                exc_info=True,
            )
            result = None
        self._cache[key] = result
        while len(self._cache) > CACHE_LIMIT:
            self._cache.popitem(last=False)
        return result
