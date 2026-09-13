"""Tests of the package surface, the Tool menu hook and the launcher."""

import os
import subprocess
import sys
from types import SimpleNamespace

from PyQt6 import QtWidgets, sip

import anylabeling.custom.rename_tool as rename_tool
from anylabeling.custom.rename_tool import dialog as rt_dialog

from conftest import FakeWidget

PROBE = (
    "import importlib.util, sys;"
    "spec = importlib.util.spec_from_file_location(\"rt_probe\", %r);"
    "module = importlib.util.module_from_spec(spec);"
    "sys.modules[\"rt_probe\"] = module;"
    "spec.loader.exec_module(module);"
    "print(module.natural_key(\"a10\"));"
    "print(\"PyQt6\" in sys.modules)"
)


#: Qt objects built by this module, kept referenced for the whole run.
#: A menu together with the action closures that capture the widget, and
#: a dialog parented to a throw away widget, form reference cycles; when
#: the collector frees such a cycle in the middle of the session the Qt
#: layer aborts the interpreter, so the tests hold on to them instead.
_ALIVE = []


def _keep(*objects):
    """Keep Qt objects alive for the rest of the session."""

    _ALIVE.extend(objects)


def _menu():
    """Return a real Tool menu."""

    return QtWidgets.QMenu()


def _core_path():
    """Return the path of the rules module."""

    root = os.path.dirname(rename_tool.__file__)
    return os.path.join(root, "rename_core.py")


class TestPackageSurface:
    """The package exports the three public entry points."""

    def test_all(self):
        assert rename_tool.__all__ == [
            "RenameDialog",
            "install_rename_tool",
            "launch_rename_tool",
        ]

    def test_symbols(self):
        assert rename_tool.RenameDialog is rt_dialog.RenameDialog
        assert callable(rename_tool.install_rename_tool)
        assert callable(rename_tool.launch_rename_tool)

    def test_rename_core_imports_without_qt(self):
        result = subprocess.run(
            [sys.executable, "-c", PROBE % _core_path()],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        lines = result.stdout.strip().splitlines()
        assert lines[0] == "[\'a\', 10]"
        assert lines[1] == "False"


class TestInstall:
    """The Tool menu gets exactly one rename entry."""

    def test_action_is_appended(self):
        menu = _menu()
        widget = FakeWidget(menu)
        action = rt_dialog.install_rename_tool(widget)
        assert action is not None
        assert action.text() == "重命名"
        assert menu.actions()[-1] is action

    def test_installed_once(self):
        menu = _menu()
        widget = FakeWidget(menu)
        first = rt_dialog.install_rename_tool(widget)
        second = rt_dialog.install_rename_tool(widget)
        assert first is second
        assert len(menu.actions()) == 1

    def test_action_keeps_its_tooltip(self):
        menu = _menu()
        widget = FakeWidget(menu)
        action = rt_dialog.install_rename_tool(widget)
        assert "zip" in action.toolTip()

    def test_without_menus(self):
        widget = FakeWidget()
        assert rt_dialog.install_rename_tool(widget) is None

    def test_without_tool_menu(self):
        widget = FakeWidget()
        widget.menus = object()
        assert rt_dialog.install_rename_tool(widget) is None

    def test_existing_action_is_reused(self):
        menu = _menu()
        widget = FakeWidget(menu)
        widget._rename_tool_action = "sentinel"
        assert rt_dialog.install_rename_tool(widget) == "sentinel"
        assert menu.actions() == []


class TestLauncher:
    """The launcher reuses one window per widget."""

    def test_returns_a_dialog(self, qapp):
        widget = QtWidgets.QWidget()
        dialog = rename_tool.launch_rename_tool(widget)
        _keep(widget, dialog)
        assert isinstance(dialog, rt_dialog.RenameDialog)
        assert widget._rename_tool_dialog is dialog

    def test_reuses_the_instance(self, qapp):
        widget = QtWidgets.QWidget()
        first = rename_tool.launch_rename_tool(widget)
        second = rename_tool.launch_rename_tool(widget)
        _keep(widget, first)
        assert first is second

    def test_destroyed_clears_the_attribute(self, qapp):
        widget = QtWidgets.QWidget()
        dialog = rename_tool.launch_rename_tool(widget)
        _keep(widget)
        sip.delete(dialog)
        assert widget._rename_tool_dialog is None

    def test_without_parent(self, qapp):
        dialog = rename_tool.launch_rename_tool(None)
        _keep(dialog)
        assert isinstance(dialog, rt_dialog.RenameDialog)
        dialog.close()

    def test_menu_action_opens_the_window(self, qapp):
        widget = QtWidgets.QWidget()
        widget.menus = SimpleNamespace(tool=_menu())
        _keep(widget, widget.menus.tool)
        action = rt_dialog.install_rename_tool(widget)
        action.trigger()
        dialog = widget._rename_tool_dialog
        _keep(dialog)
        assert isinstance(dialog, rt_dialog.RenameDialog)
        dialog.close()

    def test_dialog_title(self, qapp):
        widget = QtWidgets.QWidget()
        dialog = rename_tool.launch_rename_tool(widget)
        _keep(widget, dialog)
        assert dialog.windowTitle() == rt_dialog.WINDOW_TITLE
        dialog.close()


class TestCoreModule:
    """The rules module stays importable without the Qt layer."""

    def test_public_names(self):
        from anylabeling.custom.rename_tool import rename_core as core

        for name in core.__all__:
            assert hasattr(core, name), name

    def test_no_qt_import_in_source(self):
        with open(_core_path(), encoding="utf-8") as handle:
            text = handle.read()
        assert "PyQt6" not in text
        assert "QtWidgets" not in text
