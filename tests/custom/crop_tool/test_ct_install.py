"""Tests of the package surface, the Tool menu hook and the launcher."""

import os
import subprocess
import sys
from types import SimpleNamespace

from PyQt6 import QtWidgets, sip

import anylabeling.custom.crop_tool as crop_tool
from anylabeling.custom.crop_tool import dialog as ct_dialog

PROBE = (
    "import importlib.util, sys;"
    "spec = importlib.util.spec_from_file_location('ct_probe', %r);"
    "module = importlib.util.module_from_spec(spec);"
    "sys.modules['ct_probe'] = module;"
    "spec.loader.exec_module(module);"
    "print(module.natural_key('a10'));"
    "print(sorted(['a.jpg', '1.jpg', '10.jpg'],"
    " key=module.natural_key));"
    "print('PyQt6' in sys.modules)"
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


def _core_path():
    """Return the path of the rules module."""

    root = os.path.dirname(crop_tool.__file__)
    return os.path.join(root, "crop_core.py")


class TestPackageSurface:
    """The package exports the three public entry points."""

    def test_all(self):
        assert crop_tool.__all__ == [
            "CropDialog",
            "install_crop_tool",
            "launch_crop_tool",
        ]

    def test_symbols(self):
        assert crop_tool.CropDialog is ct_dialog.CropDialog
        assert callable(crop_tool.install_crop_tool)
        assert callable(crop_tool.launch_crop_tool)

    def test_dialog_matches_the_launcher_check(self):
        assert ct_dialog.WINDOW_TITLE == "裁图工具"

    def test_crop_core_imports_without_qt(self):
        result = subprocess.run(
            [sys.executable, "-c", PROBE % _core_path()],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        lines = result.stdout.strip().splitlines()
        assert lines[-3] == "[(1, 'a'), (0, 10)]"
        assert lines[-2] == "['1.jpg', '10.jpg', 'a.jpg']"
        assert lines[-1] == "False"

    def test_crop_core_source_has_no_qt(self):
        with open(_core_path(), encoding="utf-8") as handle:
            text = handle.read()
        assert "PyQt6" not in text
        assert "QtWidgets" not in text


class TestInstall:
    """The Tool menu gets exactly one crop entry."""

    def test_action_is_appended(self):
        menu = _menu()
        widget = FakeWidget(menu)
        action = ct_dialog.install_crop_tool(widget)
        assert action is not None
        assert action.text() == ct_dialog.ACTION_TEXT
        assert menu.actions()[-1] is action

    def test_action_carries_the_tip(self):
        menu = _menu()
        widget = FakeWidget(menu)
        action = ct_dialog.install_crop_tool(widget)
        assert action.toolTip() == ct_dialog.ACTION_TIP
        assert action.statusTip() == ct_dialog.ACTION_TIP

    def test_installed_once(self):
        menu = _menu()
        widget = FakeWidget(menu)
        first = ct_dialog.install_crop_tool(widget)
        second = ct_dialog.install_crop_tool(widget)
        assert first is second
        assert len(menu.actions()) == 1

    def test_without_menus(self):
        widget = FakeWidget()
        assert ct_dialog.install_crop_tool(widget) is None

    def test_without_tool_menu(self):
        widget = FakeWidget()
        widget.menus = object()
        assert ct_dialog.install_crop_tool(widget) is None

    def test_existing_action_is_reused(self):
        menu = _menu()
        widget = FakeWidget(menu)
        widget._crop_tool_action = "sentinel"
        assert ct_dialog.install_crop_tool(widget) == "sentinel"
        assert menu.actions() == []


class TestModuleConstants:
    """The strings the window shows are pinned by the module."""

    def test_action_constants(self):
        assert ct_dialog.ACTION_TEXT == "裁图工具"
        assert ct_dialog.ACTION_ICON == "crop"
        assert "裁切" in ct_dialog.ACTION_TIP

    def test_hint_texts(self):
        assert "A/D" in ct_dialog.HINT_TEXT
        assert "右键" in ct_dialog.HINT_TEXT
        assert "A/D" in ct_dialog.KEYS_TEXT
        assert "右键" in ct_dialog.KEYS_TEXT

    def test_message_formats(self):
        assert ct_dialog.EMPTY_HINT == (
            "把图片目录拖到这里，或点「打开目录」"
        )
        assert ct_dialog.DELETED_FORMAT % 2 == "已删除 2 个裁切子图"
        assert ct_dialog.COORD_FORMAT % (1, 2) == "坐标: (1, 2)"
        assert ct_dialog.COUNT_FORMAT % 3 == "已裁切: 3 次"
        assert ct_dialog.BOX_FORMAT % (4, 5) == "框: 4×5"
        assert ct_dialog.INDEX_FORMAT % (1, 9) == "1 / 9"


class TestLauncher:
    """The launcher keeps one window per widget."""

    def test_returns_a_dialog(self, qapp):
        widget = QtWidgets.QWidget()
        dialog = crop_tool.launch_crop_tool(widget)
        _keep(widget, dialog)
        assert isinstance(dialog, ct_dialog.CropDialog)
        assert widget._crop_tool_dialog is dialog

    def test_reuses_the_instance(self, qapp):
        widget = QtWidgets.QWidget()
        first = crop_tool.launch_crop_tool(widget)
        second = crop_tool.launch_crop_tool(widget)
        _keep(widget, first)
        assert first is second

    def test_reuse_does_not_rescan(self, qapp, ct_store, ct_dir):
        from conftest import write_pil

        write_pil(ct_dir, "a.png", (20, 20))
        settings, _ini = ct_store
        widget = QtWidgets.QWidget()
        dialog = ct_dialog.CropDialog(widget, settings)
        widget._crop_tool_dialog = dialog
        _keep(widget, dialog)
        before = dialog.image_count()
        again = crop_tool.launch_crop_tool(widget)
        assert again is dialog
        assert again.image_count() == before

    def test_destroyed_clears_the_attribute(self, qapp):
        widget = QtWidgets.QWidget()
        dialog = crop_tool.launch_crop_tool(widget)
        _keep(widget)
        sip.delete(dialog)
        assert widget._crop_tool_dialog is None

    def test_without_parent(self, qapp):
        dialog = crop_tool.launch_crop_tool(None)
        _keep(dialog)
        assert isinstance(dialog, ct_dialog.CropDialog)
        dialog.close()

    def test_menu_action_opens_the_window(self, qapp):
        widget = QtWidgets.QWidget()
        widget.menus = SimpleNamespace(tool=_menu())
        _keep(widget, widget.menus.tool)
        action = ct_dialog.install_crop_tool(widget)
        action.trigger()
        dialog = widget._crop_tool_dialog
        _keep(dialog)
        assert isinstance(dialog, ct_dialog.CropDialog)
        assert dialog.windowTitle() == ct_dialog.WINDOW_TITLE
        dialog.close()
