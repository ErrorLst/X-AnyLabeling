"""Offscreen regression net for the remote training window (spec §5.1).

Covers the four pages, the configuration form, the split preview
rendering, the local pre-check and - most importantly - the close state
machine and the application close guard of spec §5.1.4, including the
regression anchor "a window opened after the first one was destroyed is
still protected by the process wide guard".

Every test runs without a server and without network I/O: the API client
is a stub and the datasets are built in `tmp_path`.
"""

from __future__ import annotations

import json
import os
import threading

import pytest
from PyQt6 import QtCore, QtGui, QtWidgets

from anylabeling.custom.remote_training import launcher
from anylabeling.custom.remote_training.api_client import (
    HEALTH_STATE_OK,
    HEALTH_STATE_TRAINING_DISABLED,
    HEALTH_STATE_UNAUTHORIZED,
    HEALTH_STATE_UNREACHABLE,
    HealthProbe,
    HealthStatus,
    NotFoundError,
    TransportError,
    UnauthorizedError,
)
from anylabeling.custom.remote_training.splitter import SplitPreview
from anylabeling.custom.remote_training.ui.close_guard import (
    ApplicationCloseGuard,
    workers_finished,
)

CAPABILITIES = {
    "param_schema": {
        "epochs": {"type": "int", "min": 1, "max": 1000},
        "optimizer": {"type": "preset",
                      "values": ["yolo11-sgd", "yolo11-adamw",
                                 "yolo26-default", "auto"]},
    },
    "optimizer_presets": {
        "yolo11-sgd": {"optimizer": "SGD"},
        "yolo11-adamw": {"optimizer": "AdamW"},
        "yolo26-default": {"optimizer": "MuSGD"},
    },
    "model_families": {
        "yolo11": {"weights": {"detect": ["yolo11n.pt", "yolo11s.pt"],
                               "segment": ["yolo11n-seg.pt"]},
                   "presets": ["yolo11-sgd", "yolo11-adamw", "auto"]},
        "yolo26": {"weights": {"detect": ["yolo26n.pt"]},
                   "presets": ["yolo26-default", "auto"]},
    },
    "preset_policy": {"default_preset": {"yolo11": "yolo11-sgd",
                                         "yolo26": "yolo26-default"}},
    "tasks": ["detect", "segment"],
}


class Parent(QtWidgets.QMainWindow):
    """Stand in for the labeling main window."""


class Hold(QtCore.QThread):
    """A worker that runs until cancel() is called."""

    def __init__(self):
        super().__init__()
        self.stopped = False

    def run(self):  # noqa: D102 - QThread entry point
        while not self.stopped:
            self.msleep(10)

    def cancel(self):  # noqa: D102 - the close machine's hook
        self.stopped = True


class SlowCancel(QtCore.QThread):
    """A worker that ignores cancel() and ends on its own after ~1.2 s."""

    def __init__(self):
        super().__init__()
        self.stopped = False

    def run(self):  # noqa: D102 - QThread entry point
        for _ in range(120):
            if self.stopped:
                break
            self.msleep(10)

    def cancel(self):  # noqa: D102 - deliberately not honoured at once
        pass


class StubClient:
    """One canned answer for the health probe."""

    def __init__(self, probe=None, error=None):
        self._probe = probe
        self._error = error

    def probe_health(self):
        if self._error is not None:
            raise self._error
        return self._probe

    def get_capabilities(self):
        return None


def make_dataset(root, classes, per_image):
    """Write a tiny root-only dataset (image + labelme json)."""

    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "classes.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(classes) + "\n")
    for name, labels in per_image:
        shapes = [
            {"label": label, "shape_type": "rectangle",
             "points": [[0, 0], [1, 1]]}
            for label in labels
        ]
        data = {"version": "1.0", "imageWidth": 10, "imageHeight": 10,
                "shapes": shapes}
        with open(os.path.join(root, name + ".json"), "w",
                  encoding="utf-8") as fh:
            json.dump(data, fh)
        with open(os.path.join(root, name + ".jpg"), "wb") as fh:
            fh.write(b"x")
    return str(root)


def health_payload(**overrides):
    payload = {
        "enabled": True, "server_version": "1.0", "time": "t",
        "queue": {"queued": 3, "running": 1, "max_concurrent_jobs": 2},
        "jobs": {}, "devices": [{"device_index": 0}],
        "work_dir": {"free_gb": 10}, "training_env": {},
        "calibration": {"required": True, "auto_loaded": True},
        "weights": {"cached": 14, "missing": 6},
        "blobs": {"count": 12840, "materialize": "hardlink"},
        "warnings": [],
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def dialogs(qapp, pump):
    """Track the windows a test opens and close them afterwards."""

    opened = []
    parents = []

    def _open(parent=None):
        parent = parent if parent is not None else Parent()
        parents.append(parent)
        dialog = launcher.launch_remote_training(parent)
        opened.append(dialog)
        dialog.show()
        pump(30)
        return dialog

    yield _open
    for dialog in opened:
        try:
            # Never let a teardown block on the step 0 confirmation (an
            # offscreen run has nobody to answer it) and never leave a
            # thread behind.
            dialog._confirm_close = lambda _dialog: True
            dialog.cancel_workers()
            dialog.reject()
        except RuntimeError:  # pragma: no cover - already destroyed
            pass
    pump(200)


# --------------------------------------------------------------- pages


def test_four_pages_instantiate(dialogs):
    dialog = dialogs()
    assert type(dialog).__name__ == "RemoteTrainingDialog"
    assert [type(page).__name__ for page in (
        dialog.config_page, dialog.jobs_page,
        dialog.detail_page, dialog.results_page,
    )] == ["ConfigPage", "JobsPage", "DetailPage", "ResultsPage"]
    assert dialog.stack.count() == 4
    assert dialog.testAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)


