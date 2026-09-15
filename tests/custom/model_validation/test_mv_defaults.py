"""The shipped defaults: the dataclasses and the first screen.

Every default lives in exactly one place, the two dataclasses of
app_config. The configuration page reads the initial value of every
control from them instead of restating the numbers, therefore this
module pins the shipped table once and then proves that both layers
agree: an untouched page collects the dataclass defaults themselves and
a changed dataclass default reaches the form without a second literal
to edit.
"""

import ast
import inspect
import os
from dataclasses import dataclass

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6 import QtWidgets

from anylabeling.custom.model_validation.app_config import (
    BORDER_FILL,
    DEFAULT_RATIO,
    RATIO_MODE,
    AugmentParams,
    ValidationConfig,
)
from anylabeling.custom.model_validation.ui import config_page
from anylabeling.custom.model_validation.ui.config_page import ConfigPage

# The single image augmentation defaults the tool ships, field by field.
# The order is the field order of AugmentParams, which is also the grid
# order of the page and the key order of the snapshot.
AUGMENT_DEFAULTS = {
    "contrast": 0.2,
    "hsv_v": 0.4,
    "degrees": 15.0,
    "translate": 0.1,
    "scale_min": 0.8,
    "scale_max": 1.5,
    # the two flips are enable bits (bool), not probabilities
    "flipud": True,
    "fliplr": True,
    "select_prob": 0.1,
    "seed": 0,
}

# The control showing each augmentation field on the configuration page.
# The two flips are check boxes (enable bits), every other control is a
# spin box the test reads with value().
AUGMENT_CONTROLS = {
    "contrast": "contrast_spin",
    "hsv_v": "hsv_v_spin",
    "degrees": "degrees_spin",
    "translate": "translate_spin",
    "scale_min": "scale_min_spin",
    "scale_max": "scale_max_spin",
    "flipud": "flipud_check",
    "fliplr": "fliplr_check",
    "select_prob": "select_prob_spin",
    "seed": "seed_spin",
}

# The fields whose control is a check box and therefore answers
# isChecked() instead of value().
CHECK_CONTROLS = ("flipud", "fliplr")


def control_value(widget, name: str):
    "Read the current value of one control, spin box or check box alike."

    if name in CHECK_CONTROLS:
        return widget.isChecked()
    return widget.value()


# The thresholds of the run and the control carrying each one.
THRESHOLD_DEFAULTS = {
    "conf_threshold": 0.5,
    "iou_threshold": 0.45,
    "ng_iou_threshold": 0.5,
}

THRESHOLD_CONTROLS = {
    "conf_threshold": "conf_spin",
    "iou_threshold": "iou_spin",
    "ng_iou_threshold": "ng_iou_spin",
}


@pytest.fixture(scope="module")
def qt_app():
    "Create the offscreen application the configuration page needs."

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


@pytest.fixture
def page(qt_app):
    "Create a configuration page and close it afterwards."

    widget = ConfigPage()
    try:
        yield widget
    finally:
        widget.close()


def test_the_augmentation_defaults_are_the_shipped_table():
    "AugmentParams() carries the values the interface was set to."

    params = AugmentParams()
    for name, expected in AUGMENT_DEFAULTS.items():
        assert getattr(params, name) == expected, name


def test_the_threshold_defaults_are_the_shipped_table():
    "ValidationConfig() carries the chosen thresholds and keeps the rest."

    config = ValidationConfig()
    for name, expected in THRESHOLD_DEFAULTS.items():
        assert getattr(config, name) == expected, name
    # the amount of the run keeps its own defaults: augmentation is off
    # out of the box, the mode is fixed to the ratio and judge stays on
    assert config.augment_enabled is False
    assert config.augment_mode == RATIO_MODE
    assert config.ratio == DEFAULT_RATIO == 0.5
    assert config.judge_augmented is True
    assert config.augment_params == AugmentParams()


def test_the_fill_default_is_the_pure_black_one():
    "The shipped fill is pure black, and it is no longer a parameter."

    params = AugmentParams()
    # there is no fill field any more: the option was cancelled
    assert not hasattr(params, "border_mode")
    assert BORDER_FILL == "black"
    snapshot = params.snapshot()
    assert snapshot["border_fill"] == BORDER_FILL
    assert "border_mode" not in snapshot
    assert "border_fill" not in snapshot["official_names"]


def test_the_snapshot_registers_the_two_new_parameters():
    "contrast and select_prob are no official argument and say so."

    snapshot = AugmentParams().snapshot()
    assert snapshot["non_official_names"] == {
        "contrast": 0.2,
        "select_prob": 0.1,
    }
    # the two dropped names are gone from every section of the snapshot
    dropped_names = (
        "hsv_h",
        "hsv_s",
        "shear",
        "perspective",
        "bgr",
        "erasing",
        "crop_fraction",
    )
    for dropped in dropped_names:
        assert dropped not in snapshot, dropped
        assert dropped not in snapshot["official_names"], dropped
        assert dropped not in snapshot["non_official_names"], dropped
    # the two flips are enable bits in the parameter section and the
    # effective chance in the official one
    assert snapshot["flipud"] is True
    assert snapshot["official_names"]["flipud"] == 0.1


