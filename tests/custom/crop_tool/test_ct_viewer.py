"""Tests of the crop view: loading, the box, the marks and zoom."""

import os

import PIL.Image

from PyQt6 import QtCore, QtGui, QtWidgets

from anylabeling.custom.crop_tool import crop_core as core
from anylabeling.custom.crop_tool import viewer as ct_viewer

from conftest import write_pil

SRC_SIZE = (100, 60)
BOX = (20, 10)


def _view(max_pixels=None):
    """Return a view sized like a real window."""

    view = ct_viewer.CropView(max_pixels=max_pixels)
    view.resize(600, 400)
    return view


def _load(view, root, name="img.png", size=SRC_SIZE, mode="RGB"):
    """Write one image, load it and return its path."""

    path = write_pil(root, name, size, mode)
    assert view.load_image(path) is True
    return path


def _key_event(key, event_type=QtCore.QEvent.Type.KeyPress):
    """Build a key event like the widget layer would deliver it."""

    return QtGui.QKeyEvent(
        event_type, key, QtCore.Qt.KeyboardModifier.NoModifier
    )


class TestModuleConstants:
    """The overlay geometry is pinned by the module constants."""

    def test_zoom_constants(self):
        assert ct_viewer.ZOOM_STEP == 1.15
        assert ct_viewer.MIN_ZOOM == 1.0
        assert ct_viewer.MAX_ZOOM == 20.0

    def test_layer_constants(self):
        assert ct_viewer.CROP_PEN_COLOR == "#0a84ff"
        assert ct_viewer.MARK_PEN_COLOR == "#34c759"
        assert ct_viewer.PAD_FILL == (10, 132, 255, 60)
        assert (ct_viewer.BOX_Z, ct_viewer.PAD_Z, ct_viewer.MARK_Z) == (
            100.0,
            98.0,
            50.0,
        )


class TestLoadImage:
    """A source file is either shown or refused with a reason."""

    def test_real_image_is_loaded(self, ct_dir):
        view = _view()
        _load(view, ct_dir)
        assert view.has_image() is True
        assert view.image_size() == SRC_SIZE
        assert view.last_error() == ""

    def test_broken_file_is_reported(self, ct_dir):
        path = os.path.join(ct_dir, "bad.png")
        with open(path, "wb") as handle:
            handle.write(b"not an image")
        view = _view()
        assert view.load_image(path) is False
        assert view.has_image() is False
        assert view.last_error() != ""
        assert "bad.png" in view.last_error()

    def test_missing_file_is_reported(self, ct_dir):
        view = _view()
        assert view.load_image(os.path.join(ct_dir, "gone.png")) is False
        assert view.last_error() != ""

    def test_missing_file_does_not_clear_a_loaded_image(self, ct_dir):
        view = _view()
        path = _load(view, ct_dir, "keep.png", (30, 20))
        assert view.load_image(os.path.join(ct_dir, "gone.png")) is False
        assert view.image_size() == (30, 20)
        assert view.last_error() != ""
        assert path

    def test_source_size_wins_over_exif_orientation(self, ct_dir):
        """The view never applies the EXIF transform by itself."""

        path = os.path.join(ct_dir, "rotated.jpg")
        image = PIL.Image.new("RGB", (10, 4))
        exif = image.getexif()
        exif[274] = 6
        image.save(path, exif=exif)
        with PIL.Image.open(path) as opened:
            assert opened.size == (10, 4)
        view = _view()
        assert view.load_image(path) is True
        assert view.image_size() == (10, 4)

    def test_pixel_budget_can_be_injected(self, ct_dir):
        path = write_pil(ct_dir, "ten.png", (10, 10))
        view = _view(max_pixels=4)
        assert view.load_image(path) is False
        assert view.has_image() is False
        assert "图片过大" in view.last_error()

    def test_module_budget_is_used_by_default(self, ct_dir, monkeypatch):
        path = write_pil(ct_dir, "ten.png", (10, 10))
        monkeypatch.setattr(core, "MAX_IMAGE_PIXELS", 4)
        view = _view()
        assert view.load_image(path) is False
        assert "图片过大" in view.last_error()

    def test_clear_image_empties_the_view(self, ct_dir):
        view = _view()
        _load(view, ct_dir)
        view.set_marks([(0, 0, 4, 4)])
        view.clear_image()
        assert view.has_image() is False
        assert view.image_size() == (0, 0)
        assert view.mark_count() == 0


