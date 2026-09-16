"""History page of the model validation sub window.

The page lists the run folders the window scanned in the system
temporary directory and asks for one of them: a double click on a
restorable row, or the "恢复选中" button, raises restore_requested with
the staging root of that run. The scan itself belongs to the window, so
the tests hand the page fake run summaries (SimpleNamespace objects
with the frozen attribute names) and assert what the page does with
them - the seven columns, the one status derivation rule, the double
click, the busy switch, the truncated note row and the two read only
commands of the context menu. The module that scans is never imported
here, exactly as the page itself never imports it.
"""

import datetime
import os
import tempfile
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from PyQt6 import QtCore, QtGui, QtWidgets

from anylabeling.custom.model_validation.ui import dialog as dialog_module
from anylabeling.custom.model_validation.ui import history_page as page_module
from anylabeling.custom.model_validation.ui.history_page import (
    COLUMN_AUGMENTED,
    COLUMN_MARKED,
    COLUMN_ORIGINALS,
    COLUMN_SOURCE,
    COLUMN_STATE,
    COLUMN_TIME,
    COLUMN_VERDICT,
    EMPTY_TEXT,
    HEADERS,
    MINIMUM_HEIGHT,
    SCANNING_NOTE,
    TRUNCATED_NOTE,
    HistoryPage,
)

RUN_A = "/tmp/xal_validation_aaaa"
RUN_B = "/tmp/xal_validation_bbbb"
RUN_C = "/tmp/xal_validation_cccc"
STAMP = datetime.datetime(2024, 5, 17, 9, 30)
STAMP_TEXT = "2024-05-17 09:30"

# the seven status lines of the frozen rule: the current run, the plain
# restorable row, one line per known reason, the unknown reason and the
# restorable run whose state file was never written
STATE_CASES = (
    ("当前", {"staging_root": RUN_C, "reason": "missing"}),
    ("可恢复", {}),
    ("临时文件已失效", {"reason": "missing"}),
    ("缺少 meta.json", {"reason": "missing_meta"}),
    ("meta 损坏", {"reason": "bad_meta"}),
    ("没有已拷贝的标签", {"reason": "no_labels"}),
    ("不可恢复", {"reason": "of_a_later_revision"}),
    ("不可恢复", {"reason": "", "label_count": 0}),
    ("无判定数据", {"state_readable": False}),
)


@pytest.fixture(scope="module")
def qt_app():
    "Create the offscreen application shared by the page tests."

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


@pytest.fixture
def page(qt_app):
    "Create a history page and close it afterwards."

    widget = HistoryPage()
    try:
        yield widget
    finally:
        widget.close()


def run_summary(root: str, **overrides) -> SimpleNamespace:
    """Build the duck typed run summary the window hands the page.

    The defaults mirror a real run (1714 originals, 1655 augmented
    copies, 1601 OK and 113 NG) and the restorable flag follows the
    frozen rule of the summary: reason empty and at least one label.
    """

    data = {
        "staging_root": root,
        "mtime": STAMP.timestamp(),
        "staged_originals": 1714,
        "label_count": 1714,
        "augmented_count": 1655,
        "source_display": "/data/car",
        "meta_readable": True,
        "state_readable": True,
        "judged": 1714,
        "skipped": 0,
        "verdict_counts": {"OK": 1601, "NG": 113},
        "marked": 0,
        "reason": "",
        "truncated": False,
    }
    data.update(overrides)
    if "restorable" not in data:
        data["restorable"] = data["reason"] == "" and data["label_count"] > 0
    return SimpleNamespace(**data)


def cell(page, row: int, column: int) -> str:
    "Return the text of one cell of the list."

    item = page.tree.topLevelItem(row)
    assert item is not None
    return item.text(column)


def statuses(page) -> list:
    "Return the status column of every row, in the order shown."

    return [
        cell(page, row, COLUMN_STATE)
        for row in range(page.tree.topLevelItemCount())
    ]


def activate(page, row: int) -> None:
    "Double click the row of the list the way Qt does."

    item = page.tree.topLevelItem(row)
    assert item is not None
    page.tree.itemActivated.emit(item, COLUMN_TIME)


def select(page, row: int) -> None:
    "Select the row of the list the way a click does."

    item = page.tree.topLevelItem(row)
    assert item is not None
    page.tree.setCurrentItem(item)


class FakeDesktopServices:
    "Record the folder the page hands to the desktop."

    def __init__(self, answer: bool = True) -> None:
        self.answer = answer
        self.urls = []

    def openUrl(self, url):  # noqa: N802 - the Qt spelling
        "Remember the url and answer the configured result."

        self.urls.append(url)
        return self.answer


