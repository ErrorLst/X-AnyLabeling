"""Offscreen probe of the configuration page layout (spec §5.1.3).

Reads the geometry of one `ConfigPage` and prints / stores what the
layout round is judged on: the natural page size, the parameter box
height, the narrowest control, the right edge share, the active column
count, the per group heights and the split preview table.  It never
writes inside the repository: the JSON, the PNG and the comparison all
go to the paths the caller passes (use /tmp or $TMPDIR).

The file name starts with `record_`, so pytest never collects it.

Usage:

    QT_QPA_PLATFORM=offscreen python -B \
        tests/custom/remote_training/record_remote_training_layout.py \
        --width 1000 --height 900 --out /tmp/layout.json \
        --png /tmp/layout.png --compare /tmp/baseline.json \
        --collapse-all

Every number is measured after a real `resize()` + `show()` and a
settled layout; the natural size is `sizeHint()`, the height the page
asks for in the current fold state and the one the plan measures.
Folding a group only posts a LayoutRequest, so the probe settles the
layout (`layout().activate()`) before every measurement.

The baseline of the main session (offscreen / Fusion) has fallen to the
generation of the 41 parameters: 423 x 1679, parameter box 874, preview
table 70 px at 1000x900, controls 827-893 wide, right edge share 0.964.
The values below are the *same* numbers of that first generation: they
are kept only so a `--compare` run still reports a drop, never as a
target.  The layout round that follows is judged on the scroll fields of
this probe (scroll_visible / scroll_maximum / viewport_height /
content_height / content_size_hint_height / min_control_height_ratio)
plus param_count and the preview table minimum, not on those drops.

    page_size_hint = 423 x 1679   params_box_height = 874
    preview_table_height = 70     min_control_width = 827

Pass it as `--compare` to get the drop of every one of those numbers.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

#: The repository root, so the probe runs as a plain script.
REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)
    )))
)
sys.path.insert(0, REPO_ROOT)

from PyQt6 import QtWidgets  # noqa: E402  (after the platform choice)

from anylabeling.custom.remote_training.ui.config_page import (  # noqa: E402
    BATCH_CHOICES,
    BATCH_DEFAULT,
    NARROW_PAGE_WIDTH,
    PARAM_GROUPS,
    PARAM_SPECS,
    SCROLL_RESERVE_WIDTH,
    ConfigPage,
)

#: The first generation baseline of the main session, in the shape
#: `--compare` expects: the generation of 23 parameters (page height
#: 1679, parameter box 874, preview table 70 px @1000x900, controls
#: 827-893 wide).  The baseline of the current generation - 41 items and
#: a scrollable content - is the JSON this probe writes.
MAIN_SESSION_BASELINE = {
    "page_size_hint": {"width": 423, "height": 1679},
    "params_box_height": 874,
    "preview_table_height": 70,
    "min_control_width": 827,
    "right_edge_ratio": 0.964,
}
GROUP_NAMES = tuple(name for name, _names in PARAM_GROUPS)


def capabilities():
    """One realistic capabilities answer for all of the 41 parameters."""

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
            # copy_paste_mode is an enum here, so the real values go
            # in: a placeholder pair would pick the wrong entry.
            schema[name] = {"values": list(shape[1])}
        elif kind == "int":
            schema[name] = {"type": "int"}
        else:
            # The range object the server sends, not the type only
            # spelling: perspective is the one field whose bounds
            # make the step size visible.
            document = {"type": "float"}
            if len(shape) > 1 and shape[1] is not None:
                document["min"] = shape[1]
            if len(shape) > 2 and shape[2] is not None:
                document["max"] = shape[2]
            schema[name] = document
    return {
        "tasks": ["detect", "segment"],
        "model_families": {
            "yolo11": {
                "weights": {"detect": ["yolo11n.pt"]},
                "presets": ["yolo11-sgd", "yolo11-auto"],
            },
        },
        "param_schema": schema,
        "optimizer_presets": {
            "yolo11-sgd": {"optimizer": "SGD"},
            "yolo11-auto": {"optimizer": "auto"},
        },
        "preset_policy": {"default_preset": {"yolo11": "yolo11-sgd"}},
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--width", type=int, default=1000)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--out", help="where the JSON report is written")
    parser.add_argument("--png", help="where the screenshot is written")
    parser.add_argument("--compare", help="a baseline JSON to compare with")
    parser.add_argument(
        "--collapse-all",
        action="store_true",
        help="fold every parameter group before the measurement",
    )
    parser.add_argument(
        "--capabilities",
        help="a JSON file holding a capabilities answer",
    )
    parser.add_argument(
        "--no-capabilities",
        action="store_true",
        help="measure the local fallback schema instead",
    )
    return parser.parse_args(argv)


def pump(app):
    app.processEvents()


def settle(page):
    """Let a pending LayoutRequest run before anything is measured.

    A visibility change (folding a group) only posts a LayoutRequest;
    until it is processed the page still reports the sizeHint of the
    previous fold state.
    """

    pump(QtWidgets.QApplication.instance())
    page.layout().activate()
    pump(QtWidgets.QApplication.instance())


def group_heights(page):
    heights = {}
    for index, group in enumerate(GROUP_NAMES):
        box = page.params_layout.itemAt(index).widget()
        heights[group] = box.sizeHint().height()
    return heights


def control_widths(page):
    widths = []
    for group in GROUP_NAMES:
        for name in page._group_names[group]:
            widget = page._params[name].widget
            widths.append(widget.width())
    return widths


def right_edge_share(page):
    right = 0
    for group in GROUP_NAMES:
        for name in page._group_names[group]:
            widget = page._params[name].widget
            right = max(right, widget.x() + widget.width())
    return right / max(1, page.width())


def page_height(page):
    """The height the page content asks for in its current state.

    The window size hint is dominated by the scroll area, which keeps
    the height it was last laid out with.  The layout hint is the
    number the folding really moves, so it is what this probe reports
    as the page height (the scroll area pins its widget to it).
    """

    return page._content_height()


def collapse_gains(page):
    """Height one group at a time buys the page (fold, then unfold)."""

    gains = {}
    for group in GROUP_NAMES:
        before = page_height(page)
        page._group_buttons[group].setChecked(False)
        settle(page)
        gains[group] = before - page_height(page)
        page._group_buttons[group].setChecked(True)
        settle(page)
    return gains


def report_page(page):
    """One measurement pass over the page and its scroll area."""

    size = page.sizeHint()
    widths = control_widths(page)
    # One row of the label / control grid, whatever widget type sits in
    # it.  `widget.height()` would be the clipped strip at a short
    # window, and a bare checkbox is shorter than the row it owns, so
    # the number comes from the grid itself.
    min_control_height = min(
        page._group_contents[group].sizeHint().height()
        / -(-len(names) // page._param_columns)
        for group, names in PARAM_GROUPS
    )
    preview_inside = (
        page.preview_table.height()
        + page.preview_summary.height()
        + page.preview_row.height()
    )
    viewport_height = page.scroll.viewport().height()
    content = page.content
    return {
        "page_size_hint": {"width": size.width(), "height": size.height()},
        "page_size": {"width": page.width(), "height": page.height()},
        "narrow_page_width": NARROW_PAGE_WIDTH,
        "params_box_height": page.params_box.sizeHint().height(),
        "min_control_width": min(widths),
        "max_control_width": max(widths),
        "right_edge_ratio": round(right_edge_share(page), 4),
        "columns": page._param_columns,
        "collapsed": dict(page._collapsed),
        "group_heights": group_heights(page),
        "preview_table_height": page.preview_table.height(),
        "preview_table_minimum_height": page.preview_table.minimumHeight(),
        "preview_splitter_height": page.preview_splitter.height(),
        "preview_inside_height": preview_inside,
        "preview_overflow": preview_inside > page.preview_splitter.height(),
        "param_count": len(page._params),
        "batch_choices": list(BATCH_CHOICES),
        "batch_default": BATCH_DEFAULT,
        # --- the scroll area added by the 41 parameter round ----------
        "scroll_reserve_width": SCROLL_RESERVE_WIDTH,
        "scroll_visible": page.scroll.verticalScrollBar().maximum() > 0,
        "scroll_maximum": page.scroll.verticalScrollBar().maximum(),
        "scroll_bar_width": page.scroll.verticalScrollBar().width(),
        "viewport_height": viewport_height,
        "viewport_width": page.scroll.viewport().width(),
        "content_height": content.height(),
        "content_minimum_height": content.minimumHeight(),
        "content_size_hint_height": content.sizeHint().height(),
        "content_layout_hint_height": content.layout().sizeHint().height(),
        # The height of one grid row, in pixels.
        "min_control_height": round(min_control_height, 2),
        # >= 0.95 means the page has more than one row of scroll (or
        # fits): the form is scrollable and never stuck on a clipped
        # strip, whatever widget type the shortest row holds.
        "min_control_height_ratio": round(
            min(
                viewport_height,
                page.scroll.verticalScrollBar().maximum(),
            )
            / max(1.0, min_control_height),
            4,
        ),
    }


def compare(report, baseline):
    """Print the baseline / now table and return the drops."""

    rows = (
        ("page height", "page_size_hint", "height"),
        ("page width", "page_size_hint", "width"),
        ("params box", "params_box_height", None),
        ("min control", "min_control_width", None),
        ("preview table", "preview_table_height", None),
    )
    drops = {}
    print("--- baseline vs now --------------------------------")
    for label, key, inner in rows:
        was = baseline.get(key)
        if isinstance(was, dict) and inner is not None:
            was = was.get(inner)
        now = report.get(key)
        if isinstance(now, dict) and inner is not None:
            now = now.get(inner)
        if was in (None, 0) or now is None:
            continue
        drop = (was - now) / float(was) * 100.0
        drops[key] = round(drop, 2)
        print("{0:<14} {1:>8} -> {2:>8}   {3:>6.1f}%".format(
            label, was, now, drop
        ))
    was = baseline.get("right_edge_ratio")
    if was and report.get("right_edge_ratio") is not None:
        print("{0:<14} {1:>8} -> {2:>8}".format(
            "right edge", was, report["right_edge_ratio"]
        ))
    return drops


def main(argv=None):
    args = parse_args(argv)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    page = ConfigPage()
    default_height = page.sizeHint().height()
    if args.capabilities and not args.no_capabilities:
        with open(args.capabilities, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    elif args.no_capabilities:
        payload = None
    else:
        payload = capabilities()
    if payload is not None:
        page.set_capabilities(payload)
    if args.collapse_all:
        page.collapse_all_button.click()
    else:
        page.expand_all_button.click()
    page.resize(args.width, args.height)
    page.show()
    settle(page)
    # `sizeHint` is the natural size and needs no window at all: the
    # page stays exactly where the resize above put it (an adjustSize()
    # here would move it to the hint and the on screen width of the
    # controls would stop meaning anything).
    natural = page.sizeHint()
    report = report_page(page)
    report["default_page_height"] = default_height
    report["natural_size"] = {
        "width": natural.width(), "height": natural.height()
    }
    report["collapse_gain_per_group"] = collapse_gains(page)
    report["collapse_gain_total"] = sum(
        report["collapse_gain_per_group"].values()
    )
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(text)
    if args.png:
        print("png: {0}".format(page.grab().save(args.png)))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    if args.compare:
        with open(args.compare, "r", encoding="utf-8") as handle:
            baseline = json.load(handle)
        compare(report, baseline)
    page.hide()
    page.deleteLater()
    pump(app)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
