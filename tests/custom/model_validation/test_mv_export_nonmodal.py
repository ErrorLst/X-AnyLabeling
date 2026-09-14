"""The export progress dialog no longer blocks the main window.

A non modal progress dialog leaves the main window reachable while the
archive is written, so the export button itself has to refuse a second
pass. These tests pin the modality the window asks for and the button
state on every path, including the one that raises.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from PyQt6 import QtCore, QtGui, QtWidgets

from anylabeling.custom.model_validation.ui import dialog as dialog_module
from anylabeling.custom.model_validation.ui.dialog import (
    ModelValidationDialog,
)


@pytest.fixture(scope="module")
def qt_app():
    "Create the offscreen application the widgets need."

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


@pytest.fixture
def dialog(qt_app):
    "Create a window and close it when the test is over."

    window = ModelValidationDialog()
    try:
        yield window
    finally:
        window.close()


def raise_deleted(message: str) -> None:
    "Raise the error a widget deleted under its user raises."

    raise RuntimeError(message)


def record_progress_dialogs(monkeypatch) -> list:
    "Patch QProgressDialog, returning every instance the window builds."

    created = []

    class Recorder(QtWidgets.QProgressDialog):
        "A progress dialog that remembers the modality it was asked for."

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.modality_requests = []
            created.append(self)

        def setWindowModality(self, modality) -> None:  # noqa: N802
            self.modality_requests.append(modality)
            super().setWindowModality(modality)

    monkeypatch.setattr(QtWidgets, "QProgressDialog", Recorder)
    return created


def test_the_export_progress_dialog_is_not_modal(
    dialog, monkeypatch
) -> None:
    "The archive is written without blocking the window underneath."

    created = record_progress_dialogs(monkeypatch)
    monkeypatch.setattr(
        dialog_module,
        "export_zip",
        lambda *args, **kwargs: {"originals": 1, "augmented": 0},
    )

    dialog.export_zip("/tmp/out.zip")

    assert len(created) == 1
    assert created[0].modality_requests == [
        QtCore.Qt.WindowModality.NonModal
    ]
    assert (
        created[0].windowModality()
        == QtCore.Qt.WindowModality.NonModal
    )


def test_the_export_button_is_disabled_while_the_export_runs(
    dialog, monkeypatch
) -> None:
    "The button refuses a second pass and comes back after a failure."

    record_progress_dialogs(monkeypatch)
    button = dialog.results_page.export_button
    seen = []

    def failing(*args, **kwargs):
        seen.append(button.isEnabled())
        raise RuntimeError("boom")

    monkeypatch.setattr(dialog_module, "export_zip", failing)

    assert button.isEnabled() is True
    dialog.export_zip("/tmp/out.zip")

    assert seen == [False]
    assert button.isEnabled() is True
    assert "boom" in dialog.results_page.status_label.text()


def test_the_export_button_comes_back_after_a_success(
    dialog, monkeypatch
) -> None:
    "The button is disabled for the pass and enabled again afterwards."

    record_progress_dialogs(monkeypatch)
    button = dialog.results_page.export_button
    seen = []

    def export(*args, **kwargs):
        seen.append(button.isEnabled())
        return {"originals": 2, "augmented": 1}

    monkeypatch.setattr(dialog_module, "export_zip", export)

    summary = dialog.export_zip("/tmp/out.zip")

    assert seen == [False]
    assert summary == {"originals": 2, "augmented": 1}
    assert button.isEnabled() is True
    assert dialog.last_export_path == "/tmp/out.zip"
    assert "导出完成" in dialog.results_page.status_label.text()


def test_a_close_click_during_the_export_is_refused(
    dialog, monkeypatch
) -> None:
    "The window cannot be closed while the archive is being written."

    record_progress_dialogs(monkeypatch)
    seen = {}

    def export(*args, **kwargs):
        outgoing = QtGui.QCloseEvent()
        dialog.closeEvent(outgoing)
        seen["accepted"] = outgoing.isAccepted()
        seen["exporting"] = dialog._exporting
        seen["config"] = dialog.config_page.status_label.text()
        # the refusal is a line of status in Chinese, not a silent click:
        # it has to be readable on whichever page the user is looking at
        seen["results"] = dialog.results_page.status_label.text()
        return {"originals": 1, "augmented": 0}

    monkeypatch.setattr(dialog_module, "export_zip", export)

    dialog.export_zip("/tmp/out.zip")

    assert seen["accepted"] is False
    assert seen["exporting"] is True
    assert dialog._exporting is False
    guard = dialog_module.EXPORT_GUARD_STATUS
    assert guard in seen["config"]
    assert guard in seen["results"]


def test_an_export_teardown_survives_destroyed_widgets(
    dialog, monkeypatch
) -> None:
    "A report on a widget a closing window destroyed never raises."

    record_progress_dialogs(monkeypatch)
    page = dialog.results_page
    button = page.export_button
    deleted = "wrapped C/C++ object has been deleted"

    # the very RuntimeError a widget the closing window deleted raises,
    # on the page and on the button the export writes to: the button is
    # still disabled at the start of the run, and gone by its teardown
    monkeypatch.setattr(
        page, "set_summary", lambda _text, _e=deleted: raise_deleted(_e)
    )

    def set_enabled(enabled, _e=deleted):
        if enabled:
            raise_deleted(_e)

    monkeypatch.setattr(button, "setEnabled", set_enabled)
    monkeypatch.setattr(
        dialog_module,
        "export_zip",
        lambda *args, **kwargs: {"originals": 3, "augmented": 1},
    )

    # no exception escapes the slot, not even from the teardown
    summary = dialog.export_zip("/tmp/out.zip")

    assert summary == {"originals": 3, "augmented": 1}
    assert dialog._exporting is False


def test_a_second_export_while_the_first_runs_is_refused(
    dialog, monkeypatch
) -> None:
    "A click arriving during the pass never starts a second archive."

    record_progress_dialogs(monkeypatch)
    button = dialog.results_page.export_button
    calls = []

    def export(*args, **kwargs):
        calls.append(True)
        # the progress dialog is not modal any more: the click can
        # arrive while this very call is still running
        assert dialog.export_zip("/tmp/second.zip") == {}
        assert button.isEnabled() is False
        return {"originals": 0, "augmented": 0}

    monkeypatch.setattr(dialog_module, "export_zip", export)

    dialog.export_zip("/tmp/first.zip")

    assert calls == [True]
    assert button.isEnabled() is True