def desktop(monkeypatch, answer: bool = True) -> FakeDesktopServices:
    """Replace the desktop services the page reaches.

    The page reads QtGui through its own module, so swapping that one
    global catches every call - the open of a folder and the clipboard
    alike - without touching the real desktop of the test machine.
    """

    services = FakeDesktopServices(answer)
    monkeypatch.setattr(
        page_module,
        "QtGui",
        SimpleNamespace(
            QDesktopServices=services,
            QGuiApplication=QtGui.QGuiApplication,
        ),
    )
    return services


class MenuSpy:
    "Keep the menu the page builds instead of opening it."

    def __init__(self) -> None:
        self.menu = None

    def record(self, menu):
        "Remember one built menu."

        self.menu = menu


def context_menu(page, monkeypatch):
    "Build the context menu of the list without opening it."

    spy = MenuSpy()
    monkeypatch.setattr(
        QtWidgets.QMenu, "exec", lambda menu, *_args: spy.record(menu)
    )
    page._show_context_menu(QtCore.QPoint(0, 0))
    assert spy.menu is not None
    return spy.menu


def trigger(menu, text: str) -> None:
    "Trigger one entry of a context menu."

    for action in menu.actions():
        if action.text() == text:
            action.trigger()
            return
    raise AssertionError("context menu entry missing: " + text)


# ------------------------------------------------------------- the columns
def test_the_list_shows_the_seven_frozen_columns(page):
    "时间 / 数据来源 / 原图 / 增强 / 判定 / 标记 / 状态, in that order."

    assert HEADERS == (
        "时间",
        "数据来源",
        "原图",
        "增强",
        "判定",
        "标记",
        "状态",
    )
    assert (
        COLUMN_TIME,
        COLUMN_SOURCE,
        COLUMN_ORIGINALS,
        COLUMN_AUGMENTED,
        COLUMN_VERDICT,
        COLUMN_MARKED,
        COLUMN_STATE,
    ) == (0, 1, 2, 3, 4, 5, 6)
    assert page.tree.columnCount() == len(HEADERS) == 7
    header = page.tree.headerItem()
    titles = [header.text(column) for column in range(page.tree.columnCount())]
    assert titles == list(HEADERS)


def test_the_list_is_read_only_and_never_reorders_itself(page):
    "No edit trigger and no sorting: a row is a report."

    assert page.tree.editTriggers() == (
        QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
    )
    assert page.tree.isSortingEnabled() is False
    assert page.tree.rootIsDecorated() is False


def test_the_title_names_the_temp_folder_that_is_listed(page):
    "The header line says which folder the window walked."

    expected = "历史记录（临时目录：{0}）".format(tempfile.gettempdir())
    assert page.title_label.text() == expected


def test_a_row_spells_every_column_of_a_run(page):
    "One run, one row, the values of the summary in their columns."

    page.show_runs([run_summary(RUN_A)])
    assert page.tree.topLevelItemCount() == 1
    assert cell(page, 0, COLUMN_TIME) == STAMP_TEXT
    assert cell(page, 0, COLUMN_SOURCE) == "/data/car"
    assert cell(page, 0, COLUMN_ORIGINALS) == "1714"
    assert cell(page, 0, COLUMN_AUGMENTED) == "1655"
    # the counts are spelled in the order of records.VERDICT_ORDER
    # (NG before OK), the order the records layer reads them in
    assert cell(page, 0, COLUMN_VERDICT) == "NG 113 / OK 1601"
    assert cell(page, 0, COLUMN_MARKED) == EMPTY_TEXT
    assert cell(page, 0, COLUMN_STATE) == "可恢复"


def test_an_empty_source_is_shown_as_an_em_dash(page):
    "A summary without a display source keeps the cell readable."

    page.show_runs([run_summary(RUN_A, source_display="")])
    assert cell(page, 0, COLUMN_SOURCE) == EMPTY_TEXT


def test_a_run_without_a_readable_timestamp_keeps_its_row(page):
    "An unreadable mtime shows an em dash instead of raising."

    page.show_runs([run_summary(RUN_A, mtime=None)])
    assert page.tree.topLevelItemCount() == 1
    assert cell(page, 0, COLUMN_TIME) == EMPTY_TEXT


# --------------------------------------------------------- the status rule
def test_the_status_column_follows_the_one_rule(page):
    "当前 / reason line / 无判定数据 / 可恢复, in the frozen priority."

    runs = [
        run_summary(case.get("staging_root", RUN_B), **case)
        for _text, case in STATE_CASES
    ]
    page.show_runs(runs, current_root=RUN_C)
    assert page.tree.topLevelItemCount() == len(STATE_CASES)
    assert statuses(page) == [text for text, _case in STATE_CASES]