def test_jobs_page_keeps_no_whole_row_setter(qapp):
    """B5 nit: the one line setter that wiped the D5 banner is gone.

    `JobsPage.set_status` had no caller left (production and tests):
    the list summary and the reconciliation banner are composed into
    the row instead of replacing it (spec §5.4.1), so the footgun that
    caused the first D5 round is removed rather than kept around.
    """

    from anylabeling.custom.remote_training.ui.jobs_page import JobsPage

    assert not hasattr(JobsPage, "set_status")
    # The summary label and the composed row stay, they just have no
    # whole row setter any more.
    page = JobsPage()
    assert hasattr(page, "status_label")
    assert hasattr(page, "status_row")


def test_the_window_module_ends_with_a_newline():
    """B6 nit: the ui package must not lose its final newline.

    A missing trailing newline shows up as a spurious '\\ No newline at
    end of file' in every later diff of the file, which is how the
    review found it in the first place.
    """

    import anylabeling.custom.remote_training.ui.dialog as dialog_module

    path = dialog_module.__file__
    with open(path, "rb") as handle:
        content = handle.read()
    assert content.endswith(b"\n")


def test_every_feature_module_ends_with_a_newline():
    """Item 8 (rounds 2, 3 and B8): the whole feature tree, both folders.

    The same one byte defect was present in eight files when the review
    looked, and the two test side files were fixed without a guard, so
    the walk covers the package directory AND this test directory.

    The B8 checkpoint fixes a regression of the guard itself: round 3
    used os.listdir (not recursive), so the eight ui/*.py modules the
    first guard caught - ui/{__init__,close_guard,config_page,
    detail_page,dialog,jobs_page,results_page,widgets}.py, ui/ being a
    *subdirectory* of the package - were no longer asserted.  os.walk
    puts the whole subtree back under the guard, and the two coverage
    assertions below fail if the net is ever narrowed again, either to
    the ui/ subtree alone or to one directory level.
    """

    import anylabeling.custom.remote_training as package

    package_dir = os.path.dirname(os.path.abspath(package.__file__))
    tests_dir = os.path.dirname(os.path.abspath(__file__))
    scanned = set()
    offenders = []
    for folder in (package_dir, tests_dir):
        for current, dirs, names in os.walk(folder):
            dirs.sort()
            for name in sorted(names):
                if not name.endswith(".py"):
                    continue
                path = os.path.abspath(os.path.join(current, name))
                scanned.add(path)
                with open(path, "rb") as handle:
                    if not handle.read().endswith(b"\n"):
                        offenders.append(
                            os.path.relpath(path, package_dir)
                        )
    # The net really covers the ui subpackage, not only the top level.
    ui_dir = os.path.join(package_dir, "ui")
    ui_modules = {
        os.path.abspath(os.path.join(ui_dir, name))
        for name in os.listdir(ui_dir)
        if name.endswith(".py")
    }
    assert ui_modules, "the ui subpackage must contain modules"
    assert ui_modules <= scanned
    # ... and the package top level too: os.walk(ui_dir) alone would
    # still pass the assertion above.
    top_level = {
        os.path.abspath(os.path.join(package_dir, name))
        for name in ("__init__.py", "worker.py", "api_client.py")
    }
    assert top_level <= scanned
    assert offenders == []


def test_debug_area_and_read_only_paths(dialogs):
    dialog = dialogs()
    debug = dialog.results_page.debug_edit
    assert debug.objectName() == "debugInfoEdit"
    assert debug.isReadOnly()
    # Mouse and keyboard selection: the debug area stays copyable
    # (spec §5.1.5, refined by the monitoring step).
    flags = int(debug.textInteractionFlags().value)
    assert flags == int(
        QtCore.Qt.TextInteractionFlag.TextSelectableByMouse.value
        | QtCore.Qt.TextInteractionFlag.TextSelectableByKeyboard.value
    )
    assert dialog.config_page.dataset_edit.isReadOnly()
    assert dialog.config_page.classes_edit.isReadOnly()


def test_launcher_reuses_one_instance(qapp, dialogs):
    parent = Parent()
    first = dialogs(parent)
    assert parent._remote_training_dialog is first
    assert launcher.launch_remote_training(parent) is first


# ------------------------------------------------------- connection


