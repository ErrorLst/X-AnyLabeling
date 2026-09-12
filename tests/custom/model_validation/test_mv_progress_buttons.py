"""The two pages and the controls they keep.

The progress page carries a single control: the cancel. A cancel is the
only way out of a run - it stops the worker, throws the run away and
puts the configuration page back on screen. The second button this page
used to carry ("back to the form") left the worker running in the
background, so it is gone for good: one page, one button, and no signal
of its own to leave the page without cancelling.

The results page lost both of the buttons of the previous revision: the
"Copy" button next to the read only staging path (the path itself is
selectable, so it can still be copied by hand) and the "back to the
form" button, whose job the cancel path of the window already does
through show_config. The three batch commands of the list (select every
augmented copy, clear them again, toggle the delete mark of the selected
originals) left the page as well: they stay on the context menu of the
list, which takes no room of its own. What is left on the page is the
filter combo of the toolbar and the export button.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from PyQt6 import QtWidgets

from anylabeling.custom.model_validation import records as records_module
from anylabeling.custom.model_validation.ui.dialog import (
    CANCELLED_STATUS,
    ModelValidationDialog,
)
from anylabeling.custom.model_validation.ui.progress_page import (
    CANCEL_MESSAGE,
    ProgressPage,
)
from anylabeling.custom.model_validation.ui.results_page import ResultsPage

CANCEL_TEXT = "\u53d6\u6d88"
BACK_TEXT = "\u8fd4\u56de\u914d\u7f6e"
COPY_TEXT = "Copy"
# the one push button the results page keeps: the export of the current
# selection. The filter combo of the toolbar is no push button.
EXPORT_TEXT = "导出 Zip..."
# the three batch commands of the previous revision: their buttons are
# gone, the commands themselves stay on the context menu of the list.
BATCH_TEXTS = (
    "增强图全选加入",
    "全部取消",
    "删除/恢复选中原图",
)


@pytest.fixture(scope="module")
def qt_app():
    "Create the offscreen application shared by the button tests."

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


def page_button_texts(page) -> list:
    "Return the text of every push button owned by one page, in order."

    return [
        button.text() for button in page.findChildren(QtWidgets.QPushButton)
    ]


def make_record(relpath: str):
    "Build one staged original record, without touching the disk."

    return records_module.make_record(
        records_module.KIND_ORIGINAL,
        relpath,
        "/staging/original/images/" + relpath,
        "/staging/original/labels/" + os.path.splitext(relpath)[0] + ".json",
    )


def test_the_progress_page_has_exactly_one_button(dialog):
    "One control, the cancel, and nothing that leaves the run behind."

    page = dialog.progress_page
    buttons = page.findChildren(QtWidgets.QPushButton)

    assert len(buttons) == 1
    assert buttons[0] is page.cancel_button
    assert [button.text() for button in buttons] == [CANCEL_TEXT]
    assert BACK_TEXT not in page_button_texts(page)


def test_the_cancel_stays_right_aligned(dialog):
    "The single button keeps the alignment the button row always had."

    page = dialog.progress_page
    row = page.layout().itemAt(page.layout().count() - 1).layout()
    first = row.itemAt(0)

    assert len(row) == 2
    assert first.spacerItem() is not None
    assert row.itemAt(1).widget() is page.cancel_button


def test_the_progress_page_has_no_back_button_left(dialog):
    "Neither the widget nor its signal survived the removal."

    page = dialog.progress_page

    assert not hasattr(page, "back_button")
    assert not hasattr(page, "back_requested")
    assert not hasattr(ProgressPage, "back_requested")
    # the cancel signal is the declared hook of the page
    assert hasattr(page, "cancel_requested")


def test_the_cancel_button_emits_the_cancel_signal(dialog):
    "A click asks for a cancel, it never merely leaves the page."

    page = dialog.progress_page
    seen = []
    page.cancel_requested.connect(lambda: seen.append("cancel"))

    page.cancel_button.click()

    assert seen == ["cancel"]


def test_the_only_button_click_drops_the_run_and_shows_the_form(dialog):
    "The single control ends the run and takes the window back to the form."

    page = dialog.progress_page
    page.set_progress("infer", 3, 10, "Inference 3/10")
    dialog.records = [make_record("a0.png")]
    dialog.warnings = ["a leftover warning"]
    dialog.stack.setCurrentWidget(page)

    page.cancel_button.click()

    assert dialog.stack.currentWidget() is dialog.config_page
    assert dialog.config_page.status_label.text() == CANCELLED_STATUS
    assert dialog.records == []
    assert dialog.warnings == []
    assert page.cancel_button.isEnabled() is False
    assert page.message_label.text() == CANCEL_MESSAGE


def test_the_results_page_has_no_back_button_left(dialog):
    "Neither the widget nor its signal survived the removal."

    page = dialog.results_page

    assert not hasattr(page, "back_button")
    assert not hasattr(page, "back_requested")
    assert not hasattr(ResultsPage, "back_requested")
    assert BACK_TEXT not in page_button_texts(page)


def test_the_form_keeps_its_programmatic_way_back(dialog):
    """The cancel path still leaves the results page through show_config.

    The page owns no button for it any more, but the window keeps the
    switch itself: a cancel comes back to the form with the records of
    the run intact.
    """

    page = dialog.results_page
    staged = [make_record("a0.png")]
    dialog.records = list(staged)
    dialog.stack.setCurrentWidget(page)

    dialog.show_config()

    assert dialog.records == staged
    assert dialog.stack.currentWidget() is dialog.config_page
    assert dialog.config_page.status_label.text() != CANCELLED_STATUS


def test_the_results_page_has_no_copy_button(dialog):
    "The path line stays, the button that copied it is gone."

    page = dialog.results_page
    page.set_context([], "/staging/root")

    assert not hasattr(page, "copy_button")
    assert not hasattr(page, "_copy_staging")
    assert COPY_TEXT not in page_button_texts(page)
    # the path is still on screen and still read only, so the user
    # selects the text and copies it by hand
    assert page.staging_edit.isReadOnly() is True
    assert page.staging_edit.text() == "/staging/root"


def test_the_results_page_owns_no_batch_button(dialog):
    "The three batch commands left the toolbar together with their widgets."

    page = dialog.results_page

    for name in ("select_all_button", "clear_all_button", "delete_button"):
        assert not hasattr(page, name)
    texts = page_button_texts(page)
    for text in BATCH_TEXTS:
        assert text not in texts
    # the commands themselves stay: the context menu of the list is their
    # entry point (see test_mv_results_marks)
    for name in (
        "select_all_augmented",
        "clear_all_augmented",
        "toggle_selected_deleted",
    ):
        assert callable(getattr(page, name))


def test_the_results_page_keeps_the_export_alone(dialog):
    "The bottom line holds the export and the page owns no other button."

    page = dialog.results_page
    buttons = page.findChildren(QtWidgets.QPushButton)

    assert [button.text() for button in buttons] == [EXPORT_TEXT]
    assert buttons == [page.export_button]


def test_the_dialog_only_wires_the_cancel_of_the_progress_page(dialog):
    "The window has no leftover connection to a back signal."

    assert not hasattr(dialog.progress_page, "back_requested")
    assert not hasattr(dialog.results_page, "back_requested")
    assert dialog.stack.indexOf(dialog.progress_page) == 1