class TestCropBox:
    """The box is clamped inside the image and never clipped."""

    def test_default_size_is_one_pixel(self, ct_dir):
        view = _view()
        _load(view, ct_dir)
        assert view.crop_size() == (1, 1)

    def test_new_image_centers_the_box(self, ct_dir):
        view = _view()
        view.set_crop_size(*BOX)
        _load(view, ct_dir)
        expected_x = (SRC_SIZE[0] - BOX[0]) // 2
        expected_y = (SRC_SIZE[1] - BOX[1]) // 2
        assert view.crop_pos() == (expected_x, expected_y)

    def test_move_clamps_to_the_bottom_right(self, ct_dir):
        view = _view()
        _load(view, ct_dir)
        view.set_crop_size(*BOX)
        assert view.move_crop_to(1e9, 1e9) == (
            SRC_SIZE[0] - BOX[0],
            SRC_SIZE[1] - BOX[1],
        )

    def test_move_clamps_to_the_top_left(self, ct_dir):
        view = _view()
        _load(view, ct_dir)
        view.set_crop_size(*BOX)
        assert view.move_crop_to(-50, -50) == (0, 0)

    def test_move_rounds_the_coordinates(self, ct_dir):
        view = _view()
        _load(view, ct_dir)
        view.set_crop_size(*BOX)
        assert view.move_crop_to(3.4, 4.6) == (3, 5)

    def test_box_larger_than_the_image_is_pinned(self, ct_dir):
        view = _view()
        _load(view, ct_dir, "small.png", (8, 6))
        view.set_crop_size(100, 100)
        assert view.move_crop_to(20, 20) == (0, 0)

    def test_resize_keeps_the_corner_when_it_still_fits(self, ct_dir):
        view = _view()
        _load(view, ct_dir)
        view.set_crop_size(*BOX)
        view.move_crop_to(10, 20)
        view.set_crop_size(10, 10)
        assert view.crop_pos() == (10, 20)

    def test_resize_reclamps_the_corner(self, ct_dir):
        view = _view()
        _load(view, ct_dir)
        view.set_crop_size(10, 10)
        view.move_crop_to(1e9, 1e9)
        view.set_crop_size(30, 30)
        assert view.crop_pos() == (
            SRC_SIZE[0] - 30,
            SRC_SIZE[1] - 30,
        )

    def test_empty_view_has_no_corner(self, ct_dir):
        view = _view()
        assert view.move_crop_to(5, 5) == (0, 0)
        assert view.center_crop() == (0, 0)

    def test_size_is_never_zero(self, ct_dir):
        view = _view()
        _load(view, ct_dir)
        view.set_crop_size(0, -3)
        assert view.crop_size() == (1, 1)

    def test_padding_is_kept(self, ct_dir):
        view = _view()
        _load(view, ct_dir)
        view.set_pad(4, 5)
        assert view.pad_size() == (4, 5)
        view.set_pad(-1, -1)
        assert view.pad_size() == (0, 0)


