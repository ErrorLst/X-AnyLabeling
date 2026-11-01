"""Tests of the package surface, the Tool menu hook and the launcher."""

import os.path as osp
import subprocess
import sys
from types import SimpleNamespace

from PyQt6 import QtWidgets, sip

import anylabeling.custom.yolo_to_labelme as package
from anylabeling.custom.yolo_to_labelme import dialog as y2l_dialog
from anylabeling.custom.yolo_to_labelme import installer

from conftest import FakeWidget

PROBE = (
    "import importlib.util, json, sys;"
    "spec = importlib.util.spec_from_file_location('y2l_core', %r);"
    "module = importlib.util.module_from_spec(spec);"
    "sys.modules['y2l_core'] = module;"
    "spec.loader.exec_module(module);"
    "print(module.MODE_AUTO);"
    "print('PyQt6' in sys.modules);"
    "print('cv2' in sys.modules);"
    "print('numpy' in sys.modules)"
)

_ALIVE = []


def _keep(*objects):
    _ALIVE.extend(objects)


def _menu():
    return QtWidgets.QMenu()


def _core_path():
    root = osp.dirname(package.__file__)
    return osp.join(root, "yolo_core.py")


def _conversion_path():
    root = osp.dirname(package.__file__)
    return osp.join(root, "conversion.py")


class TestPackageSurface:
    def test_all(self):
        assert package.__all__ == [
            "install_yolo_to_labelme",
            "launch_yolo_to_labelme",
        ]

    def test_symbols(self):
        assert callable(package.install_yolo_to_labelme)
        assert callable(package.launch_yolo_to_labelme)

    def test_core_imports_without_desktop_stack(self):
        result = subprocess.run(
            [sys.executable, "-c", PROBE % _core_path()],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        lines = result.stdout.strip().splitlines()
        assert lines == ["auto", "False", "False", "False"]

    def test_conversion_has_no_top_level_qt(self):
        with open(_conversion_path(), encoding="utf-8") as handle:
            text = handle.read()
        assert "PyQt6" not in text
        assert "import cv2" not in text
        assert "import numpy" not in text


class TestInstall:
    def test_action_is_appended(self):
        menu = _menu()
        widget = FakeWidget(menu)
        action = installer.install_yolo_to_labelme(widget)
        assert action is not None
        assert action.text() == "YOLO 转 LabelMe"
        assert menu.actions()[-1] is action
        assert "class.txt" in action.toolTip()

    def test_installed_once(self):
        menu = _menu()
        widget = FakeWidget(menu)
        first = installer.install_yolo_to_labelme(widget)
        second = installer.install_yolo_to_labelme(widget)
        assert first is second
        assert len(menu.actions()) == 1

    def test_without_menus(self):
        widget = FakeWidget()
        assert installer.install_yolo_to_labelme(widget) is None

    def test_without_tool_menu(self):
        widget = FakeWidget()
        widget.menus = object()
        assert installer.install_yolo_to_labelme(widget) is None

    def test_existing_action_is_reused(self):
        menu = _menu()
        widget = FakeWidget(menu)
        widget._yolo_to_labelme_action = "sentinel"
        assert installer.install_yolo_to_labelme(widget) == "sentinel"
        assert menu.actions() == []


class TestLauncher:
    def test_returns_a_dialog(self, qapp):
        widget = QtWidgets.QWidget()
        dialog = package.launch_yolo_to_labelme(widget)
        _keep(widget, dialog)
        assert isinstance(dialog, y2l_dialog.YoloToLabelmeDialog)
        assert widget._yolo_to_labelme_dialog is dialog
        dialog.close()

    def test_reuses_the_instance(self, qapp):
        widget = QtWidgets.QWidget()
        first = package.launch_yolo_to_labelme(widget)
        second = package.launch_yolo_to_labelme(widget)
        _keep(widget, first)
        assert first is second
        first.close()

    def test_destroyed_clears_the_attribute(self, qapp):
        widget = QtWidgets.QWidget()
        dialog = package.launch_yolo_to_labelme(widget)
        _keep(widget)
        sip.delete(dialog)
        assert widget._yolo_to_labelme_dialog is None

    def test_without_parent(self, qapp):
        dialog = package.launch_yolo_to_labelme(None)
        _keep(dialog)
        assert dialog.windowTitle() == "YOLO 转 LabelMe"
        dialog.close()

    def test_menu_action_opens_the_window(self, qapp):
        widget = QtWidgets.QWidget()
        widget.menus = SimpleNamespace(tool=_menu())
        _keep(widget, widget.menus.tool)
        action = installer.install_yolo_to_labelme(widget)
        action.trigger()
        dialog = widget._yolo_to_labelme_dialog
        _keep(dialog)
        assert dialog is not None
        assert dialog.windowTitle() == "YOLO 转 LabelMe"
        dialog.close()
