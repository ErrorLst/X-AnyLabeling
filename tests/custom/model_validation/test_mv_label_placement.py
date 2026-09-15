"""Pixel level regression of the in place box labels.

A label is measured and painted in widget space while its box is painted
in image space, and the two spaces meet exactly once: the upper left
corner of the bounding box of a shape is converted with image_to_widget
and every number of the label is derived from that point. When the label
pass keeps the image transform of the shapes, the font is scaled by the
view scale and the text lands about seventeen pixels away from its own
box - the defect these tests pin down.

Every assertion reads the rendered pixels of a canvas and locates the
green outline of a GT box with three rulers of that render: the box of
its bright glyph pixels, the band a label alone paints - the difference
between the canvas and the same canvas with show_labels turned off - and
the ink box of the label font, measured on the platform at hand and
turned into widget pixels by the placement itself. A bound is written
against the font of the machine when a pixel threshold or an absolute
width of the text would only hold on the machine it was written on, so
the geometry is checked as the user sees it and not as the code intends
it.

The same pixels pin the display rule of a box: a box is an outline and
nothing else, so the pixels inside it stay the pixels of the original
picture, and hiding the labels removes the text alone.
"""

import os
import os.path as osp

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import cv2
import numpy as np
import pytest

from PyQt6 import QtCore, QtGui, QtWidgets

from anylabeling.custom.model_validation import dataset
from anylabeling.custom.model_validation import records as records_module
from anylabeling.custom.model_validation.ui import (
    image_view as image_view_module,
)
from anylabeling.custom.model_validation.ui.image_view import (
    GT_COLOR,
    LABEL_GAP,
    LABEL_MARGIN,
    LABEL_OUTLINE_WIDTH,
    LABEL_PADDING,
    TEXT_COLOR,
    ImageCanvas,
)
from anylabeling.custom.model_validation.ui.results_page import ResultsPage

# the canvas refuses a size below its own minimum (320 x 320)
WIDGET_SIZE = (400, 320)
# width, height of the synthetic picture: 260 x 200 like the existing
# canvas tests, which fits the widget at a scale of about 1.52
IMAGE_SIZE = (260, 200)
SCORE = 0.89
# The picture is a flat grey, so every bright pixel is a glyph of a
# label and every darker one is its outline; the same grey is also the
# one value the inside of a box has to keep, because a box is never
# filled.
PICTURE_FILL = 40
TEXT_LEVEL = 150
# the vertical placement rules, with one pixel of raster slack
TIGHT = 4


