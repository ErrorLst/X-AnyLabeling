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
    TOOLTIP_FLIPLR,
    TOOLTIP_FLIPUD,
    TOOLTIP_INFER_WORKERS,
    TOOLTIP_MODE_FIXED,
    TOOLTIP_RATIO,
    TOOLTIP_SELECT_PROB,
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
    dialog.config_page.ratio_spin.setValue(0.75)
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
    # the mode is fixed to the ratio: 2 valid originals x 0.5 = 1 copy
    assert page.collect_config().augment_mode == RATIO_MODE
    dialog._refresh_preview()
    assert page.preview_label.text() == (
        "有效原图 2 / 将生成增强 1 / 验证总数 3 / 预计导出 2"
    )
    # the ratio input itself drives the preview: 2 x 1 = 2 copies
    page.ratio_spin.setValue(1.0)
    dialog._refresh_preview()
    assert len(calls) == 1
    assert page.preview_label.text() == (
        "有效原图 2 / 将生成增强 2 / 验证总数 4 / 预计导出 2"
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
    """Return the parameter controls of the augment grid, in grid order.

    The select probability is not one of them any more: it shares the
    amount row with the ratio, see amount_row_widgets.
    """

    return [
        widget for _label, _suffix, widget, _tip in page._augment_specs()
    ]


def augment_param_widgets(page):
    """Return the controls the augment checkbox gates.

    The parameter grid plus the select probability, whose control lives
    on the amount row.
    """

    return list(page._augment_param_widgets())


def amount_row_widgets(page) -> list:
    """Return the widgets of the amount row, in layout order.

    The row is the horizontal layout that holds the fixed mode note, the
    ratio and the select probability: it is a layout, not a widget, so
    its widgets are collected from its items and the row is found
    through the ratio spin box it owns.
    """

    owner = page.ratio_spin.parentWidget()
    rows = [
        layout
        for layout in owner.findChildren(QtWidgets.QHBoxLayout)
        if layout.indexOf(page.ratio_spin) >= 0
    ]
    assert len(rows) == 1
    row = rows[0]
    return [row.itemAt(index).widget() for index in range(row.count())]


def amount_row_label(page, control):
    """Return the row label that stands right before a row control."""

    row = amount_row_widgets(page)
    index = row.index(control)
    if index == 0:
        return None
    label = row[index - 1]
    if isinstance(label, QtWidgets.QLabel):
        return label
    return None


def test_every_configuration_control_has_a_tooltip(dialog):
    "Every interactive configuration control explains itself on hover."

    page = dialog.config_page
    controls = interactive_controls(page)
    # 3 source line edits, 3 browse buttons + the history and the start
    # button, 3 thresholds, the judge checkbox, the augment checkbox,
    # the ratio and the ten parameter controls of the grid
    assert len(controls) == 24
    assert {type(control).__name__ for control in controls} == {
        "QLineEdit",
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
    # augment grid and the three hints all contribute a labelled tooltip.
    assert len(labels) == 26

    by_text = {label.text(): label for label in labels}
    pairs = (
        ("数据来源目录", page.dataset_edit),
        ("类别表 classes.txt", page.classes_edit),
        ("ONNX 模型", page.model_edit),
        ("NG 分数 score", page.conf_spin),
        ("NMS IoU iou", page.iou_spin),
        ("判定 NG IoU", page.ng_iou_spin),
        ("对比度 contrast", page.contrast_spin),
        ("垂直翻转", page.flipud_check),
        ("水平翻转", page.fliplr_check),
        ("缩放下限 scale min", page.scale_min_spin),
        ("缩放上限 scale max", page.scale_max_spin),
    )
    for text, control in pairs:
        label = by_text.get(text)
        assert label is not None, text
        assert control.toolTip() != ""
        # the field name repeats the explanation of its own control
        assert label.toolTip() == control.toolTip()
    assert page.scale_min_spin.toolTip() == page.scale_max_spin.toolTip()


def test_the_amount_row_states_the_fixed_ratio(dialog):
    "The mode is a read only note; only the ratio input stays editable."

    page = dialog.config_page
    # the mode is fixed: no combo, no multiplier, no count input exists
    assert not hasattr(page, "mode_combo")
    assert not hasattr(page, "multiplier_spin")
    assert not hasattr(page, "count_spin")
    assert page.mode_note.text() == "数量模式：比例 r（固定）"
    assert page.mode_note.toolTip() == TOOLTIP_MODE_FIXED
    assert page.mode_note.isEnabled() is True
    assert page.ratio_spin.toolTip() == TOOLTIP_RATIO
    assert page.ratio_spin.isVisibleTo(page)
    assert page.augment_check.isChecked() is False
    assert not page.ratio_spin.isEnabled()
    page.augment_check.setChecked(True)
    assert page.ratio_spin.isEnabled()
    # the select probability shares the amount row with the ratio: it
    # explains the draw that turns the ratio into a copy count, so it is
    # no cell of the parameter grid any more
    row = amount_row_widgets(page)
    assert page.select_prob_spin in row
    assert page.ratio_spin in row
    assert page.mode_note in row
    assert page.select_prob_spin not in augment_grid_params(page)
    assert page.select_prob_spin not in grid_cells(page)
    # the row carries its own label for p, not only the bare control
    p_label = amount_row_label(page, page.select_prob_spin)
    assert p_label is not None
    assert p_label.text() == "选中概率 p"
    assert p_label.toolTip() == page.select_prob_spin.toolTip()
    # the two cancelled modes are named as history, never as a choice
    assert "倍数 k" not in page.mode_note.text()
    assert "总数 N" not in page.mode_note.text()
    assert "已取消" in TOOLTIP_MODE_FIXED


def test_the_default_amount_mode_is_ratio(dialog):
    "A fresh window derives the amount with the ratio mode."

    page = dialog.config_page
    assert page.collect_config().augment_mode == RATIO_MODE
    assert page.ratio_spin.value() == DEFAULT_RATIO == 0.5
    assert page.ratio_spin.minimum() == 0.0
    assert page.ratio_spin.maximum() == 1.0
    assert page.ratio_spin.minimumWidth() == SPIN_WIDTH


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
    assert page.ratio_spin.acceptDrops() is False
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
        page.ratio_spin,
        page.contrast_spin,
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
    # the shipped default keeps the augmentation off, so the preview
    # counts the two originals alone
    assert page.augment_check.isChecked() is False
    assert page.preview_label.text() == (
        "有效原图 2 / 将生成增强 0 / 验证总数 2 / 预计导出 2"
    )
    # switching it on derives the copies with the fixed ratio 0.5
    page.augment_check.setChecked(True)
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


def test_the_augment_default_keeps_the_controls_grey(dialog):
    "生成增强副本 is off when the page opens, so its controls are grey."

    page = dialog.config_page
    assert page.augment_check.isChecked() is False
    config = page.collect_config()
    assert config.augment_enabled is False
    assert config.augment_mode == RATIO_MODE
    # the ratio and the whole parameter face follow the checkbox
    assert not page.ratio_spin.isEnabled()
    assert page.ratio_spin.isVisibleTo(page)
    params = augment_param_widgets(page)
    # the nine grid cells plus the select probability of the amount row
    assert len(params) == 10
    assert page.select_prob_spin in params
    for control in params:
        assert control.isEnabled() is False, type(control).__name__
    assert page.augment_check.toolTip() == TOOLTIP_AUGMENT_ENABLED
    # switching it on makes the whole grid usable out of the box
    page.augment_check.setChecked(True)
    assert page.ratio_spin.isEnabled() is True
    for control in params:
        assert control.isEnabled() is True, type(control).__name__
    assert page.flipud_check.isChecked() is True
    assert page.fliplr_check.isChecked() is True


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
    # the screen default keeps the augmentation off: no copy is planned
    dialog._refresh_preview()
    counts = dialog.preview_counts(2, page.collect_config())
    assert counts["originals"] == 2
    assert counts["augmented"] == 0
    assert counts["judged"] == 2
    assert counts["exported"] == 2
    assert page.preview_label.text() == (
        "有效原图 2 / 将生成增强 0 / 验证总数 2 / 预计导出 2"
    )
    # and the checked checkbox alone derives them with the ratio 0.5
    page.augment_check.setChecked(True)
    dialog._refresh_preview()
    counts = dialog.preview_counts(2, page.collect_config())
    assert counts["augmented"] == 1
    assert counts["judged"] == 3
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
    # the ratio is the one amount input, and it is grey as well
    assert page.ratio_spin.isEnabled() is False
    # the whole parameter face greys out with it, the select
    # probability of the amount row included
    params = augment_param_widgets(page)
    assert page.select_prob_spin in params
    for control in params:
        assert control.isEnabled() is False, type(control).__name__
    dialog._refresh_preview()
    counts = dialog.preview_counts(1, page.collect_config())
    assert counts["augmented"] == 0
    assert counts["judged"] == 1
    assert page.preview_label.text() == (
        "有效原图 1 / 将生成增强 0 / 验证总数 1 / 预计导出 1"
    )
    # switching it back on restores the ratio and parameter controls
    page.augment_check.setChecked(True)
    assert page.ratio_spin.isEnabled() is True
    assert page.contrast_spin.isEnabled() is True
    assert page.seed_spin.isEnabled() is True
    assert page.flipud_check.isEnabled() is True
    assert page.fliplr_check.isEnabled() is True
    # the select probability is greyed and restored with them, and it
    # lives on the amount row next to the ratio
    assert page.select_prob_spin.isEnabled() is True
    assert page.select_prob_spin in amount_row_widgets(page)
    page.augment_check.setChecked(False)
    assert page.select_prob_spin.isEnabled() is False
    assert page.ratio_spin.isEnabled() is False


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


def test_the_fixed_ratio_explains_the_amount(dialog):
    "The read only note names the mode, r documents the count."

    page = dialog.config_page
    assert page.mode_note.toolTip() == TOOLTIP_MODE_FIXED
    assert page.ratio_spin.toolTip() == TOOLTIP_RATIO
    # the note names the cancelled modes as history, not as a choice
    assert "比例 r" in TOOLTIP_MODE_FIXED
    assert "已取消" in TOOLTIP_MODE_FIXED
    assert "倍数" in TOOLTIP_MODE_FIXED
    assert "总数 N" in TOOLTIP_MODE_FIXED
    # the ratio tooltip describes the without replacement subset
    for phrase in ("无放回", "每张最多 1 份", "范围 0-1", "r=0"):
        assert phrase in TOOLTIP_RATIO
    # the wording of the repeated draw is gone for good
    assert "被抽 0 次" not in TOOLTIP_RATIO
    assert "被抽多次" not in TOOLTIP_RATIO
    assert "可重复" not in TOOLTIP_RATIO
    # the inline labels repeat the explanation of their own control
    labelled = {
        label.text(): label
        for label in page.findChildren(QtWidgets.QLabel)
        if label.toolTip()
    }
    for text, tooltip in (
        ("比例 r", TOOLTIP_RATIO),
        ("数量模式：比例 r（固定）", TOOLTIP_MODE_FIXED),
    ):
        assert text in labelled
        assert labelled[text].toolTip() == tooltip


def test_the_ratio_drives_the_preview_behind_the_switch(
    dialog, tmp_path
):
    "The preview follows the one amount control the page still owns."

    source = str(tmp_path / "source")
    write_image(osp.join(source, "a.png"))
    dataset.write_json(osp.join(source, "a.json"), label_payload("a.png"))
    page = dialog.config_page
    page.set_dataset(source)
    assert wait_for_source_scan(dialog, source)
    # the switched off default plans no copy at all, whatever r says
    page.ratio_spin.setValue(1.0)
    dialog._refresh_preview()
    assert page.preview_label.text().startswith("有效原图 1 / 将生成增强 0")
    # switching the augmentation on makes the ratio the only driver
    page.augment_check.setChecked(True)
    dialog._refresh_preview()
    assert page.preview_label.text() == (
        "有效原图 1 / 将生成增强 1 / 验证总数 2 / 预计导出 1"
    )
    page.ratio_spin.setValue(0.0)
    dialog._refresh_preview()
    assert page.preview_label.text() == (
        "有效原图 1 / 将生成增强 0 / 验证总数 1 / 预计导出 1"
    )


def test_every_augment_spin_uses_the_compact_width(dialog):
    "The arrows are gone and every spin box shares one narrow width."

    page = dialog.config_page
    spins = page.findChildren(QtWidgets.QAbstractSpinBox)
    # 3 thresholds + 7 amplitude spins + the ratio and the seed
    assert len(spins) == 12
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
        if grid.count() >= 9
    ]
    assert len(grids) == 1
    grid = grids[0]
    # nine parameters over three columns: three rows, the last one
    # filled by the seed alone; the select probability is not a cell any
    # more, it lives on the amount row
    assert (grid.rowCount(), grid.columnCount()) == (3, 3)
    assert grid.count() == 9
    return grid


def grid_cells(page) -> list:
    "Return the cells of the grid, in row major order, gaps dropped."

    grid = augment_grid(page)
    cells = []
    for row in range(grid.rowCount()):
        for column in range(grid.columnCount()):
            item = grid.itemAtPosition(row, column)
            # every position of the three rows carries a cell
            if item is not None:
                cells.append(item.widget())
    return cells


def grid_labels(page) -> list:
    "Return the parameter names of the grid, in row major order."

    return [
        cell.findChild(QtWidgets.QLabel).text() for cell in grid_cells(page)
    ]


# The label of every row of the grid: the Chinese meaning first, the
# official Ultralytics argument name behind it, so the page stays
# comparable 1:1 with the documentation of the augmentation arguments.
AUGMENT_LABELS = (
    "对比度 contrast",
    "亮度 hsv_v",
    "旋转角度 degrees（°）",
    "平移 translate",
    "缩放下限 scale min",
    "缩放上限 scale max",
    "垂直翻转",
    "水平翻转",
    "随机种子 seed",
)


def has_chinese(text: str) -> bool:
    "Return True when a text carries at least one Chinese character."

    return any("\u4e00" <= char <= "\u9fff" for char in str(text))


def test_the_augment_grid_names_every_shipped_parameter(dialog):
    "Every parameter name is Chinese plus the official argument name."

    page = dialog.config_page

    assert grid_labels(page) == list(AUGMENT_LABELS)
    # the select probability is no grid label any more: it stands on the
    # amount row next to the ratio it explains
    assert "选中概率 p" not in grid_labels(page)
    for label in AUGMENT_LABELS:
        assert has_chinese(label), label
    # the six official arguments that survive keep their exact name
    for official in (
        "hsv_v",
        "degrees",
        "translate",
        "scale min",
        "scale max",
        "seed",
    ):
        assert any(official in label for label in AUGMENT_LABELS), official
    # the two flips are labelled in Chinese alone: their official name
    # lives in the tooltip, which the next test pins
    for label in AUGMENT_LABELS:
        assert "flipud" not in label
        assert "fliplr" not in label
    # the seven dropped arguments are gone from the grid
    for dropped in (
        "hsv_h",
        "hsv_s",
        "shear",
        "perspective",
        "bgr",
        "erasing",
        "crop_fraction",
    ):
        assert not any(dropped in label for label in AUGMENT_LABELS), dropped


def test_the_flip_checkboxes_carry_their_official_name(dialog):
    "The two enable bits are check boxes keeping the official name."

    page = dialog.config_page

    assert isinstance(page.flipud_check, QtWidgets.QCheckBox)
    assert isinstance(page.fliplr_check, QtWidgets.QCheckBox)
    assert page.flipud_check.text() == ""
    assert page.fliplr_check.text() == ""
    assert page.flipud_check.toolTip() == TOOLTIP_FLIPUD
    assert page.fliplr_check.toolTip() == TOOLTIP_FLIPLR
    assert "flipud" in page.flipud_check.toolTip()
    assert "fliplr" in page.fliplr_check.toolTip()
    for tip in (TOOLTIP_FLIPUD, TOOLTIP_FLIPLR):
        assert "启用位" in tip
        assert "official_names" in tip
    # the check boxes answer the enable bits of the form
    params = page.augment_params()
    assert params.flipud is True
    assert params.fliplr is True
    page.flipud_check.setChecked(False)
    page.fliplr_check.setChecked(False)
    params = page.augment_params()
    assert params.flipud is False
    assert params.fliplr is False
    # an unchecked flip never fires, a checked one fires with p
    assert params.to_official_dict()["flipud"] == 0.0
    assert params.to_official_dict()["fliplr"] == 0.0
    page.flipud_check.setChecked(True)
    assert page.augment_params().to_official_dict()["flipud"] == (
        page.select_prob_spin.value()
    )


def test_the_select_probability_names_the_retry(dialog):
    "The p control explains the draw and the retry it can trigger."

    page = dialog.config_page

    assert page.select_prob_spin.toolTip() == TOOLTIP_SELECT_PROB
    for phrase in ("选中概率", "独立抽签", "discarded_empty_selection"):
        assert phrase in TOOLTIP_SELECT_PROB
    assert page.select_prob_spin.minimum() == 0.05
    assert page.select_prob_spin.maximum() == 1.0
    assert page.select_prob_spin.value() == 0.1
    # p is no cell of the parameter grid any more: it shares the amount
    # row with the fixed ratio it explains
    assert page.select_prob_spin not in augment_grid_params(page)
    assert page.select_prob_spin not in grid_cells(page)
    assert page.select_prob_spin in amount_row_widgets(page)
    assert amount_row_label(page, page.select_prob_spin) is not None
    # and it still follows the augment checkbox like the grid does
    assert page.select_prob_spin.isEnabled() is False
    assert page.select_prob_spin in augment_param_widgets(page)
    page.augment_check.setChecked(True)
    assert page.select_prob_spin.isEnabled() is True


def test_the_threshold_and_amount_labels_are_chinese(dialog):
    "score / iou / NG IoU / k / r / N keep their name behind a Chinese one."

    page = dialog.config_page
    labels = {label.text() for label in page.findChildren(QtWidgets.QLabel)}

    for text in (
        "NG 分数 score",
        "NMS IoU iou",
        "判定 NG IoU",
        "比例 r",
        "数量模式：比例 r（固定）",
    ):
        assert text in labels, text
    # the cancelled modes are not offered as a choice any more, and the
    # bare English names of the previous revision are gone
    for text in ("倍数 k", "总数 N", "数量模式", "conf", "iou", "NG IoU",
                 "k", "r", "N"):
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
    # the browse buttons of the form read Chinese as well, and the
    # history entry sits between them and the start it belongs beside
    assert [
        button.text() for button in page.findChildren(QtWidgets.QPushButton)
    ] == ["浏览…", "浏览…", "浏览…", "历史记录…", "开始验证"]

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
            if item is None:
                # every cell of the three rows is filled
                continue
            cell = item.widget()
            assert cell is not None
            label = cell.findChild(QtWidgets.QLabel)
            # the control is the widget the cell layout places after the
            # name: a spin box for eight parameters, the check box of a
            # flip for the two others (findChild would answer the label)
            layout = cell.layout()
            control = layout.itemAt(1).widget()
            assert label is not None and control is not None
            assert control is not label
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
    # The configuration page hugs its own content now (the amount row is
    # a read only note plus the one ratio input), so the upper bound the
    # compact form was pinned to still holds and the window keeps its
    # 600px lower bound instead of the old hard coded 680px.
    hint = dialog.config_page.sizeHint().height()
    assert 520 <= hint <= 660
    assert dialog.minimumHeight() == MINIMUM_HEIGHT == 600
    assert dialog.minimumWidth() == MINIMUM_WIDTH == 1024
    assert dialog.height() <= hint + 56
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


def test_the_page_height_stays_inside_the_screen_budget(dialog):
    """The amount row is one note plus one input, no combo at all.

    The height the page asks for is a function of the font metrics of
    the environment, therefore the test pins the window bounds and the
    budget of the compact form instead of one absolute pixel count.
    """

    dialog.show()
    QtWidgets.QApplication.processEvents()
    hint = dialog.config_page.sizeHint()
    # 563px of content on the font this form was measured with; a wider
    # font of another environment may ask for more, never for a page
    # that dwarfs the window budget
    assert 520 <= hint.height() <= 660
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

    # the page opens on NG and this test looks at the whole list; the
    # switch only asks for the rebuild, so the events are driven once
    dialog.results_page.filter_combo.setCurrentIndex(0)
    QtWidgets.QApplication.processEvents()
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