@pytest.mark.parametrize("case,expected,severity", [
    ("ok", "连接成功", "info"),
    ("unauthorized", "Token 无效或已过期", "red"),
    ("not_ready", "服务端正在启动", "red"),
    ("wrong_address", "地址不正确", "red"),
    ("disabled", "服务端未启用远程训练", "red"),
])
def test_connection_test_states(dialogs, pump, case, expected, severity):
    dialog = dialogs()
    if case == "ok":
        dialog.client = StubClient(
            HealthProbe(HEALTH_STATE_OK, HealthStatus(health_payload())))
    elif case == "unauthorized":
        dialog.client = StubClient(HealthProbe(
            HEALTH_STATE_UNAUTHORIZED, None,
            UnauthorizedError(401, "UNAUTHORIZED", "bad")))
    elif case == "not_ready":
        dialog.client = StubClient(HealthProbe(
            HEALTH_STATE_UNREACHABLE, None, TransportError("refused")))
    elif case == "wrong_address":
        dialog.client = StubClient(
            error=NotFoundError(404, "NOT_FOUND", "no route"))
    else:
        dialog.client = StubClient(HealthProbe(
            HEALTH_STATE_TRAINING_DISABLED,
            HealthStatus({"enabled": False})))
    worker = dialog.test_connection()
    worker.wait(5000)
    pump(50)
    lines = dialog.config_page.status_row.lines()
    assert expected in lines[0]
    assert dialog.config_page.status_row.severities()[0] == severity
    assert dialog.workers == []


# ---------------------------------------------------------- the form


def test_parameter_surface_follows_capabilities(dialogs):
    dialog = dialogs()
    page = dialog.config_page
    page.set_capabilities(dict(CAPABILITIES))
    assert len(page._params) == 23
    # the only initial value is the family's auto sentinel (spec §3.8.4):
    # every other parameter stays out of the request until it is set
    assert page.values().params == {"optimizer": "auto"}
    page._params["epochs"].widget.setValue(7)
    assert page.values().params == {"epochs": 7, "optimizer": "auto"}


def test_weights_follow_family_and_task(dialogs):
    dialog = dialogs()
    page = dialog.config_page
    page.set_capabilities(dict(CAPABILITIES))

    def items(combo):
        return [combo.itemText(i) for i in range(combo.count())]

    assert items(page.model_combo) == ["yolo11n.pt", "yolo11s.pt"]
    page.set_task("segment")
    assert items(page.model_combo) == ["yolo11n-seg.pt"]
    page.set_task("detect")
    page.model_family_combo.setCurrentIndex(
        items(page.model_family_combo).index("yolo26"))
    assert items(page.model_combo) == ["yolo26n.pt"]


def test_presets_follow_the_family(dialogs):
    dialog = dialogs()
    page = dialog.config_page
    page.set_capabilities(dict(CAPABILITIES))

    def items(combo):
        return [combo.itemText(i) for i in range(combo.count())]

    widget = page._params["optimizer"].widget
    # the first entry lets the server pick by preset_policy, the second
    # is the auto sentinel the form starts on (§3.8.4)
    assert items(widget) == ["（由服务端默认策略决定；家族默认 yolo11-sgd）",
                             "auto", "yolo11-adamw", "yolo11-sgd"]
    assert widget.currentData() == "auto"
    page.model_family_combo.setCurrentIndex(
        items(page.model_family_combo).index("yolo26"))
    assert items(widget) == ["（由服务端默认策略决定；家族默认 yolo26-default）",
                             "auto", "yolo26-default"]
    assert widget.currentData() == "auto"


def test_a_family_without_the_sentinel_keeps_the_server_policy(dialogs):
    """No `auto` in the family presets: the first entry stays (§3.8.4)."""

    dialog = dialogs()
    page = dialog.config_page
    page.set_capabilities({
        "param_schema": CAPABILITIES["param_schema"],
        "optimizer_presets": CAPABILITIES["optimizer_presets"],
        "model_families": {
            "yolo26": {"weights": {"detect": ["yolo26n.pt"]},
                       "presets": ["yolo26-default"]},
        },
        "preset_policy": {"default_preset": {
            "yolo26": "yolo26-default"}},
        "tasks": ["detect"],
    })
    widget = page._params["optimizer"].widget
    items = [widget.itemText(index) for index in range(widget.count())]
    assert items == ["（由服务端默认策略决定；家族默认 yolo26-default）",
                     "yolo26-default"]
    assert widget.currentData() is None
    assert page._params["optimizer"].explicit is False
    assert page.values().params == {}


def test_family_switch_to_an_auto_family_selects_the_sentinel(dialogs):
    """Switching into a family with `auto` selects it (spec §3.8.4).

    The family without the sentinel leaves the combo on the first entry
    (server policy).  The switch has to resolve `auto` against the new
    item list, not against the stale one of the family just left.
    """

    dialog = dialogs()
    page = dialog.config_page
    page.set_capabilities({
        "param_schema": CAPABILITIES["param_schema"],
        "optimizer_presets": CAPABILITIES["optimizer_presets"],
        "model_families": {
            "yolo26": {"weights": {"detect": ["yolo26n.pt"]},
                       "presets": ["yolo26-default"]},
            "yolo11": {"weights": {"detect": ["yolo11n.pt"]},
                       "presets": ["auto", "yolo11-adamw",
                                   "yolo11-sgd"]},
        },
        "preset_policy": {"default_preset": {
            "yolo11": "yolo11-sgd", "yolo26": "yolo26-default"}},
        "tasks": ["detect"],
    })
    widget = page._params["optimizer"].widget
    families = [page.model_family_combo.itemText(index)
                for index in range(page.model_family_combo.count())]
    assert families == ["yolo26", "yolo11"]
    # the family without `auto` starts on the server policy entry
    assert widget.currentData() is None
    assert page.values().params == {}
    page.model_family_combo.setCurrentIndex(families.index("yolo11"))
    assert widget.currentData() == "auto"
    assert page.values().params == {"optimizer": "auto"}


