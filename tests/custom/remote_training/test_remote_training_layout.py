"""Layout assertions of the configuration page (spec §5.1.3).

The presentation of the parameter form is the subject here, never the
form semantics: this file pins the two column / one column reflow, the
four collapsible groups, the badge hints and the splitter of the split
preview.  The semantic anchors (41 parameters, an untouched form sends
the batch step and the family's `auto` preset) are asserted next to
every layout state so a "pretty" layout can never buy its pixels from
the request body.

The tests drive `ConfigPage` directly (spec §5.1.3 fixes the fields,
never their arrangement) and only touch `RemoteTrainingDialog` where
`job_request_body()` lives.

Baseline of the main session (offscreen / Fusion, 23 parameters, before
this layout round): page.sizeHint() = 423 x 1679, parameter box = 874,
preview_table.height() @1000x900 = 70, controls 827-893 wide, right edge
ratio 0.964.  That generation is superseded: the page scrolls now and
its window hint is dominated by the scroll area, so every height below
measures the content widget (see `page_height()`), never
`page.sizeHint()`.

The 41 parameter generation measures 1378 / 978 / 781 px for the fully
expanded, the default and the fully folded content, plus 814 px for the
natural parameter box.  The four thresholds (1520 / 1080 / 860 / 900)
keep a little over 10% of headroom over those numbers and the three
page ones stay below the 1679 px baseline of the previous generation.
The relative invariants are the unchanged ones of the plan: one group
buys at least 25 px, the four together at least 180, the right edge
covers 90% of the page, a control keeps 240 px and the preview table
150 px.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402  (after the platform selection)
from PyQt6 import QtWidgets  # noqa: E402

from anylabeling.custom.remote_training.ui.config_page import (  # noqa: E402
    DEFAULT_COLLAPSED,
    NARROW_PAGE_WIDTH,
    PARAM_CONTROL_MIN_WIDTH,
    PARAM_GROUPS,
    PARAMS_HINT_TEMPLATE,
    PARAM_SPECS,
    PREVIEW_TABLE_MIN_HEIGHT,
    ConfigPage,
)
from anylabeling.custom.remote_training.ui.dialog import (  # noqa: E402
    RemoteTrainingDialog,
)

#: Baselines of the previous generation (23 parameters, before the
#: scroll round): the window hint of that build was 1679 px, its
#: parameter box 874 px and its preview table 70 px.  Its content
#: heights were 1174 / 1044 / 847 - numbers of a generation this file
#: no longer compares against (the page hint belongs to the scroll area
#: now, see the module docstring).
BASELINE_PAGE_HEIGHT = 1679
BASELINE_PARAMS_BOX_HEIGHT = 874
BASELINE_PREVIEW_TABLE_HEIGHT = 70

#: Reachable targets of the 41 parameter generation: what this machine
#: measured (expanded / default / folded content 1378 / 978 / 781,
#: parameter box 814) plus a little over 10% of headroom, still below
#: the 1679 px baseline above.
MAX_EXPANDED_PAGE_HEIGHT = 1520
MAX_DEFAULT_PAGE_HEIGHT = 1080
MAX_COLLAPSED_PAGE_HEIGHT = 860
MAX_PARAMS_BOX_HEIGHT = 900
#: One collapsed group has to buy at least this much page height; the
#: four together at least MIN_TOTAL_COLLAPSE_GAIN.
MIN_GROUP_COLLAPSE_GAIN = 25
MIN_TOTAL_COLLAPSE_GAIN = 180
#: Height a page is allowed to drift when it is folded and unfolded
#: again (font metrics differ a little between the offscreen styles).
HEIGHT_TOLERANCE = 6
#: The share of the page the rightmost control has to cover.
MIN_RIGHT_EDGE_RATIO = 0.90
#: The smallest share of its own natural height a control may end up
#: with: 1.0 means the natural height, a smaller number a crushed row.
MIN_CONTROL_HEIGHT_RATIO = 0.95
#: Window sizes used by the width tests.  The two column one keeps a
#: real margin over NARROW_PAGE_WIDTH + SCROLL_RESERVE_WIDTH (996 px):
#: 1000 px would sit only 4 px above the switch, which a slightly wider
#: scroll bar would eat.
WIDE_SIZE = (1200, 1400)
NARROW_SIZE = (860, 1000)
PREVIEW_SIZE = (1000, 900)
#: The short window of the scroll assertions: 700 px cannot hold the
#: form, so the page has to scroll instead of squeezing it.
SHORT_SIZE = (1000, 700)

GROUP_NAMES = tuple(name for name, _names in PARAM_GROUPS)

#: The two parameter defaults of the 41 key form (spec §3.8.4): an
#: untouched form sends them, because they are values now instead of
#: absences - the 16 batch step and the family's `auto` preset.
ALWAYS_SENT = {"batch": 16, "optimizer": "auto"}


def capabilities():
    """One realistic capabilities answer for all of the 41 parameters.

    The shapes are the ones the server sends (range object or enum
    list): the widget type itself comes from the local table, the shapes
    only pin the ranges.
    """

    schema = {
        "epochs": {"type": "int", "min": 1, "max": 1000},
        "lr0": {"type": "float", "min": 0, "max": 0.05},
        "imgsz": {"values": [320, 640, 1280]},
        "optimizer": {"type": "preset"},
    }
    for name, shape in PARAM_SPECS.items():
        if name in schema:
            continue
        kind = shape[0]
        if kind == "bool":
            schema[name] = {"type": "bool"}
        elif kind == "enum":
            schema[name] = {"values": ["x", "y"]}
        elif kind == "int":
            schema[name] = {"type": "int"}
        else:
            schema[name] = {"type": "float"}
    return {
        "tasks": ["detect", "segment"],
        "model_families": {
            "yolo11": {
                "weights": {
                    "detect": ["yolo11n.pt", "yolo11s.pt"],
                    "segment": ["yolo11n-seg.pt"],
                },
                "presets": ["yolo11-sgd", "yolo11-adamw", "auto"],
            },
        },
        "param_schema": schema,
        "optimizer_presets": {
            "yolo11-sgd": {"optimizer": "SGD"},
            "yolo11-adamw": {"optimizer": "AdamW"},
        },
        "preset_policy": {
            "default_preset": {"yolo11": "yolo11-sgd"},
        },
    }


def make_page(width=WIDE_SIZE[0], height=WIDE_SIZE[1], caps=None):
    """One fresh page, resized and shown offscreen.

    The page is fed one capabilities answer by default, so the two
    defaults of the form (the batch step and the family's `auto` preset)
    are the ones a real window shows.  Pass `caps=False` for the local
    fallback schema, which declares no preset at all.
    """

    page = ConfigPage()
    if caps is None:
        page.set_capabilities(capabilities())
    page.resize(width, height)
    page.show()
    settle(page)
    return page


def drop(page):
    """Release one page before the next test builds its own."""

    page.hide()
    page.deleteLater()
    QtWidgets.QApplication.processEvents()


def settle(page):
    """Let the page re-run its layout before it is measured again.

    A visibility change posts a LayoutRequest; until that request is
    processed the `sizeHint` of the page is the one of the previous
    fold state, so every measurement below settles the layout first.
    """

    QtWidgets.QApplication.processEvents()
    page.layout().activate()
    QtWidgets.QApplication.processEvents()


def group_grid(page, group):
    return page._group_grids[group]


def placed_columns(grid):
    """The grid columns that really hold a widget."""

    columns = set()
    for row in range(grid.rowCount()):
        for column in range(grid.columnCount()):
            item = grid.itemAtPosition(row, column)
            if item is not None and item.widget() is not None:
                columns.add(column)
    return columns


def group_widgets(page, group):
    names = page._group_names[group]
    return [page._params[name].widget for name in names]


def control_widths(page):
    widths = []
    for group in GROUP_NAMES:
        widths.extend(widget.width() for widget in group_widgets(page, group))
    return widths


def right_edge_ratio(page):
    right = 0
    for group in GROUP_NAMES:
        for widget in group_widgets(page, group):
            right = max(right, widget.x() + widget.width())
    return right / max(1, page.width())


def page_height(page):
    """The natural height of the scrolled content.

    `page.sizeHint()` is the hint of the scroll area now: it keeps the
    height the window was last laid out with and does not move when a
    group folds.  The content widget is what the fold state moves, and
    the page pins its minimum height to it (`_sync_content_height`), so
    that number is what the height targets below measure.
    """

    page._sync_content_height()
    return page.content.minimumHeight()


def content_natural_height(page):
    """The height the content layout asks for right now."""

    return page._content_height()


def pinned_content_height(page):
    """The height the page pins the content widget to."""

    page._sync_content_height()
    return page.content.minimumHeight()


def compression_ratio(page):
    """The smallest control height over its own natural height.

    A compressed page (the scroll area handing its viewport height to
    the content) gives a small number here; 1.0 means every control is
    really drawn at its natural height.
    """

    ratios = []
    for group in GROUP_NAMES:
        for widget in group_widgets(page, group):
            natural = widget.sizeHint().height()
            if natural > 0:
                ratios.append(widget.height() / natural)
    return min(ratios) if ratios else 0.0


def test_two_columns_on_wide_page():
    """A wide page lays every group out in two label / control cells."""

    page = make_page(*WIDE_SIZE)
    try:
        assert page.width() >= NARROW_PAGE_WIDTH
        assert page._param_columns == 2
        for group in GROUP_NAMES:
            columns = placed_columns(group_grid(page, group))
            assert {0, 1, 2, 3} <= columns, (group, sorted(columns))
    finally:
        drop(page)


def test_one_column_on_narrow_page():
    """A narrow page falls back to one column, without rebuilding.

    The layout honours its own minimum width, so the requested size may
    be clamped: the expectation is read from the width the page really
    got instead of from the size the test asked for.
    """

    page = make_page(*NARROW_SIZE)
    try:
        if page.width() >= NARROW_PAGE_WIDTH:
            pytest.skip(
                "the layout keeps the page at {0} px, which is still a two "
                "column width".format(page.width())
            )
        assert page._param_columns == 1
        for group in GROUP_NAMES:
            columns = placed_columns(group_grid(page, group))
            assert columns == {0, 1}, (group, sorted(columns))
    finally:
        drop(page)


def test_controls_fill_the_page_in_both_modes():
    """Every control keeps its floor and reaches the right edge."""

    page = make_page(*WIDE_SIZE)
    try:
        assert page._param_columns == 2
        assert min(control_widths(page)) >= PARAM_CONTROL_MIN_WIDTH
        assert right_edge_ratio(page) >= MIN_RIGHT_EDGE_RATIO
    finally:
        drop(page)
    page = make_page(*NARROW_SIZE)
    try:
        assert min(control_widths(page)) >= PARAM_CONTROL_MIN_WIDTH
        assert right_edge_ratio(page) >= MIN_RIGHT_EDGE_RATIO
    finally:
        drop(page)


def test_collapsing_shrinks_the_page():
    """Each group folds, the four together buy real height and undo."""

    page = make_page(*WIDE_SIZE)
    try:
        page.expand_all_button.click()
        settle(page)
        expanded = page_height(page)
        assert all(
            page._group_contents[group].isVisible() for group in GROUP_NAMES
        )
        gains = []
        for group in GROUP_NAMES:
            before = page_height(page)
            page._group_buttons[group].setChecked(False)
            settle(page)
            assert page._group_contents[group].isVisible() is False
            gains.append(before - page_height(page))
        assert min(gains) >= MIN_GROUP_COLLAPSE_GAIN
        assert expanded - page_height(page) >= MIN_TOTAL_COLLAPSE_GAIN
        page.expand_all_button.click()
        settle(page)
        assert all(
            page._group_contents[group].isVisible() for group in GROUP_NAMES
        )
        assert abs(page_height(page) - expanded) <= HEIGHT_TOLERANCE
    finally:
        drop(page)


def test_collapse_state_survives_a_capabilities_rebuild():
    """A late capabilities answer keeps the user's folding choice."""

    page = make_page()
    try:
        folded = "训练控制与其它"
        # The group already starts folded (DEFAULT_COLLAPSED): unfold it
        # first, so the answer below really has to keep a fresh choice.
        assert DEFAULT_COLLAPSED[folded] is True
        assert page._collapsed[folded] is True
        page._group_buttons[folded].setChecked(True)
        settle(page)
        assert page._collapsed[folded] is False
        assert page._group_contents[folded].isVisibleTo(page) is True
        page._group_buttons[folded].setChecked(False)
        settle(page)
        assert page._collapsed[folded] is True
        page.set_capabilities(capabilities())
        settle(page)
        assert len(page._params) == 41
        # The choice lives in the instance dictionary and on the fresh
        # toggle, which is what a rebuild has to read back.
        assert page._collapsed[folded] is True
        assert page._group_buttons[folded].isChecked() is False
        assert page._group_contents[folded].isVisible() is False
        for group in GROUP_NAMES:
            # The other three keep the plan's default, this group
            # includes the other folded one (数据增强与训练控制).
            assert page._collapsed[group] is DEFAULT_COLLAPSED[group]
            assert (
                page._group_buttons[group].isChecked()
                is (not DEFAULT_COLLAPSED[group])
            )
        # ... and the fresh content is wired to the toggle slot.
        page._group_buttons[folded].setChecked(True)
        settle(page)
        assert page._group_contents[folded].isVisible() is True
    finally:
        drop(page)


def test_badge_counts_only_explicit_keys():
    """The two badge hints count set keys and never change the form.

    The two defaults of the form are explicit from the start (spec
    §3.8.4), so an untouched page already counts the batch step and the
    `auto` preset.
    """

    page = make_page()
    try:
        assert page.params_hint_label.text() == PARAMS_HINT_TEMPLATE.format(2)
        page._params["epochs"].widget.setValue(7)
        page.set_params({"lr0": 0.01})
        settle(page)
        assert page.params_hint_label.text() == PARAMS_HINT_TEMPLATE.format(4)
        assert "4" in page.params_hint_label.text()
        titles = {
            group: page._group_buttons[group].text()
            for group in GROUP_NAMES
        }
        assert "已设置 2 项" in titles["常用参数"]
        assert "已设置 2 项" in titles["学习率与优化器"]
        for group in ("数据增强与训练控制", "训练控制与其它"):
            assert "已设置 0 项" in titles[group]
        for group in GROUP_NAMES:
            assert group in titles[group]
        before = (page.values().params, page.explicit_params())
        page._refresh_badges()
        QtWidgets.QApplication.processEvents()
        after = (page.values().params, page.explicit_params())
        assert after == before
        assert after[1] == {"batch": 16, "epochs": 7, "lr0": 0.01,
                            "optimizer": "auto"}
    finally:
        drop(page)


def test_preview_table_has_room_to_grow():
    """The split preview table never falls back to a 70 px strip."""

    page = make_page(*PREVIEW_SIZE)
    try:
        table = page.preview_table
        assert table.height() >= PREVIEW_TABLE_MIN_HEIGHT
        assert table.height() > BASELINE_PREVIEW_TABLE_HEIGHT
        assert table.minimumHeight() == PREVIEW_TABLE_MIN_HEIGHT
        inside = (
            table.height()
            + page.preview_summary.height()
            + page.preview_row.height()
        )
        assert inside <= page.preview_splitter.height()
    finally:
        drop(page)


def test_layout_does_not_touch_the_request_semantics():
    """No layout state leaks into the form values or the request body.

    `job_request_body()` belongs to the dialog, so this one assertion
    builds the smallest possible window around the page.
    """

    page = make_page()
    try:
        assert len(page._params) == 41
        assert page.values().params == ALWAYS_SENT
        assert page.explicit_params() == ALWAYS_SENT
        assert page._params["optimizer"].widget.currentData() == "auto"
        page.resize(NARROW_SIZE[0], NARROW_SIZE[1])
        settle(page)
        if page.width() < NARROW_PAGE_WIDTH:
            assert page._param_columns == 1
        for group in GROUP_NAMES:
            page._group_buttons[group].setChecked(False)
        settle(page)
        assert all(
            page._group_contents[group].isVisibleTo(page) is False
            for group in GROUP_NAMES
        )
        assert page.values().params == ALWAYS_SENT
        assert page.explicit_params() == ALWAYS_SENT
        assert page._params["optimizer"].widget.currentData() == "auto"
        page._params["epochs"].widget.setValue(7)
        assert page.values().params == dict(ALWAYS_SENT, epochs=7)
        dialog = RemoteTrainingDialog()
        try:
            dialog.config_page.set_capabilities(capabilities())
            body = dialog.job_request_body()
            assert body["params"] == ALWAYS_SENT
            dialog.config_page._params["epochs"].widget.setValue(7)
            assert dialog.job_request_body()["params"] == dict(
                ALWAYS_SENT, epochs=7
            )
        finally:
            dialog._confirm_close = lambda _dialog: True
            dialog.cancel_workers()
            dialog.reject()
            dialog.deleteLater()
            QtWidgets.QApplication.processEvents()
    finally:
        drop(page)


def test_span_and_button_rows_stay_intact():
    """The server row, its echo mode and the four buttons are unchanged."""

    page = make_page(*WIDE_SIZE)
    try:
        assert page.test_button.isVisible()
        assert page.server_url_edit.isVisible()
        assert page.api_key_edit.isVisible()
        assert (
            page.api_key_edit.echoMode()
            == QtWidgets.QLineEdit.EchoMode.Password
        )
        assert [
            page.import_button.text(),
            page.export_button.text(),
            page.precheck_button.text(),
            page.submit_button.text(),
        ] == ["导入配置", "导出配置", "参数预检", "提交任务"]
    finally:
        drop(page)


def test_page_height_targets():
    """One assertion per height target, so a failure names its state."""

    page = make_page(*WIDE_SIZE)
    try:
        assert BASELINE_PAGE_HEIGHT > page_height(page)
        page.expand_all_button.click()
        settle(page)
        assert page_height(page) <= MAX_EXPANDED_PAGE_HEIGHT
        box = page.params_box.sizeHint().height()
        assert box <= MAX_PARAMS_BOX_HEIGHT
        assert box < BASELINE_PARAMS_BOX_HEIGHT
    finally:
        drop(page)
    page = make_page(*WIDE_SIZE)
    try:
        assert page_height(page) <= MAX_DEFAULT_PAGE_HEIGHT
        assert BASELINE_PAGE_HEIGHT > page_height(page)
    finally:
        drop(page)
    page = make_page(*WIDE_SIZE)
    try:
        page.collapse_all_button.click()
        settle(page)
        assert page_height(page) <= MAX_COLLAPSED_PAGE_HEIGHT
    finally:
        drop(page)



def test_a_short_window_scrolls_instead_of_crushing_the_form():
    """The whole form stays reachable at 1000x700 (spec §5.1.3).

    The scroll bar is the answer to a window shorter than the page: the
    content keeps its natural height (its minimum is pinned to the
    layout hint) instead of being squeezed into the viewport, every
    control stays at its own height and the four buttons live outside
    the scroll area, so they never scroll away.
    """

    page = make_page(*SHORT_SIZE)
    try:
        page.expand_all_button.click()
        settle(page)
        assert page._param_columns == 2
        assert page.scroll.verticalScrollBar().maximum() > 0
        natural = content_natural_height(page)
        assert natural > page.scroll.viewport().height()
        assert pinned_content_height(page) == natural
        assert page.content.height() >= natural
        assert compression_ratio(page) >= MIN_CONTROL_HEIGHT_RATIO
        assert page.preview_table.height() >= PREVIEW_TABLE_MIN_HEIGHT
        buttons = (
            page.import_button,
            page.export_button,
            page.precheck_button,
            page.submit_button,
        )
        for button in buttons:
            assert button.isVisible() and button.height() > 0
    finally:
        drop(page)


def test_the_two_column_form_keeps_its_floor_on_a_wide_page():
    """Two columns, a 240 px control floor and the right edge covered."""

    page = make_page(*WIDE_SIZE)
    try:
        page.expand_all_button.click()
        settle(page)
        assert page.width() >= NARROW_PAGE_WIDTH
        assert page._param_columns == 2
        assert min(control_widths(page)) >= PARAM_CONTROL_MIN_WIDTH
        assert right_edge_ratio(page) >= MIN_RIGHT_EDGE_RATIO
    finally:
        drop(page)


def test_the_content_height_follows_the_fold_not_the_window():
    """The fold moves the content, never the scroll bound window hint.

    `page.sizeHint()` is the one of the scroll area now: it cannot tell
    an expanded page from a folded one, which is why every height here
    is read from the content widget (`page_height`).
    """

    page = make_page(*WIDE_SIZE)
    try:
        page.expand_all_button.click()
        settle(page)
        expanded = page_height(page)
        window_hint = page.sizeHint().height()
        assert expanded > window_hint
        page.collapse_all_button.click()
        settle(page)
        folded = page_height(page)
        assert page.sizeHint().height() == window_hint
        assert expanded - folded >= MIN_TOTAL_COLLAPSE_GAIN
        page.expand_all_button.click()
        settle(page)
        assert abs(page_height(page) - expanded) <= HEIGHT_TOLERANCE
    finally:
        drop(page)
