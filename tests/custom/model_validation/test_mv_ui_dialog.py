"""Dialog wiring tests: live preview and staged records."""

import os
import os.path as osp
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from PyQt6 import QtCore, QtTest, QtWidgets

from anylabeling.custom.model_validation import dataset
from anylabeling.custom.model_validation import records as records_module
from anylabeling.custom.model_validation.app_config import (
    BORDER_FILL,
    COUNT_MODE,
    DEFAULT_AUGMENT_WORKERS,
    DEFAULT_RATIO,
    DEFAULT_WORKERS,
    MULTIPLIER_MODE,
    RATIO_MODE,
    ValidationConfig,
    default_workers,
)
from anylabeling.custom.model_validation.pipeline import ValidationWorker
from anylabeling.custom.model_validation.report import build_report
from anylabeling.custom.model_validation.ui.config_page import (
    DRAG_SUPPORTED_HINT,
    SPIN_WIDTH,
    TOOLTIP_AUGMENT_ENABLED,
    TOOLTIP_AUGMENT_WORKERS,
    TOOLTIP_COUNT,
    TOOLTIP_INFER_WORKERS,
    TOOLTIP_MODE,
    TOOLTIP_MULTIPLIER,
    TOOLTIP_RATIO,
    workers_note,
)
from anylabeling.custom.model_validation.ui.dialog import (
    MINIMUM_HEIGHT,
    MINIMUM_WIDTH,
    ModelValidationDialog,
    initial_window_height,
)
from anylabeling.custom.model_validation.ui.results_page import (
    COLUMN_EXPORT,
    COLUMN_KIND,
    model_note_text,
)

CLASSES = ["car"]


@pytest.fixture(scope="module")
def qt_app():
    "Create the offscreen application shared by the dialog tests."

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


def write_image(path: str) -> None:
    "Write a tiny png file."

    import cv2

    os.makedirs(osp.dirname(path), exist_ok=True)
    image = np.zeros((24, 32, 3), dtype=np.uint8)
    image[:, :, 1] = 160
    ok, buffer = cv2.imencode(".png", image)
    assert ok
    buffer.tofile(path)


def wait_for_source_scan(dialog, source: str, timeout_ms: int = 5000) -> bool:
    """Let the debounced scan of one source folder answer.

    The pair count of a source directory is computed behind a worker
    thread and a debounce now (see _on_dataset_changed), so a test that
    wants the preview of a folder has to let that answer arrive before
    it reads the line.
    """

    def answered() -> bool:
        return (
            dialog.source_dataset_dir == source
            and not dialog._scan_pending
        )

    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        if answered():
            return True
        QtTest.QTest.qWait(10)
    return answered()


def label_payload(relpath: str) -> dict:
    "Return the xlabel payload of a staged pair."

    return {
        "version": "3.0.0",
        "flags": {},
        "checked": False,
        "shapes": [
            {
                "label": "car",
                "shape_type": "rectangle",
                "points": [[4, 4], [20, 4], [20, 20], [4, 20]],
                "group_id": None,
                "description": "",
                "flags": {},
            }
        ],
        "imagePath": osp.basename(relpath),
        "imageData": None,
        "imageHeight": 24,
        "imageWidth": 32,
    }


def make_staging_layout(scratch: str) -> str:
    """Create the staging folder layout the tool expects.

    dataset.create_staging_root is not used here: it relies on
    tempfile.mkdtemp and the sandbox of this workspace denies every
    write inside such a folder.
    """

    staging = osp.join(scratch, dataset.STAGING_PREFIX + "dialog")
    for folder in (dataset.ORIGINAL_DIRNAME, dataset.AUGMENTED_DIRNAME):
        for sub in (dataset.IMAGES_DIRNAME, dataset.LABELS_DIRNAME):
            os.makedirs(osp.join(staging, folder, sub), exist_ok=True)
    return staging


def staged_original(staging: str, relpath: str, with_label: bool = True):
    "Create one staged original record."

    paths = dataset.staging_paths(
        staging, records_module.KIND_ORIGINAL, relpath
    )
    write_image(paths["image"])
    if not with_label:
        record = records_module.make_record(
            records_module.KIND_ORIGINAL, relpath, paths["image"], ""
        )
        record.verdict = records_module.SKIPPED
        record.reasons = ["NO_LABEL"]
        return record
    dataset.write_json(paths["label"], label_payload(relpath))
    return records_module.make_record(
        records_module.KIND_ORIGINAL, relpath, paths["image"], paths["label"]
    )


def staged_child(staging: str, relpath: str, parent_record_id: str):
    "Create one staged augmented record."

    paths = dataset.staging_paths(
        staging, records_module.KIND_AUGMENTED, relpath
    )
    write_image(paths["image"])
    dataset.write_json(paths["label"], label_payload(relpath))
    return records_module.make_record(
        records_module.KIND_AUGMENTED,
        relpath,
        paths["image"],
        paths["label"],
        parent_record_id=parent_record_id,
    )


def build_worker(staging: str, records: list) -> ValidationWorker:
    "Build an unstarted worker holding the given records."

    config = ValidationConfig(
        dataset_dir="/source",
        model_path="/model.onnx",
        classes_file="/classes.txt",
    )
    worker = ValidationWorker(config, CLASSES, staging)
    worker.records = records
    return worker


def test_preview_refresh_survives_the_first_configuration(dialog):
    "Toggling the augment controls before a run must not crash."

    assert dialog.records == []
    dialog.config_page.set_dataset("/does-not-exist")
    dialog.config_page.augment_check.setChecked(False)
    dialog.config_page.mode_combo.setCurrentIndex(0)
    dialog.config_page.multiplier_spin.setValue(4)
    dialog.config_page.ratio_spin.setValue(2.0)
    dialog.config_page.count_spin.setValue(10)
    dialog.config_page.judge_augmented_check.setChecked(False)
    dialog._refresh_preview()
    assert dialog.preview_counts(0, dialog.config_page.collect_config()) == {
        "originals": 0,
        "augmented": 0,
        "judged": 0,
        "exported": 0,
    }


