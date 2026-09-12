"""Shared fixtures for the automatic empty label file tests.

Every scratch directory lives in the system temporary folder: the test
suite never creates anything inside the repository, and it never deletes
anything either. A finished scratch directory is moved into the temp
trash folder (TEMP/dsh-trash/<timestamp>-<name>) so that it stays
recoverable.

Two kinds of widget are built here. Most tests use a fake widget built
from the real widget methods (save_labels, _annotation_checked,
_set_file_item_checked, get_label_file, has_label_file), so the code
under test reads the same state the real widget exposes. Only the test
that has to show where the wrapper is installed builds a real
LabelingWidget, because the constructor is the mount point; see
make_real_widget.
"""

import contextlib
import datetime
import importlib.resources as pkg_resources
import os
import shutil
import tempfile
import uuid
from types import MethodType, SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtGui, QtWidgets

import anylabeling.services.auto_labeling.model_manager as model_manager
import anylabeling.views.labeling.label_widget as label_widget
from anylabeling import configs as anylabeling_configs
from anylabeling.views.labeling.widgets.label_list_widget import (
    LabelListWidget,
)

SCRATCH_PREFIX = "xal_elf_test_"
TRASH_DIRNAME = "dsh-trash"
PROBE_NAME = "write_probe.tmp"
CONFIG_NAME = "xanylabeling_config.yaml"

SANDBOX_SKIP_REASON = (
    "the sandbox denies file writes inside a freshly created scratch "
    "folder; run these tests outside the restricted sandbox"
)

#: Size of the image every fake widget reports.
IMAGE_WIDTH = 320
IMAGE_HEIGHT = 200


def _stamp() -> str:
    "Return a unique, sortable timestamp."

    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def trash_root() -> str:
    "Return the temp trash directory that keeps the finished scratch dirs."

    return os.path.join(tempfile.gettempdir(), TRASH_DIRNAME)


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
    trash = trash_root()
    try:
        os.makedirs(trash, exist_ok=True)
        target = os.path.join(trash, _stamp() + "-" + os.path.basename(path))
        shutil.move(path, target)
    except OSError:
        # The sandbox may refuse the move: the directory is then left in
        # place on purpose, this module never deletes anything.
        pass


def _probe_writable(directory: str) -> None:
    "Skip the test when the scratch directory cannot be written to."

    probe = os.path.join(directory, PROBE_NAME)
    try:
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("probe")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        pytest.skip(SANDBOX_SKIP_REASON)
    # The file is not deleted: it is moved into the temp trash folder, as
    # every other artefact of a test run.
    try:
        trash = trash_root()
        os.makedirs(trash, exist_ok=True)
        shutil.move(
            probe,
            os.path.join(trash, _stamp() + "-" + PROBE_NAME),
        )
    except OSError:
        pass


def write_image(path: str, size=(IMAGE_WIDTH, IMAGE_HEIGHT)) -> str:
    "Write a real PNG image of the given size and return its path."

    image = QtGui.QImage(size[0], size[1], QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor("white"))
    if not image.save(path, "PNG"):
        pytest.skip(SANDBOX_SKIP_REASON)
    return path


def bundled_config():
    "Return a fresh copy of the configuration the application ships with."

    with (
        pkg_resources.files(anylabeling_configs)
        .joinpath(CONFIG_NAME)
        .open(encoding="utf-8") as handle
    ):
        return yaml.safe_load(handle)


class FakeSettings:
    "In memory stand in for the QSettings of a real widget."

    def __init__(self, *args, **kwargs):
        self.values = {}

    def value(self, key, default=None, **kwargs):
        return self.values.get(key, default)

    def setValue(self, key, value):
        self.values[key] = value

    def sync(self):
        "Accept the sync the real settings offer."


def _bound(widget, name: str) -> None:
    "Bind a real LabelingWidget method to the fake widget."

    method = getattr(label_widget.LabelingWidget, name)
    setattr(widget, name, MethodType(method, widget))


