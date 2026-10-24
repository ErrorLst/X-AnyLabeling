"""Tests of the view components: overlay, view, list and categories.

Every widget is built offscreen and thrown away at the end of its
test; the images come from the shared conftest, so nothing is written
inside the repository.
"""

import os
from types import SimpleNamespace

from PyQt6 import QtCore, QtGui

from anylabeling.custom.preview_tool import category_filter, list_panel
from anylabeling.custom.preview_tool import overlay, viewer
from anylabeling.custom.preview_tool.core import BACKGROUND_LABEL, ShapeInfo

from conftest import write_image

TIMES = "\u00d7"


def _shape(
    label="cat",
    score=None,
    shape_type="rectangle",
    points=((10.0, 10.0), (20.0, 20.0)),
    width=10.0,
    height=10.0,
):
    """Build one core.ShapeInfo without a disk."""

    return ShapeInfo(
        label=label,
        score=score,
        shape_type=shape_type,
        points=tuple(points),
        width=width,
        height=height,
    )


def _drop_event(*paths):
    """Build a stand in drop event carrying local paths."""

    mime = QtCore.QMimeData()
    mime.setUrls([QtCore.QUrl.fromLocalFile(path) for path in paths])
    state = {"accepted": False, "ignored": False}
    event = SimpleNamespace(
        mimeData=lambda: mime,
        acceptProposedAction=lambda: state.update(accepted=True),
        ignore=lambda: state.update(ignored=True),
    )
    return event, state


# --------------------------------------------------------------- overlay


def test_expand_rect_with_zero_and_with_a_gap():
    points = [(10.0, 10.0), (20.0, 20.0)]
    plain = [(point.x(), point.y())
             for point in overlay.expand_rect(points, 0)]
    assert plain == [
        (10.0, 10.0),
        (20.0, 10.0),
        (20.0, 20.0),
        (10.0, 20.0),
    ]
    grown = [(point.x(), point.y())
             for point in overlay.expand_rect(points, 5)]
    assert grown == [
        (5.0, 5.0),
        (25.0, 5.0),
        (25.0, 25.0),
        (5.0, 25.0),
    ]


def test_shape_text_uses_a_dash_without_a_score():
    assert overlay.shape_text(_shape(score=None)) == (
        "cat - 10" + TIMES + "10px"
    )
    assert overlay.shape_text(_shape(score=0.5)) == (
        "cat 0.50 10" + TIMES + "10px"
    )
    assert overlay.shape_text(_shape(score="?")) == (
        "cat - 10" + TIMES + "10px"
    )


def test_layer_builds_one_outline_and_one_text_per_shape():
    layer = overlay.AnnotationLayer()
    layer.build([_shape(), _shape(shape_type="polygon")], 0)
    assert len(layer.rects()) == 2
    assert len(layer.texts()) == 2
    layer.clear()
    assert layer.rects() == []
    assert layer.texts() == []
    assert layer.shapes() == ()


def test_layer_grows_only_the_rectangle():
    layer = overlay.AnnotationLayer()
    layer.build([_shape()], 0)
    before = layer.rects()[0]
    layer.set_expand_px(5)
    after = layer.rects()[0]
    assert after.width() == before.width() + 10.0
    assert after.height() == before.height() + 10.0

    layer.build([_shape(shape_type="rotation")], 0)
    before = layer.rects()[0]
    layer.set_expand_px(9)
    after = layer.rects()[0]
    assert after.width() == before.width()
    assert after.height() == before.height()


def test_layer_hides_and_shows():
    layer = overlay.AnnotationLayer()
    layer.set_visible(False)
    assert layer.isVisible() is False
    layer.set_visible(True)
    assert layer.isVisible() is True


# ------------------------------------------------------------------ view


def test_view_shows_an_image_and_clears_it(pt_dir):
    path = write_image(pt_dir, "a.png", size=(12, 8))
    view = viewer.PreviewView()
    try:
        assert view.has_image() is False
        assert view.zoom_level() == 1.0
        assert view.show_image(QtGui.QImage(path), (_shape(),)) is True
        assert view.has_image() is True
        assert view.image_size() == (12, 8)
        assert len(view.annotation_layer().rects()) == 1
        view.clear()
        assert view.has_image() is False
        assert view.image_size() == (0, 0)
        assert view.last_error() == ""
    finally:
        view.deleteLater()


def test_view_reports_a_missing_image(pt_dir):
    view = viewer.PreviewView()
    try:
        assert view.show_image(QtGui.QImage()) is False
        view.show_error("boom.png")
        assert view.has_image() is False
        assert view.last_error() == "boom.png"
    finally:
        view.deleteLater()