def test_source_directory_is_enumerated_once(dialog, tmp_path, monkeypatch):
    "The scheduler walks the selected directory, and only once."

    source = str(tmp_path / "source")
    write_image(osp.join(source, "a.png"))
    dataset.write_json(osp.join(source, "a.json"), label_payload("a.png"))
    write_image(osp.join(source, "b.png"))
    dataset.write_json(osp.join(source, "b.json"), label_payload("b.png"))
    calls = []
    collect = dataset.collect_pairs

    def counting_collect(directory):
        calls.append(directory)
        return collect(directory)

    monkeypatch.setattr(dataset, "collect_pairs", counting_collect)
    page = dialog.config_page
    page.set_dataset(source)
    # the slot that watches the path line edit never walks the folder:
    # the debounced scan of the scheduler does, once
    assert calls == []
    assert dialog._scan_pending is True
    assert wait_for_source_scan(dialog, source)
    assert calls == [source]
    assert dialog.source_pair_count == 2
    page.augment_check.setChecked(True)
    # the default mode is ratio: 2 valid originals x 0.5 = 1 copy
    assert page.mode_combo.currentData() == RATIO_MODE
    dialog._refresh_preview()
    assert page.preview_label.text() == (
        "有效原图 2 / 将生成增强 1 / 验证总数 3 / 预计导出 2"
    )
    # an explicit count mode still wins over the ratio default
    page.mode_combo.setCurrentIndex(page.mode_combo.findData(COUNT_MODE))
    page.count_spin.setValue(5)
    dialog._refresh_preview()
    assert len(calls) == 1
    assert page.preview_label.text() == (
        "有效原图 2 / 将生成增强 5 / 验证总数 7 / 预计导出 2"
    )


def test_a_failing_scan_leaves_the_count_at_zero(
    dialog, tmp_path, monkeypatch
):
    "An enumeration error must not abort the slot that asked for it."

    source = str(tmp_path / "broken")
    write_image(osp.join(source, "a.png"))

    def failing_collect(directory):
        raise TypeError("'<' not supported between 'str' and 'int'")

    monkeypatch.setattr(dataset, "collect_pairs", failing_collect)
    page = dialog.config_page
    page.set_dataset(source)
    assert dialog.source_pair_count == 0


def interactive_controls(page):
    """Return the interactive controls owned by a configuration page."""

    control_types = (
        QtWidgets.QLineEdit,
        QtWidgets.QComboBox,
        QtWidgets.QSpinBox,
        QtWidgets.QDoubleSpinBox,
        QtWidgets.QCheckBox,
        QtWidgets.QPushButton,
    )
    return [
        widget
        for widget in page.findChildren(QtWidgets.QWidget)
        if isinstance(widget, control_types)
        and not isinstance(widget.parent(), QtWidgets.QAbstractSpinBox)
        and not widget.isAncestorOf(page)
    ]


def augment_grid_params(page):
    """Return the (control, stays grey) pairs of the augment grid.

    The two parameters the tool never applies (erasing, crop_fraction)
    stay grey whatever the augment checkbox says; every other control of
    the grid follows the checkbox.
    """

    grey = page.always_disabled_names
    return [
        (widget, widget.objectName() in grey)
        for _label, _suffix, widget, _tip in page._augment_specs()
    ]


def test_every_configuration_control_has_a_tooltip(dialog):
    "Every interactive configuration control explains itself on hover."

    page = dialog.config_page
    controls = interactive_controls(page)
    assert len(controls) >= 30
    assert {type(control).__name__ for control in controls} >= {
        "QLineEdit",
        "QComboBox",
        "QSpinBox",
        "QDoubleSpinBox",
        "QCheckBox",
        "QPushButton",
    }
    missing = [
        type(control).__name__
        for control in controls
        if not control.toolTip().strip()
    ]
    assert missing == []


def test_field_labels_carry_the_tooltip_of_their_control(dialog):
    "A field name shows the same explanation as its control."

    page = dialog.config_page
    labels = [
        label
        for label in page.findChildren(QtWidgets.QLabel)
        if label.toolTip()
    ]
    # the source rows, the three thresholds, the amount row, the whole
    # augment grid and the two hints all contribute a labelled tooltip.
    assert len(labels) >= 20

    by_text = {label.text(): label for label in labels}
    pairs = (
        ("数据来源目录", page.dataset_edit),
        ("类别表 classes.txt", page.classes_edit),
        ("ONNX 模型", page.model_edit),
        ("置信度 conf", page.conf_spin),
        ("NMS IoU iou", page.iou_spin),
        ("判定 NG IoU", page.ng_iou_spin),
        ("色调 hsv_h", page.hsv_h_spin),
        ("缩放下限 scale min", page.scale_min_spin),
        ("缩放上限 scale max", page.scale_max_spin),
        ("剪切 shear（°）", page.shear_spin),
    )
    for text, control in pairs:
        label = by_text.get(text)
        assert label is not None, text
        assert control.toolTip() != ""
        # the field name repeats the explanation of its own control
        assert label.toolTip() == control.toolTip()
    assert page.scale_min_spin.toolTip() == page.scale_max_spin.toolTip()