@pytest.fixture
def elf_scratch():
    "System temp scratch directory holding the files of one test."

    path = _make_scratch()
    try:
        _probe_writable(path)
    except BaseException:
        _release_scratch(path)
        raise
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
def make_widget(qapp):
    "Return a factory building a fake labeling widget of one test."

    created = []

    def _make(
        filename,
        image_path=None,
        output_dir=None,
        dirty=False,
        label_file=None,
        other_data=None,
        canvas=None,
        actions=None,
    ):
        label_list = LabelListWidget()
        flag_widget = QtWidgets.QListWidget()
        file_list_widget = QtWidgets.QListWidget()
        widget = SimpleNamespace(
            filename=filename,
            image_path=filename if image_path is None else image_path,
            label_file=label_file,
            dirty=dirty,
            output_dir=output_dir,
            other_data=(
                {label_widget.CHECKED_FIELD: False}
                if other_data is None
                else other_data
            ),
            _config={"store_data": False},
            image_data=None,
            image=QtGui.QImage(
                IMAGE_WIDTH, IMAGE_HEIGHT, QtGui.QImage.Format.Format_RGB32
            ),
            # A freshly loaded image without annotation leaves a canvas
            # without shapes behind; a test can replace it to model the
            # canvas of an image that still shows shapes.
            canvas=(SimpleNamespace(shapes=[]) if canvas is None else canvas),
            # The real widget uses LabelListWidget, which is iterable and
            # is what save_labels walks over; a plain QListWidget raises
            # a TypeError there.
            label_list=label_list,
            flag_widget=flag_widget,
            file_list_widget=file_list_widget,
            actions=(
                SimpleNamespace(delete_file=Mock())
                if actions is None
                else actions
            ),
            tr=lambda text, *args: text,
            error_message=Mock(),
            status=Mock(),
        )
        for name in (
            "save_labels",
            "_annotation_checked",
            "_set_file_item_checked",
            "get_label_file",
            "has_label_file",
        ):
            _bound(widget, name)
        # The Qt views are tracked on their own: a test may replace the
        # label list of the widget to model a widget that holds shapes.
        created.append((widget, (label_list, flag_widget, file_list_widget)))
        return widget

    yield _make
    for widget, views in created:
        for view in views:
            try:
                view.close()
            except RuntimeError:
                # The C++ object may already be gone, which is fine here.
                pass
    qapp.processEvents()


@pytest.fixture
def make_real_widget(qapp):
    "Return a factory building a real labeling widget on shipped config."

    created = []

    def _make(image_path):
        queued = []
        host = QtWidgets.QMainWindow()
        # The widget reaches its menu bar and its status bar through the
        # parent of its parent, as it does in the application.
        host.parent = QtWidgets.QMainWindow()
        with contextlib.ExitStack() as stack:
            # The real widget reads and writes the configuration of the
            # user, which a test may not touch: the shipped default
            # configuration is patched in for the widget and for the
            # model manager it builds, and the window settings stay in
            # memory.
            stack.enter_context(
                patch.object(label_widget, "get_config", bundled_config)
            )
            stack.enter_context(
                patch.object(label_widget, "save_config", Mock())
            )
            stack.enter_context(
                patch.object(
                    model_manager,
                    "get_config",
                    lambda *args, **kwargs: {"custom_models": []},
                )
            )
            stack.enter_context(
                patch.object(model_manager, "save_config", Mock())
            )
            stack.enter_context(
                patch.object(label_widget.QtCore, "QSettings", FakeSettings)
            )
            # The constructor queues the first load, and capturing it is
            # what shows whether the mount point already ran.
            stack.enter_context(
                patch.object(
                    label_widget.LabelingWidget,
                    "queue_event",
                    lambda self, function: queued.append(function),
                )
            )
            widget = label_widget.LabelingWidget(
                parent=host, filename=image_path
            )
        real = SimpleNamespace(widget=widget, queued=queued, host=host)
        created.append(real)
        return real

    yield _make
    for real in created:
        real.widget.deleteLater()
        real.host.parent.deleteLater()
        real.host.deleteLater()
    qapp.processEvents()