def test_view_expand_and_annotation_visibility(pt_dir):
    view = viewer.PreviewView()
    try:
        view.set_expand_px(7)
        assert view.expand_px() == 7.0
        view.set_annotations([_shape()])
        rect = view.annotation_layer().rects()[0]
        assert rect.width() == 24.0
        view.set_annotations_visible(False)
        assert view.annotations_visible() is False
        view.set_annotations_visible(True)
        assert view.annotations_visible() is True
    finally:
        view.deleteLater()


def test_view_zoom_is_clamped_and_reset(pt_dir):
    path = write_image(pt_dir, "a.png", size=(40, 30))
    view = viewer.PreviewView()
    try:
        view.show_image(QtGui.QImage(path))
        assert view.apply_zoom(2.0) > 1.0
        view.apply_zoom(viewer.MAX_ZOOM * 10)
        assert view.zoom_level() <= viewer.MAX_ZOOM
        view.reset_view()
        assert view.zoom_level() == 1.0
        assert view.apply_zoom(0.1) == 1.0
    finally:
        view.deleteLater()


def test_a_same_size_switch_keeps_the_zoom_and_the_position(
    qapp, pt_dir
):
    first = write_image(pt_dir, "first.png", size=(800, 600))
    second = write_image(pt_dir, "second.png", size=(800, 600))
    view = viewer.PreviewView()
    try:
        view.resize(600, 400)
        view.show()
        qapp.processEvents()
        view.show_image(QtGui.QImage(first))
        qapp.processEvents()
        view.apply_zoom(2.0, QtCore.QPoint(450, 300))
        qapp.processEvents()
        horizontal = view.horizontalScrollBar()
        vertical = view.verticalScrollBar()
        horizontal.setValue(horizontal.value() + 37)
        vertical.setValue(vertical.value() + 21)
        qapp.processEvents()
        saved = (
            view.zoom_level(),
            horizontal.value(),
            vertical.value(),
            view.mapToScene(view.viewport().rect().center()),
        )

        view.show_image(QtGui.QImage(second))
        qapp.processEvents()

        center = view.mapToScene(view.viewport().rect().center())
        assert view.image_size() == (800, 600)
        assert view.zoom_level() == saved[0]
        assert horizontal.value() == saved[1]
        assert vertical.value() == saved[2]
        assert abs(center.x() - saved[3].x()) < 0.01
        assert abs(center.y() - saved[3].y()) < 0.01
    finally:
        view.deleteLater()


def test_a_size_change_fits_the_image_again(qapp, pt_dir):
    big = write_image(pt_dir, "big.png", size=(800, 600))
    small = write_image(pt_dir, "small.png", size=(400, 300))
    view = viewer.PreviewView()
    try:
        view.resize(600, 400)
        view.show()
        qapp.processEvents()
        view.show_image(QtGui.QImage(big))
        qapp.processEvents()
        view.apply_zoom(2.0, QtCore.QPoint(450, 300))
        qapp.processEvents()

        view.show_image(QtGui.QImage(small))
        qapp.processEvents()

        center = view.mapToScene(view.viewport().rect().center())
        assert view.image_size() == (400, 300)
        assert view.zoom_level() == 1.0
        assert view.horizontalScrollBar().value() == 0
        assert view.verticalScrollBar().value() == 0
        assert abs(center.x() - 200.0) < 1.0
        assert abs(center.y() - 150.0) < 1.0
    finally:
        view.deleteLater()


def _wheel_event(position, delta=120):
    """Build a wheel event at one viewport position."""

    point = QtCore.QPointF(position)
    return QtGui.QWheelEvent(
        point,
        point,
        QtCore.QPoint(0, 0),
        QtCore.QPoint(0, delta),
        QtCore.Qt.MouseButton.NoButton,
        QtCore.Qt.KeyboardModifier.NoModifier,
        QtCore.Qt.ScrollPhase.NoScrollPhase,
        False,
    )


def test_the_wheel_zoom_keeps_the_point_under_the_pointer(qapp, pt_dir):
    path = write_image(pt_dir, "big.png", size=(800, 600))
    view = viewer.PreviewView()
    try:
        view.resize(600, 400)
        view.show()
        qapp.processEvents()
        view.show_image(QtGui.QImage(path))
        position = QtCore.QPoint(450, 300)
        view.apply_zoom(2.0, position)
        for _step in range(3):
            before = view.mapToScene(position)
            event = _wheel_event(position)
            view.wheelEvent(event)
            after = view.mapToScene(position)
            assert event.isAccepted() is True
            assert abs(after.x() - before.x()) < 1.0
            assert abs(after.y() - before.y()) < 1.0
    finally:
        view.deleteLater()


def test_view_toggles_the_background(pt_dir):
    view = viewer.PreviewView()
    try:
        assert view.plain_background() is False
        assert view.toggle_background() is True
        assert view.toggle_background() is False
    finally:
        view.deleteLater()