def test_the_current_run_wins_over_its_own_reason(page):
    "The row on screen right now is 当前 whatever else it is."

    page.show_runs(
        [run_summary(RUN_C, reason="missing")], current_root=RUN_C
    )
    assert statuses(page) == ["当前"]


def test_without_a_current_root_no_row_is_current(page):
    "An empty current_root never matches a run."

    page.show_runs([run_summary(RUN_A)])
    assert statuses(page) == ["可恢复"]


# ------------------------------------------- the verdict and mark columns
def test_the_verdict_column_reads_the_state_file(page):
    "The counted verdicts, or the no judgement line without a state."

    page.show_runs(
        [
            run_summary(
                RUN_A, verdict_counts={"OK": 7, "NG": 2, "SKIPPED": 1}
            ),
            run_summary(RUN_B, state_readable=False, verdict_counts={}),
            run_summary(RUN_C, verdict_counts={}),
        ]
    )
    assert cell(page, 0, COLUMN_VERDICT) == "NG 2 / OK 7 / SKIPPED 1"
    assert cell(page, 1, COLUMN_VERDICT) == "无判定数据"
    assert cell(page, 2, COLUMN_VERDICT) == "无判定数据"
    # a run without a state file is never restorable either way
    assert cell(page, 1, COLUMN_STATE) == "无判定数据"


def test_the_mark_column_shows_an_em_dash_at_zero(page):
    "0 marked records is the em dash, anything else its number."

    page.show_runs(
        [run_summary(RUN_A, marked=0), run_summary(RUN_B, marked=7)]
    )
    assert cell(page, 0, COLUMN_MARKED) == EMPTY_TEXT
    assert cell(page, 1, COLUMN_MARKED) == "7"


# ----------------------------------------------------------- the restore
def test_the_activation_of_a_restorable_row_asks_for_that_run(page):
    "A double click restores the very staging root of the row."

    page.show_runs([run_summary(RUN_A), run_summary(RUN_B)])
    seen = []
    page.restore_requested.connect(seen.append)

    activate(page, 1)

    assert seen == [RUN_B]


def test_the_activation_of_a_broken_row_is_ignored(page):
    "No signal, no button: a row that cannot come back asks nothing."

    page.show_runs([run_summary(RUN_A, reason="missing")])
    seen = []
    page.restore_requested.connect(seen.append)
    select(page, 0)

    assert page.selected_run() is not None
    assert page.restore_button.isEnabled() is False
    activate(page, 0)
    assert page.restore_selected() is False
    page.restore_button.click()
    assert seen == []


def test_the_restore_button_carries_the_selected_run(page):
    "The toolbar button follows the selection, the broken one included."

    page.show_runs([run_summary(RUN_A, reason="missing"), run_summary(RUN_B)])
    seen = []
    page.restore_requested.connect(seen.append)

    assert page.restore_button.isEnabled() is False
    select(page, 0)
    assert page.restore_button.isEnabled() is False
    select(page, 1)
    assert page.restore_button.isEnabled() is True

    page.restore_button.click()

    assert seen == [RUN_B]


def test_a_busy_page_disables_and_refuses_the_restore(page):
    "While the folder is scanned no restore may start."

    page.show_runs([run_summary(RUN_A)])
    select(page, 0)
    seen = []
    page.restore_requested.connect(seen.append)

    page.set_busy(True)

    assert page.restore_button.isEnabled() is False
    activate(page, 0)
    assert page.restore_selected() is False
    page.restore_button.click()
    assert seen == []

    page.set_busy(False)

    assert page.restore_button.isEnabled() is True
    assert page.restore_selected() is True
    assert seen == [RUN_A]


def test_a_new_list_drops_the_selection_and_disables_the_button(page):
    "A scan that replaces the list leaves nothing to restore."

    page.show_runs([run_summary(RUN_A)])
    select(page, 0)
    assert page.restore_button.isEnabled() is True

    page.show_runs([run_summary(RUN_B)], scanning=True)

    assert page.selected_run() is None
    assert page.restore_button.isEnabled() is False


# ----------------------------------------------------------- the listing
def test_the_scan_note_and_the_status_line(page):
    "scanning says so, a finished list leaves the line empty."

    page.show_runs([], scanning=True)
    assert page.status_label.text() == SCANNING_NOTE

    page.set_note("已找到 2 条记录")
    assert page.status_label.text() == "已找到 2 条记录"

    page.show_runs([run_summary(RUN_A)])
    assert page.status_label.text() == ""


def test_a_truncated_listing_gets_one_note_row_at_the_end(page):
    "The note row is not a run: it can neither restore nor be copied."

    page.show_runs(
        [run_summary(RUN_A), run_summary(RUN_B, truncated=True)]
    )
    assert page.tree.topLevelItemCount() == 3
    assert cell(page, 2, COLUMN_STATE) == TRUNCATED_NOTE.format(count=2)
    assert cell(page, 2, COLUMN_VERDICT) == EMPTY_TEXT

    seen = []
    page.restore_requested.connect(seen.append)
    select(page, 2)
    assert page.selected_run() is None
    assert page.restore_button.isEnabled() is False
    activate(page, 2)
    assert seen == []