def test_import_and_export_round_trip(dialogs, tmp_path):
    dialog = dialogs()
    page = dialog.config_page
    page.set_capabilities(dict(CAPABILITIES))
    source = tmp_path / "config.json"
    source.write_text(json.dumps({
        "schema_version": 1, "server_url": "http://h:8000",
        "dataset_dir": "/tmp/ds", "classes_file": "/tmp/ds/classes.txt",
        "task": "segment", "model_family": "yolo11",
        "model": "yolo11n-seg.pt", "val_ratio": 0.25, "seed": 20260101,
        "split_strategy": "per_class", "params": {"epochs": 50},
        "api_key": "SECRET",
    }), encoding="utf-8")
    values = dialog.import_config(str(source))
    assert "api_key" not in values
    assert page.dataset_edit.text() == "/tmp/ds"
    assert page.current_task() == "segment"
    assert page.seed_text() == "20260101"
    assert page.values().params == {"epochs": 50, "optimizer": "auto"}

    target = tmp_path / "out.json"
    assert dialog.write_config_document(str(target)) is True
    exported = json.loads(target.read_text(encoding="utf-8"))
    assert exported["task"] == "segment"
    assert exported["seed"] == 20260101
    assert "api_key" not in exported
    assert sorted(exported) == [
        "classes_file", "dataset_dir", "model", "model_family", "params",
        "schema_version", "seed", "server_url", "split_strategy", "task",
        "val_ratio",
    ]


def test_bad_seed_and_bad_import_are_reported(dialogs, tmp_path):
    dialog = dialogs()
    page = dialog.config_page
    page.set_capabilities(dict(CAPABILITIES))
    page.seed_edit.setText("not-a-number")
    assert dialog.write_config_document(str(tmp_path / "x.json")) is False
    assert page.status_row.severities()[:1] == ["red"]

    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps({"server_url": "x", "seed": 1}),
                      encoding="utf-8")
    assert dialog.import_config(str(broken)) == {}
    assert page.status_row.severities()[:1] == ["red"]


def test_capabilities_rebuild_keeps_explicit_params(dialogs):
    dialog = dialogs()
    page = dialog.config_page
    page.set_capabilities(dict(CAPABILITIES))
    page._params["epochs"].widget.setValue(50)
    page.set_capabilities(dict(CAPABILITIES))
    assert page.values().params == {"epochs": 50, "optimizer": "auto"}


def test_optimizer_defaults_to_auto_in_the_request(dialogs):
    """An untouched form submits `auto` (spec §3.8.4, §5.2.2)."""

    dialog = dialogs()
    page = dialog.config_page
    page.set_capabilities(dict(CAPABILITIES))
    widget = page._params["optimizer"].widget
    items = [widget.itemText(index) for index in range(widget.count())]
    assert "auto" in items
    assert widget.currentData() == "auto"
    assert page._params["optimizer"].explicit is True
    assert page.explicit_params() == {"optimizer": "auto"}
    assert page.values().params == {"optimizer": "auto"}
    params = dialog.job_request_body()["params"]
    assert params == {"optimizer": "auto"}


def test_server_policy_entry_keeps_optimizer_out_of_the_request(dialogs):
    """Picking the first entry hands the decision back (spec §3.8.4)."""

    dialog = dialogs()
    page = dialog.config_page
    page.set_capabilities(dict(CAPABILITIES))
    widget = page._params["optimizer"].widget
    widget.setCurrentIndex(0)
    assert widget.currentData() is None
    assert "optimizer" not in page.explicit_params()
    assert "optimizer" not in page.values().params
    assert "optimizer" not in dialog.job_request_body()["params"]
    # and back again: the sentinel is one explicit choice of two
    widget.setCurrentIndex(widget.findData("auto"))
    assert page.values().params == {"optimizer": "auto"}


def test_a_capabilities_refresh_keeps_the_server_policy_choice(dialogs):
    """The first entry survives a late `capabilities` answer (§3.8.4).

    A second `set_capabilities` is realistic: re-clicking 「测试连接」
    or the error driven capabilities refresh runs it again.  The
    explicit but valueless choice must not flip the form back to the
    `auto` default and send `optimizer: "auto"` on the next submit.
    """

    dialog = dialogs()
    page = dialog.config_page
    page.set_capabilities(dict(CAPABILITIES))
    widget = page._params["optimizer"].widget
    widget.setCurrentIndex(0)
    assert widget.currentData() is None
    assert "optimizer" not in page.values().params
    page.set_capabilities(dict(CAPABILITIES))
    widget = page._params["optimizer"].widget
    assert widget.currentIndex() == 0
    assert widget.currentData() is None
    assert "optimizer" not in page.explicit_params()
    assert "optimizer" not in page.values().params
    # ... and the state is stable over further answers
    page.set_capabilities(dict(CAPABILITIES))
    widget = page._params["optimizer"].widget
    assert widget.currentIndex() == 0
    assert "optimizer" not in page.values().params


def test_explicit_preset_and_imported_optimizer_are_kept(dialogs):
    """A chosen preset stays the request value (spec §5.2.8)."""

    dialog = dialogs()
    page = dialog.config_page
    page.set_capabilities(dict(CAPABILITIES))
    widget = page._params["optimizer"].widget
    widget.setCurrentIndex(widget.findData("yolo11-sgd"))
    assert page.values().params == {"optimizer": "yolo11-sgd"}
    page.set_params({"optimizer": "auto"})
    assert page.values().params == {"optimizer": "auto"}