def test_the_components_leave_the_style_to_the_application():
    """No component carries a style sheet of its own any more."""

    panel = list_panel.PreviewFileTree()
    button = category_filter.CategoryFilterButton()
    view = viewer.PreviewView()
    try:
        assert panel.styleSheet() == ""
        assert button.styleSheet() == ""
        assert view.styleSheet() == ""
        assert view.backgroundBrush().style() == QtCore.Qt.BrushStyle.NoBrush
        view.toggle_background()
        assert view.backgroundBrush().style() != QtCore.Qt.BrushStyle.NoBrush
        view.toggle_background()
        assert view.backgroundBrush().style() == QtCore.Qt.BrushStyle.NoBrush
    finally:
        for widget in (panel, button, view):
            widget.deleteLater()


def test_view_accepts_only_a_dropped_directory(pt_dir):
    view = viewer.PreviewView()
    try:
        path = write_image(pt_dir, "a.png", size=(4, 4))
        event, state = _drop_event(path)
        view.dragEnterEvent(event)
        assert state == {"accepted": False, "ignored": True}
        event, state = _drop_event(pt_dir)
        view.dragEnterEvent(event)
        assert state == {"accepted": True, "ignored": False}
        dropped = []
        view.directory_dropped.connect(dropped.append)
        event, state = _drop_event(pt_dir)
        view.dropEvent(event)
        assert [os.path.normpath(path) for path in dropped] == [
            os.path.normpath(pt_dir)
        ]
    finally:
        view.deleteLater()


# ------------------------------------------------------------- file tree


def test_file_tree_accepts_only_directories(pt_dir):
    panel = list_panel.PreviewFileTree()
    try:
        path = write_image(pt_dir, "a.png", size=(4, 4))
        event, state = _drop_event(path)
        panel.dragEnterEvent(event)
        assert state == {"accepted": False, "ignored": True}
        event, state = _drop_event(pt_dir)
        panel.dragEnterEvent(event)
        assert state == {"accepted": True, "ignored": False}
        dropped = []
        panel.directory_dropped.connect(dropped.append)
        event, state = _drop_event(pt_dir)
        panel.dropEvent(event)
        assert [os.path.normpath(path) for path in dropped] == [
            os.path.normpath(pt_dir)
        ]
    finally:
        panel.deleteLater()


def test_file_tree_refuses_drops_on_demand(pt_dir):
    panel = list_panel.PreviewFileTree()
    try:
        panel.ignore_drops(True)
        event, state = _drop_event(pt_dir)
        panel.dragEnterEvent(event)
        assert state["ignored"] is True
        panel.ignore_drops(False)
        event, state = _drop_event(pt_dir)
        panel.dragEnterEvent(event)
        assert state["accepted"] is True
    finally:
        panel.deleteLater()


# ------------------------------------------------------- category button


def test_category_button_labels_and_checked_state():
    button = category_filter.CategoryFilterButton()
    try:
        assert button.text() == category_filter.ALL_TEXT
        assert button.checked() is None
        button.set_categories(["dog", "cat"])
        assert button.categories()[0] == BACKGROUND_LABEL
        assert button.categories() == [BACKGROUND_LABEL, "dog", "cat"]
        assert button.text() == category_filter.ALL_TEXT
        assert button.checked() is None
        assert button.is_checked(BACKGROUND_LABEL) is True
        button._boxes["cat"].setChecked(False)
        assert button.text() == "2/3"
        assert button.checked() == frozenset({BACKGROUND_LABEL, "dog"})
        button._clear_all()
        assert button.text() == category_filter.NONE_TEXT
        assert button.checked() == frozenset()
        button._select_all()
        assert button.checked() is None
    finally:
        button.deleteLater()


def test_category_button_emits_changed_and_keeps_the_state():
    button = category_filter.CategoryFilterButton()
    try:
        button.set_categories(["cat", "dog"])
        seen = []
        button.changed.connect(lambda: seen.append(1))
        button._boxes["cat"].setChecked(False)
        assert seen == [1]
        button.set_categories(["cat", "dog", "bird"])
        assert button.is_checked("cat") is False
        assert button.is_checked("bird") is True
        button.set_categories(["cat", "dog"], checked=["dog"])
        assert button.is_checked("dog") is True
        assert button.is_checked("cat") is False
        assert button.is_checked(BACKGROUND_LABEL) is False
        button.set_categories(["cat"], checked=[])
        assert button.is_checked(BACKGROUND_LABEL) is True
        assert button.checked() is None
        button.reset()
        assert button.categories() == []
        assert button.checked() is None
        assert button.text() == category_filter.ALL_TEXT
    finally:
        button.deleteLater()
