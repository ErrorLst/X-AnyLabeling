"""History wiring of the model validation window.

The window lists the run folders the system temporary directory still
holds and restores one of them: the history page carries the list, the
window carries the scan and the restore. What is pinned here is that
whole path - the button of the form, the background scan of the temp
folder, the restore onto the results page (records, class table, filter
and bridge), the two exits of the history mode and the debounced
state.json the current run is saved back into.

Every run folder is a fake one under the scratch directory of the test
(meta.json, original/labels, augmented/labels), and the only directory
scanned from here is that scratch directory: the real temp folder and
the real run folders of the machine are never touched.
"""

import json
import os
import os.path as osp
import time
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from PyQt6 import QtCore, QtTest, QtWidgets

from anylabeling.custom.model_validation import dataset
from anylabeling.custom.model_validation import history
from anylabeling.custom.model_validation import records as records_module
from anylabeling.custom.model_validation.ui import dialog as dialog_module
from anylabeling.custom.model_validation.ui.dialog import (
    HISTORY_BUSY_STATUS,
    HISTORY_MISSING_STATUS,
    HISTORY_OPEN_STATUS,
    ModelValidationDialog,
)

CLASSES = ["car"]
HISTORY_TITLE = "历史记录…"
HISTORY_TOOLTIP = "列出系统临时目录里的历次验证，可恢复到某次的判定与标记。"

# Windows whose Python wrapper is collected while a deferred delete of
# a child is still queued crash the Qt event loop of the next test: the
# closed windows are kept referenced until the session ends.
_ALIVE: list = []


def keep_alive(*widgets) -> None:
    "Hold references to closed widgets for the rest of the session."

    _ALIVE.extend(widgets)


class _FakeScanWorker(QtCore.QObject):
    """Stub of the history scan thread for the lifetime tests.

    No walk of the temporary directory is started by it. The two probes
    the window uses to let a scan thread go are driven by values: wait()
    answers what the test set, isRunning() answers what the test set. A
    stub is finished by emitting finished by hand, which is what a real
    thread does when its run() returns.
    """

    ready = QtCore.pyqtSignal(list)
    failed = QtCore.pyqtSignal(str)
    finished = QtCore.pyqtSignal()

    def __init__(self, wait_result=False, running=True):
        super().__init__()
        self.wait_result = bool(wait_result)
        self.running = bool(running)
        self.waited = 0
        self.last_timeout = None

    def start(self) -> None:
        pass

    def wait(self, timeout_ms: int = 0) -> bool:
        self.waited += 1
        self.last_timeout = int(timeout_ms)
        return self.wait_result

    def isRunning(self) -> bool:  # noqa: N802
        return self.running

    def quit(self) -> None:
        pass

    def release(self) -> None:
        "Report the thread as finished and leave its run()."

        self.running = False
        self.finished.emit()


@pytest.fixture
def history_scans(qt_app):
    "Run a test against a clean registry of detached scan threads."

    dialogs = []
    kept = list(dialog_module._ORPHAN_SCANS)
    dialog_module._ORPHAN_SCANS.clear()
    try:
        yield dialogs
    finally:
        for dialog in dialogs:
            dialog.close()
            keep_alive(dialog)
        dialog_module._ORPHAN_SCANS.clear()
        dialog_module._ORPHAN_SCANS.update(kept)


def follow_spy(dialog, monkeypatch) -> list:
    """Collect every follow of the window instead of opening a record.

    The spy records the id the follow was run for and applies the one
    state change the real follow applies once it went through: the
    window keeps no second path of its own to observe, so this is what
    lets a test read "the follow really happened".
    """

    followed = []

    def spy() -> None:
        followed.append(dialog._pending_record_id)
        dialog._followed_record_id = dialog._pending_record_id

    monkeypatch.setattr(dialog, "_follow_current_record", spy)
    return followed


class StubWorker(QtCore.QObject):
    "Stand-in for the validation worker, so that no run ever starts."

    progress = QtCore.pyqtSignal(str, int, int, str)
    stage_finished = QtCore.pyqtSignal(str, int)
    warnings_ready = QtCore.pyqtSignal(list)
    dataset_ready = QtCore.pyqtSignal(str, list, dict)
    failed = QtCore.pyqtSignal(str, str)
    cancelled = QtCore.pyqtSignal()
    finished_ok = QtCore.pyqtSignal()

    def __init__(self, config=None, classes=(), staging="", parent=None):
        super().__init__(parent)
        self.staging = staging
        self.records: list = []
        self.started = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        pass

    def wait(self, timeout_ms: int = 0) -> bool:
        return True

    def isRunning(self) -> bool:  # noqa: N802
        return False


