"""Tests of the filter window: list order, parameters and confirm."""

from __future__ import annotations

from PyQt6 import QtCore, QtGui

from anylabeling.custom.filter_tool import core, installer
from anylabeling.custom.filter_tool.dialog import (
    FilterDialog,
    HINT_TEXT,
)
from anylabeling.custom.filter_tool.display import VIEW_HINT

from conftest import (
    FakeWidget,
    keep,
    make_image,
    make_pixmap,
    make_rgb,
)


def _dialog(image=None, filename="sample.png"):
    if image is None:
        image = make_image(make_rgb())
    widget = keep(FakeWidget(image=image, filename=filename))
    dialog = keep(FilterDialog(widget))
    return widget, dialog


def _preview_color(dialog, x=1, y=1):
    return dialog.preview_pixmap().toImage().pixelColor(x, y).getRgb()


class TestLayout:
    def test_window_shows_both_panes_and_an_empty_list(self):
        widget, dialog = _dialog()
        assert dialog.windowTitle() == "滤镜工具"
        assert dialog.original_view.has_image()
        assert dialog.preview_view.has_image()
        assert dialog.filter_list.count() == 0
        assert "sample.png" in dialog.source_label.text()
        assert "无" in dialog.status_label.text()

    def test_panes_are_linked_for_zoom(self):
        widget, dialog = _dialog()
        dialog.original_view.apply_zoom(1.4)
        assert dialog.preview_view.zoom_level() == dialog.original_view.zoom_level()
        assert dialog.preview_view.transform() == dialog.original_view.transform()

    def test_view_toggle_is_documented(self):
        widget, dialog = _dialog()
        assert "显示原图" in dialog.confirm_button.toolTip()
        assert VIEW_HINT in HINT_TEXT

    def test_canvas_pixmap_falls_back_to_widget_image(self):
        image = make_image(make_rgb())
        widget = keep(FakeWidget(image=image))
        widget.canvas.pixmap = QtGui.QPixmap()
        dialog = keep(FilterDialog(widget))
        assert dialog.original_view.has_image()
        assert dialog.preview_view.has_image()

    def test_dialog_can_be_created_without_an_image(self):
        widget = keep(FakeWidget(image=None, with_canvas=True))
        dialog = keep(FilterDialog(widget))
        assert not dialog.original_view.has_image()
        assert not dialog.preview_view.has_image()
        assert dialog.add_filter(core.KIND_PIXEL) == 0
        assert dialog.confirm() is True
        assert widget._filter_tool_controller.active()


class TestChainEditing:
    def test_add_each_kind_in_order(self):
        widget, dialog = _dialog()
        dialog.add_filter(core.KIND_PIXEL)
        dialog.add_filter(core.KIND_EQUALIZE)
        dialog.add_filter(core.KIND_GAUSSIAN)
        dialog.add_filter(core.KIND_GRADIENT)
        assert [spec.kind for spec in dialog.specs()] == [
            core.KIND_PIXEL,
            core.KIND_EQUALIZE,
            core.KIND_GAUSSIAN,
            core.KIND_GRADIENT,
        ]
        assert dialog.filter_list.count() == 4
        texts = [
            dialog.filter_list.item(row).text() for row in range(4)
        ]
        assert "像素值过滤" in texts[0]
        assert "直方图均衡化" in texts[1]
        assert "高斯滤波" in texts[2]
        assert "梯度增强" in texts[3]

    def test_move_up_and_down_reorders(self):
        widget, dialog = _dialog()
        dialog.add_filter(core.KIND_PIXEL)
        dialog.add_filter(core.KIND_GAUSSIAN)
        assert dialog.filter_list.currentRow() == 1
        assert dialog.move_current(-1) is True
        assert [spec.kind for spec in dialog.specs()] == [
            core.KIND_GAUSSIAN,
            core.KIND_PIXEL,
        ]
        assert dialog.move_current(1) is True
        assert [spec.kind for spec in dialog.specs()] == [
            core.KIND_PIXEL,
            core.KIND_GAUSSIAN,
        ]

    def test_remove_and_clear(self):
        widget, dialog = _dialog()
        dialog.add_filter(core.KIND_PIXEL)
        dialog.add_filter(core.KIND_GAUSSIAN)
        dialog.filter_list.setCurrentRow(0)
        assert dialog.remove_current() is True
        assert [spec.kind for spec in dialog.specs()] == [
            core.KIND_GAUSSIAN
        ]
        assert dialog.clear_filters() is True
        assert dialog.specs() == []
        assert dialog.filter_list.count() == 0

    def test_unchecked_filter_is_kept_but_disabled(self):
        widget, dialog = _dialog()
        dialog.add_filter(core.KIND_GAUSSIAN)
        assert dialog.set_enabled(0, False) is True
        assert dialog.specs()[0].enabled is False
        item = dialog.filter_list.item(0)
        assert item.checkState() == QtCore.Qt.CheckState.Unchecked
        assert "已停用" in item.text()
        dialog.add_filter(core.KIND_PIXEL)
        dialog.set_enabled(1, False)
        dialog.confirm()
        assert widget._filter_tool_controller.active() is False


