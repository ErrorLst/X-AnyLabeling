"""Shared fixtures for the edit extras tests.

Every scratch directory lives in the system temporary folder: the test
suite never creates anything inside the repository, and it never deletes
anything either. A finished scratch directory is moved into the temp
trash folder (TEMP/dsh-trash/<timestamp>-<name>) so that it stays
recoverable.
"""

import datetime
import os
import shutil
import tempfile
import uuid

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtCore, QtGui, QtWidgets

from anylabeling.views.labeling.widgets.canvas import Canvas

SCRATCH_PREFIX = "xal_ee_test_"
TRASH_DIRNAME = "dsh-trash"

#: Side of the square canvas used by the wheel tests.
CANVAS_SIZE = 200

#: One wheel notch, as reported by QWheelEvent.angleDelta.
WHEEL_NOTCH = 120


def _stamp() -> str:
    "Return a unique, sortable timestamp."

    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def _make_scratch() -> str:
    "Create a writable scratch directory in the system temp folder."

    # tempfile.mkdtemp is avoided on purpose: on Windows it creates the
    # folder with mode 0o700 and the sandbox of this workspace denies
    # every write inside such a folder, while os.makedirs stays usable.
    for _attempt in range(16):
        path = os.path.join(
            tempfile.gettempdir(),
            SCRATCH_PREFIX + _stamp() + "-" + uuid.uuid4().hex[:6],
        )
        try:
            os.makedirs(path, exist_ok=False)
        except FileExistsError:
            continue
        return path
    raise RuntimeError("could not allocate a scratch directory")


def _release_scratch(path: str) -> None:
    "Move a finished scratch directory into the temp trash folder."

    if not path or not os.path.isdir(path):
        return
    trash = os.path.join(tempfile.gettempdir(), TRASH_DIRNAME)
    try:
        os.makedirs(trash, exist_ok=True)
        target = os.path.join(trash, _stamp() + "-" + os.path.basename(path))
        shutil.move(path, target)
    except OSError:
        # The sandbox may refuse the move: the directory is then left in
        # place on purpose, this module never deletes anything.
        pass


@pytest.fixture
def ee_scratch():
    "System temp scratch directory holding the label file of one test."

    path = _make_scratch()
    try:
        yield path
    finally:
        _release_scratch(path)


@pytest.fixture(scope="session")
def qapp():
    "Return the shared offscreen QApplication."

    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])
    return app


@pytest.fixture(scope="session", autouse=True)
def _qt_application(qapp):
    "Give every test of this package a QApplication to build widgets."

    return qapp


@pytest.fixture
def make_canvas(qapp):
    "Return a factory building real canvases whose pixmap fits them."

    created = []

    def _make(size=CANVAS_SIZE, **kwargs):
        canvas = Canvas(parent=None, **kwargs)
        canvas.pixmap = QtGui.QPixmap(size, size)
        canvas.pixmap.fill(QtGui.QColor("black"))
        canvas.resize(size, size)
        created.append(canvas)
        return canvas

    yield _make
    for canvas in created:
        canvas.close()
    qapp.processEvents()


@pytest.fixture
def canvas(make_canvas):
    "One real canvas with a zero widget to image offset."

    return make_canvas()


@pytest.fixture
def send_wheel(qapp):
    "Return a helper delivering one QWheelEvent to a widget."

    def _send(widget, delta=(0, WHEEL_NOTCH), modifiers=None, position=None):
        if modifiers is None:
            modifiers = QtCore.Qt.KeyboardModifier.NoModifier
        if position is None:
            position = (50.0, 50.0)
        pos = QtCore.QPointF(*position)
        event = QtGui.QWheelEvent(
            pos,
            pos,
            QtCore.QPoint(),
            QtCore.QPoint(*delta),
            QtCore.Qt.MouseButton.NoButton,
            modifiers,
            QtCore.Qt.ScrollPhase.ScrollUpdate,
            False,
        )
        QtWidgets.QApplication.sendEvent(widget, event)
        return event

    return _send