# ------------------------------------------------------- the preview


def test_preview_rendering_and_blocking_states(dialogs, tmp_path):
    dialog = dialogs()
    page = dialog.config_page
    dataset = make_dataset(
        tmp_path / "ds", ["cat", "dog"],
        [("img_%02d" % index, ["cat"]) for index in range(6)])
    page.dataset_edit.setText(dataset)
    page.classes_edit.setText(os.path.join(dataset, "classes.txt"))
    page.set_task("detect")
    page.seed_edit.setText("")
    pipeline = dialog.build_pipeline()
    assert page.seed_text().isdigit()
    assert int(page.seed_text()) == pipeline.effective_seed()
    pipeline.assemble()
    page.set_preview(pipeline.split_preview)
    assert page.preview_table.rowCount() == 2
    assert "实际占比" in page.preview_summary.text()
    # the yellow row carries the warnings, never the per class counts
    assert all("train=" not in line for line in page.preview_row.lines())

    synthetic = SplitPreview(
        total_images=6, val_total=2, val_ratio_target=0.2, nominal_total=2,
        unresolved=["dog"],
        split_stats={"cat": {"train": 4, "val": 2},
                     "dog": {"train": 0, "val": 0}},
        warnings=["实际 val 占比与目标相差较大"])
    page.set_preview(synthetic)
    assert page.preview_row.lines() == ["实际 val 占比与目标相差较大"]
    assert page.preview_table.item(1, 0).background().color().name() \
        == "#ff0000"
    red = dialog.preview_status_lines(synthetic)
    assert red and red[0][0] == "red"


def test_blocking_pipeline_error_renders_red(dialogs, tmp_path):
    from anylabeling.custom.remote_training.pipeline import PipelineError

    dialog = dialogs()
    page = dialog.config_page
    dataset = make_dataset(tmp_path / "blocked", ["cat", "dog"],
                           [("a", ["cat"]), ("b", ["dog"])])
    page.dataset_edit.setText(dataset)
    page.classes_edit.setText(os.path.join(dataset, "classes.txt"))
    pipeline = dialog.build_pipeline()
    with pytest.raises(PipelineError) as error:
        pipeline.prepare()
    class Caps:
        payload = {}

        def vram_entry(self, model, task):
            return {"model": model, "task": task, "max_batch": 96,
                    "source": "auto"}

        def unschedulable_reason(self, model, task):
            return None

    dialog.capabilities = Caps()
    lines = dialog.report_precheck_error(error.value)
    assert page.status_row.severities()[:1] == ["red"]
    assert "验证侧（val）为空" in error.value.status_text()
    # the estimate does not depend on the dataset (spec §5.1.3)
    assert any("max_batch=96" in text for _severity, text in lines), lines


def test_server_receipt_is_informational(dialogs):
    dialog = dialogs()
    page = dialog.config_page
    page.status_row.clear()
    lines = dialog.show_server_receipt([
        {"code": "SPLIT_CLASS_MISSING_VAL", "message": "dog missing val",
         "details": {"classes": ["dog"]}},
        {"code": "BACKGROUND_IMAGES", "message": "background",
         "files": ["a.jpg"]},
    ])
    assert lines
    assert all(severity != "red" for severity, _text in lines)


def test_precheck_shows_vram_estimate(dialogs, tmp_path):
    dialog = dialogs()
    page = dialog.config_page
    page.set_capabilities(dict(CAPABILITIES))
    dataset = make_dataset(tmp_path / "pre", ["cat"],
                           [("a_%d" % index, ["cat"]) for index in range(4)])
    page.dataset_edit.setText(dataset)
    page.classes_edit.setText(os.path.join(dataset, "classes.txt"))

    class Caps:
        payload = {}

        def vram_entry(self, model, task):
            if model in ("yolo11n.pt", "yolo11n"):
                return {"model": model, "task": task, "max_batch": 96,
                        "source": "auto"}
            return None

        def unschedulable_reason(self, model, task):
            return None

    dialog.capabilities = Caps()
    lines = dialog.vram_summary_lines()
    assert "max_batch=96" in lines[0][1] and "auto" in lines[0][1]


# ------------------------------------------------- results and health