class TestParameters:
    def test_pixel_parameters_and_bounds(self):
        widget, dialog = _dialog()
        dialog.add_filter(core.KIND_PIXEL)
        assert dialog.set_current_params(low=10, high=200, fill=7)
        assert dialog.specs()[0].params == {
            "low": 10,
            "high": 200,
            "fill": 7,
        }
        assert "10" in dialog.filter_list.item(0).text()
        dialog.pixel_low.setValue(220)
        assert dialog.pixel_high.value() == 220
        assert dialog.specs()[0].params["low"] == 220
        assert dialog.specs()[0].params["high"] == 220

    def test_equalize_exclude_parameter(self):
        widget, dialog = _dialog()
        dialog.add_filter(core.KIND_EQUALIZE)
        assert dialog.specs()[0].params == {"exclude": None}
        assert dialog.set_current_params(exclude=0)
        assert dialog.specs()[0].params == {"exclude": 0}
        assert "排除 0" in dialog.filter_list.item(0).text()
        assert dialog.set_current_params(exclude=None)
        assert dialog.specs()[0].params == {"exclude": None}

    def test_gaussian_parameters_normalize_even_kernel(self):
        widget, dialog = _dialog()
        dialog.add_filter(core.KIND_GAUSSIAN)
        assert dialog.set_current_params(kernel=4, sigma=1.5)
        assert dialog.specs()[0].params == {"kernel": 5, "sigma": 1.5}
        assert dialog.blur_kernel.value() == 5
        assert dialog.set_current_params(kernel=7, sigma=0.0)
        assert dialog.specs()[0].params == {"kernel": 7, "sigma": 0.0}

    def test_gradient_parameters_and_mode(self):
        widget, dialog = _dialog()
        dialog.add_filter(core.KIND_GRADIENT)
        assert dialog.specs()[0].params == {
            "mode": "overlay",
            "smooth": 1.0,
            "gain": 3.0,
            "threshold": 0,
        }
        assert dialog.gradient_mode.currentData() == "overlay"
        assert dialog.set_current_params(
            mode="gradient", smooth=0.5, gain=3.0, threshold=20
        )
        assert dialog.specs()[0].params == {
            "mode": "gradient",
            "smooth": 0.5,
            "gain": 3.0,
            "threshold": 20,
        }
        text = dialog.filter_list.item(0).text()
        assert "梯度增强" in text
        assert "梯度图" in text
        assert dialog.param_stack.currentIndex() == dialog._page_index[
            core.KIND_GRADIENT
        ]
        dialog.gradient_gain.setValue(4.0)
        assert dialog.specs()[0].params["gain"] == 4.0
        dialog.gradient_mode.setCurrentIndex(0)
        assert dialog.specs()[0].params["mode"] == "overlay"

    def test_invalid_gradient_mode_is_rejected(self):
        widget, dialog = _dialog()
        dialog.add_filter(core.KIND_GRADIENT)
        assert dialog.set_current_params(mode="bogus") is False
        assert "错误" in dialog.status_label.text()

    def test_invalid_parameters_are_rejected(self):
        widget, dialog = _dialog()
        dialog.add_filter(core.KIND_PIXEL)
        assert dialog.set_current_params(low=200, high=10) is False
        assert "错误" in dialog.status_label.text()


