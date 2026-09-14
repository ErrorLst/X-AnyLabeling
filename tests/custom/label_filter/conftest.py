"""Shared fixtures for the label filter tests.

Every scratch directory lives in the system temporary folder: the
suite never creates anything inside the repository, and it never
deletes anything either. A finished scratch directory is moved into
the temp trash folder (<temp>/dsh-trash/<timestamp>-<name>) so that it
stays recoverable.

The widget stand-in of this package is built around the real
import_image_folder of LabelingWidget, bound to a SimpleNamespace that
carries the minimal state that method reads. The code under test then
works on the very same file list the application builds, which is the
part the filter rewrites.
"""

from __future__ import annotations

import datetime
import json
import os
import os.path as osp
import shutil
import tempfile
import uuid
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtGui, QtWidgets  # noqa: E402

import anylabeling.views.labeling.label_widget as label_widget  # noqa: E402

SCRATCH_PREFIX = "xal_lf_test_"
TRASH_DIRNAME = "dsh-trash"
PROBE_NAME = "write_probe.tmp"

SANDBOX_SKIP_REASON = (
    "the sandbox denies file writes inside a freshly created scratch "
    "folder; run these tests outside the restricted sandbox"
)


def _stamp() -> str:
    """Return a unique, sortable timestamp."""

    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def trash_root() -> str:
    """Return the temp trash folder of the suite."""

    return osp.join(tempfile.gettempdir(), TRASH_DIRNAME)


def _make_scratch() -> str:
    """Create a writable scratch directory in the temp folder."""

    for _attempt in range(16):
        path = osp.join(
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
    """Move a finished scratch directory into the temp trash."""

    if not path or not osp.isdir(path):
        return
    trash = trash_root()
    try:
        os.makedirs(trash, exist_ok=True)
        target = osp.join(trash, _stamp() + "-" + osp.basename(path))
        shutil.move(path, target)
    except OSError:
        # The sandbox may refuse the move: the directory is then left
        # in place on purpose, this module never deletes anything.
        pass


def _probe_writable(directory: str) -> None:
    """Skip a test whose scratch directory cannot be written to."""

    probe = osp.join(directory, PROBE_NAME)
    try:
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("probe")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        pytest.skip(SANDBOX_SKIP_REASON)
    try:
        trash = trash_root()
        os.makedirs(trash, exist_ok=True)
        shutil.move(
            probe,
            osp.join(trash, _stamp() + "-" + PROBE_NAME),
        )
    except OSError:
        pass


def write_image(root, name, payload=None):
    """Write a tiny stand-in image and return its path."""

    path = osp.join(root, name)
    data = payload if payload is not None else ("image:" + name).encode()
    with open(path, "wb") as handle:
        handle.write(data)
    return path


def write_json(root, name, shapes=None, image_path=None, raw=None):
    """Write a label file and return its path.

    With raw the exact bytes are written instead of a json document,
    which is how the broken json cases are built.
    """

    path = osp.join(root, name)
    if raw is not None:
        with open(path, "wb") as handle:
            handle.write(raw)
        return path
    data = {
        "version": "1.0",
        "flags": {},
        "shapes": list(shapes or []),
        "imagePath": image_path if image_path is not None else name,
        "imageData": None,
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    return path


def shape(label, points=None):
    """Return one rectangle shape with the given label."""

    return {
        "label": label,
        "points": points or [[0, 0], [10, 10]],
        "shape_type": "rectangle",
    }


def make_image(root, stem, labels=None, ext=".jpg", output_dir=None,
               raw=None):
    """Create one image and its json file.

    labels None writes a json without shapes, so the image is one of
    the background category; raw writes those exact bytes instead of
    a json document.
    """

    image = write_image(root, stem + ext)
    target = output_dir or root
    shapes = None if labels is None else [shape(name) for name in labels]
    path = write_json(
        target,
        stem + ".json",
        shapes=shapes,
        image_path=stem + ext,
        raw=raw,
    )
    return image, path


def read_text(path):
    """Return the text of a file."""

    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture
def lf_scratch():
    """System temp scratch directory holding the files of a test."""

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


@pytest.fixture
def lf_make_scratch():
    """Factory of scratch directories, released at the end."""

    paths = []

    def _make():
        path = _make_scratch()
        _probe_writable(path)
        paths.append(path)
        return path

    try:
        yield _make
    finally:
        for path in paths:
            _release_scratch(path)


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


def _bind(widget, name):
    """Bind a real LabelingWidget method to the fake widget."""

    method = getattr(label_widget.LabelingWidget, name)
    setattr(widget, name, MethodType(method, widget))


def bind_import(widget):
    """Bind the real import_image_folder to a stand-in widget.

    The suite is bound to the signature the wrapper of the label
    filter forwards (dirpath, pattern, load): a change upstream has to
    fail here, loudly, instead of silently losing the wrapper.
    """

    _bind(widget, "import_image_folder")
    bound = widget.import_image_folder
    assert bound.__func__ is label_widget.LabelingWidget.import_image_folder
    return widget


class Actions(SimpleNamespace):
    """Everything the upstream methods of the fake widget touch."""

    def __init__(self):
        super().__init__()
        for name in (
            "open_next_image",
            "open_prev_image",
            "open_next_unchecked_image",
            "open_prev_unchecked_image",
            "shape_manager",
            "edit_brush_mode",
        ):
            setattr(self, name, Mock())
        self.zoom_actions = []
        self.on_load_active = []


@pytest.fixture
def lf_widget(qapp):
    """Factory of fake labeling widgets holding a real file list."""

    created = []

    def _make(last_open_dir=None, output_dir=None, config=None,
              actions=None):
        file_list = QtWidgets.QListWidget()
        status_bar = Mock()
        widget = SimpleNamespace(
            file_list_widget=file_list,
            fn_to_index={},
            last_open_dir=last_open_dir,
            output_dir=output_dir,
            filename=None,
            _config=config if config is not None else {
                "exif_scan_enabled": False,
                "file_list_checkbox_editable": False,
            },
            # The real item builder paints a check icon per state.
            file_status_icons={
                True: QtGui.QIcon(),
                False: QtGui.QIcon(),
            },
            actions=actions if actions is not None else Actions(),
            # The real folder import asks the compare view whether it
            # is active first; a Mock returning a Mock would look like
            # "yes" and send the real method into the wrong branch.
            compare_view_manager=SimpleNamespace(
                is_active=Mock(return_value=False),
                close_compare_view=Mock(),
            ),
            async_exif_scanner=SimpleNamespace(start_scan=Mock()),
            may_continue=Mock(return_value=True),
            toggle_actions=Mock(),
            statusBar=Mock(return_value=status_bar),
            tr=lambda text, *args: text,
            load_file=Mock(),
        )
        widget.status_bar = status_bar
        bind_import(widget)
        _bind(widget, "_create_file_list_item")
        _bind(widget, "_label_file_checked")
        _bind(widget, "_set_file_item_checked")
        widget.open_next_image = Mock()
        created.append(widget)
        return widget

    yield _make
    for widget in created:
        try:
            widget.file_list_widget.close()
        except RuntimeError:
            pass
    qapp.processEvents()


def shown_message(widget):
    """Return the last message written into the status bar."""

    calls = widget.status_bar.showMessage.call_args_list
    if not calls:
        return None
    return calls[-1][0][0]
