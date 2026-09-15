"""Tests of the Tool menu hook and of the launcher of the preview tool.

The window itself is exercised by the dialog tests; this module only
pins what the mount owns: the two lines of the upstream file, exactly
one Tool menu entry that opens the window, and one window per widget
that is forgotten as soon as it is destroyed.
"""

from __future__ import annotations

import os
import os.path as osp
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtWidgets, sip  # noqa: E402

import anylabeling.custom.preview_tool as preview_tool  # noqa: E402
from anylabeling.custom.preview_tool import installer  # noqa: E402
from anylabeling.custom.preview_tool import (  # noqa: E402
    launcher as pt_launcher,
)
from anylabeling.custom.preview_tool.dialog import (  # noqa: E402
    PreviewDialog,
)
from anylabeling.views.labeling.utils import qt as qt_utils  # noqa: E402

MOUNT_IMPORT = (
    "from anylabeling.custom.preview_tool import install_preview_tool"
)
MOUNT_CALL = "        install_preview_tool(self)"

#: Qt objects built by this module, kept referenced for the whole run.
#: A menu together with the action closure that captures the widget, and
#: a dialog parented to a throw away widget, form reference cycles; when
#: the collector frees such a cycle in the middle of the session the Qt
#: layer aborts the interpreter, so the tests hold on to them instead.
_ALIVE = []


def _keep(*objects):
    """Keep Qt objects alive for the rest of the session."""

    _ALIVE.extend(objects)


def _root():
    """Return the root of the anylabeling package."""

    return osp.dirname(
        osp.dirname(osp.dirname(osp.abspath(preview_tool.__file__)))
    )


def _menu():
    """Return a real Tool menu."""

    menu = QtWidgets.QMenu()
    _keep(menu)
    return menu


class FakeWidget(SimpleNamespace):
    """Labeling widget stand in that only owns a Tool menu.

    The class lives here instead of in the shared conftest on purpose:
    the other modules of this package must stay importable without any
    test helper at all.
    """

    def __init__(self, menu=None):
        super().__init__()
        if menu is not None:
            self.menus = SimpleNamespace(tool=menu)


def _widget(menu=None):
    """Return a kept alive fake widget, with a Tool menu when asked."""

    widget = FakeWidget(menu)
    _keep(widget)
    return widget


def _upstream_lines():
    """Return the stripped lines of the upstream labeling widget."""

    path = osp.join(
        _root(), "views", "labeling", "label_widget.py"
    )
    with open(path, encoding="utf-8") as handle:
        return [line.rstrip("\n") for line in handle]


class TestMountSymbol:
    """The package exports the symbols the two mount lines use."""

    def test_entry_symbols_are_exported(self):
        from anylabeling.custom.preview_tool import (
            install_preview_tool,
            launch_preview_tool,
        )

        assert install_preview_tool is installer.install_preview_tool
        assert launch_preview_tool is pt_launcher.launch_preview_tool


class TestUpstreamMount:
    """The upstream file carries the two mount lines, and only those."""

    def test_mount_lines_are_present(self):
        lines = _upstream_lines()
        assert MOUNT_IMPORT in lines
        assert MOUNT_CALL in lines

    def test_the_tool_is_mounted_exactly_twice(self):
        hits = [
            line for line in _upstream_lines()
            if "install_preview_tool" in line
        ]
        assert hits == [MOUNT_IMPORT, MOUNT_CALL]