class TestZoom:
    """The wheel multiplies a zoom relative to the fitted view."""

    def test_zoom_starts_fitted(self, ct_dir):
        view = _view()
        _load(view, ct_dir)
        assert view.zoom_level() == 1.0

    def test_one_step(self, ct_dir):
        view = _view()
        _load(view, ct_dir)
        assert view.apply_zoom(ct_viewer.ZOOM_STEP) == 1.15

    def test_upper_clamp(self, ct_dir):
        view = _view()
        _load(view, ct_dir)
        assert view.apply_zoom(100) == ct_viewer.MAX_ZOOM

    def test_below_the_fit_falls_back(self, ct_dir):
        view = _view()
        _load(view, ct_dir)
        view.apply_zoom(2.0)
        assert view.apply_zoom(0.1) == 1.0

    def test_bad_factor_is_ignored(self, ct_dir):
        view = _view()
        _load(view, ct_dir)
        assert view.apply_zoom("nope") == 1.0

    def test_reset_returns_to_the_fit(self, ct_dir):
        view = _view()
        _load(view, ct_dir)
        view.apply_zoom(3.0)
        view.reset_view()
        assert view.zoom_level() == 1.0

    def test_wheel_without_an_image_does_nothing(self, ct_dir):
        view = _view()
        event = QtGui.QWheelEvent(
            QtCore.QPointF(10.0, 10.0),
            QtCore.QPointF(10.0, 10.0),
            QtCore.QPoint(0, 0),
            QtCore.QPoint(0, 120),
            QtCore.Qt.MouseButton.NoButton,
            QtCore.Qt.KeyboardModifier.NoModifier,
            QtCore.Qt.ScrollPhase.NoScrollPhase,
            False,
        )
        view.wheelEvent(event)
        assert view.zoom_level() == 1.0

    def test_wheel_zooms_a_loaded_image(self, ct_dir):
        view = _view()
        _load(view, ct_dir)
        event = QtGui.QWheelEvent(
            QtCore.QPointF(10.0, 10.0),
            QtCore.QPointF(10.0, 10.0),
            QtCore.QPoint(0, 0),
            QtCore.QPoint(0, 120),
            QtCore.Qt.MouseButton.NoButton,
            QtCore.Qt.KeyboardModifier.NoModifier,
            QtCore.Qt.ScrollPhase.NoScrollPhase,
            False,
        )
        view.wheelEvent(event)
        assert view.zoom_level() == 1.15


class TestMarks:
    """The mark layer holds the rectangles of the existing crops."""

    def test_set_and_count(self, ct_dir):
        view = _view()
        _load(view, ct_dir)
        view.set_marks([(1, 2, 3, 4), (5, 6, 7, 8)])
        assert view.mark_count() == 2

    def test_degenerate_rectangles_are_dropped(self, ct_dir):
        view = _view()
        _load(view, ct_dir)
        view.set_marks([(1, 2, 0, 4), (1, 2, 3, 0), (3, 4, 5, 6)])
        assert view.mark_count() == 1

    def test_garbage_is_ignored(self, ct_dir):
        view = _view()
        _load(view, ct_dir)
        view.set_marks([None, "xx", ("a", "b", "c", "d"), (1, 1, 2, 2)])
        assert view.mark_count() == 1

    def test_clear_marks(self, ct_dir):
        view = _view()
        _load(view, ct_dir)
        view.set_marks([(1, 2, 3, 4)])
        view.clear_marks()
        assert view.mark_count() == 0

    def test_empty_argument_is_allowed(self, ct_dir):
        view = _view()
        _load(view, ct_dir)
        view.set_marks(None)
        assert view.mark_count() == 0