def test_the_amount_controls_collapse_into_one_row(dialog):
    "Only the control of the selected amount mode stays visible."

    page = dialog.config_page
    pairs = (
        (COUNT_MODE, page.count_spin),
        (RATIO_MODE, page.ratio_spin),
        (MULTIPLIER_MODE, page.multiplier_spin),
    )
    assert page.mode_combo.currentData() == RATIO_MODE
    page.augment_check.setChecked(True)
    for mode, control in pairs:
        page.mode_combo.setCurrentIndex(page.mode_combo.findData(mode))
        for other_mode, other in pairs:
            assert other.isVisibleTo(page) == (other_mode == mode)
            assert other.isEnabled() == (other_mode == mode)
    # the checkbox still gates the visible control
    page.mode_combo.setCurrentIndex(page.mode_combo.findData(COUNT_MODE))
    page.augment_check.setChecked(False)
    assert page.count_spin.isVisibleTo(page)
    assert not page.count_spin.isEnabled()
    page.augment_check.setChecked(True)
    assert page.count_spin.isEnabled()


def test_the_default_amount_mode_is_ratio(dialog):
    "A fresh window derives the amount with the ratio mode."

    page = dialog.config_page
    assert page.mode_combo.currentData() == RATIO_MODE
    assert page.collect_config().augment_mode == RATIO_MODE
    assert page.ratio_spin.value() == DEFAULT_RATIO == 0.5
    assert page.ratio_spin.minimumWidth() == SPIN_WIDTH
    # the option texts name the default instead of hard coding an index
    texts = [
        page.mode_combo.itemText(index)
        for index in range(page.mode_combo.count())
    ]
    assert texts == [
        "倍数 multiplier",
        "比例 ratio（默认）",
        "总数 count",
    ]


def same_path(left: str, right: str) -> bool:
    "Compare two paths ignoring the separator and case differences."

    return osp.normcase(str(left)) == osp.normcase(str(right))


def write_classes(path: str, names) -> str:
    "Write a classes.txt file with one class name per line."

    with open(path, "w", encoding="utf-8") as handle:
        handle.write(chr(10).join(names) + chr(10))
    return path


# Why these tests drive the handlers with an event double instead of a
# real QDropEvent: Qt only routes DragEnter/Drop through its own drag
# session (a hand built event sent with QApplication.sendEvent returns
# False and is dropped), and calling a widget handler with one directly
# makes PyQt reuse an already freed C++ object (access violation). The
# double below carries the very same payload, so the page logic under
# test - mimeData, acceptProposedAction, ignore - behaves identically.
class FakeDropEvent:
    """Minimal stand in for the QDropEvent Qt hands to the page.

    Qt routes drag and drop through its own drag manager: a hand built
    QDropEvent cannot be delivered with QApplication.sendEvent (Qt
    drops it) and calling a widget handler with one directly makes the
    binding reuse a freed C++ object. The event double below carries
    the very same payload (a QMimeData holding local file urls) so the
    page logic is exercised end to end: mimeData, acceptProposedAction
    and ignore behave exactly like the real event.
    """

    def __init__(self, paths, with_urls: bool = True) -> None:
        self._mime = QtCore.QMimeData()
        if with_urls:
            self._mime.setUrls([QtCore.QUrl.fromLocalFile(p) for p in paths])
        self.accepted = 0
        self.ignored = 0

    def mimeData(self):
        "Return the drag payload."

        return self._mime

    def acceptProposedAction(self) -> None:
        "Record that the page accepted the drop."

        self.accepted += 1

    def ignore(self) -> None:
        "Record that the page refused the drop."

        self.ignored += 1


def drop_urls(page, paths):
    "Drop the given local paths onto a configuration page."

    event = FakeDropEvent(paths)
    page.dropEvent(event)
    return event


# ----------------------------------------------------------------- drops


def test_the_page_owns_the_drops_of_its_children(dialog):
    """The line edits and the combo box must not swallow a drag.

    The drop ownership is asserted structurally because Qt only
    delivers a real QDropEvent inside its own drag session (see
    FakeDropEvent above).
    """

    page = dialog.config_page
    assert page.acceptDrops() is True
    assert page.dataset_edit.acceptDrops() is False
    assert page.classes_edit.acceptDrops() is False
    assert page.model_edit.acceptDrops() is False
    assert page.mode_combo.acceptDrops() is False
    remaining = [
        type(child).__name__
        for child in page.findChildren(QtWidgets.QWidget)
        if child.acceptDrops()
    ]
    assert remaining == []

    # Qt hands a drag to the first ancestor accepting drops, so every drop
    # target of the page reaches the page itself
    def drop_owner(widget):
        "Return the widget that would handle a drop on the given child."

        parent = widget.parentWidget()
        while parent is not None and not parent.acceptDrops():
            parent = parent.parentWidget()
        return parent

    for target in (
        page.dataset_edit,
        page.classes_edit,
        page.model_edit,
        page.mode_combo,
        page.hsv_h_spin,
    ):
        assert drop_owner(target) is page
    # the supported payload is announced on the page itself
    assert page.drop_hint.text() == DRAG_SUPPORTED_HINT
    for edit in (
        page.dataset_edit,
        page.classes_edit,
        page.model_edit,
    ):
        assert "拖到窗口" in edit.toolTip()


def test_dropping_a_directory_fills_the_source_and_counts_it(
    dialog, tmp_path, monkeypatch
):
    """A dropped folder drives the very same cache as the browse button.

    The drop itself uses the event double documented above: a real
    QDropEvent only travels inside a Qt drag session.
    """

    source = str(tmp_path / "source")
    write_image(osp.join(source, "a.png"))
    dataset.write_json(osp.join(source, "a.json"), label_payload("a.png"))
    write_image(osp.join(source, "b.png"))
    dataset.write_json(osp.join(source, "b.json"), label_payload("b.png"))
    calls = []
    collect = dataset.collect_pairs

    def counting_collect(directory):
        calls.append(directory)
        return collect(directory)

    monkeypatch.setattr(dataset, "collect_pairs", counting_collect)
    page = dialog.config_page
    event = drop_urls(page, [source])
    assert event.accepted == 1
    assert same_path(page.dataset_edit.text(), source)
    # textChanged -> _on_dataset_changed asked for one scan, and the
    # slot itself never walked the folder
    assert calls == []
    assert wait_for_source_scan(dialog, source)
    assert calls == [source]
    assert dialog.source_pair_count == 2
    # and the preview followed the default augmentation settings
    assert page.preview_label.text() == (
        "有效原图 2 / 将生成增强 1 / 验证总数 3 / 预计导出 2"
    )