def test_the_page_starts_from_the_dataclass_defaults(page):
    "Every control of the first screen shows the dataclass default."

    params = AugmentParams()
    for name, control in AUGMENT_CONTROLS.items():
        widget = getattr(page, control)
        assert control_value(widget, name) == getattr(params, name), name
    # the page owns no fill control at all: there is nothing to select
    assert not hasattr(page, "border_mode_combo")
    # the two flips are check boxes, the other eight parameters spins
    assert isinstance(page.flipud_check, QtWidgets.QCheckBox)
    assert isinstance(page.fliplr_check, QtWidgets.QCheckBox)
    assert isinstance(page.contrast_spin, QtWidgets.QDoubleSpinBox)

    config = ValidationConfig()
    for name, control in THRESHOLD_CONTROLS.items():
        widget = getattr(page, control)
        assert widget.value() == getattr(config, name), name
    # the amount mode is no choice any more: the page shows the fixed
    # ratio and keeps only the ratio editable
    assert page.collect_config().augment_mode == RATIO_MODE
    assert config.augment_mode == RATIO_MODE
    assert page.ratio_spin.value() == config.ratio
    assert page.ratio_spin.maximum() == 1.0
    assert page.select_prob_spin.minimum() == 0.05
    assert page.select_prob_spin.maximum() == 1.0
    assert page.augment_check.isChecked() is config.augment_enabled
    assert page.augment_check.isChecked() is False
    assert page.judge_augmented_check.isChecked() is config.judge_augmented


def test_the_page_keeps_the_select_probability_on_the_amount_row(page):
    """The p control is an augment parameter, not a grid cell.

    It stays readable through select_prob_spin - the defaults table
    above pins its value - but the amount row is where the page shows
    it: it is the chance of the same draw that turns the ratio into a
    copy count, so it sits next to ratio_spin and takes no cell of the
    parameter grid.
    """

    # the parameter grid lists no select probability at all
    grid_controls = [
        control for _label, _suffix, control, _tip in page._augment_specs()
    ]
    assert page.select_prob_spin not in grid_controls
    # the amount row is the horizontal layout of the augment group box
    # that owns the ratio, and it carries the p control with its label
    owner = page.ratio_spin.parentWidget()
    rows = [
        layout
        for layout in owner.findChildren(QtWidgets.QHBoxLayout)
        if layout.indexOf(page.ratio_spin) >= 0
    ]
    assert len(rows) == 1
    row = [rows[0].itemAt(i).widget() for i in range(rows[0].count())]
    assert page.select_prob_spin in row
    index = row.index(page.select_prob_spin)
    label = row[index - 1]
    assert isinstance(label, QtWidgets.QLabel)
    assert label.text() == "选中概率 p"
    assert label.toolTip() == page.select_prob_spin.toolTip()


def test_the_page_collects_the_defaults_unchanged(page):
    "An untouched page produces the dataclass defaults themselves."

    config = page.collect_config()
    assert config.augment_params == AugmentParams()
    defaults = ValidationConfig()
    assert config.conf_threshold == defaults.conf_threshold
    assert config.iou_threshold == defaults.iou_threshold
    assert config.ng_iou_threshold == defaults.ng_iou_threshold
    assert config.augment_mode == defaults.augment_mode
    assert config.ratio == defaults.ratio


def test_the_page_reads_the_augmentation_defaults(qt_app, monkeypatch):
    "A changed AugmentParams default reaches the form without an edit."

    @dataclass
    class Shifted(AugmentParams):
        degrees: float = 33.0
        flipud: bool = False
        scale_min: float = 0.9

    monkeypatch.setattr(config_page, "AugmentParams", Shifted)
    widget = ConfigPage()
    try:
        assert widget.degrees_spin.value() == 33.0
        assert widget.flipud_check.isChecked() is False
        assert widget.scale_min_spin.value() == 0.9
    finally:
        widget.close()


def test_the_page_reads_the_threshold_defaults(qt_app, monkeypatch):
    "A changed ValidationConfig default reaches the form as well."

    @dataclass
    class Strict(ValidationConfig):
        conf_threshold: float = 0.77
        ng_iou_threshold: float = 0.66

    monkeypatch.setattr(config_page, "ValidationConfig", Strict)
    widget = ConfigPage()
    try:
        assert widget.conf_spin.value() == 0.77
        assert widget.ng_iou_spin.value() == 0.66
        assert widget.iou_spin.value() == Strict.iou_threshold
    finally:
        widget.close()


def _spin_value_arguments(source: str):
    "Return the value argument of every spin constructor of a module."

    tree = ast.parse(source)
    arguments = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", "")
        if name not in ("_double_spin", "_int_spin"):
            continue
        # (minimum, maximum, value, step): the third argument is the
        # initial value the control shows
        if len(node.args) > 2:
            arguments.append(node.args[2])
    return arguments


def test_the_spin_constructors_never_restate_a_default():
    "Every initial value is read from a dataclass, never typed again."

    arguments = _spin_value_arguments(inspect.getsource(config_page))
    assert arguments
    for argument in arguments:
        assert not isinstance(argument, ast.Constant), ast.dump(argument)
