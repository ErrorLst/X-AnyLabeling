"""Tests of the upstream mount lines and of the window launcher."""

from __future__ import annotations

import os.path as osp

from anylabeling.custom.filter_tool import core, installer, launcher
import anylabeling.custom.filter_tool as filter_tool

from conftest import FakeWidget, keep, make_image, make_rgb


MOUNT_IMPORT = (
    "from anylabeling.custom.filter_tool import install_filter_tool"
)
MOUNT_CALL = "install_filter_tool(self)"


def _root():
    return osp.dirname(
        osp.dirname(osp.dirname(osp.abspath(filter_tool.__file__)))
    )


def _upstream_lines():
    path = osp.join(
        _root(), "views", "labeling", "label_widget.py"
    )
    with open(path, encoding="utf-8") as handle:
        return [line.rstrip("\n") for line in handle]


class TestUpstreamMount:
    def test_import_and_call_lines_are_present(self):
        lines = _upstream_lines()
        assert MOUNT_IMPORT in lines
        assert any(line.strip().startswith(MOUNT_CALL) for line in lines)

    def test_the_tool_is_mounted_exactly_twice(self):
        hits = [
            line for line in _upstream_lines()
            if "install_filter_tool" in line
        ]
        assert len(hits) == 2


class TestInstaller:
    def test_action_is_appended_once(self):
        widget = keep(FakeWidget(image=make_image(make_rgb())))
        action = installer.install_filter_tool(widget)
        assert action is not None
        assert action.text() == "滤镜工具"
        assert action.toolTip() == installer.ACTION_TIP
        assert action.statusTip() == installer.ACTION_TIP
        assert action.shortcut().isEmpty()
        assert widget._filter_tool_action is action
        assert len(widget.menus.tool.actions()) == 1
        second = installer.install_filter_tool(widget)
        assert second is action
        assert len(widget.menus.tool.actions()) == 1

    def test_without_tool_menu_returns_none(self):
        widget = keep(
            FakeWidget(image=make_image(make_rgb()), with_menu=False)
        )
        assert installer.install_filter_tool(widget) is None

    def test_controller_and_paint_hook_are_installed(self):
        widget = keep(FakeWidget(image=make_image(make_rgb())))
        installer.install_filter_tool(widget)
        controller = widget._filter_tool_controller
        assert controller is not None
        assert widget.canvas._filter_tool_paint_wrapped is True
        controller.set_chain([core.default_spec(core.KIND_GAUSSIAN)])
        assert "生效中" in widget._filter_tool_action.toolTip()


class TestLauncher:
    def test_launcher_reuses_one_window(self):
        widget = keep(FakeWidget(image=make_image(make_rgb())))
        first = launcher.launch_filter_tool(widget)
        keep(first)
        second = launcher.launch_filter_tool(widget)
        assert first is second
        assert widget._filter_tool_dialog is first

    def test_hidden_window_is_reloaded(self):
        widget = keep(FakeWidget(image=make_image(make_rgb())))
        dialog = keep(launcher.launch_filter_tool(widget))
        widget._filter_tool_controller.set_chain(
            [core.default_spec(core.KIND_PIXEL)]
        )
        dialog.hide()
        again = launcher.launch_filter_tool(widget)
        assert again is dialog
        assert [spec.kind for spec in dialog.specs()] == [
            core.KIND_PIXEL
        ]