def test_dropping_a_classes_file_loads_it(dialog, tmp_path):
    """A dropped .txt is the classes table and is parsed right away.

    A QDropEvent double, see FakeDropEvent above.
    """

    classes = write_classes(str(tmp_path / "classes.txt"), ["cat", "dog"])
    page = dialog.config_page
    event = drop_urls(page, [classes])
    assert event.accepted == 1
    assert same_path(page.classes_edit.text(), classes)
    assert page.classes == ["cat", "dog"]
    assert same_path(page.collect_config().classes_file, classes)


def test_dropping_an_onnx_model_fills_the_model_path(dialog, tmp_path):
    """A dropped .onnx is the detector to validate.

    A QDropEvent double, see FakeDropEvent above.
    """

    model = str(tmp_path / "yolo11n.onnx")
    with open(model, "wb") as handle:
        handle.write(b"onnx")
    page = dialog.config_page
    event = drop_urls(page, [model])
    assert event.accepted == 1
    assert same_path(page.model_edit.text(), model)
    assert same_path(page.collect_config().model_path, model)


def test_dropping_the_three_targets_at_once_fills_every_field(
    dialog, tmp_path
):
    """One drop may carry the folder, the classes and the model.

    A QDropEvent double, see FakeDropEvent above.
    """

    source = str(tmp_path / "source")
    write_image(osp.join(source, "a.png"))
    dataset.write_json(osp.join(source, "a.json"), label_payload("a.png"))
    classes = write_classes(str(tmp_path / "classes.txt"), ["car"])
    model = str(tmp_path / "model.onnx")
    with open(model, "wb") as handle:
        handle.write(b"onnx")
    page = dialog.config_page
    event = drop_urls(page, [source, classes, model])
    assert event.accepted == 1
    assert same_path(page.dataset_edit.text(), source)
    assert same_path(page.classes_edit.text(), classes)
    assert same_path(page.model_edit.text(), model)
    assert page.classes == ["car"]
    config = page.collect_config()
    assert (config.dataset_dir, config.classes_file, config.model_path) == (
        source,
        classes,
        model,
    )


def test_dropping_the_same_kind_twice_uses_the_last_one(dialog, tmp_path):
    """Two .txt files: the last one wins and the note says so.

    A QDropEvent double, see FakeDropEvent above.
    """

    first = write_classes(str(tmp_path / "a.txt"), ["cat"])
    second = write_classes(str(tmp_path / "b.txt"), ["cat", "dog"])
    page = dialog.config_page
    drop_urls(page, [first, second])
    assert same_path(page.classes_edit.text(), second)
    assert page.classes == ["cat", "dog"]
    assert "已使用最后一个" in page.status_label.text()
    assert "b.txt" in page.status_label.text()


def test_unsupported_drops_are_ignored_with_a_note(dialog, tmp_path):
    """A .png is refused, the status line explains what is accepted.

    A QDropEvent double, see FakeDropEvent above.
    """

    page = dialog.config_page
    image = str(tmp_path / "photo.png")
    with open(image, "wb") as handle:
        handle.write(b"png")
    event = drop_urls(page, [image])
    assert event.accepted == 1
    assert page.dataset_edit.text() == ""
    assert page.classes_edit.text() == ""
    assert page.model_edit.text() == ""
    status = page.status_label.text()
    assert "已忽略 1 个不支持的文件" in status
    assert "photo.png" in status


def test_a_drop_without_urls_is_refused(dialog):
    """A drag that carries no file url is not a valid drop.

    A QDropEvent double, see FakeDropEvent above.
    """

    page = dialog.config_page
    event = FakeDropEvent([], with_urls=False)
    event.mimeData().setText("just some text")
    assert event.mimeData().hasUrls() is False
    page.dropEvent(event)
    assert event.accepted == 0
    assert event.ignored == 1


def test_dropping_a_broken_classes_file_reports_the_reason(dialog, tmp_path):
    """An empty classes file keeps the form as it was and explains why.

    A QDropEvent double, see FakeDropEvent above.
    """

    page = dialog.config_page
    bad = write_classes(str(tmp_path / "empty.txt"), ["   "])
    drop_urls(page, [bad])
    assert page.classes_edit.text() == ""
    assert page.classes == []
    assert page.status_label.text() != ""
    assert "empty" in page.status_label.text().lower()


def test_the_augment_default_opens_the_amount_and_parameter_controls(dialog):
    "生成增强副本 is on when the page opens, so its controls are usable."

    page = dialog.config_page
    assert page.augment_check.isChecked() is True
    config = page.collect_config()
    assert config.augment_enabled is True
    assert config.augment_mode == RATIO_MODE
    # the visible amount control and the whole parameter grid are live
    assert page.mode_combo.isEnabled()
    assert page.ratio_spin.isEnabled()
    assert page.ratio_spin.isVisibleTo(page)
    params = augment_grid_params(page)
    # the 15 grid controls minus the two parameters the tool never applies
    assert len(params) == 15
    for spin, stays_grey in params:
        # the two official classification only parameters stay grey while
        # every other control of the grid is usable out of the box
        assert spin.isEnabled() is (not stays_grey), spin.objectName()
    assert page.augment_check.toolTip() == TOOLTIP_AUGMENT_ENABLED