def test_jobs_detail_and_results_render(dialogs):
    from anylabeling.custom.remote_training.ui.jobs_page import (
        job_duration_seconds,
    )

    dialog = dialogs()
    job = {
        "job_id": "job_20260101_7f2a91", "status": "running",
        "progress": {"epoch": 12, "total_epochs": 100, "percent": 12.0},
        "device_index": 0, "metrics": {"mAP50": 0.512}, "resume_cycles": 1,
        "needs_attention": False, "queued_reason": "WAITING_CONCURRENCY_SLOT",
        "partial_available": True, "artifact_suspect": False,
        "is_terminal": False, "attempt": 2, "max_attempts": 3,
        "resume_mode_available": [],
        "created_at": "2026-01-01T10:00:00Z",
        "started_at": "2026-01-01T10:01:00Z",
        "finished_at": "2026-01-01T10:03:30Z",
    }
    assert job_duration_seconds(job) == 150.0
    dialog.jobs_page.set_jobs([job])
    assert dialog.jobs_page.displayed_cell(0, 1) == "job_20260101_7f2a91"
    assert dialog.jobs_page.displayed_cell(0, 2) == "训练中"
    # column 0 is the check box, then name / status / progress / device /
    # duration / attention / resume cycles
    assert dialog.jobs_page.displayed_cell(0, 5) == "2m30s"

    dialog.detail_page.set_job(job)
    assert dialog.detail_page.badge.text() == "训练中"
    assert dialog.detail_page.resume_button.isEnabled() is False
    assert "并发位已满" in dialog.detail_page.queued_reason_label.text()

    failed = dict(job, needs_attention=True,
                  needs_attention_reason="attempts_exhausted",
                  error_summary="boom")
    dialog.detail_page.set_job(failed)
    assert "并发位已满" in dialog.detail_page.queued_reason_label.text()
    assert "需关注" in dialog.detail_page.attention_label.text()

    dialog.results_page.set_job(job, [
        {"file_id": "f_1", "path": "weights/best.pt", "size": 18621442},
        {"file_id": "f_2", "path": "partial/weights/last.pt", "size": 1024,
         "partial": True},
    ])
    assert dialog.results_page.tree.rowCount() == 2
    assert dialog.results_page.tree.item(0, 1).text() == "17.8 MB"
    assert dialog.results_page.tree.item(1, 4).text() == "部分结果"
    assert dialog.results_page.partial_banner.text().startswith(
        "该任务保留了部分结果")


def test_results_retry_hint_uses_the_terminal_formula(dialogs):
    """An old server sends only finished_at: the hint must go away.

    The auto retry hint is driven by the one terminal formula of spec
    §5.5.4, never by the bare is_terminal field, so a job the old server
    reports as finished is not announced as "still auto retrying".
    """

    dialog = dialogs()
    files = [{"file_id": "f_1", "path": "weights/best.pt",
              "size": 1024}]
    old = {
        "job_id": "job_old", "status": "failed", "attempt": 2,
        "max_attempts": 3, "finished_at": "2026-01-01T11:00:00Z",
    }
    dialog.results_page.set_job(old, files)
    assert dialog.results_page.status_label.text() == ""

    live = {
        "job_id": "job_live", "status": "failed", "attempt": 2,
        "max_attempts": 3, "is_terminal": False, "finished_at": None,
    }
    dialog.results_page.set_job(live, files)
    assert "2/3" in dialog.results_page.status_label.text()


def test_health_lines_and_space_warning(dialogs):
    dialog = dialogs()
    texts = [text for _severity, text in
             dialog.health_lines(HealthStatus(health_payload()))]
    assert any("排队 3" in text for text in texts)
    assert any("已缓存 14" in text for text in texts)
    assert any("12840" in text for text in texts)
    dialog.health_payload = {"work_dir": {"free_gb": 0.001}}
    warning = dialog.space_warning(5000000000)
    assert warning is not None and warning[0] == "yellow"
    dialog.health_payload = {"work_dir": {"free_gb": 500}}
    assert dialog.space_warning(5000000000) is None


# ------------------------------------------------ close state machine


def test_esc_and_reject_take_the_close_path(dialogs, qapp, pump):
    parent = Parent()
    dialog = dialogs(parent)
    destroyed = []
    dialog.destroyed.connect(lambda *_: destroyed.append(True))
    qapp.sendEvent(dialog, QtGui.QKeyEvent(
        QtGui.QKeyEvent.Type.KeyPress, QtCore.Qt.Key.Key_Escape,
        QtCore.Qt.KeyboardModifier.NoModifier))
    pump(200)
    assert destroyed and parent._remote_training_dialog is None
    assert dialog._closing is True

    other = dialogs(parent)
    assert other is not dialog
    other.reject()
    pump(200)
    assert parent._remote_training_dialog is None


def test_close_waits_for_the_worker(dialogs, qapp, pump):
    parent = Parent()
    dialog = dialogs(parent)
    worker = SlowCancel()
    worker.start()
    dialog.set_worker_operation(worker, "convert")
    dialog.workers.append(worker)
    asked = []
    qapp._test_answers = asked
    dialog._confirm_close = lambda _d: (asked.append(1), True)[1]
    dialog.close()
    pump(300)
    assert len(asked) == 1
    assert parent._remote_training_dialog is dialog
    assert dialog._closing is True
    assert dialog.config_page.status_row.lines() == [
        "正在停止（等待 SlowCancel 结束）…"]
    assert not (
        dialog.windowFlags() & QtCore.Qt.WindowType.WindowCloseButtonHint)
    assert dialog._close_timer.isActive()
    assert dialog._close_timer.interval() == 200
    pump(1700)
    # the window waited for the thread, it never killed it
    assert parent._remote_training_dialog is None


def test_guard_vetoes_and_reissues(qapp, dialogs, pump):
    parent = Parent()
    parent.show()
    pump(30)
    dialog = dialogs(parent)
    worker = SlowCancel()
    worker.start()
    dialog.set_worker_operation(worker, "pack")
    dialog.workers.append(worker)
    guard = dialog.close_guard
    assert guard is qapp._remote_training_close_guard
    asked = []
    dialog._confirm_close = lambda _d: (asked.append(1), True)[1]

    event = QtGui.QCloseEvent()
    vetoed = guard.eventFilter(parent, event)
    assert vetoed is True and event.isAccepted() is False
    assert dialog._pending_quit is True
    assert guard.vetoed_window() is parent
    assert workers_finished(dialog) is False
    pump(1700)
    # the owner window itself was re-issued its close() and is gone
    assert parent.isVisible() is False
    assert parent._remote_training_dialog is None
    assert guard._timer.isActive() is False


