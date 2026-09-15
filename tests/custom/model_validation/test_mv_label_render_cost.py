"""Render cost of the box labels: the label pass is cached.

The labels of a crowded view - one box per prediction, a text of its
own for every one of them - used to be the whole cost of a repaint: the
glyph path of a label was stroked and filled once per frame and per
label, which made a pan or a zoom of a hundred box view crawl. The
canvas now renders the band of a label into a transparent pixmap once
and re-blits it (see ImageCanvas._draw_label_at and _render_label_pixmap
in image_view), and the cases below pin that cache from the
outside: what the labels cost against the very same render without
them, that the second render of one canvas is not slower than the first,
that a ten box view stays far inside a frame, and that a paint device
the platform scales is drawn with the very geometry of the direct path
paint.

The thresholds are deliberately loose: this is a wall clock test, and a
busy machine must not turn it red. The pass a broken cache produces - a
miss on every label of every frame - costs several times the shapes of
the view, which the first case still catches with margin.
"""

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from PyQt6 import QtCore, QtGui, QtWidgets

from anylabeling.custom.model_validation.ui.image_view import ImageCanvas

# The view the cost is measured on: the widget of the results page and a
# picture that does not fit it, so the box outlines are really painted.
WIDGET_SIZE = (900, 600)
IMAGE_SIZE = (1280, 960)
# One render is timed RUNS times and the median is taken: a single
# render of a loaded machine says nothing about the cache.
RUNS = 5
# The ratio of the first and the second render of one canvas: the second
# render re-blits every label, so it is the tight lower bound - and the
# count of renders the hit / miss ratio of the second case is read from
WARM_RUNS = 12
# A hundred boxes of pairwise different texts may not cost more than two
# and a half times the very same view without its labels
LABEL_BUDGET = 2.5
# Ten boxes are a view of one frame, whatever the labels cost
FRAME_BUDGET_SECONDS = 0.040
# The device pixel ratios of the scaled device case: the offscreen
# platform never scales on its own, and 1.25 is the ratio of the common
# Windows display scaling this fork is developed on
SCALED_RATIOS = (1.25, 2.0)
# The probe picture of the scaled device case: one label fits inside it
# whole, so the ink box of the paint is never cut by the image
PROBE_SIZE = (240, 80)
# A cached label and the direct path paint may differ by this many
# logical pixels: the cached band is a table rounded to whole device
# pixels, so one device pixel of antialiased edge is the most the two
# ink boxes can drift apart (0.8 logical pixels at a ratio of 1.25)
SCALED_TOLERANCE = 1.0


@pytest.fixture(scope="module")
def qt_app():
    "Create the offscreen application shared by the cost tests."

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def make_canvas() -> ImageCanvas:
    "Return a fitted canvas of WIDGET_SIZE showing a grey picture."

    canvas = ImageCanvas("cost")
    canvas.resize(*WIDGET_SIZE)
    pixmap = QtGui.QPixmap(*IMAGE_SIZE)
    pixmap.fill(QtGui.QColor(128, 128, 128))
    canvas.set_image(pixmap)
    return canvas