def test_the_first_screen_previews_the_enabled_augmentation(dialog, tmp_path):
    "Two valid originals x the default ratio 0.5 = one augmented copy."

    source = str(tmp_path / "source")
    write_image(osp.join(source, "a.png"))
    dataset.write_json(osp.join(source, "a.json"), label_payload("a.png"))
    write_image(osp.join(source, "b.png"))
    dataset.write_json(osp.join(source, "b.json"), label_payload("b.png"))
    page = dialog.config_page
    page.set_dataset(source)
    assert wait_for_source_scan(dialog, source)
    # no explicit setChecked(True) anywhere: the screen default alone counts
    dialog._refresh_preview()
    counts = dialog.preview_counts(2, page.collect_config())
    assert counts["originals"] == 2
    assert counts["augmented"] == 1
    assert counts["judged"] == 2 + 1
    assert counts["exported"] == 2
    assert page.preview_label.text() == (
        "有效原图 2 / 将生成增强 1 / 验证总数 3 / 预计导出 2"
    )


def test_turning_the_augment_default_off_greys_the_controls(dialog, tmp_path):
    "The checkbox still gates the amount and the parameter controls."

    source = str(tmp_path / "source")
    write_image(osp.join(source, "a.png"))
    dataset.write_json(osp.join(source, "a.json"), label_payload("a.png"))
    page = dialog.config_page
    page.set_dataset(source)
    assert wait_for_source_scan(dialog, source)
    page.augment_check.setChecked(False)
    assert page.mode_combo.isEnabled() is False
    # the three amount inputs, hidden and visible ones alike, are grey
    assert [control for _mode, control in page.mode_specs] == [
        page.multiplier_spin,
        page.ratio_spin,
        page.count_spin,
    ]
    for _mode, control in page.mode_specs:
        assert control.isEnabled() is False
    # the parameter grid greys out as well, except the two controls the
    # tool never applies: those were already grey and stay that way
    for spin, stays_grey in augment_grid_params(page):
        assert spin.isEnabled() is False, spin.objectName()
    dialog._refresh_preview()
    counts = dialog.preview_counts(1, page.collect_config())
    assert counts["augmented"] == 0
    assert counts["judged"] == 1
    assert page.preview_label.text() == (
        "有效原图 1 / 将生成增强 0 / 验证总数 1 / 预计导出 1"
    )
    # switching it back on restores the amount and parameter controls
    page.augment_check.setChecked(True)
    assert page.ratio_spin.isEnabled() is True
    assert page.hsv_h_spin.isEnabled() is True
    assert page.seed_spin.isEnabled() is True
    assert page.erasing_spin.isEnabled() is False
    assert page.crop_fraction_spin.isEnabled() is False


def test_a_missing_mode_falls_back_to_ratio_everywhere(dialog):
    "The preview and the pipeline share the same default mode."

    page = dialog.config_page
    page.set_dataset("/does-not-exist")
    page.augment_check.setChecked(True)
    config = page.collect_config()
    assert config.augment_mode == RATIO_MODE
    config.augment_mode = ""
    # 12 valid originals x the default ratio 0.5 = 6 augmented copies
    assert dialog.preview_counts(12, config)["augmented"] == 6
    worker = ValidationWorker(config, CLASSES, "/staging")
    plan = worker._augment_plan([object()] * 12)
    # the ratio mode draws the sources at random, the total is fixed
    assert len(plan) == 12
    assert sum(plan) == 6


def test_the_three_amount_modes_explain_the_difference(dialog):
    "k, r and N document how they derive the augmented amount."

    page = dialog.config_page
    hint = "每张有效原图各生成 k 份"
    drawn = "来源从有效原图中随机抽取（可重复）"
    spread = "余数分给前 N % 原图数 张"
    assert page.multiplier_spin.toolTip() == TOOLTIP_MULTIPLIER
    assert page.ratio_spin.toolTip() == TOOLTIP_RATIO
    assert page.count_spin.toolTip() == TOOLTIP_COUNT
    assert hint in TOOLTIP_MULTIPLIER
    assert drawn in TOOLTIP_RATIO
    assert "r=1 与 k=1 总量相同但来源分布不同" in TOOLTIP_RATIO
    assert spread in TOOLTIP_COUNT
    assert "总量语义但分配确定" in TOOLTIP_COUNT
    assert TOOLTIP_RATIO != TOOLTIP_COUNT != TOOLTIP_MULTIPLIER
    # the inline k / r / N labels repeat the explanation of the control
    labelled = {
        label.text(): label
        for label in page.findChildren(QtWidgets.QLabel)
        if label.toolTip()
    }
    for text, tooltip in (
        ("倍数 k", TOOLTIP_MULTIPLIER),
        ("比例 r", TOOLTIP_RATIO),
        ("总数 N", TOOLTIP_COUNT),
        ("数量模式", TOOLTIP_MODE),
    ):
        assert text in labelled
        assert labelled[text].toolTip() == tooltip
    assert "② 比例 r（默认）" in TOOLTIP_MODE


