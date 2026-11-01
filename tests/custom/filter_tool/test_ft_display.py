"""Tests of the QImage helpers and of the display-only paint hook."""

from __future__ import annotations

import numpy as np

from PyQt6 import QtGui, QtWidgets

from anylabeling.custom.filter_tool import core, display, installer
from anylabeling.custom.filter_tool.display import FilterDisplayController
from anylabeling.views.labeling.widgets.canvas import Canvas

from conftest import (
    FakeWidget,
    keep,
    make_image,
    make_pixmap,
    make_rgb,
)


def _pixel_color(pixmap, x=1, y=1):
    return pixmap.toImage().pixelColor(x, y).getRgb()


class TestQImageConversion:
    def test_roundtrip_preserves_rgb_alpha_and_ratio(self):
        rgb = make_rgb(6, 4)
        alpha = np.full((4, 6), 128, dtype=np.uint8)
        image = make_image(rgb, alpha, device_pixel_ratio=2.0)
        back_rgb, back_alpha, ratio = display.qimage_to_rgba(image)
        assert np.array_equal(back_rgb, rgb)
        assert np.array_equal(back_alpha, alpha)
        assert ratio == 2.0

    def test_rebuilt_image_keeps_transparency(self):
        rgb = np.array([[[10, 20, 30]]], dtype=np.uint8)
        alpha = np.array([[128]], dtype=np.uint8)
        image = display.rgba_to_qimage(rgb, alpha, 1.5)
        assert image.pixelColor(0, 0).getRgb() == (10, 20, 30, 128)
        assert image.devicePixelRatio() == 1.5

    def test_premultiplied_source_is_unpremultiplied(self):
        image = QtGui.QImage(
            2, 1, QtGui.QImage.Format.Format_ARGB32_Premultiplied
        )
        image.setPixelColor(0, 0, QtGui.QColor(10, 20, 30, 128))
        image.setPixelColor(1, 0, QtGui.QColor(200, 100, 50, 255))
        pixmap = QtGui.QPixmap.fromImage(image)
        rgb, alpha, _ratio = display.qimage_to_rgba(pixmap.toImage())
        assert rgb[0, 0].tolist() == [10, 20, 30]
        assert int(alpha[0, 0]) == 128
        assert rgb[0, 1].tolist() == [200, 100, 50]

    def test_filtered_qimage_keeps_the_alpha_channel(self):
        rgb = np.array([[[5, 5, 5], [200, 200, 200]]], dtype=np.uint8)
        alpha = np.array([[64, 255]], dtype=np.uint8)
        image = display.rgba_to_qimage(rgb, alpha)
        result = display.filtered_qimage(
            image, [core.default_spec(core.KIND_GAUSSIAN)]
        )
        assert result.pixelColor(0, 0).getRgb()[3] == 64
        assert result.pixelColor(1, 0).getRgb()[3] == 255

    def test_filtered_qimage_keeps_alpha_with_gradient(self):
        rgb = np.full((8, 8, 3), 40, dtype=np.uint8)
        rgb[:, 4:] = 200
        alpha = np.full((8, 8), 64, dtype=np.uint8)
        alpha[:, 0] = 255
        image = display.rgba_to_qimage(rgb, alpha)
        result = display.filtered_qimage(
            image, [core.default_spec(core.KIND_GRADIENT)]
        )
        assert result.pixelColor(0, 0).getRgb()[3] == 255
        assert result.pixelColor(1, 1).getRgb()[3] == 64

    def test_filtered_qimage_applies_the_chain(self):
        rgb = np.array([[[5, 5, 5], [200, 200, 200]]], dtype=np.uint8)
        image = display.rgba_to_qimage(rgb)
        spec = core.FilterSpec(
            core.KIND_PIXEL, True, {"low": 10, "high": 255, "fill": 0}
        )
        result = display.filtered_qimage(image, [spec])
        assert result.pixelColor(0, 0).getRgb()[:3] == (0, 0, 0)
        assert result.pixelColor(1, 0).getRgb()[:3] == (200, 200, 200)


