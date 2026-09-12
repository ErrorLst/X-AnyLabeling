"""The tool is titled 模型验证: no user visible English title is left.

The window title, the title of the configuration page, the Tools menu
entry of the main window and the model snapshot that reaches the report
all spell the tool in Chinese. The English name of the previous revision
is refused by the guard below: it appears in no user visible line of the
model validation tree, and in no line of the one menu of the main window
that opens the tool.
"""

import os
import os.path as osp

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from PyQt6 import QtWidgets

from anylabeling.custom.model_validation import inference as inference_module
from anylabeling.custom.model_validation.inference import MODEL_DISPLAY_NAME
from anylabeling.custom.model_validation.ui.dialog import (
    WINDOW_TITLE,
    ModelValidationDialog,
)

TITLE = "模型验证"
# the spelling the guard of this file refuses: the needle of the source
# scan, never a title any user reads
ENGLISH = "Model Validation"
HERE = osp.dirname(osp.abspath(__file__))
REPO_ROOT = osp.dirname(osp.dirname(osp.dirname(HERE)))
MENU_SOURCE = osp.join(
    REPO_ROOT, "anylabeling", "views", "labeling", "label_widget.py"
)
PACKAGE_ROOT = osp.join(REPO_ROOT, "anylabeling", "custom", "model_validation")


@pytest.fixture(scope="module")
def qt_app():
    "Create the offscreen application the title tests need."

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


@pytest.fixture
def dialog(qt_app):
    "Create a model validation window and close it afterwards."

    window = ModelValidationDialog()
    try:
        yield window
    finally:
        window.close()


def source_files() -> list:
    "Return every source file that used to spell the English title."

    paths = [MENU_SOURCE]
    for root, _dirs, names in os.walk(PACKAGE_ROOT):
        paths += [
            osp.join(root, name)
            for name in sorted(names)
            if name.endswith(".py")
        ]
    return paths


def test_no_user_visible_english_title_is_left():
    "The English spelling is gone from the page, the window and the menu."

    offenders = []
    for path in source_files():
        with open(path, encoding="utf-8") as handle:
            if ENGLISH in handle.read():
                offenders.append(osp.relpath(path, REPO_ROOT))
    assert offenders == []

    # and the Chinese title really is written down where the user reads it
    with open(MENU_SOURCE, encoding="utf-8") as handle:
        menu_lines = [line.strip() for line in handle.read().splitlines()]
    assert 'self.tr("模型验证"),' in menu_lines


def test_the_window_title_is_chinese(dialog):
    "The title bar of the window reads 模型验证."

    assert WINDOW_TITLE == TITLE
    assert dialog.windowTitle() == TITLE


def test_the_configuration_page_is_titled_in_chinese(dialog):
    "The first line of the form reads 模型验证, with no English twin."

    labels = [
        label.text()
        for label in dialog.config_page.findChildren(QtWidgets.QLabel)
    ]
    assert TITLE in labels
    assert ENGLISH not in labels


def test_the_model_snapshot_names_the_tool_in_chinese():
    "The name a run hands to the model snapshot and to the report."

    assert MODEL_DISPLAY_NAME == TITLE
    with open(inference_module.__file__, encoding="utf-8") as handle:
        source = handle.read()
    assert '"display_name": MODEL_DISPLAY_NAME' in source