def test_guard_protects_a_window_opened_after_the_first_one(
        qapp, dialogs, pump):
    """The D1 regression anchor (spec §5.1.2 / §5.1.4).

    The guard is a process wide singleton; a window opened after the
    first one was destroyed must still be the object it protects.
    """

    parent = Parent()
    first = dialogs(parent)
    guard = first.close_guard
    first.reject()
    pump(200)
    assert parent._remote_training_dialog is None

    second = dialogs(parent)
    assert second is not first
    assert second.close_guard is guard
    assert guard.dialog() is second

    worker = SlowCancel()
    worker.start()
    second.set_worker_operation(worker, "upload")
    second.workers.append(worker)
    asked = []
    second._confirm_close = lambda _d: (asked.append(1), True)[1]

    quit_event = QtCore.QEvent(QtCore.QEvent.Type.Quit)
    vetoed = guard.eventFilter(qapp, quit_event)
    assert vetoed is True and quit_event.isAccepted() is False
    assert asked == [1]                      # one request, one question
    assert second._pending_quit is True
    assert guard.vetoed_window() is parent
    pump(1700)
    assert parent._remote_training_dialog is None
    assert guard._timer.isActive() is False
    assert second.workers == []


def test_guard_leaves_a_destroyed_window_alone(qapp, dialogs, pump):
    """A destroyed dialog must release the exit request, not raise."""

    parent = Parent()
    dialog = dialogs(parent)
    dialog.reject()
    pump(200)
    guard = qapp._remote_training_close_guard
    quit_event = QtCore.QEvent(QtCore.QEvent.Type.Quit)
    assert guard.eventFilter(qapp, quit_event) is False
    assert quit_event.isAccepted() is True


def test_guard_does_not_filter_the_dialog_itself(qapp, dialogs, pump):
    """The dialog's own close must reach its own state machine."""

    dialog = dialogs()
    worker = SlowCancel()
    worker.start()
    dialog.set_worker_operation(worker, "convert")
    dialog.workers.append(worker)
    asked = []
    dialog._confirm_close = lambda _d: (asked.append(1), True)[1]
    dialog.close()
    pump(300)
    assert asked == [1]                       # no recursive confirmation
    assert dialog.config_page.status_row.lines() == [
        "正在停止（等待 SlowCancel 结束）…"]
    pump(1700)
    assert dialog.workers == []


def test_application_quit_is_vetoed_while_a_worker_runs(dialogs, qapp, pump):
    parent = Parent()
    parent.show()
    pump(30)
    dialog = dialogs(parent)
    worker = SlowCancel()
    worker.start()
    dialog.set_worker_operation(worker, "scan")
    dialog.workers.append(worker)
    guard = dialog.close_guard
    dialog._confirm_close = lambda _d: True
    quit_event = QtCore.QEvent(QtCore.QEvent.Type.Quit)
    assert guard.eventFilter(qapp, quit_event) is True
    assert quit_event.isAccepted() is False
    assert guard.vetoed_window() is parent
    pump(1700)
    assert parent._remote_training_dialog is None


def test_close_guard_standalone_decline_keeps_everything(dialogs):
    dialog = dialogs()
    worker = Hold()
    worker.start()
    dialog.set_worker_operation(worker, "upload")
    dialog.workers.append(worker)
    guard = ApplicationCloseGuard(dialog.parent())
    guard.rebind(dialog.parent())
    guard._confirm = lambda _dialog: False
    event = QtGui.QCloseEvent()
    assert guard.eventFilter(dialog.parent(), event) is True
    assert event.isAccepted() is False
    assert dialog._pending_quit is False
    assert dialog._closing is False
    assert guard._timer.isActive() is False
    worker.cancel()
    worker.wait(3000)

# ------------------------------------------- real delivery (R1) and R2


class StubRun:
    """Minimal stand in for Pipeline.assemble()'s PackerRun."""

    image_size = {}

    def split_check(self):
        return StubCheck()


class StubCheck:
    blocked = False

    def reason(self):
        return ""


class StubPipeline:
    """A pipeline whose assemble() blocks and polls should_stop()."""

    def __init__(self, config=None):
        self.config = config
        self.thread_ids = []
        self.saw_stop = False
        self.release = threading.Event()
        self.split_preview = None

    def validate_config(self):
        pass

    def effective_seed(self):
        return 1

    def assemble(self, should_stop=None):
        self.thread_ids.append(QtCore.QThread.currentThread())
        for _ in range(600):
            if should_stop is not None and should_stop():
                self.saw_stop = True
                raise InterruptedError("标签转换已取消")
            if self.release.is_set():
                break
            QtCore.QThread.msleep(10)
        return StubRun()