class TestController:
    def test_chain_is_copied_and_active(self):
        widget = keep(FakeWidget(image=make_image(make_rgb())))
        controller = FilterDisplayController(widget)
        controller.set_chain([core.default_spec(core.KIND_PIXEL)])
        assert controller.active()
        chain = controller.chain()
        chain[0].params["low"] = 99
        assert controller.chain()[0].params["low"] == 0

    def test_empty_chain_returns_none(self):
        widget = keep(FakeWidget(image=make_image(make_rgb())))
        controller = FilterDisplayController(widget)
        assert not controller.active()
        assert controller.filtered_pixmap(widget.canvas.pixmap) is None

    def test_filtered_pixmap_is_cached_and_invalidated(self):
        widget = keep(FakeWidget(image=make_image(make_rgb())))
        controller = FilterDisplayController(widget)
        spec = core.FilterSpec(
            core.KIND_PIXEL, True, {"low": 0, "high": 0, "fill": 0}
        )
        controller.set_chain([spec])
        source = widget.canvas.pixmap
        first = controller.filtered_pixmap(source)
        second = controller.filtered_pixmap(source)
        assert first is second
        controller.set_chain([spec, core.default_spec(core.KIND_GAUSSIAN)])
        third = controller.filtered_pixmap(source)
        assert third is not first

    def test_failed_filter_returns_none_and_reports(self, monkeypatch):
        widget = keep(FakeWidget(image=make_image(make_rgb())))
        controller = FilterDisplayController(widget)
        controller.set_chain([core.default_spec(core.KIND_PIXEL)])

        def explode(*_args, **_kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(display.core, "apply_chain", explode)
        assert controller.filtered_pixmap(widget.canvas.pixmap) is None
        assert "boom" in controller.last_error


class TestPaintHook:
    def _installed(self, image=None):
        widget = keep(
            FakeWidget(image=image if image is not None else make_image(
                make_rgb()
            ))
        )
        controller = FilterDisplayController(widget)
        spec = core.FilterSpec(
            core.KIND_PIXEL, True, {"low": 0, "high": 0, "fill": 0}
        )
        controller.set_chain([spec])
        return widget, controller

    def test_paint_swaps_and_restores_the_raw_pixmap(self):
        widget, controller = self._installed()
        raw = widget.canvas.pixmap
        widget.canvas.paintEvent(None)
        inside, inside_compare = widget.canvas.seen[-1]
        assert inside is not raw
        assert inside_compare is None
        assert widget.canvas.pixmap is raw

    def test_bypass_paints_the_raw_pixmap_until_released(self):
        widget, controller = self._installed()
        raw = widget.canvas.pixmap
        assert controller.set_bypass(True) is True
        assert controller.bypassed() is True
        widget.canvas.paintEvent(None)
        assert widget.canvas.seen[-1][0] is raw
        assert controller.set_bypass(False) is False
        assert controller.bypassed() is False
        widget.canvas.paintEvent(None)
        assert widget.canvas.seen[-1][0] is not raw

    def test_compare_pixmap_is_filtered_too(self):
        widget, controller = self._installed()
        compare = make_pixmap(make_rgb())
        widget.canvas.compare_pixmap = compare
        widget.canvas.paintEvent(None)
        inside, inside_compare = widget.canvas.seen[-1]
        assert inside is not widget.canvas.pixmap
        assert inside_compare is not compare
        assert widget.canvas.compare_pixmap is compare

    def test_no_chain_paints_the_raw_pixmap(self):
        widget = keep(FakeWidget(image=make_image(make_rgb())))
        controller = FilterDisplayController(widget)
        raw = widget.canvas.pixmap
        widget.canvas.paintEvent(None)
        assert widget.canvas.seen[-1][0] is raw
        assert controller.last_error == ""

    def test_paint_failure_falls_back_to_raw(self, monkeypatch):
        widget, controller = self._installed()
        raw = widget.canvas.pixmap

        def explode(*_args, **_kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(display.core, "apply_chain", explode)
        widget.canvas.paintEvent(None)
        assert widget.canvas.seen[-1][0] is raw
        assert "boom" in controller.last_error

    def test_next_source_is_filtered_automatically(self):
        widget, controller = self._installed()
        first_raw = widget.canvas.pixmap
        widget.canvas.paintEvent(None)
        first_inside = widget.canvas.seen[-1][0]
        assert _pixel_color(first_inside) != _pixel_color(first_raw)

        second_rgb = make_rgb(16, 12)
        second_rgb[:] = 200
        second_raw = make_pixmap(second_rgb)
        widget.canvas.pixmap = second_raw
        widget.canvas.paintEvent(None)
        second_inside = widget.canvas.seen[-1][0]
        assert second_inside is not second_raw
        assert _pixel_color(second_inside) == (0, 0, 0, 255)


class TestRealCanvas:
    def test_render_is_filtered_but_canvas_pixmap_stays_raw(self):
        parent = keep(QtWidgets.QWidget())
        canvas = keep(Canvas(parent=parent))
        canvas.resize(16, 12)
        canvas.scale = 1.0
        canvas.cross_line_show = False
        raw = make_pixmap(make_rgb(16, 12))
        canvas.load_pixmap(raw)

        widget = keep(FakeWidget(image=raw.toImage()))
        widget.canvas = canvas
        controller = FilterDisplayController(widget)
        controller.set_chain(
            [
                core.FilterSpec(
                    core.KIND_PIXEL,
                    True,
                    {"low": 0, "high": 0, "fill": 0},
                )
            ]
        )

        rendered = QtGui.QImage(
            16, 12, QtGui.QImage.Format.Format_RGB32
        )
        rendered.fill(QtGui.QColor(0, 0, 0))
        painter = QtGui.QPainter(rendered)
        try:
            canvas.render(painter)
        finally:
            painter.end()
        assert rendered.pixelColor(1, 1).getRgb()[:3] == (0, 0, 0)
        raw_color = canvas.pixmap.toImage().pixelColor(1, 1).getRgb()
        assert raw_color[:3] == _pixel_color(raw)[:3]
        assert raw_color[:3] != (0, 0, 0)


class TestMount:
    def test_install_is_idempotent(self):
        widget = keep(FakeWidget(image=make_image(make_rgb())))
        first = installer.install_filter_tool(widget)
        wrapper = widget.canvas.paintEvent
        second = installer.install_filter_tool(widget)
        assert first is second
        assert len(widget.menus.tool.actions()) == 2
        assert widget.canvas.paintEvent == wrapper

    def test_install_without_menu_returns_none(self):
        widget = keep(
            FakeWidget(image=make_image(make_rgb()), with_menu=False)
        )
        assert installer.install_filter_tool(widget) is None