def boxes(count: int):
    "Return count prediction boxes whose labels are all different."

    width = IMAGE_SIZE[0] // 8
    height = IMAGE_SIZE[1] // 40
    step = IMAGE_SIZE[0] // (count + 1)
    shapes = []
    for index in range(count):
        left = 20 + (index % 10) * step
        top = 10 + (index // 10) * (height + 6)
        right = min(left + width, IMAGE_SIZE[0] - 1)
        bottom = min(top + height, IMAGE_SIZE[1] - 1)
        shapes.append(
            {
                "label": "s%02d" % index,
                "shape_type": "rectangle",
                "points": [
                    [left, top],
                    [right, top],
                    [right, bottom],
                    [left, bottom],
                ],
                "score": 0.5 + (index % 50) / 100.0,
            }
        )
    return shapes


def show(canvas: ImageCanvas, count: int) -> None:
    "Put count distinct boxes on a canvas, as the ground truth pass."

    canvas.set_shapes(boxes(count), [], [""] * count, ())


def render_seconds(canvas: ImageCanvas, runs: int) -> float:
    "Return the median seconds of runs renders of one canvas."

    target = QtGui.QImage(canvas.size(), QtGui.QImage.Format.Format_RGB32)
    target.fill(QtGui.QColor(0, 0, 0))
    samples = []
    for _ in range(runs):
        painter = QtGui.QPainter(target)
        begin = time.perf_counter()
        canvas.render(
            painter,
            QtCore.QPoint(0, 0),
            QtGui.QRegion(canvas.rect()),
            QtWidgets.QWidget.RenderFlag.DrawWindowBackground,
        )
        samples.append(time.perf_counter() - begin)
        painter.end()
    samples.sort()
    return samples[len(samples) // 2]


def scaled_label_ink(
    canvas: ImageCanvas,
    glyphs: QtGui.QPainterPath,
    left: float,
    top: float,
    ratio: float,
    direct: bool,
):
    "Return the logical ink box of one label painted on a scaled device."

    image = QtGui.QImage(
        int(round(PROBE_SIZE[0] * ratio)),
        int(round(PROBE_SIZE[1] * ratio)),
        QtGui.QImage.Format.Format_RGBA8888,
    )
    image.setDevicePixelRatio(ratio)
    image.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(image)
    try:
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        if direct:
            canvas._draw_label_path(painter, glyphs, left, top)
        else:
            canvas._draw_label_at(painter, glyphs, left, top)
    finally:
        painter.end()
    pointer = image.constBits()
    pointer.setsize(image.sizeInBytes())
    pixels = np.frombuffer(
        bytes(pointer), np.uint8, count=image.sizeInBytes()
    )
    alpha = pixels.reshape(image.height(), image.width(), 4)[:, :, 3]
    rows = np.flatnonzero(alpha.any(axis=1))
    columns = np.flatnonzero(alpha.any(axis=0))
    assert rows.size and columns.size
    return (
        float(columns[0]) / ratio,
        float(rows[0]) / ratio,
        float(columns[-1] + 1) / ratio,
        float(rows[-1] + 1) / ratio,
    )


def test_a_crowded_label_pass_stays_near_the_cost_of_its_boxes(qt_app):
    "A hundred labels may not cost two and a half times the boxes."

    canvas = make_canvas()
    show(canvas, 100)
    labelled = render_seconds(canvas, RUNS)
    canvas.show_labels = False
    plain = render_seconds(canvas, RUNS)

    assert plain > 0
    assert labelled <= LABEL_BUDGET * plain, (labelled, plain)


def test_the_second_render_of_a_canvas_is_not_slower(qt_app):
    "The warm render re-blits the labels instead of painting them anew."

    canvas = make_canvas()
    show(canvas, 30)
    cold = render_seconds(canvas, 1)
    warm = render_seconds(canvas, WARM_RUNS)

    # the second render of the very same view is the cached one: it may
    # not cost more than the first - the ten percent is the jitter of a
    # wall clock, not a budget for the cache to spend
    assert cold >= 0.9 * warm, (cold, warm)
    # and the counters say the render really was a cache hit: one cold
    # pass over thirty labels, then WARM_RUNS of them served from the
    # table, so the hits outnumber the misses by the warm runs
    assert canvas._label_cache_misses > 0
    assert canvas._label_cache_hits > 0
    assert canvas._label_cache_hits >= 10 * canvas._label_cache_misses


def test_a_ten_box_view_stays_inside_a_frame(qt_app):
    "Ten labelled boxes are a cheap view, far inside forty milliseconds."

    canvas = make_canvas()
    show(canvas, 10)
    seconds = render_seconds(canvas, RUNS)

    assert seconds <= FRAME_BUDGET_SECONDS, seconds


@pytest.mark.parametrize("ratio", SCALED_RATIOS)
def test_a_scaled_device_draws_the_cached_label_in_full_size(qt_app, ratio):
    "The cached band is the path band on a device the platform scales."

    canvas = make_canvas()
    glyphs = canvas._cached_glyphs(canvas._label_font(), "s00 0.86")
    left, top = 20.0, 30.0
    cached = scaled_label_ink(canvas, glyphs, left, top, ratio, direct=False)
    direct = scaled_label_ink(canvas, glyphs, left, top, ratio, direct=True)

    # the pixmap of a label is allocated in device pixels (see
    # _render_label_pixmap): allocating the logical size instead hands
    # the glyphs a table of their width over the ratio, which clips the
    # band and draws it at half size on a ratio of two
    for edge, expected in zip(cached, direct):
        assert abs(edge - expected) <= SCALED_TOLERANCE, (
            ratio,
            cached,
            direct,
        )
    # and the cached band really is the size of its own glyphs: a table
    # holding a fraction of the label is caught on its own as well
    ink = glyphs.boundingRect()
    assert cached[2] - cached[0] >= float(ink.width()) - SCALED_TOLERANCE