class TestCropSignal:
    """Only the right button asks for a crop."""

    def test_trigger_emits_the_box_corner(self, ct_dir):
        view = _view()
        _load(view, ct_dir)
        view.set_crop_size(*BOX)
        view.move_crop_to(7, 9)
        seen = []
        view.crop_requested.connect(
            lambda x, y: seen.append((x, y))
        )
        assert view.trigger_crop() is True
        assert seen == [(7, 9)]

    def test_trigger_without_an_image(self, ct_dir):
        view = _view()
        seen = []
        view.crop_requested.connect(
            lambda x, y: seen.append((x, y))
        )
        assert view.trigger_crop() is False
        assert seen == []

    def test_context_menu_crops(self, ct_dir):
        view = _view()
        _load(view, ct_dir)
        seen = []
        view.crop_requested.connect(
            lambda x, y: seen.append((x, y))
        )
        event = QtGui.QContextMenuEvent(
            QtGui.QContextMenuEvent.Reason.Mouse,
            QtCore.QPoint(5, 5),
            QtCore.QPoint(5, 5),
        )
        view.contextMenuEvent(event)
        assert len(seen) == 1
        assert event.isAccepted() is True

    def test_context_menu_without_an_image(self, ct_dir):
        view = _view()
        seen = []
        view.crop_requested.connect(
            lambda x, y: seen.append((x, y))
        )
        event = QtGui.QContextMenuEvent(
            QtGui.QContextMenuEvent.Reason.Mouse,
            QtCore.QPoint(5, 5),
            QtCore.QPoint(5, 5),
        )
        view.contextMenuEvent(event)
        assert seen == []
        assert event.isAccepted() is False


class TestKeyEscape:
    """The view leaves the shortcuts to the dialog."""

    def test_navigation_and_delete_keys_are_ignored(self, ct_dir):
        view = _view()
        _load(view, ct_dir)
        for key in (
            QtCore.Qt.Key.Key_Left,
            QtCore.Qt.Key.Key_Right,
            QtCore.Qt.Key.Key_Delete,
            QtCore.Qt.Key.Key_Escape,
        ):
            event = _key_event(key)
            view.keyPressEvent(event)
            assert event.isAccepted() is False, key

    def test_return_and_enter_are_not_consumed_either(self, ct_dir):
        """Return and Enter carry no crop meaning in the view."""

        view = _view()
        _load(view, ct_dir)
        for key in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter):
            event = _key_event(key)
            view.keyPressEvent(event)
            assert event.isAccepted() is False, key

    def test_other_keys_keep_their_default(self, ct_dir):
        view = _view()
        _load(view, ct_dir)
        event = _key_event(QtCore.Qt.Key.Key_A)
        view.keyPressEvent(event)
        assert event.isAccepted() is False

    def test_leave_event_reports_outside(self, ct_dir):
        view = _view()
        _load(view, ct_dir)
        seen = []
        view.cursor_moved.connect(lambda x, y: seen.append((x, y)))
        view.leaveEvent(QtCore.QEvent(QtCore.QEvent.Type.Leave))
        assert seen == [(-1, -1)]


class TestPainting:
    """Every overlay layer can be painted without raising."""

    def test_draw_foreground(self, ct_dir):
        view = _view()
        _load(view, ct_dir)
        view.set_crop_size(*BOX)
        view.set_pad(3, 2)
        view.set_marks([(0, 0, 5, 5)])
        image = QtGui.QImage(600, 400, QtGui.QImage.Format.Format_RGB32)
        painter = QtGui.QPainter(image)
        view.drawForeground(painter, QtCore.QRectF(0.0, 0.0, 100.0, 60.0))
        painter.end()
        assert view.mark_count() == 1

    def test_draw_without_an_image(self, ct_dir):
        view = _view()
        image = QtGui.QImage(60, 40, QtGui.QImage.Format.Format_RGB32)
        painter = QtGui.QPainter(image)
        view.drawForeground(painter, QtCore.QRectF(0.0, 0.0, 1.0, 1.0))
        painter.end()
        assert view.has_image() is False

    def test_empty_placeholder_is_a_child_of_the_viewport(self, ct_dir):
        view = _view()
        labels = view.viewport().findChildren(QtWidgets.QLabel)
        assert len(labels) == 1
        assert labels[0].text() == ct_viewer.EMPTY_TEXT
        assert (
            labels[0].testAttribute(
                QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents
            )
            is True
        )

    def test_placeholder_follows_the_viewport(self, ct_dir):
        view = _view()
        _load(view, ct_dir)
        view.resize(500, 300)
        labels = view.viewport().findChildren(QtWidgets.QLabel)
        assert labels[0].geometry() == view.viewport().rect()