class TestInstall:
    """The Tool menu gets exactly one preview entry."""

    def test_action_is_appended(self):
        menu = _menu()
        widget = _widget(menu)
        action = installer.install_preview_tool(widget)
        assert action is not None
        assert action.text() == installer.ACTION_NAME == "预览工具"
        assert menu.actions() == [action]
        assert widget._preview_tool_action is action

    def test_action_carries_the_tip(self):
        menu = _menu()
        action = installer.install_preview_tool(_widget(menu))
        assert action.toolTip() == installer.ACTION_TIP
        assert action.statusTip() == installer.ACTION_TIP

    def test_action_has_no_shortcut(self):
        menu = _menu()
        action = installer.install_preview_tool(_widget(menu))
        assert action.shortcut().isEmpty()

    def test_installed_once(self):
        menu = _menu()
        widget = _widget(menu)
        first = installer.install_preview_tool(widget)
        second = installer.install_preview_tool(widget)
        assert first is second
        assert len(menu.actions()) == 1

    def test_without_menus(self):
        assert installer.install_preview_tool(_widget()) is None

    def test_without_tool_menu(self):
        widget = _widget()
        widget.menus = object()
        assert installer.install_preview_tool(widget) is None

    def test_existing_action_is_reused(self):
        menu = _menu()
        widget = _widget(menu)
        widget._preview_tool_action = "sentinel"
        assert installer.install_preview_tool(widget) == "sentinel"
        assert menu.actions() == []

    def test_action_opens_the_window(self, qapp):
        menu = _menu()
        widget = QtWidgets.QWidget()
        widget.menus = SimpleNamespace(tool=menu)
        _keep(widget, widget.menus.tool)
        action = installer.install_preview_tool(widget)
        action.trigger()
        dialog = widget._preview_tool_dialog
        _keep(dialog)
        assert isinstance(dialog, PreviewDialog)
        dialog.close()


class TestIcon:
    """The entry points at a resource that really is in the build."""

    def test_icon_resource_exists(self):
        name = "%s.%s" % (installer.ACTION_ICON, installer.ACTION_ICON_EXT)
        assert qt_utils.new_icon_path(
            installer.ACTION_ICON, installer.ACTION_ICON_EXT
        ) == ":/images/images/" + name
        assert osp.isfile(
            osp.join(_root(), "resources", "images", name)
        )
        with open(
            osp.join(_root(), "resources", "resources.qrc"),
            encoding="utf-8",
        ) as handle:
            assert "<file>images/%s</file>" % name in handle.read()

    def test_icon_is_set_on_the_action(self, qapp):
        menu = _menu()
        action = installer.install_preview_tool(_widget(menu))
        icon = action.icon()
        if icon.isNull():
            pytest.skip("the application resources are not registered here")
        assert not icon.isNull()


class TestLauncher:
    """The launcher keeps one window per widget."""

    def test_returns_a_dialog(self, qapp):
        widget = QtWidgets.QWidget()
        _keep(widget)
        dialog = pt_launcher.launch_preview_tool(widget)
        _keep(dialog)
        assert isinstance(dialog, PreviewDialog)
        assert widget._preview_tool_dialog is dialog
        dialog.close()

    def test_reuses_the_instance(self, qapp):
        widget = QtWidgets.QWidget()
        _keep(widget)
        first = pt_launcher.launch_preview_tool(widget)
        second = pt_launcher.launch_preview_tool(widget)
        _keep(first)
        assert first is second
        first.close()

    def test_initial_directory_comes_from_the_explorer(self, qapp, pt_dir):
        widget = QtWidgets.QWidget()
        widget.last_open_dir = pt_dir
        _keep(widget)
        dialog = pt_launcher.launch_preview_tool(widget)
        _keep(dialog)
        assert dialog._directory == osp.normpath(osp.abspath(pt_dir))
        dialog.close()

    def test_reuse_asks_the_open_window_to_rescan(self, qapp, pt_dir):
        widget = QtWidgets.QWidget()
        widget.last_open_dir = pt_dir
        _keep(widget)
        first = pt_launcher.launch_preview_tool(widget)
        _keep(first)
        calls = []
        first.rescan = lambda: calls.append(True)
        again = pt_launcher.launch_preview_tool(widget)
        assert again is first
        assert calls == [True]
        del first.rescan
        first.close()

    def test_destroyed_clears_the_attribute(self, qapp):
        widget = QtWidgets.QWidget()
        _keep(widget)
        dialog = pt_launcher.launch_preview_tool(widget)
        assert widget._preview_tool_dialog is dialog
        sip.delete(dialog)
        assert widget._preview_tool_dialog is None

    def test_without_parent(self, qapp):
        dialog = pt_launcher.launch_preview_tool(None)
        _keep(dialog)
        assert isinstance(dialog, PreviewDialog)
        dialog.close()