@pytest.fixture(scope="module")
def qt_app():
    "Create the offscreen application shared by the label tests."

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def original_picture(gradient: bool = False) -> np.ndarray:
    "Return the synthetic picture as a (height, width, 3) RGB array."

    width, height = IMAGE_SIZE
    image = np.full((width, height, 3), PICTURE_FILL, dtype=np.uint8)
    if gradient:
        # a bright right half and a dark left half: the two extremes a
        # label has to survive
        image[width // 2 :, :] = 255
    return np.ascontiguousarray(image.transpose(1, 0, 2))


def make_canvas(gradient: bool = False) -> ImageCanvas:
    "Return a fitted canvas showing a synthetic picture."

    canvas = ImageCanvas("")
    canvas.resize(*WIDGET_SIZE)
    width, height = IMAGE_SIZE
    image = original_picture(gradient)
    ok, buffer = cv2.imencode(".png", image)
    assert ok
    pixmap = QtGui.QPixmap()
    assert pixmap.loadFromData(buffer.tobytes())
    canvas.set_image(pixmap)
    assert canvas.image_size().width() == width
    return canvas


def render(canvas) -> np.ndarray:
    "Render a canvas offscreen and return its pixels as an RGB array."

    result = QtGui.QImage(canvas.size(), QtGui.QImage.Format.Format_RGBA8888)
    result.fill(QtGui.QColor(0, 0, 0))
    painter = QtGui.QPainter(result)
    canvas.render(
        painter,
        QtCore.QPoint(0, 0),
        QtGui.QRegion(canvas.rect()),
        QtWidgets.QWidget.RenderFlag.DrawWindowBackground,
    )
    painter.end()
    pointer = result.constBits()
    pointer.setsize(result.sizeInBytes())
    pixels = np.frombuffer(
        bytes(pointer), np.uint8, count=result.sizeInBytes()
    )
    pixels = pixels.reshape(result.height(), result.width(), 4)
    return np.ascontiguousarray(pixels[:, :, :3])


def rect_shape(start, end, label: str = "a0_dian", score=SCORE) -> dict:
    "Return one rectangle of the picture coordinate system."

    shape = {
        "label": label,
        "shape_type": "rectangle",
        "points": [
            list(start),
            [end[0], start[1]],
            list(end),
            [start[0], end[1]],
        ],
    }
    if score is not None:
        shape["score"] = score
    return shape


def drawn_region(reference, rendered):
    "Return (rect, pixel count) of what rendered adds over reference."

    mask = np.any(
        rendered.astype(np.int16) != reference.astype(np.int16), axis=2
    )
    if not mask.any():
        return None, 0
    rows, columns = np.nonzero(mask)
    rect = (
        int(columns.min()),
        int(rows.min()),
        int(columns.max()),
        int(rows.max()),
    )
    return rect, int(mask.sum())


def rect_of(mask):
    "Return the (left, top, right, bottom) rect of a boolean mask."

    if not mask.any():
        return None
    rows, columns = np.nonzero(mask)
    return (
        int(columns.min()),
        int(rows.min()),
        int(columns.max()),
        int(rows.max()),
    )


def text_mask(image) -> np.ndarray:
    "Return the mask of the bright glyph pixels of a render."

    return np.all(image >= TEXT_LEVEL, axis=2)


def box_mask(image) -> np.ndarray:
    "Return the mask of the green outline of a render."

    target = np.array([GT_COLOR.red(), GT_COLOR.green(), GT_COLOR.blue()])
    return np.all(np.abs(image.astype(np.int16) - target) <= 60, axis=2)


def measure(canvas, start):
    "Return the render, the widget corner, the box rect and the text rect."

    rendered = render(canvas)
    corner = canvas.image_to_widget(
        QtCore.QPointF(float(start[0]), float(start[1]))
    )
    return (
        rendered,
        corner,
        rect_of(box_mask(rendered)),
        rect_of(text_mask(rendered)),
    )


def label_band(canvas, shapes, predictions=()) -> tuple:
    """Return (rect, count) of the pixels a label alone paints.

    The band measures what the canvas really paints on the platform at
    hand instead of an absolute pixel threshold: the bright pixel rule of
    text_mask only sees pixels a glyph covers at least half, so the anti
    aliased rows above and below the ink box of a label go unread and its
    bright rect ends about three rows higher than the ink the placement
    measures. The width of a label is platform dependent for the same
    reason - the same point size yields different ink under another font
    - so a bound on a pixel count or on an absolute width only ever holds
    on the machine it was written on. The band is the difference between
    the canvas and the very same canvas with show_labels turned off, so
    it holds the text and its outline alone. It requires the picture to
    be flat and far darker than the glyphs, the grey backdrop of this
    file: a bright pixel of the picture would be counted as a glyph.
    """

    reference = make_canvas()
    reference.show_labels = False
    reference.set_shapes(list(shapes), list(predictions))
    return drawn_region(render(reference), render(canvas))


def label_ink(canvas, text):
    "Return the ink box of one label, as the placement measures it."

    font = canvas._label_font()
    return canvas._stacked_glyphs(font, text).boundingRect()


def interior_pixels(canvas, rendered, start, end, inset=4.0):
    """Return the rendered pixels inside a box and the source pixels.

    The sample starts inset image pixels inside the box, which keeps it
    clear of the outline band: a box only ever paints its own border, so
    everything deeper inside it has to be the original picture. Every
    widget pixel of the sample is converted back to image space one by
    one, so the comparison holds for the picture itself and not only for
    its flat value.
    """

    picture = original_picture()
    top_left = canvas.image_to_widget(
        QtCore.QPointF(start[0] + inset, start[1] + inset)
    )
    bottom_right = canvas.image_to_widget(
        QtCore.QPointF(end[0] - inset, end[1] - inset)
    )
    left = int(np.ceil(top_left.x()))
    top = int(np.ceil(top_left.y()))
    right = int(np.floor(bottom_right.x()))
    bottom = int(np.floor(bottom_right.y()))
    inside = rendered[top : bottom + 1, left : right + 1]
    source = np.zeros_like(inside)
    for row in range(top, bottom + 1):
        for column in range(left, right + 1):
            point = canvas.widget_to_image(
                QtCore.QPointF(float(column), float(row))
            )
            source[row - top, column - left] = picture[
                int(point.y()), int(point.x())
            ]
    return inside, source


# --------------------------------------------------------------- geometry
def test_a_label_hangs_just_above_its_own_box(qt_app):
    "With room above, the text sits right on top of its own box."

    start, end = (100, 100), (200, 160)
    shapes = [rect_shape(start, end)]
    canvas = make_canvas()
    canvas.set_shapes(shapes, [])
    rendered, corner, box_rect, text = measure(canvas, start)
    band, count = label_band(canvas, shapes)

    assert box_rect is not None and text is not None
    # the outline of the box is where the image mapping puts it
    assert abs(box_rect[0] - corner.x()) <= 2
    assert abs(box_rect[1] - corner.y()) <= 2
    # the text starts at the left edge of the box ...
    assert abs(text[0] - corner.x()) <= LABEL_MARGIN + 2
    # ... and the band the label paints stays in the two pixel gap above
    # the box, measured against the very same canvas without its label
    assert band is not None and count > 0
    assert 0 <= box_rect[1] - band[3] <= TIGHT
    # the label hangs above the corner, it is never below or across it
    assert band[3] < corner.y()


def test_a_tiny_box_keeps_its_label_right_next_to_it(qt_app):
    "An eight pixel box is labelled next to it, not away from it."

    start, end = (100, 100), (108, 108)
    shapes = [rect_shape(start, end)]
    canvas = make_canvas()
    canvas.set_shapes(shapes, [])
    rendered, corner, box_rect, text = measure(canvas, start)
    band, count = label_band(canvas, shapes)

    assert box_rect is not None and text is not None
    assert abs(text[0] - corner.x()) <= LABEL_MARGIN + 2
    assert band is not None and count > 0
    assert 0 <= box_rect[1] - band[3] <= TIGHT
    assert band[3] < corner.y()


def test_a_label_of_a_box_at_the_top_edge_flips_below_it(qt_app):
    "Without room above, the label is written below the corner instead."

    start, end = (60, 0), (140, 40)
    canvas = make_canvas()
    canvas.set_shapes([rect_shape(start, end)], [])
    rendered, corner, box_rect, text = measure(canvas, start)

    assert text is not None and box_rect is not None
    assert corner.y() < 10.0, "the corner is not near the top edge"
    # the corner leaves no room for the label above it, so the glyphs run
    # down from the very top border into that corner and hang below it as
    # far as their height allows: they are never pushed off the view
    assert text[1] <= corner.y()
    assert text[3] < box_rect[3]
    assert 0 <= text[1] <= int(LABEL_PADDING) + TIGHT
    # and the text stays horizontally aligned with the box
    assert abs(text[0] - corner.x()) <= LABEL_MARGIN + 2


def test_a_label_of_a_box_leaving_the_view_is_pulled_back(qt_app):
    "A box hanging over the right edge keeps a fully visible label."

    start, end = (150, 100), (200, 160)
    shapes = [rect_shape(start, end)]
    canvas = make_canvas()
    canvas.set_shapes(shapes, [])
    rendered, corner, box_rect, text = measure(canvas, start)
    band, count = label_band(canvas, shapes)
    ink = label_ink(canvas, image_view_module.shape_label_text(shapes[0]))

    assert text is not None and box_rect is not None
    # the text was pulled back inside the widget ...
    assert text[2] <= WIDGET_SIZE[0] - LABEL_MARGIN
    assert text[0] >= LABEL_MARGIN
    # ... without losing the two pixel gap above its own box
    assert band is not None and count > 0
    assert 0 <= box_rect[1] - band[3] <= TIGHT
    # the whole text is still there, never clipped by the border: the
    # band is one glyph box wide plus the outline centred on its edge
    assert abs((band[2] - band[0]) - (ink.width() + LABEL_OUTLINE_WIDTH)) <= 2


def test_a_label_never_drifts_to_the_scaled_position(qt_app):
    "The label is measured in widget pixels, not scaled by the view."

    start, end = (100, 100), (200, 160)
    shapes = [rect_shape(start, end)]
    canvas = make_canvas()
    canvas.set_shapes(shapes, [])
    rendered, corner, box_rect, text = measure(canvas, start)

    # the whole label ends in the two pixel gap above the corner: against
    # the same canvas without its label, the painted band is one glyph
    # band high and its lowest pixel sits that gap above the box
    reference = make_canvas()
    reference.show_labels = False
    reference.set_shapes([rect_shape(start, end)], [])
    drawn, count = drawn_region(render(reference), rendered)
    ink = label_ink(canvas, image_view_module.shape_label_text(shapes[0]))

    scale = canvas.view_scale()
    assert scale > 1.2
    # the defect scaled the font by the view scale and pushed the text
    # about seventeen pixels below the corner of its own box
    assert drawn[3] < corner.y()
    # the outlined band ends LABEL_GAP above the corner, so the glyphs
    # themselves stop half an outline width higher
    assert corner.y() - drawn[3] <= TIGHT + 2
    # the band keeps the height of the fixed screen font, whatever the
    # view scale, within the ink the placement measures on this platform
    assert text[3] - text[1] <= ink.height() + 1
    assert drawn is not None and count > 0
    # the outline is centred on the border of the glyphs, so the painted
    # band reaches half of its width beyond them
    assert drawn[3] - drawn[1] <= ink.height() + LABEL_OUTLINE_WIDTH + 2


def test_a_box_pushed_out_of_the_view_keeps_a_readable_label(qt_app):
    "The label of a box outside the picture is clamped, never dropped."

    start, end = (-120, -60), (40, 40)
    canvas = make_canvas()
    canvas.set_shapes([rect_shape(start, end)], [])
    corner = canvas.image_to_widget(
        QtCore.QPointF(float(start[0]), float(start[1]))
    )
    assert corner.x() < 0 and corner.y() < 0
    shapes = [rect_shape(start, end)]
    text = rect_of(text_mask(render(canvas)))
    band, count = label_band(canvas, shapes)
    ink = label_ink(canvas, image_view_module.shape_label_text(shapes[0]))

    assert text is not None
    assert text[0] >= LABEL_MARGIN and text[1] >= LABEL_MARGIN
    assert text[2] < WIDGET_SIZE[0] and text[3] < WIDGET_SIZE[1]
    # the whole text is there, never clipped by the border: the painted
    # band is one glyph box wide plus the outline centred on its edge
    assert band is not None and count > 0
    assert abs((band[2] - band[0]) - (ink.width() + LABEL_OUTLINE_WIDTH)) <= 2


# -------------------------------------------------------------- switches
def test_a_label_never_carries_a_background_plate(qt_app):
    "A label is its glyphs and their outline, never a plate behind them."

    start, end = (100, 100), (200, 160)
    shape = rect_shape(start, end)
    canvas = make_canvas()
    canvas.set_shapes([shape], [])
    corner = canvas.image_to_widget(
        QtCore.QPointF(float(start[0]), float(start[1]))
    )
    with_label = render(canvas)
    text = rect_of(text_mask(with_label))
    assert text is not None
    ink = label_ink(canvas, image_view_module.shape_label_text(shape))

    # the reference is the very same picture and box without its label
    reference = make_canvas()
    reference.show_labels = False
    reference.set_shapes([shape], [])
    plain = render(reference)

    drawn, count = drawn_region(plain, with_label)
    assert drawn is not None and count > 0
    # the two bounds below measure the band against the ink box the
    # placement itself works on - the glyphs of this platform, not the
    # bright pixels of the text. The corner is the anchor of the label,
    # so the paint of the glyphs runs from half an outline width above
    # the top of that box to LABEL_GAP above the corner; a band that
    # stays within one outline width of the two edges is the glyphs and
    # their stroke, while the plate of the previous revision sits
    # LABEL_PADDING = 3.0 further out and fails here. Only containing
    # the bright pixel box, as this test did before, holds by
    # construction and proves nothing
    font = canvas._label_font()
    glyphs = canvas._stacked_glyphs(
        font, image_view_module.shape_label_text(shape)
    )
    box = glyphs.boundingRect()
    outline = LABEL_OUTLINE_WIDTH / 2.0
    origin_y = corner.y() - LABEL_GAP - float(box.bottom()) - outline
    ink_top = origin_y + float(box.top())
    ink_bottom = origin_y + float(box.bottom()) + outline
    assert ink_top - drawn[1] <= LABEL_OUTLINE_WIDTH
    assert drawn[3] - ink_bottom <= LABEL_OUTLINE_WIDTH
    assert drawn[3] - drawn[1] <= ink.height() + LABEL_OUTLINE_WIDTH + 2
    assert drawn[2] - drawn[0] <= ink.width() + LABEL_OUTLINE_WIDTH + 2
    # and the padding margin a plate would have covered is the flat
    # picture: the columns left of the leftmost painted pixel and the
    # row above the painted band carry no colour at all
    left = max(drawn[0] - 4, 0)
    assert left < drawn[0]
    band = with_label[drawn[1] : drawn[3] + 1, left : drawn[0]]
    assert np.array_equal(band, np.full(band.shape, PICTURE_FILL))
    top = max(drawn[1] - 3, 0)
    assert top < drawn[1]
    row_above = with_label[top, max(drawn[0] - 1, 0) : drawn[2] + 2]
    assert np.array_equal(row_above, np.full(row_above.shape, PICTURE_FILL))


def test_a_label_is_only_text_and_its_dark_outline(qt_app):
    "White glyphs on a dark outline: that is all a label paints."

    shapes = [rect_shape((100, 100), (200, 160))]
    canvas = make_canvas()
    canvas.set_shapes(shapes, [])
    rendered, corner, box_rect, text = measure(canvas, (100, 100))
    painted, count = label_band(canvas, shapes)

    assert text is not None
    assert painted is not None and count > 0
    assert 0 <= box_rect[1] - painted[3] <= TIGHT
    # the padding band left of the first glyph is untouched: a plate
    # would have covered it with the blend of the picture and its colour
    row = (text[1] + text[3]) // 2
    outside = rendered[row - 6, max(text[0] - 6, 0) : text[0] + 1]
    assert np.array_equal(outside, np.full(outside.shape, PICTURE_FILL))
    # the glyphs keep their outline and their bright fill
    band = rendered[text[1] : text[3] + 1, text[0] : text[2] + 1]
    assert text_mask(band).any()
    assert band.astype(np.int16).min() < PICTURE_FILL


# ------------------------------------------------------------- no fill
def test_a_box_is_only_an_outline_and_never_a_filled_plate(qt_app):
    """A box paints its border alone: its inside keeps the picture.

    The sample of the inside of the box states the rule exactly: every
    rendered pixel there is the source pixel it covers, never a blend
    with a plate. The sample keeps its distance from the outline band,
    which is the only thing a box ever paints.
    """

    start, end = (100, 100), (200, 160)
    canvas = make_canvas()
    canvas.set_shapes([rect_shape(start, end)], [])
    rendered = render(canvas)

    # the box is really there: its outline and its label were painted
    assert box_mask(rendered).any()
    assert text_mask(rendered).any()
    inside, source = interior_pixels(canvas, rendered, start, end)
    assert inside.size > 0 and source.size > 0
    # every pixel inside the box is the source pixel under it
    assert np.array_equal(inside, source), np.unique(
        inside.reshape(-1, 3), axis=0
    )
    # and the source really is the flat picture, so the equality above
    # rules out a darkened as well as a tinted inside
    assert np.array_equal(source, np.full(source.shape, PICTURE_FILL))
    assert not hasattr(canvas, "box_fill")


def test_a_gt_and_a_pred_box_leave_the_same_picture_inside(qt_app):
    "Both canvases outline their own box and neither one fills it."

    start, end = (100, 100), (200, 160)
    ground_truth = make_canvas()
    ground_truth.set_shapes([rect_shape(start, end)], [])
    prediction = make_canvas()
    prediction.set_shapes([], [rect_shape(start, end)])

    gt_inside, gt_source = interior_pixels(
        ground_truth, render(ground_truth), start, end
    )
    pred_inside, pred_source = interior_pixels(
        prediction, render(prediction), start, end
    )
    # the two interiors are the very same picture and match it exactly
    assert np.array_equal(gt_source, pred_source)
    assert np.array_equal(gt_inside, gt_source)
    assert np.array_equal(pred_inside, pred_source)
    # the two outlines really differ, so the identical interiors above
    # are the untouched picture and not a coincidence of two identical
    # canvases
    assert not np.array_equal(
        box_mask(render(ground_truth)), box_mask(render(prediction))
    )


def test_the_fill_switch_and_its_colours_are_gone(qt_app):
    "The canvas owns no fill switch and the module no fill colour."

    canvas = make_canvas()
    assert not hasattr(canvas, "box_fill")
    assert not hasattr(image_view_module, "BOX_FILL_COLOR")
    assert not hasattr(image_view_module, "BOX_FILL_ALPHA")
    assert "BOX_FILL_COLOR" not in image_view_module.__all__
    assert "BOX_FILL_ALPHA" not in image_view_module.__all__


def test_hiding_the_labels_leaves_the_picture_and_its_boxes(qt_app):
    "show_labels False paints no text at all."

    start, end = (100, 100), (200, 160)
    canvas = make_canvas()
    canvas.show_labels = False
    canvas.set_shapes([rect_shape(start, end)], [])
    rendered, corner, box_rect, text = measure(canvas, start)

    assert text is None
    assert not text_mask(rendered).any()
    # the box of the shape survives the switch, untouched
    assert box_rect is not None
    shown = make_canvas()
    shown.set_shapes([rect_shape(start, end)], [])
    assert np.array_equal(
        rect_of(box_mask(rendered)), rect_of(box_mask(render(shown)))
    )
    # above the box not one pixel differs from the picture alone: no
    # text and no plate of a hidden label was painted there
    plain = render(make_canvas())
    changed = np.any(
        rendered.astype(np.int16) != plain.astype(np.int16), axis=2
    )
    assert not changed[: int(corner.y()) - 2].any()


def test_a_label_stays_readable_on_a_bright_picture(qt_app):
    "The dark outline keeps the white glyphs readable over white."

    # the corner sits on the bright half of the gradient picture, so the
    # whole label is written over white
    start, end = (150, 40), (250, 150)
    corner_probe = make_canvas(gradient=True)
    corner_probe.set_shapes([rect_shape(start, end)], [])
    corner = corner_probe.image_to_widget(
        QtCore.QPointF(float(start[0]), float(start[1]))
    )
    assert corner.x() > IMAGE_SIZE[0] // 2 * corner_probe.view_scale()

    # the geometry comes from the flat picture: on the white half of the
    # gradient the picture itself would be read as a glyph
    placed = make_canvas()
    placed.set_shapes([rect_shape(start, end)], [])
    _flat, _corner, _box_rect, text = measure(placed, start)
    assert text is not None, "no label was painted at all"
    row = (text[1] + text[3]) // 2
    left = max(text[0] - 2, 0)
    # the label lies on the white half and leaves room right of it
    white = text[2] + int(LABEL_PADDING) + 2
    assert text[0] > IMAGE_SIZE[0] // 2 * placed.view_scale()
    assert white < WIDGET_SIZE[0] - 2

    bright = make_canvas(gradient=True)
    bright.set_shapes([rect_shape(start, end)], [])
    rendered = render(bright)

    # the picture around the label is untouched: no plate and no stroke
    # of a label reaches beyond the band of its own glyphs
    assert rendered[row, white].min() == 255
    assert rendered[max(text[1] - 6, 0), left : text[2] + 1].min() == 255
    # on white the glyph row holds both the white fill of the letters
    # and the dark outline that keeps them readable
    glyph_row = rendered[row, left : text[2] + 2]
    assert glyph_row.min() <= 120, "the outline is missing on white"
    assert glyph_row.max() == 255, "the glyphs lost their white fill"
    # and the letters are filled, never hollowed out
    assert text_mask(rendered[text[1] : text[3] + 1, left : text[2] + 2]).any()


# ------------------------------------------------------------- the page
# the four display switches of the page, in the order they are laid out
# from left to right
DISPLAY_SWITCHES = ("GT 绿色", "Pred 蓝色", "叠加", "显示标签")


def test_the_page_owns_the_four_display_switches_and_no_fill_one(qt_app):
    "GT 绿色 / Pred 蓝色 / 叠加 / 显示标签: four switches, no fill."

    page = ResultsPage()
    switches = [
        check.text() for check in page.findChildren(QtWidgets.QCheckBox)
    ]
    assert switches == list(DISPLAY_SWITCHES)
    # the switch that used to fill a box is gone, and no control of the
    # page explains a fill any more
    assert not hasattr(page, "box_fill_check")
    assert not [
        widget
        for widget in page.findChildren(QtWidgets.QWidget)
        if "填充" in widget.toolTip()
    ]

    assert page.gt_check.isChecked()
    assert page.gt_check.text() == "GT 绿色"
    assert page.pred_check.isChecked()
    assert page.pred_check.text() == "Pred 蓝色"
    assert page.overlay_check.isChecked()
    assert page.overlay_check.text() == "叠加"
    assert page.label_check.isChecked()
    assert page.label_check.text() == "显示标签"
    assert page.label_check.toolTip()
    # the label itself never has a background any more: the switch that
    # used to draw one is gone
    assert not hasattr(page, "label_background_check")
    assert "背景" in page.label_check.toolTip()
    page.deleteLater()


def test_the_page_switches_reach_both_canvases_and_keep_the_view(
    qt_app, tmp_path
):
    "Toggling a display switch repaints the pair, it never refits it."

    staging = osp.join(str(tmp_path), dataset.STAGING_PREFIX + "label_switch")
    for folder in (dataset.ORIGINAL_DIRNAME, dataset.AUGMENTED_DIRNAME):
        for sub in (dataset.IMAGES_DIRNAME, dataset.LABELS_DIRNAME):
            os.makedirs(osp.join(staging, folder, sub), exist_ok=True)
    paths = dataset.staging_paths(
        staging, records_module.KIND_ORIGINAL, "a.png"
    )
    image = np.full((IMAGE_SIZE[0], IMAGE_SIZE[1], 3), PICTURE_FILL, np.uint8)
    image = np.ascontiguousarray(image.transpose(1, 0, 2))
    ok, buffer = cv2.imencode(".png", image)
    assert ok
    buffer.tofile(paths["image"])
    dataset.write_json(
        paths["label"],
        {
            "version": "3.0.0",
            "flags": {},
            "checked": False,
            "shapes": [rect_shape((100, 100), (200, 160))],
            "imagePath": "a.png",
            "imageData": None,
            "imageHeight": IMAGE_SIZE[1],
            "imageWidth": IMAGE_SIZE[0],
        },
    )
    record = records_module.make_record(
        records_module.KIND_ORIGINAL, "a.png", paths["image"], paths["label"]
    )

    page = ResultsPage()
    page.resize(900, 600)
    page.set_context(["a0_dian"], staging)
    page.set_records([record])
    # the page opens on NG and this test looks at the whole list; the
    # switch only asks for the rebuild, so the events are driven once
    page.filter_combo.setCurrentIndex(0)
    QtWidgets.QApplication.processEvents()
    page.table.selectRow(0)
    gt, pred = page.gt_canvas, page.pred_canvas
    gt.resize(*WIDGET_SIZE)
    pred.resize(*WIDGET_SIZE)
    gt.zoom_steps_at(QtCore.QPointF(120.0, 90.0), 1.0)
    gt.finish_zoom()
    state = gt.view_state()
    assert gt.zoom_factor() > 1.0

    # the fill switch is gone and none of the four display switches
    # touches the view the user zoomed by hand
    assert not hasattr(page, "box_fill_check")
    page.label_check.setChecked(False)
    assert not gt.show_labels and not pred.show_labels
    assert gt.view_state() == pytest.approx(state, abs=1e-9)
    page.gt_check.setChecked(False)
    assert not gt.show_ground_truth
    assert gt.view_state() == pytest.approx(state, abs=1e-9)
    page.pred_check.setChecked(False)
    assert not pred.show_predictions
    assert gt.view_state() == pytest.approx(state, abs=1e-9)
    page.overlay_check.setChecked(False)
    assert not gt.show_overlay and not pred.show_overlay
    assert gt.view_state() == pytest.approx(state, abs=1e-9)
    for check in (
        page.label_check,
        page.gt_check,
        page.pred_check,
        page.overlay_check,
    ):
        check.setChecked(True)
    assert gt.show_labels and pred.show_labels
    assert gt.show_ground_truth and pred.show_predictions
    assert gt.show_overlay and pred.show_overlay
    assert gt.view_state() == pytest.approx(state, abs=1e-9)
    page.deleteLater()


def test_the_label_switch_changes_what_a_canvas_paints(qt_app):
    "Hiding the labels removes the text and leaves the rest alone."

    start, end = (100, 100), (200, 160)
    canvas = make_canvas()
    canvas.set_shapes([rect_shape(start, end)], [])
    labelled = render(canvas)
    corner = canvas.image_to_widget(
        QtCore.QPointF(float(start[0]), float(start[1]))
    )
    text = rect_of(text_mask(labelled))
    canvas.show_labels = False
    plain = render(canvas)

    assert text is not None
    assert text_mask(labelled).any()
    assert not text_mask(plain).any()
    assert not np.array_equal(labelled, plain)
    # the text is still above the corner of its box
    assert text[3] < corner.y()
    # the switch moves the text alone: the outline of the box is the
    # very same in both renders, and it really is painted
    outline = rect_of(box_mask(plain))
    assert outline is not None
    assert np.array_equal(box_mask(labelled), box_mask(plain))
    # the hide_labels reference render is exactly the plain one
    reference = make_canvas()
    reference.show_labels = False
    reference.set_shapes([rect_shape(start, end)], [])
    assert np.array_equal(plain, render(reference))
    # every pixel the switch moves lies above the box: the outline and
    # the inside of the box survive hiding the labels untouched
    different = np.any(
        labelled.astype(np.int16) != plain.astype(np.int16), axis=2
    )
    assert different.any()
    rows, _columns = np.nonzero(different)
    assert rows.max() <= int(corner.y())
    # and the inside of the box is the original picture either way
    inside_labelled, source_labelled = interior_pixels(
        canvas, labelled, start, end
    )
    inside_plain, source_plain = interior_pixels(canvas, plain, start, end)
    assert np.array_equal(inside_labelled, source_labelled)
    assert np.array_equal(inside_plain, source_plain)
    assert np.array_equal(inside_labelled, inside_plain)