class TestPreview:
    def test_preview_follows_the_chain(self):
        widget, dialog = _dialog()
        original = dialog.original_view.pixmap()
        assert original.toImage().pixelColor(1, 1).getRgb()[:3] != (
            0,
            0,
            0,
        )
        dialog.add_filter(core.KIND_PIXEL)
        assert dialog.set_current_params(low=0, high=0, fill=0)
        dialog._refresh_preview()
        assert _preview_color(dialog)[:3] == (0, 0, 0)
        original_after = dialog.original_view.pixmap()
        assert original_after.toImage().pixelColor(1, 1).getRgb()[:3] != (
            0,
            0,
            0,
        )

    def test_clearing_the_chain_restores_the_preview(self):
        widget, dialog = _dialog()
        dialog.add_filter(core.KIND_PIXEL)
        dialog.set_current_params(low=0, high=0, fill=0)
        dialog._refresh_preview()
        assert _preview_color(dialog)[:3] == (0, 0, 0)
        dialog.clear_filters()
        assert _preview_color(dialog)[:3] != (0, 0, 0)


class TestConfirm:
    def test_confirm_applies_the_chain_to_the_controller(self):
        widget, dialog = _dialog()
        dialog.add_filter(core.KIND_EQUALIZE)
        dialog.add_filter(core.KIND_GAUSSIAN)
        assert dialog.confirm() is True
        assert [
            spec.kind for spec in widget._filter_tool_controller.chain()
        ] == [core.KIND_EQUALIZE, core.KIND_GAUSSIAN]
        messages = widget._status_bar.messages
        assert messages
        assert "滤镜已应用" in messages[-1]
        assert "显示原图" in messages[-1]

    def test_cancel_does_not_change_the_controller(self):
        widget, dialog = _dialog()
        controller = widget._filter_tool_controller
        controller.set_chain([core.default_spec(core.KIND_PIXEL)])
        dialog.add_filter(core.KIND_GAUSSIAN)
        dialog.cancel()
        assert [spec.kind for spec in controller.chain()] == [
            core.KIND_PIXEL
        ]

    def test_confirm_with_an_empty_chain_clears_the_display(self):
        widget, dialog = _dialog()
        controller = widget._filter_tool_controller
        controller.set_chain([core.default_spec(core.KIND_GAUSSIAN)])
        dialog.reload()
        assert dialog.filter_list.count() == 1
        dialog.clear_filters()
        assert dialog.confirm() is True
        assert controller.active() is False
        assert "滤镜已关闭" in widget._status_bar.messages[-1]

    def test_confirm_never_touches_the_raw_image_data(self):
        widget, dialog = _dialog()
        raw_pixmap = widget.canvas.pixmap
        raw_image = widget.image
        raw_pixel = raw_pixmap.toImage().pixelColor(1, 1).getRgb()
        dialog.add_filter(core.KIND_PIXEL)
        dialog.set_current_params(low=0, high=0, fill=0)
        dialog.confirm()
        assert widget.canvas.pixmap is raw_pixmap
        assert widget.image is raw_image
        assert widget.canvas.pixmap.toImage().pixelColor(1, 1).getRgb() == (
            raw_pixel
        )

    def test_invalid_spec_keeps_the_window_open(self):
        widget, dialog = _dialog()
        dialog._specs = [core.FilterSpec("bogus", True, {})]
        assert dialog.confirm() is False
        assert "错误" in dialog.status_label.text()


class TestReload:
    def test_reload_reads_the_active_chain(self):
        widget, dialog = _dialog()
        widget._filter_tool_controller.set_chain(
            [core.default_spec(core.KIND_GAUSSIAN)]
        )
        dialog.reload()
        assert [spec.kind for spec in dialog.specs()] == [
            core.KIND_GAUSSIAN
        ]
        assert dialog.filter_list.count() == 1

    def test_refresh_source_reads_a_new_canvas_pixmap(self):
        widget, dialog = _dialog()
        new_rgb = make_rgb(16, 12)
        new_rgb[:] = 99
        widget.canvas.pixmap = make_pixmap(new_rgb)
        assert dialog.refresh_source() is True
        assert dialog.original_view.pixmap().toImage().pixelColor(
            1, 1
        ).getRgb()[:3] == (99, 99, 99)


def test_install_adds_one_tool_action():
    widget, dialog = _dialog()
    action = installer.install_filter_tool(widget)
    assert action is not None
    assert action.text() == "滤镜工具"
    assert action.toolTip() == installer.ACTION_TIP
    assert len(widget.menus.tool.actions()) == 2
