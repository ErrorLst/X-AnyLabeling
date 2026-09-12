"""Shared fixtures for the smudge tool tests.

Every scratch directory lives in the system temporary folder: the suite
never creates anything inside the repository, and it never deletes
anything either. A finished scratch directory is moved into
``%TEMP%/dsh-trash`` so that it stays recoverable.

The Qt tests run on the offscreen platform and use a real ``Canvas``,
because the tool talks to the canvas through an event filter, while the
labeling widget itself is replaced by a small stand in: building a real
``LabelingWidget`` would pull in the model manager, the settings dialog
and the whole application, none of which the tool touches.
"""

import datetime
import io
import os
import shutil
import tempfile
import uuid
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import PIL.Image  # noqa: E402
from PyQt6 import QtCore, QtGui, QtWidgets  # noqa: E402

from anylabeling.custom.smudge_tool import install_smudge_tool  # noqa: E402
from anylabeling.views.labeling.utils.image import (  # noqa: E402
    img_data_to_pil,
)
from anylabeling.views.labeling.widgets.canvas import (  # noqa: E402
    Canvas,
)
from anylabeling.views.labeling.widgets.canvas_adjustment import (  # noqa
    BrightnessContrastProcessor,
)

SCRATCH_PREFIX = "xal_smudge_test_"
TRASH_DIRNAME = "dsh-trash"

#: Size of the pixmap of the canvas used by the Qt tests.
IMAGE_SIZE = (100, 80)

def _stamp():
    "Return a unique, sortable timestamp."

    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def _make_scratch():
    "Create a writable scratch directory in the system temp folder."

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


def _release_scratch(path):
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
def st_scratch():
    "System temp scratch directory of one test."

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
def st_image():
    "A written and decoded RGB image of the size of the test canvas."

    width, height = IMAGE_SIZE
    grid = np.indices((height, width))
    data = np.zeros((height, width, 3), np.uint8)
    data[:, :, 0] = (grid[1] * 2) % 256
    data[:, :, 1] = (grid[0] * 3) % 256
    data[:, :, 2] = ((grid[0] + grid[1]) * 5) % 256
    return data


@pytest.fixture
def st_pixmap(st_image):
    "A QPixmap of the test image."

    height, width = st_image.shape[:2]
    buffer = st_image.tobytes()
    qimage = QtGui.QImage(
        buffer,
        width,
        height,
        width * 3,
        QtGui.QImage.Format.Format_RGB888,
    )
    return QtGui.QPixmap.fromImage(qimage.copy())


@pytest.fixture
def st_canvas(qapp, st_pixmap):
    "A real canvas, one image pixel per widget pixel."

    host = QtWidgets.QWidget()
    canvas = Canvas(parent=host)
    canvas.move(0, 0)
    canvas.resize(st_pixmap.width(), st_pixmap.height())
    canvas.pixmap = st_pixmap
    canvas.scale = 1.0
    canvas.setEnabled(True)
    host.resize(st_pixmap.width(), st_pixmap.height())
    # The host has to be on screen, otherwise every visibility assertion
    # of the tests would be true for free, whether the preview widget is
    # shown or not.
    host.show()
    qapp.processEvents()
    yield canvas
    host.close()
    qapp.processEvents()


@pytest.fixture
def st_png(st_scratch, st_image):
    "The test image stored in a PNG file inside the scratch directory."

    path = os.path.join(st_scratch, "case.png")
    PIL.Image.fromarray(st_image, "RGB").save(path)
    return path


def _image_bytes(path):
    "Return the bytes of an image file."

    with open(path, "rb") as handle:
        return handle.read()


@pytest.fixture
def make_widget(qapp, st_canvas, st_png):
    "Build a stand in labeling widget carrying a real canvas."

    created = []

    def _make(image_path=st_png, filename=None, image_data=None):
        if image_data is None and os.path.isfile(image_path):
            image_data = _image_bytes(image_path)
        data = image_data
        board = st_canvas
        host = board.parentWidget()
        tools = QtWidgets.QToolBar("Tools", host)
        status_bar = QtWidgets.QStatusBar(host)
        processor = BrightnessContrastProcessor()
        processor.update_image(img_data_to_pil(data))
        widget = SimpleNamespace(
            canvas=board,
            tools=tools,
            host=host,
            filename=filename or image_path,
            image_path=image_path,
            image_data=data,
            image=None,
            dirty=False,
            brightness_contrast_values={},
            brightness_contrast_processor=processor,
        )
        widget.messages = []
        widget.errors = []
        widget.status = widget.messages.append
        widget.repopulations = []

        def error_message(title, message):
            widget.errors.append((title, message))

        def statusBar():
            return status_bar

        def import_image_folder(dirpath, pattern=None, load=True):
            del dirpath, pattern, load
            widget.folder_calls.append(1)
            return None

        def populate_mode_actions():
            widget.repopulations.append(1)
            tools.clear()
            return None

        widget.folder_calls = []
        widget.error_message = error_message
        widget.statusBar = statusBar
        widget.import_image_folder = import_image_folder
        widget.populate_mode_actions = populate_mode_actions
        widget.status_bar = status_bar
        created.append((widget, tools, status_bar, processor))
        return widget

    yield _make
    for widget, tools, status_bar, processor in created:
        processor.clear_image()
        tools.clear()
        tools.setParent(None)
        status_bar.setParent(None)
    created.clear()
    qapp.processEvents()


@pytest.fixture
def st_tool(make_widget):
    "A stand in widget with the smudge tool installed on it."

    widget = make_widget()
    controller = install_smudge_tool(widget)
    return SimpleNamespace(widget=widget, controller=controller)


def image_bytes(pil_image):
    "Return the PNG bytes of a PIL image."

    buffer = io.BytesIO()
    pil_image.save(buffer, format="PNG")
    return buffer.getvalue()


def send_mouse(
    canvas, kind, position, button=QtCore.Qt.MouseButton.LeftButton
):
    "Deliver one mouse event to a widget and return it."

    point = QtCore.QPointF(*position)
    buttons = QtCore.Qt.MouseButton.NoButton
    if kind != QtCore.QEvent.Type.MouseButtonRelease:
        buttons = button
    event = QtGui.QMouseEvent(
        kind,
        point,
        point,
        button,
        buttons,
        QtCore.Qt.KeyboardModifier.NoModifier,
    )
    QtWidgets.QApplication.sendEvent(canvas, event)
    return event


def send_key(canvas, key, modifiers=QtCore.Qt.KeyboardModifier.NoModifier):
    "Deliver one key press to a widget and return it."

    event = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, key, modifiers)
    QtWidgets.QApplication.sendEvent(canvas, event)
    return event


def press(canvas, position, button=QtCore.Qt.MouseButton.LeftButton):
    "Deliver a mouse press."

    return send_mouse(
        canvas, QtCore.QEvent.Type.MouseButtonPress, position, button
    )


def move(canvas, position):
    "Deliver a mouse move."

    return send_mouse(
        canvas,
        QtCore.QEvent.Type.MouseMove,
        position,
        QtCore.Qt.MouseButton.NoButton,
    )


def release(canvas, position, button=QtCore.Qt.MouseButton.LeftButton):
    "Deliver a mouse release."

    return send_mouse(
        canvas, QtCore.QEvent.Type.MouseButtonRelease, position, button
    )


def drag(canvas, start, end, button=QtCore.Qt.MouseButton.LeftButton):
    "Deliver a full press, move, release sequence."

    press(canvas, start, button)
    move(canvas, end)
    release(canvas, end, button)
