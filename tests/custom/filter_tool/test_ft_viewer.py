"""Tests of the linked original/preview image panes."""

from __future__ import annotations

from PyQt6 import QtCore

from anylabeling.custom.filter_tool.viewer import ImageView

from conftest import keep, make_pixmap, make_rgb


def _centre(view):
    point = view.mapToScene(view.viewport().rect().center())
    return QtCore.QPointF(point)


def _make_views():
    left = keep(ImageView())
    right = keep(ImageView())
    left.resize(320, 220)
    right.resize(320, 220)
    pixmap = make_pixmap(make_rgb(160, 120))
    left.set_pixmap(pixmap)
    right.set_pixmap(pixmap)
    left.sync_with(right)
    return left, right


def test_zoom_is_synchronized(qapp):
    left, right = _make_views()
    left.apply_zoom(1.5, QtCore.QPoint(120, 90))
    assert right.zoom_level() == left.zoom_level()
    assert right.transform() == left.transform()
    assert _centre(right).x() == _centre(left).x()
    assert _centre(right).y() == _centre(left).y()


def test_center_is_synchronized_after_pan(qapp):
    left, right = _make_views()
    left.apply_zoom(1.7)
    horizontal = left.horizontalScrollBar()
    vertical = left.verticalScrollBar()
    horizontal.setValue(horizontal.value() + 35)
    vertical.setValue(vertical.value() + 20)
    left._sync_to_peer()
    assert right.zoom_level() == left.zoom_level()
    assert right.transform() == left.transform()
    assert _centre(right).x() == _centre(left).x()
    assert _centre(right).y() == _centre(left).y()


def test_reset_is_synchronized(qapp):
    left, right = _make_views()
    left.apply_zoom(1.8)
    left.reset_view()
    assert right.zoom_level() == left.zoom_level()
    assert right.transform() == left.transform()
    assert _centre(right).x() == _centre(left).x()
    assert _centre(right).y() == _centre(left).y()