def test_the_hidden_amount_control_does_not_feed_the_preview(dialog, tmp_path):
    "The preview follows the visible amount control only."

    source = str(tmp_path / "source")
    write_image(osp.join(source, "a.png"))
    dataset.write_json(osp.join(source, "a.json"), label_payload("a.png"))
    page = dialog.config_page
    page.set_dataset(source)
    assert wait_for_source_scan(dialog, source)
    page.augment_check.setChecked(True)
    page.mode_combo.setCurrentIndex(page.mode_combo.findData(MULTIPLIER_MODE))
    page.multiplier_spin.setValue(4)
    dialog._refresh_preview()
    assert page.preview_label.text().startswith("有效原图 1 / 将生成增强 4")
    page.mode_combo.setCurrentIndex(page.mode_combo.findData(COUNT_MODE))
    page.count_spin.setValue(5)
    dialog._refresh_preview()
    assert not page.multiplier_spin.isVisibleTo(page)
    # the hidden multiplier (4) must not win over the visible count (5)
    assert page.preview_label.text() == (
        "有效原图 1 / 将生成增强 5 / 验证总数 6 / 预计导出 1"
    )


def test_every_augment_spin_uses_the_compact_width(dialog):
    "The arrows are gone and every spin box shares one narrow width."

    page = dialog.config_page
    spins = page.findChildren(QtWidgets.QAbstractSpinBox)
    assert len(spins) >= 18
    for spin in spins:
        assert spin.width() == SPIN_WIDTH
        assert spin.minimumWidth() == spin.maximumWidth() == SPIN_WIDTH
        assert spin.buttonSymbols() == (
            QtWidgets.QAbstractSpinBox.ButtonSymbols.NoButtons
        )


def test_the_concurrency_can_only_be_displayed(dialog):
    """The page shows the fixed concurrency, it can not configure it.

    Both stages run half of the logical processors of the machine, so
    the two spin boxes the page used to own are gone: the value can
    not be changed from the form any more and the read only notes show
    the value a run derives from this machine.
    """

    page = dialog.config_page
    assert hasattr(page, "workers_spin") is False
    assert hasattr(page, "infer_workers_spin") is False
    field_names = {
        label.text() for label in page.findChildren(QtWidgets.QLabel)
    }
    assert "并发线程数" not in field_names
    assert "推理并发数" not in field_names

    fixed = default_workers()
    assert fixed == DEFAULT_WORKERS == DEFAULT_AUGMENT_WORKERS
    assert page.augment_workers_note.text() == workers_note("增强", fixed)
    assert page.infer_workers_note.text() == workers_note("推理", fixed)
    assert page.augment_workers_note.text() == (
        f"增强并发：{fixed}（固定为逻辑处理器数量的一半）"
    )
    assert str(fixed) in page.augment_workers_note.text()
    assert page.augment_workers_note.toolTip() == TOOLTIP_AUGMENT_WORKERS
    assert page.infer_workers_note.toolTip() == TOOLTIP_INFER_WORKERS
    for phrase in ("固定为逻辑处理器数量的一半", "不可配置", "超订"):
        assert phrase in TOOLTIP_AUGMENT_WORKERS
    for phrase in ("与增强共用同一个固定值", "会话数为 1"):
        assert phrase in TOOLTIP_INFER_WORKERS
    # the form hands the fixed value over to the configuration
    config = page.collect_config()
    assert config.augment_workers == fixed
    assert config.infer_workers == fixed
    # both notes are read only: they own no control at all
    for note in (page.augment_workers_note, page.infer_workers_note):
        assert note.findChildren(QtWidgets.QAbstractSpinBox) == []
        assert note.findChildren(QtWidgets.QComboBox) == []


def test_the_concurrency_notes_survive_the_augment_checkbox(dialog):
    "The notes describe the run: they are never greyed out."

    page = dialog.config_page
    page.augment_check.setChecked(False)
    assert page.augment_workers_note.isEnabled() is True
    assert page.infer_workers_note.isEnabled() is True
    assert page.augment_workers_note.text() == workers_note(
        "增强", default_workers()
    )
    assert page.collect_config().augment_workers == default_workers()
    page.augment_check.setChecked(True)
    assert page.augment_workers_note.isEnabled() is True
    assert page.infer_workers_note.isEnabled() is True


def augment_grid(page):
    "Return the three column grid holding the augment parameters."

    grids = [
        grid
        for grid in page.findChildren(QtWidgets.QGridLayout)
        if grid.count() >= 15
    ]
    assert len(grids) == 1
    grid = grids[0]
    assert (grid.rowCount(), grid.columnCount()) == (5, 3)
    return grid


def grid_labels(page) -> list:
    "Return the parameter names of the grid, in row major order."

    grid = augment_grid(page)
    return [
        grid.itemAtPosition(row, column)
        .widget()
        .findChild(QtWidgets.QLabel)
        .text()
        for row in range(grid.rowCount())
        for column in range(grid.columnCount())
    ]


# The label of every row of the grid: the Chinese meaning first, the
# official Ultralytics argument name behind it, so the page stays
# comparable 1:1 with the documentation of the augmentation arguments.
AUGMENT_LABELS = (
    "色调 hsv_h",
    "饱和度 hsv_s",
    "亮度 hsv_v",
    "旋转角度 degrees（°）",
    "平移 translate",
    "缩放下限 scale min",
    "缩放上限 scale max",
    "剪切 shear（°）",
    "透视 perspective",
    "垂直翻转 flipud",
    "水平翻转 fliplr",
    "通道互换 bgr",
    "随机擦除 erasing",
    "裁剪 crop_fraction",
    "随机种子 seed",
)


def has_chinese(text: str) -> bool:
    "Return True when a text carries at least one Chinese character."

    return any("\u4e00" <= char <= "\u9fff" for char in str(text))


def test_the_augment_grid_labels_keep_the_official_names(dialog):
    "Every parameter name is Chinese plus the official argument name."

    page = dialog.config_page

    assert grid_labels(page) == list(AUGMENT_LABELS)
    for label in AUGMENT_LABELS:
        assert has_chinese(label), label
    for official in (
        "hsv_h",
        "hsv_s",
        "hsv_v",
        "degrees",
        "translate",
        "scale min",
        "scale max",
        "shear",
        "perspective",
        "flipud",
        "fliplr",
        "bgr",
        "erasing",
        "crop_fraction",
        "seed",
    ):
        assert any(official in label for label in AUGMENT_LABELS), official