@pytest.fixture(scope="module")
def qt_app():
    "Create the offscreen application shared by the history tests."

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


@pytest.fixture
def window(qt_app):
    "Create a model validation window and close it afterwards."

    dialog = ModelValidationDialog()
    try:
        yield dialog
    finally:
        dialog.close()
        keep_alive(dialog)


# ------------------------------------------------------------- the fakes
def make_staging(parent, name):
    "Create a staging folder with the layout the tool expects."

    root = osp.join(str(parent), dataset.STAGING_PREFIX + name)
    for folder in (dataset.ORIGINAL_DIRNAME, dataset.AUGMENTED_DIRNAME):
        for sub in (dataset.IMAGES_DIRNAME, dataset.LABELS_DIRNAME):
            os.makedirs(osp.join(root, folder, sub), exist_ok=True)
    return root


def write_bytes(path, payload=b"\x89PNG\r\n\x1a\n"):
    "Write one small file, creating its folder."

    os.makedirs(osp.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(payload)


def write_label(path):
    "Write one staged xlabel document."

    os.makedirs(osp.dirname(path), exist_ok=True)
    dataset.write_json(path, {"shapes": []})


def stage_original(root, relpath):
    "Create one staged original, with its picture and its label."

    paths = dataset.staging_paths(
        root, records_module.KIND_ORIGINAL, relpath
    )
    write_bytes(paths["image"])
    write_label(paths["label"])
    return records_module.make_record(
        records_module.KIND_ORIGINAL,
        relpath,
        paths["image"],
        paths["label"],
    )


def stage_augmented(root, relpath):
    "Create one staged augmented copy, with its picture and its label."

    paths = dataset.staging_paths(
        root, records_module.KIND_AUGMENTED, relpath
    )
    write_bytes(paths["image"])
    write_label(paths["label"])
    return records_module.make_record(
        records_module.KIND_AUGMENTED,
        relpath,
        paths["image"],
        paths["label"],
        parent_record_id="original::" + history.augment_suffix_stem(relpath),
    )


def write_meta(root, originals, source_display="/data/src"):
    "Write the meta.json a restored run reads its originals from."

    dataset.write_json(
        osp.join(root, dataset.META_FILENAME),
        {
            "staging_root": root,
            "source_display": source_display,
            "counts": {"original": len(originals)},
            "originals": originals,
            "skipped": [],
        },
    )


def make_run(parent, name="hist", judged=True, with_state=True):
    """Create one finished looking run with its records and judgements.

    The run carries two originals and one augmented copy. With judged
    the originals are OK and the copy is NG, and the state file of the
    run is written; without it the folder is left as a legacy run comes
    out of an older revision - no state.json at all.
    """

    root = make_staging(parent, name)
    originals = [stage_original(root, "a.png"), stage_original(root, "b.png")]
    augmented = stage_augmented(root, "a_aug1.png")
    kept = []
    for record in originals:
        record.verdict = (
            records_module.OK if judged else records_module.PENDING
        )
        record.judged = bool(judged)
        record.source_display = "/data/src"
        kept.append(record)
    augmented.verdict = (
        records_module.NG if judged else records_module.PENDING
    )
    augmented.judged = bool(judged)
    augmented.source_display = "/data/src"
    kept.append(augmented)
    write_meta(root, [original.to_dict() for original in originals])
    if with_state:
        history.save_restore_state(
            root,
            kept,
            classes=CLASSES,
            source_display="/data/src",
        )
    return root, kept


def state_file(root):
    "Return the path of the state.json of one run folder."

    return osp.join(str(root), history.STATE_FILENAME)


def state_of(root):
    "Read the state.json of one run folder."

    with open(state_file(root), "r", encoding="utf-8") as handle:
        return json.load(handle)


def move_state_away(root):
    "Rename the state file away: the run looks like a legacy folder."

    os.rename(state_file(root), state_file(root) + ".moved")


def wait_for(predicate, timeout_ms=5000):
    "Process events until the predicate holds or the timeout expires."

    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        if predicate():
            return True
        QtTest.QTest.qWait(10)
    return predicate()


def scan(scratch, *roots):
    "Return the real summaries of the given runs, newest first."

    wanted = {str(root) for root in roots}
    found = [
        run
        for run in history.list_runs(temp_root=str(scratch))
        if run.staging_root in wanted
    ]
    assert len(found) == len(wanted)
    return sorted(found, key=lambda run: (-run.mtime, run.staging_root))


def open_history(dialog, runs):
    "Fill the history page with the given summaries, without a thread."

    def fake_list_runs(temp_root=None, scan_limit=history.DEFAULT_SCAN_LIMIT):
        return list(runs)

    original = history.list_runs
    history.list_runs = fake_list_runs
    try:
        dialog.show_history()
        assert wait_for(lambda: dialog._history_scan is None)
    finally:
        history.list_runs = original


def flush_table(dialog):
    "Let the deferred rebuild of the results table run."

    QtWidgets.QApplication.processEvents()


def row_roots(page):
    "Return the run root of every row of the history list."

    role = QtCore.Qt.ItemDataRole.UserRole + 1
    return [
        page.tree.topLevelItem(row).data(0, role).staging_root
        for row in range(page.tree.topLevelItemCount())
    ]


# ------------------------------------------------------------ the entries
def test_the_form_button_opens_the_history_page(window, mv_scratch):
    "The button of the form asks for the page and the window switches."

    root, _kept = make_run(mv_scratch, "entry")
    runs = scan(mv_scratch, root)
    asked = []
    window.config_page.history_requested.connect(
        lambda: asked.append("history")
    )
    started = []
    window._start_history_scan = lambda: started.append("scan")

    window.config_page.history_button.click()

    assert asked == ["history"]
    assert started == ["scan"]
    assert window._history_mode is True
    assert window.stack.currentWidget() is window.history_page
    assert window.history_page.status_label.text() == "正在扫描临时目录…"
    window._history_runs = list(runs)
    window._close_history()


def test_the_button_sits_left_of_the_start(window):
    "The form keeps its one button row and its order."

    page = window.config_page
    row = page.layout().itemAt(page.layout().count() - 1).layout()
    widgets = [row.itemAt(index).widget() for index in range(row.count())]
    assert page.history_button in widgets
    assert page.start_button in widgets
    assert page.history_button.text() == HISTORY_TITLE
    assert page.history_button.toolTip() == HISTORY_TOOLTIP
    assert widgets.index(page.history_button) < widgets.index(
        page.start_button
    )


def test_the_history_page_is_the_fourth_page(window):
    "The three existing pages keep their indexes."

    assert window.stack.count() == 4
    assert window.stack.indexOf(window.history_page) == 3
    assert window.stack.indexOf(window.progress_page) == 1
    assert window.stack.indexOf(window.results_page) == 2


# -------------------------------------------------------------- the scan
def test_the_window_refuses_the_history_while_a_worker_runs(window):
    "A live worker owns the staging folder the history would replace."

    window.worker = StubWorker()
    try:
        window.show_history()
        assert window.stack.currentWidget() is window.config_page
        assert window.config_page.status_label.text() == HISTORY_BUSY_STATUS
        assert window._history_mode is False
        assert window.history_page.status_label.text() == ""
    finally:
        window.worker = None


def test_the_scan_fills_the_page_and_marks_the_current_run(
    window, mv_scratch
):
    "The list shows every scanned run, the one on screen as 当前."

    current, _kept = make_run(mv_scratch, "scan_current")
    other, _kept_other = make_run(mv_scratch, "scan_other")
    window.staging_root = current

    open_history(window, scan(mv_scratch, current, other))

    page = window.history_page
    assert window.stack.currentWidget() is page
    assert page.tree.topLevelItemCount() == 2
    roots = row_roots(page)
    assert set(roots) == {current, other}
    states = [
        page.tree.topLevelItem(row).text(6) for row in range(2)
    ]
    assert states.count("当前") == 1
    assert states[roots.index(current)] == "当前"
    assert "找到 2 个历史运行目录" in window.config_page.status_label.text()


def test_a_scan_that_fails_is_reported(window):
    "A walk that cannot list the folder says so and leaves the list empty."

    def boom(temp_root=None, scan_limit=history.DEFAULT_SCAN_LIMIT):
        raise OSError("unreadable")

    original = history.list_runs
    history.list_runs = boom
    try:
        window.show_history()
        assert wait_for(lambda: window._history_scan is None)
    finally:
        history.list_runs = original

    page = window.history_page
    assert page.tree.topLevelItemCount() == 0
    assert "扫描历史失败" in page.status_label.text()
    assert "扫描历史失败" in window.config_page.status_label.text()
    assert window._history_runs == []


# ----------------------------------------------------------- the restore
def test_restore_loads_the_run_on_the_results_page(window, mv_scratch):
    "A restored run replaces the records, the classes and the filter."

    root, kept = make_run(mv_scratch, "restore")
    window.staging_root = "/somewhere/else"
    window.classes = []
    open_history(window, scan(mv_scratch, root))

    assert window.restore_run(root) is True

    page = window.results_page
    assert window.stack.currentWidget() is page
    assert window.staging_root == root
    assert window.classes == CLASSES
    assert {record.record_id for record in window.records} == {
        record.record_id for record in kept
    }
    assert page.filter_combo.currentIndex() == 0
    assert page.filter_combo.currentData() == ""
    flush_table(window)
    assert page.table.rowCount() == 3
    assert window.bridge.sync.attached is True
    assert page.staging_edit.text() == root
    assert window._history_mode is False
    assert "已恢复历史运行" in window.config_page.status_label.text()
    assert "已恢复历史运行" in window.history_page.status_label.text()
    # the summary line of the results page stays the export formula of
    # the restored selection: the restore note never occupies it
    summary = window.results_page.status_label.text()
    assert "已恢复历史运行" not in summary
    assert "导出 = 未删原图 2 + 勾选增强 0 = 共 2 组" in summary


def test_restore_of_an_unknown_root_is_refused(window, mv_scratch):
    "A root the last scan does not hold is refused with a status line."

    root, _kept = make_run(mv_scratch, "known")
    open_history(window, scan(mv_scratch, root))

    assert window.restore_run("/nowhere/xal_validation_gone") is False

    assert window.stack.currentWidget() is window.history_page
    assert window.config_page.status_label.text() == HISTORY_MISSING_STATUS
    assert window._history_mode is True


def test_a_running_worker_refuses_the_restore(window, mv_scratch):
    "The guard covers the restore as well as the listing."

    root, _kept = make_run(mv_scratch, "busy")
    open_history(window, scan(mv_scratch, root))
    window.worker = StubWorker()
    try:
        assert window.restore_run(root) is False
        assert window.stack.currentWidget() is window.history_page
        assert window.config_page.status_label.text() == HISTORY_BUSY_STATUS
    finally:
        window.worker = None


def test_start_is_blocked_on_the_history_page(window, mv_scratch):
    "A start from the history page never launches a worker."

    root, _kept = make_run(mv_scratch, "blocked")
    open_history(window, scan(mv_scratch, root))

    window.start_validation()

    assert window.worker is None
    assert window.config_page.status_label.text() == HISTORY_OPEN_STATUS


def test_closing_the_history_page_allows_a_start_again(
    window, mv_scratch, monkeypatch
):
    "The close entry point clears the flag a restore also clears."

    root, _kept = make_run(mv_scratch, "closed")
    window._history_runs = list(scan(mv_scratch, root))
    window._history_mode = True
    window.stack.setCurrentWidget(window.history_page)

    window.history_page.close_button.click()

    assert window._history_mode is False
    assert window.stack.currentWidget() is window.config_page
    # a start with an empty form is refused by the validation of the
    # form, which is the proof that the history guard no longer answers
    # first (it builds no worker either, so the status line is the one
    # that tells the two refusals apart)
    window.start_validation()

    assert window.worker is None
    assert "请选择有效的数据来源目录" in (
        window.config_page.status_label.text()
    )
    # and a form that is really filled starts a run again
    render = make_run(mv_scratch, "closed_source")
    source = osp.join(str(mv_scratch), "closed_data")
    os.makedirs(source, exist_ok=True)
    model = osp.join(str(mv_scratch), "closed_model.onnx")
    write_bytes(model, b"onnx")
    classes = osp.join(str(mv_scratch), "closed_classes.txt")
    with open(classes, "w", encoding="utf-8") as handle:
        handle.write("car\n")
    page = window.config_page
    monkeypatch.setattr(dialog_module, "ValidationWorker", StubWorker)
    monkeypatch.setattr(
        dataset, "create_staging_root", lambda parent=None: render[0]
    )
    page.set_dataset(source)
    page.set_model(model)
    page.set_classes(classes, list(CLASSES))

    window.start_validation()

    assert isinstance(window.worker, StubWorker)
    assert window.worker.started is True
    assert window.config_page.status_label.text() != HISTORY_OPEN_STATUS


# ------------------------------------------------- scan thread lifetime
def test_a_start_on_the_history_page_says_so_and_starts_nothing(
    window, mv_scratch
):
    "The refusal of a start is the status line, not a silent no-op."

    root, _kept = make_run(mv_scratch, "open_guard")
    open_history(window, scan(mv_scratch, root))

    window.start_validation()

    assert window.worker is None
    assert window.stack.currentWidget() is window.history_page
    assert window._history_mode is True
    assert window.config_page.status_label.text() == HISTORY_OPEN_STATUS


def test_a_scan_that_outlasts_the_wait_is_never_owned_by_the_window(
    history_scans, monkeypatch
):
    """A walk still running at the close must not be destroyed with it.

    The stub answers the one bounded wait with False and still reports
    itself as running: the window has to let it go instead of keeping
    it as a running child, because Qt aborts the process when the
    QThread of a destroyed window is still running.
    """

    stub = _FakeScanWorker(wait_result=False, running=True)
    monkeypatch.setattr(dialog_module, "_HistoryScanWorker", lambda **k: stub)
    window = ModelValidationDialog()
    history_scans.append(window)

    window.show_history()
    assert window._history_scan is stub

    window.close()

    # the return value of the wait was consumed and the thread that
    # outlasted it is no longer a running child of this window: it is
    # owned by the module level set until it reports its own end
    assert stub.waited >= 1
    assert stub.parent() is None
    assert stub in dialog_module._ORPHAN_SCANS
    assert window._running_history_scans() == []

    # the thread reports its own end afterwards: the registry lets it go
    stub.release()

    assert stub not in dialog_module._ORPHAN_SCANS
    assert window._running_history_scans() == []


def test_a_scan_that_times_out_and_finishes_early_stays_tracked(
    history_scans, monkeypatch
):
    "A wait that timed out is not a licence to drop the thread."

    stub = _FakeScanWorker(wait_result=False, running=True)
    monkeypatch.setattr(dialog_module, "_HistoryScanWorker", lambda **k: stub)
    window = ModelValidationDialog()
    history_scans.append(window)

    window.show_history()
    window._retire_history_scan(timeout_ms=5)

    # the return value of wait() was consumed and the thread is still
    # watched: it was handed to the set that owns it until it ends
    assert stub.waited >= 1
    assert stub.last_timeout == 5
    assert window._history_scan is None
    assert window._running_history_scans() == []
    assert stub in dialog_module._ORPHAN_SCANS

    stub.release()

    assert stub not in dialog_module._ORPHAN_SCANS


# ------------------------------------------------------------- the follow
def test_restore_rearms_the_follow_of_the_same_record_id(
    history_scans, mv_scratch, monkeypatch
):
    "A restore must not be answered with the follow of the old run."

    root, kept = make_run(mv_scratch, "follow")
    first = next(
        record for record in kept if record.record_id == "original::a.png"
    )
    window = ModelValidationDialog()
    history_scans.append(window)
    open_history(window, scan(mv_scratch, root))
    window.show()
    QtWidgets.QApplication.processEvents()
    # the state the previous run of the very same dataset leaves behind:
    # the record of the run that is about to be restored was followed
    # once already, so both the pending id and the followed id carry it
    window._followed_record_id = first.record_id
    window._pending_record_id = first.record_id
    followed = follow_spy(window, monkeypatch)

    assert window.restore_run(root) is True

    assert window._followed_record_id == ""
    # the restored run announces its own first record again, and that is
    # the record id the previous run was left on: the debounce is armed
    # with it (the dead id of the old run would have swallowed it)
    assert window._pending_record_id == first.record_id
    assert window.follow_timer.isActive() is True
    # the record the debounce was armed with is the very record the
    # previous run was left on, and the follow of it really runs: a
    # state that kept the old id would have returned right here
    assert window._follow_current_record() is None
    assert followed == [first.record_id]
    assert window.results_page.current_record().record_id == first.record_id


# ------------------------------------------------------- the state on disk
def test_a_mark_updates_the_state_of_the_run(window, mv_scratch):
    "A restored run writes its verdicts and its marks back."

    root, _kept = make_run(mv_scratch, "state")
    open_history(window, scan(mv_scratch, root))
    assert window.restore_run(root) is True
    before = state_of(root)
    assert before["record_count"] == 3
    assert before["summary"]["verdicts"] == {"OK": 2, "NG": 1}
    assert before["summary"]["marked"] == 0

    window.on_toggle_deleted(["original::a.png"], True)
    window._save_state_now()

    after = state_of(root)
    assert after["summary"]["marked"] == 1
    marked = [
        item
        for item in after["records"]
        if item["record_id"] == "original::a.png"
    ]
    assert marked and marked[0]["deleted"] is True
    assert after["classes"] == CLASSES
    assert after["source_display"] == "/data/src"


def test_the_state_write_is_debounced(window, mv_scratch):
    "A burst of marks costs one write once the user stopped."

    root, _kept = make_run(mv_scratch, "debounce")
    open_history(window, scan(mv_scratch, root))
    assert window.restore_run(root) is True
    move_state_away(root)
    window._state_dirty = False

    window.on_toggle_export(["augmented::a_aug1.png"], True)
    window.on_toggle_deleted(["original::a.png"], True)

    assert window._state_dirty is True
    assert osp.isfile(state_file(root)) is False
    assert wait_for(lambda: osp.isfile(state_file(root)))
    assert state_of(root)["summary"]["marked"] == 2


def test_a_pending_write_is_flushed_on_close(window, mv_scratch):
    "The window that closes writes the state it still owes."

    root, _kept = make_run(mv_scratch, "closing")
    open_history(window, scan(mv_scratch, root))
    assert window.restore_run(root) is True
    move_state_away(root)
    window.on_toggle_export(["augmented::a_aug1.png"], True)
    assert osp.isfile(state_file(root)) is False

    window.close()
    keep_alive(window)

    assert osp.isfile(state_file(root)) is True
    assert state_of(root)["summary"]["marked"] == 1


# --------------------------------------------------------------- the export
def test_the_export_follows_the_restored_selection(window, mv_scratch,
                                                   tmp_path):
    "The archive holds the pairs the restored run selected."

    root, _kept = make_run(mv_scratch, "export")
    open_history(window, scan(mv_scratch, root))
    assert window.restore_run(root) is True
    flush_table(window)
    window.on_toggle_export(["augmented::a_aug1.png"], True)
    target = osp.join(str(tmp_path), "restored.zip")

    summary = window.export_zip(target)

    assert window.default_export_name().endswith(".zip")
    assert osp.isfile(target) is True
    assert summary["originals"] == 2
    assert summary["augmented"] == 1
    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
    # the class table plus the two originals and the one selected copy,
    # each as its picture and its json
    assert names == [
        "classes.txt",
        "images/a.png",
        "images/a.json",
        "images/b.png",
        "images/b.json",
        "images/a_aug1.png",
        "images/a_aug1.json",
    ]
    assert summary["zip_entries"] == 6


# ------------------------------------------------------------ the legacy run
def test_a_run_without_state_reports_no_judgement_data(window, mv_scratch):
    "A legacy folder restores its pictures and says why it has no verdict."

    root, kept = make_run(
        mv_scratch, "legacy", judged=False, with_state=False
    )
    assert osp.isfile(state_file(root)) is False
    open_history(window, scan(mv_scratch, root))

    assert window.restore_run(root) is True

    text = window.config_page.status_label.text()
    assert history.NO_STATE_NOTE in text
    assert "记录 3" in text
    assert {record.record_id for record in window.records} == {
        record.record_id for record in kept
    }
    assert all(
        record.verdict == records_module.PENDING
        for record in window.records
    )
