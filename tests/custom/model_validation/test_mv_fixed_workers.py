"""Fixed concurrency: one source, half of the logical processors.

The augment pool and the inference session pool share a single, fixed
count - half of the logical processors of the machine the tool runs on -
and the configuration page only displays it. These tests pin the formula
down for the machines that matter (1, 2, 3, 4, 8, 16 and 22 processors,
plus a machine that does not report its count at all), prove that the two
stages share the one source and that no module writes the count of one
machine down as a literal.
"""

import inspect
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6 import QtWidgets

from anylabeling.custom.model_validation import app_config
from anylabeling.custom.model_validation.app_config import (
    AUGMENT_WORKERS_LIMIT,
    DEFAULT_AUGMENT_WORKERS,
    DEFAULT_INFER_WORKERS,
    DEFAULT_WORKERS,
    INFER_WORKERS_LIMIT,
    ValidationConfig,
    default_workers,
    infer_session_threads,
    infer_threads_snapshot,
    logical_cores,
    resolve_augment_workers,
    resolve_infer_workers,
)
from anylabeling.custom.model_validation.ui.config_page import (
    ConfigPage,
    workers_note,
)


@pytest.fixture(scope="module")
def qt_app():
    "Create the offscreen application the configuration page needs."

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


# ------------------------------------------------------------- the formula
@pytest.mark.parametrize("cores", [None, 1, 2, 3, 4, 8, 16, 22])
def test_the_fixed_count_follows_the_machine(monkeypatch, cores):
    """os.cpu_count() alone decides, and it is read at run time.

    The count is never a constant of the code: a machine answering None
    counts as a single processor and every result stays at one or more.
    """

    monkeypatch.setattr(os, "cpu_count", lambda: cores)
    processors = max(1, cores or 1)
    expected = max(1, processors // 2)

    assert logical_cores() == processors
    assert default_workers() == expected
    assert expected == (1 if processors < 4 else processors // 2)
    # the defensive clamps never leave the fixed count either
    assert resolve_augment_workers(None) == expected
    assert resolve_infer_workers(None) == expected
    assert resolve_augment_workers(10**6) == expected
    assert resolve_infer_workers(10**6) == expected


def test_the_documented_machines_are_covered():
    "22 cores run 11, 8 run 4, 4 run 2 and 2 or 1 run a single worker."

    assert [default_workers(cores) for cores in (1, 2, 4, 8, 22)] == [
        1,
        1,
        2,
        4,
        11,
    ]
    assert default_workers(0) == 1
    assert default_workers(-4) == 1
    assert default_workers(None) == max(1, (os.cpu_count() or 1) // 2)


def test_the_count_is_derived_and_never_written_down():
    """No function of the source carries the count of one machine.

    A 22 processor machine and an 8 processor one must run the very same
    code, so the value can only come from os.cpu_count() at run time.
    """

    default_source = inspect.getsource(default_workers)
    assert "cpu_count" in default_source
    assert "// 2" in default_source
    assert "11" not in default_source
    assert "22" not in default_source
    cores_source = inspect.getsource(logical_cores)
    assert "cpu_count" in cores_source
    assert "11" not in cores_source
    assert "22" not in cores_source


# --------------------------------------------------------- the one source
def test_the_two_stages_share_the_one_source():
    "The augment pool and the session pool are the very same value."

    assert DEFAULT_WORKERS == default_workers()
    assert DEFAULT_AUGMENT_WORKERS == DEFAULT_WORKERS
    assert DEFAULT_INFER_WORKERS == DEFAULT_WORKERS
    assert AUGMENT_WORKERS_LIMIT == DEFAULT_WORKERS
    assert INFER_WORKERS_LIMIT == DEFAULT_WORKERS
    config = ValidationConfig()
    assert config.augment_workers == DEFAULT_WORKERS
    assert config.infer_workers == DEFAULT_WORKERS
    payload = config.to_dict()
    assert payload["augment_workers"] == payload["infer_workers"]
    assert payload["workers_fixed"] is True
    assert payload["workers_rule"] == "max(1, os.cpu_count() // 2)"
    assert payload["infer_session_threads"]["workers"] == DEFAULT_WORKERS
    assert infer_threads_snapshot()["workers"] == DEFAULT_WORKERS
    intra, inter = infer_session_threads()
    if DEFAULT_WORKERS > 1:
        assert intra == max(1, logical_cores() // DEFAULT_WORKERS)
        assert inter == 1
    else:
        # a single session keeps what the runtime decides
        assert (intra, inter) == (0, 0)


def test_the_clamps_stay_inside_the_fixed_range():
    "A hand written configuration can not leave the fixed range."

    for value in (-5, 0, 1, 3, 3.7, DEFAULT_WORKERS + 1, 10**6, None, "many"):
        assert 1 <= resolve_augment_workers(value) <= DEFAULT_WORKERS
        assert 1 <= resolve_infer_workers(value) <= DEFAULT_WORKERS
    assert resolve_augment_workers(1) == resolve_infer_workers(1) == 1
    assert app_config.OPENCV_THREADS == 1


# ------------------------------------------------------------- the page
def test_the_page_displays_the_fixed_value_and_owns_no_control(qt_app):
    """The form shows the count; it can not be configured any more."""

    page = ConfigPage()
    try:
        fixed = default_workers()
        assert hasattr(page, "workers_spin") is False
        assert hasattr(page, "infer_workers_spin") is False
        assert page.augment_workers_note.text() == workers_note("增强", fixed)
        assert page.infer_workers_note.text() == workers_note("推理", fixed)
        assert str(fixed) in page.augment_workers_note.text()
        assert str(fixed) in page.infer_workers_note.text()
        for note in (page.augment_workers_note, page.infer_workers_note):
            assert note.toolTip()
            assert note.findChildren(QtWidgets.QAbstractSpinBox) == []
        config = page.collect_config()
        assert config.augment_workers == fixed
        assert config.infer_workers == fixed
    finally:
        page.deleteLater()
