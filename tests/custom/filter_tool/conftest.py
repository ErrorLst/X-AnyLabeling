"""Shared fixtures of the filter tool tests.

The suite is headless, writes nothing and deletes nothing: every test
builds its own QImage arrays in memory. Qt objects that take part in a
reference cycle are kept in ALIVE for the whole session because the Qt
layer aborts the interpreter when a cycle is collected in the middle of
a run.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtGui, QtWidgets  # noqa: E402


ALIVE = []


def keep(*objects):
    """Keep Qt objects alive for the rest of the session."""

    ALIVE.extend(objects)
    return objects[-1] if objects else None


def make_rgb(width=16, height=12):
    """Return a deterministic RGB gradient."""

    yy, xx = np.mgrid[0:height, 0:width]
    red = (xx * 17 + yy * 3) % 256
    green = (xx * 5 + yy * 29) % 256
    blue = (xx * 11 + yy * 7) % 256
    return np.dstack((red, green, blue)).astype(np.uint8)


def make_image(rgb, alpha=None, device_pixel_ratio=1.0):
    """Build a QImage from an RGB array."""

    from anylabeling.custom.filter_tool.display import rgba_to_qimage

    return rgba_to_qimage(rgb, alpha, device_pixel_ratio)


def make_pixmap(rgb, alpha=None, device_pixel_ratio=1.0):
    """Build a QPixmap from an RGB array."""

    return QtGui.QPixmap.fromImage(
        make_image(rgb, alpha, device_pixel_ratio)
    )


class RecordingStatusBar(QtWidgets.QStatusBar):
    """QStatusBar that records the messages it is given."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.messages = []

    def showMessage(self, text, *args, **kwargs):
        self.messages.append(str(text))
        return super().showMessage(text, *args, **kwargs)


class FakeCanvas(QtWidgets.QWidget):
    """A canvas stand in with the three members the hook uses."""

    def __init__(self, image=None):
        super().__init__()
        self.pixmap = QtGui.QPixmap()
        if image is not None:
            self.pixmap = QtGui.QPixmap.fromImage(image)
        self.compare_pixmap = None
        self.seen = []
        self.updates = 0

    def paintEvent(self, event):
        self.seen.append((self.pixmap, self.compare_pixmap))

    def update(self):
        self.updates += 1
        return super().update()


class FakeWidget(QtWidgets.QWidget):
    """A labeling widget stand in for the dialog and the hook."""

    def __init__(
        self,
        image=None,
        filename="sample.png",
        with_canvas=True,
        with_menu=True,
    ):
        super().__init__()
        self.canvas = FakeCanvas(image) if with_canvas else None
        self.image = image if image is not None else QtGui.QImage()
        self.filename = filename
        tool = QtWidgets.QMenu(self) if with_menu else None
        self.menus = SimpleNamespace(tool=tool)
        self._status_bar = RecordingStatusBar(self)

    def statusBar(self):
        return self._status_bar


@pytest.fixture(scope="session")
def qapp():
    """Return the shared offscreen QApplication."""

    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])
    return app


@pytest.fixture(scope="session", autouse=True)
def _qt_application(qapp):
    """Give every test of this package a QApplication."""

    return qapp