def test_the_threshold_and_amount_labels_are_chinese(dialog):
    "conf / iou / NG IoU / k / r / N keep their name behind a Chinese one."

    page = dialog.config_page
    labels = {label.text() for label in page.findChildren(QtWidgets.QLabel)}

    for text in (
        "置信度 conf",
        "NMS IoU iou",
        "判定 NG IoU",
        "倍数 k",
        "比例 r",
        "总数 N",
    ):
        assert text in labels, text
    # the bare English names of the previous revision are gone
    for text in ("conf", "iou", "NG IoU", "k", "r", "N"):
        assert text not in labels, text


def test_the_file_dialogs_speak_chinese(dialog, monkeypatch):
    "Every title and filter of the file dialogs is written in Chinese."

    page = dialog.config_page
    seen = []

    def fake_dialog(*args):
        seen.append((args[1], args[-1]))
        return "", ""

    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileName", fake_dialog)
    page.browse_classes()
    page.browse_model()
    assert seen == [
        ("选择类别表 classes.txt", "类别表 (*.txt);;所有文件 (*)"),
        ("选择 ONNX 模型", "ONNX 模型 (*.onnx);;所有文件 (*)"),
    ]
    # the browse buttons of the form read Chinese as well
    assert [
        button.text() for button in page.findChildren(QtWidgets.QPushButton)
    ] == ["浏览…", "浏览…", "浏览…", "开始验证"]

    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName", fake_dialog)
    dialog.staging_root = "/staging"
    dialog.export_zip_dialog()
    assert seen[-1] == ("导出 Zip", "Zip 文件 (*.zip)")


def test_the_three_grid_columns_are_equal_and_aligned(dialog):
    "Same column x, same width and a common control line per column."

    page = dialog.config_page
    grid = augment_grid(page)
    label_x = {}
    control_x = {}
    widths = {}
    for row in range(grid.rowCount()):
        for column in range(grid.columnCount()):
            item = grid.itemAtPosition(row, column)
            assert item is not None, (row, column)
            cell = item.widget()
            assert cell is not None
            label = cell.findChild(QtWidgets.QLabel)
            control = cell.findChild(QtWidgets.QAbstractSpinBox)
            assert label is not None and control is not None
            widths.setdefault(column, set()).add(cell.width())
            label_x.setdefault(column, set()).add(
                label.mapTo(page, QtCore.QPoint(0, 0)).x()
            )
            # every parameter name owns the very same label column, so
            # the controls of one column start on a single line
            assert label.width() == grid.columnMinimumWidth(column) - (
                SPIN_WIDTH + 6
            )
            control_x.setdefault(column, set()).add(
                control.mapTo(page, QtCore.QPoint(0, 0)).x()
            )
    for column in range(grid.columnCount()):
        assert len(label_x[column]) == 1, (column, label_x[column])
        assert len(control_x[column]) == 1, (column, control_x[column])
    for column in range(grid.columnCount()):
        assert max(widths[column]) - min(widths[column]) <= 1
        assert grid.columnMinimumWidth(column) == grid.columnMinimumWidth(0)
        assert grid.columnStretch(column) == grid.columnStretch(0) == 1


def test_the_window_height_follows_the_configuration_page(dialog):
    "A freshly shown window must not pad the compact page with blank space."

    dialog.show()
    QtWidgets.QApplication.processEvents()
    # the configuration page needs 563px (544px plus the drop hint line,
    # the fill mode combo of the amount row takes no height of its own),
    # so the window stays at the lower bound instead of the old hard coded
    # 680px.
    assert dialog.config_page.sizeHint().height() <= 580
    assert dialog.minimumHeight() == MINIMUM_HEIGHT == 600
    assert dialog.minimumWidth() == MINIMUM_WIDTH == 1024
    assert dialog.height() <= dialog.config_page.sizeHint().height() + 56
    assert dialog.height() < 680
    first = dialog.size()
    assert dialog.apply_initial_size() == first
    assert dialog.size() == first


def test_the_page_offers_no_fill_option(dialog):
    """The fill cannot be selected: no control, no label, no text.

    The three way option of the previous revision is cancelled: the combo
    of the amount row is gone, no visible label of the page reads 边缘填充
    and no widget of the page names reflect or replicate anywhere - a
    tooltip, a text, a placeholder or a combo entry included.
    """

    page = dialog.config_page
    assert not hasattr(page, "border_mode_combo")
    visible = [
        label.text()
        for label in page.findChildren(QtWidgets.QLabel)
        if label.isVisibleTo(page)
    ]
    assert visible
    assert not [text for text in visible if "边缘填充" in text]
    assert not [text for text in visible if "填充" in text]
    for widget in [page] + page.findChildren(QtWidgets.QWidget):
        for text in (
            widget.toolTip(),
            getattr(widget, "text", lambda: "")(),
            getattr(widget, "placeholderText", lambda: "")(),
        ):
            lowered = str(text).lower()
            assert "reflect" not in lowered, text
            assert "replicate" not in lowered, text
    for combo in page.findChildren(QtWidgets.QComboBox):
        for index in range(combo.count()):
            item = combo.itemText(index)
            assert "填充" not in item, item
            assert "reflect" not in item.lower(), item
            assert "replicate" not in item.lower(), item
    # the collected configuration carries no fill selection: the field is
    # gone and the snapshot states the fixed value of the run alone
    collected = page.collect_config()
    assert not hasattr(collected.augment_params, "border_mode")
    snapshot = collected.to_dict()["augment_params"]
    assert snapshot["border_fill"] == BORDER_FILL == "black"
    assert "border_mode" not in snapshot