def test_a_finished_list_has_no_note_row(page):
    "Without the truncated flag the list holds the runs alone."

    page.show_runs([run_summary(RUN_A), run_summary(RUN_B)])
    assert page.tree.topLevelItemCount() == 2


# ------------------------------------------------------- the context menu
def test_the_context_menu_offers_only_the_two_read_only_commands(
    page, monkeypatch
):
    "Open and copy, and nothing that could change or hide a run."

    desktop(monkeypatch)
    page.show_runs([run_summary(RUN_A)])
    select(page, 0)

    menu = context_menu(page, monkeypatch)
    texts = tuple(action.text() for action in menu.actions())

    assert texts == ("打开所在目录", "复制路径")
    for text in texts:
        assert "删除" not in text
        assert "隐藏" not in text


def test_the_open_command_hands_the_folder_to_the_desktop(page, monkeypatch):
    "The selected staging root is what the desktop is asked to open."

    services = desktop(monkeypatch)
    page.show_runs([run_summary(RUN_A)])
    select(page, 0)

    menu = context_menu(page, monkeypatch)
    trigger(menu, "打开所在目录")

    assert len(services.urls) == 1
    url = services.urls[0]
    assert url.isLocalFile() is True
    assert url.toLocalFile() == RUN_A


def test_a_refused_open_is_written_on_the_status_line(page, monkeypatch):
    "A desktop that says no is reported, not swallowed."

    desktop(monkeypatch, answer=False)
    page.show_runs([run_summary(RUN_A)])
    select(page, 0)

    assert page.open_selected() is False
    assert page.status_label.text() == "无法打开目录：" + RUN_A


def test_the_copy_command_puts_the_path_in_the_clipboard(page, monkeypatch):
    "The copy entry fills the clipboard with the staging root."

    desktop(monkeypatch)
    page.show_runs([run_summary(RUN_A)])
    select(page, 0)
    clipboard = QtGui.QGuiApplication.clipboard()
    clipboard.setText("")

    menu = context_menu(page, monkeypatch)
    trigger(menu, "复制路径")

    assert clipboard.text() == RUN_A


def test_without_a_selection_both_commands_say_so(page, monkeypatch):
    "An empty selection writes the one line instead of acting."

    services = desktop(monkeypatch)
    page.show_runs([])

    assert page.open_selected() is False
    assert page.copy_selected() is False
    assert page.status_label.text() == "未选择任何运行目录"
    assert services.urls == []


# ------------------------------------------------------- the room it asks
def test_the_page_stays_under_the_floor_of_the_window(page):
    """The page may never raise the minimum size of the window.

    dialog.stack_minimum_size takes the maximum minimum size hint of
    the pages of the stack and pads it up to dialog.MINIMUM_HEIGHT
    (600). This page asks for 320px (MINIMUM_HEIGHT), so its own
    minimum size hint stays far below that floor and can never push the
    window taller than the results page already asks for.
    """

    assert MINIMUM_HEIGHT == 320
    assert MINIMUM_HEIGHT <= dialog_module.MINIMUM_HEIGHT
    assert page.minimumHeight() == MINIMUM_HEIGHT
    hint = page.minimumSizeHint()
    assert hint.height() > 0
    assert hint.height() <= dialog_module.MINIMUM_HEIGHT

    stack = QtWidgets.QStackedWidget()
    stack.addWidget(HistoryPage())
    try:
        size = dialog_module.stack_minimum_size(stack)
        assert size.height() == dialog_module.MINIMUM_HEIGHT
        assert size.width() == dialog_module.MINIMUM_WIDTH
    finally:
        stack.close()


# ----------------------------------------------------------- the selects
def test_selected_run_is_none_without_a_selection(page):
    "No row, no selection, no run - and the note row is not a run."

    assert page.selected_run() is None

    page.show_runs([run_summary(RUN_A)])
    assert page.selected_run() is None

    select(page, 0)
    run = page.selected_run()
    assert run is not None
    assert run.staging_root == RUN_A

    page.tree.clear()
    assert page.selected_run() is None


def test_the_refresh_and_the_close_buttons_ask_the_window(page):
    "The two navigation buttons only raise their own signal."

    seen = []
    page.refresh_requested.connect(lambda: seen.append("refresh"))
    page.close_requested.connect(lambda: seen.append("close"))

    page.refresh_button.click()
    page.close_button.click()

    assert seen == ["refresh", "close"]