def test_close_from_the_real_main_window(qapp, pump):
    """R1: the owner is a CHILD widget and the close goes to the window.

    The launcher is handed the labeling widget (a child), and the
    application's own QCloseEvent is delivered to the top level window;
    the guard must veto that close and re-issue it on the same window
    once the worker is done.
    """

    main_window = QtWidgets.QMainWindow()
    child = QtWidgets.QWidget()
    main_window.setCentralWidget(child)
    main_window.show()
    pump(30)
    dialog = launcher.launch_remote_training(child)
    dialog.show()
    pump(30)
    guard = dialog.close_guard
    assert dialog.parent() is child
    assert guard._guarded_window() is main_window

    worker = SlowCancel()
    worker.start()
    dialog.set_worker_operation(worker, "upload")
    dialog.workers.append(worker)
    asked = []
    dialog._confirm_close = lambda _d: (asked.append(1), True)[1]

    event = QtGui.QCloseEvent()
    qapp.sendEvent(main_window, event)
    assert event.isAccepted() is False          # the close was vetoed
    assert asked == [1]
    assert dialog._pending_quit is True
    assert guard._pending_exit is True
    assert guard.vetoed_window() is main_window
    assert main_window.isVisible() is True
    pump(1700)
    assert main_window.isVisible() is False     # re-issued and closed
    assert child._remote_training_dialog is None
    assert guard._timer.isActive() is False


def test_close_from_the_real_main_window_via_close(qapp, pump):
    """R1, second shape: MainWindow.close() must be vetoed too."""

    main_window = QtWidgets.QMainWindow()
    child = QtWidgets.QWidget()
    main_window.setCentralWidget(child)
    main_window.show()
    pump(30)
    dialog = launcher.launch_remote_training(child)
    dialog.show()
    pump(30)
    guard = dialog.close_guard
    worker = SlowCancel()
    worker.start()
    dialog.set_worker_operation(worker, "pack")
    dialog.workers.append(worker)
    asked = []
    dialog._confirm_close = lambda _d: (asked.append(1), True)[1]

    main_window.close()
    pump(300)
    assert main_window.isVisible() is True      # the close was vetoed
    assert asked == [1]
    assert guard._pending_exit is True
    assert guard.vetoed_window() is main_window
    pump(1700)
    assert main_window.isVisible() is False
    assert child._remote_training_dialog is None


def test_precheck_runs_in_a_worker_not_on_the_gui_thread(
        qapp, dialogs, pump):
    """R2: no freeze - the pipeline runs in a QThread and stays cancellable."""

    parent = Parent()
    dialog = dialogs(parent)
    stub = StubPipeline()
    dialog._pipeline_factory = lambda **kwargs: stub
    ticks = []
    QtCore.QTimer.singleShot(0, lambda: ticks.append(1))

    worker = dialog.start_precheck()
    assert worker is not None
    assert worker.isRunning()
    assert worker in dialog.workers
    assert getattr(worker, "operation", None) == "scan"
    # the GUI event loop still runs while the pipeline is busy
    pump(200)
    assert ticks == [1]
    assert len(stub.thread_ids) == 1
    assert stub.thread_ids[0] is not QtCore.QThread.currentThread()
    assert dialog.pending_operations() == ["扫描"]

    # step 3 of the close machine cancels it through should_stop
    dialog._confirm_close = lambda _d: True
    dialog.close()
    pump(600)
    assert stub.saw_stop is True
    assert parent._remote_training_dialog is None
    assert dialog.workers == []


def test_precheck_renders_preview_and_estimate(dialogs, pump, tmp_path):
    """The success path: preview, split assertion and the vram line."""

    dialog = dialogs()
    page = dialog.config_page
    page.set_capabilities(dict(CAPABILITIES))
    dataset = make_dataset(tmp_path / "pre", ["cat", "dog"],
                           [("a_%d" % index, ["cat"]) for index in range(6)])
    page.dataset_edit.setText(dataset)
    page.classes_edit.setText(os.path.join(dataset, "classes.txt"))

    class Caps:
        payload = {}

        def vram_entry(self, model, task):
            return {"model": model, "task": task, "max_batch": 96,
                    "source": "auto"}

        def unschedulable_reason(self, model, task):
            return None

    dialog.capabilities = Caps()
    worker = dialog.start_precheck()
    worker.wait(20000)
    pump(150)
    assert page.preview_table.rowCount() == 2
    assert any("max_batch=96" in text
               for text in page.status_row.lines()), page.status_row.lines()

def test_vetoed_exit_survives_the_dialog_being_destroyed(qapp, pump):
    """N1: the pending exit request lives on the guard.

    If the dialog is gone before the guard's timer fires (its own timer
    completed the teardown first), the vetoed request must still be
    re-issued on the window that carried it.
    """

    main_window = QtWidgets.QMainWindow()
    child = QtWidgets.QWidget()
    main_window.setCentralWidget(child)
    main_window.show()
    pump(30)
    dialog = launcher.launch_remote_training(child)
    dialog.show()
    pump(30)
    guard = dialog.close_guard
    worker = SlowCancel()
    worker.start()
    dialog.set_worker_operation(worker, "polling")
    dialog.workers.append(worker)
    dialog._confirm_close = lambda _d: True

    event = QtGui.QCloseEvent()
    qapp.sendEvent(main_window, event)
    assert event.isAccepted() is False
    assert guard._pending_exit is True

    # the dialog disappears before the guard's own timer fires
    child._remote_training_dialog = None
    pump(1700)
    assert guard._pending_exit is False
    assert main_window.isVisible() is False