def test_the_page_height_is_unchanged_without_the_combo(dialog):
    """Removing the combo left the measured layout of the page alone.

    The fill mode combo shared the row of the amount and thread controls
    and took no height of its own, therefore the page still needs 563px
    and the window keeps its 600px lower bound and its 1024px width.
    """

    dialog.show()
    QtWidgets.QApplication.processEvents()
    hint = dialog.config_page.sizeHint()
    assert hint.height() == 563
    assert hint.height() <= 580
    assert dialog.minimumHeight() == MINIMUM_HEIGHT == 600
    assert dialog.minimumWidth() == MINIMUM_WIDTH == 1024


def test_the_initial_height_hugs_the_content_and_the_screen():
    "A tall page grows the window, a short screen clamps it."

    assert initial_window_height(592, 0) == 600
    assert initial_window_height(592, 200) == 600
    assert initial_window_height(544, 900) == 600
    assert initial_window_height(1200, 900) == 900
    assert initial_window_height(1200, 0) == 1200


def test_the_preview_line_stays_a_single_line(dialog):
    "有效原图 / 将生成增强 / 验证总数 / 预计导出 never wraps."

    page = dialog.config_page
    page.augment_check.setChecked(False)
    assert not page.preview_label.wordWrap()
    assert page.preview_label.isVisibleTo(page)
    assert page.preview_label.text().startswith("有效原图 0 / 将生成增强 0")


def test_results_page_receives_the_augmented_rows(dialog, tmp_path):
    "The results page must show the children added after dataset_ready."

    staging = make_staging_layout(str(tmp_path))
    original = staged_original(staging, "a.png")
    skipped = staged_original(staging, "b.png", with_label=False)
    records = [original, skipped]
    worker = build_worker(staging, records)
    dialog.worker = worker
    dialog.classes = list(CLASSES)

    dialog.on_dataset_ready(
        staging, records, {"counts": {"original": 1, "skipped_no_label": 1}}
    )
    # _stage_augment appends the children to the very same list
    child = staged_child(staging, "a_aug1.png", original.record_id)
    records.append(child)

    dialog.on_worker_finished()

    # the page opens on NG and this test looks at the whole list
    dialog.results_page.filter_combo.setCurrentIndex(0)
    kinds = [record.kind for record in dialog.records]
    assert records_module.KIND_AUGMENTED in kinds
    assert kinds.count(records_module.KIND_AUGMENTED) == 1
    assert kinds.count(records_module.KIND_ORIGINAL) == 2
    page_kinds = [record.kind for record in dialog.results_page.records]
    assert records_module.KIND_AUGMENTED in page_kinds
    assert dialog.results_page.table.rowCount() == 3
    rows = [
        row
        for row in range(dialog.results_page.table.rowCount())
        if dialog.results_page.table.item(row, COLUMN_KIND) is not None
        and dialog.results_page.table.item(row, COLUMN_KIND).text(COLUMN_KIND)
        == records_module.KIND_AUGMENTED
    ]
    assert len(rows) == 1
    checkbox = dialog.results_page.table.cellWidget(rows[0], COLUMN_EXPORT)
    assert isinstance(checkbox, QtWidgets.QCheckBox)
    assert checkbox.isChecked() is False


def test_skipped_records_are_excluded_from_the_preview(dialog, tmp_path):
    "A staged no-label image is neither augmented nor exported."

    staging = make_staging_layout(str(tmp_path))
    original = staged_original(staging, "a.png")
    skipped = staged_original(staging, "b.png", with_label=False)
    dialog.records = [original, skipped]
    dialog._refresh_preview()
    assert dialog.config_page.preview_label.text().startswith("有效原图 1")
    assert (
        dialog.preview_counts(1, dialog.config_page.collect_config())[
            "exported"
        ]
        == 1
    )


def test_results_page_states_the_effective_class_table(dialog):
    "The results page tells which class table produced the labels."

    page = dialog.results_page
    assert page.model_note.text() == ""
    assert page.model_note.isVisible() is False
    page.set_context(list(CLASSES), "staging")
    assert page.model_note.text().startswith("生效类别表：1 类")
    assert "classes.txt" in page.model_note.text()

    for diff_count, expected in ((0, ""), (2, "差异 2 处")):
        text = model_note_text(["a0_dian", "a1_xian"], diff_count)
        assert text.startswith("生效类别表：2 类")
        assert expected in text
    assert model_note_text([]) == ""

    page.set_model_note(
        {
            "classes": ["a0_dian", "a1_xian"],
            "names_model": ["class_0", "class_1"],
            "classes_name_diff": ["0: model='class_0' vs txt='a0_dian'"],
        }
    )
    note = page.model_note.text()
    assert note.startswith("生效类别表：2 类")
    assert "差异 1 处" in note


def test_worker_warnings_reach_the_log_and_the_report(dialog, tmp_path):
    "The placeholder name hint must be visible, not only stored."

    staging = make_staging_layout(str(tmp_path))
    worker = build_worker(staging, [staged_original(staging, "a.png")])
    worker.warnings_ready.connect(dialog.on_worker_warnings)
    worker.warnings_ready.emit(
        ["已以 classes.txt 的类名为准（占位名，差异 2 处）"]
    )
    assert dialog.warnings == [
        "已以 classes.txt 的类名为准（占位名，差异 2 处）"
    ]
    assert (
        "已以 classes.txt 的类名为准" in dialog.progress_page.log.toPlainText()
    )
    # the same list ends up in the exported report
    report = build_report(
        staging,
        dialog.records,
        model_info={"classes": list(CLASSES)},
        classes=list(CLASSES),
        warnings=dialog.warnings,
    )
    assert report["warnings"] == dialog.warnings
