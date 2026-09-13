"""Shared fixtures for the rename tool tests.

Every scratch directory lives in the system temporary folder: the
suite never creates anything inside the repository, and it never
deletes anything either. A finished scratch directory is moved into
the temp trash folder (<temp>/dsh-trash/<timestamp>-<name>) so that it
stays recoverable.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import tempfile
import uuid
import zipfile
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtWidgets  # noqa: E402

SCRATCH_PREFIX = "xal_rename_test_"
TRASH_DIRNAME = "dsh-trash"


def _stamp() -> str:
    """Return a unique, sortable timestamp."""

    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def _make_scratch() -> str:
    """Create a writable scratch directory in the system temp folder."""

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
    """Move a finished scratch directory into the temp trash folder."""

    if not path or not os.path.isdir(path):
        return
    trash = os.path.join(tempfile.gettempdir(), TRASH_DIRNAME)
    try:
        os.makedirs(trash, exist_ok=True)
        target = os.path.join(trash, _stamp() + "-" + os.path.basename(path))
        shutil.move(path, target)
    except OSError:
        # The sandbox may refuse the move: the directory is then left
        # in place on purpose, this module never deletes anything.
        pass


@pytest.fixture
def rt_dataset():
    """Empty scratch directory that stands in for the source folder."""

    path = _make_scratch()
    try:
        yield path
    finally:
        _release_scratch(path)


@pytest.fixture
def rt_make():
    """Factory of scratch directories, all released at the end."""

    paths = []

    def _make():
        path = _make_scratch()
        paths.append(path)
        return path

    try:
        yield _make
    finally:
        for path in paths:
            _release_scratch(path)


@pytest.fixture
def rt_out():
    """Empty scratch directory that stands in for the output folder."""

    path = _make_scratch()
    try:
        yield path
    finally:
        _release_scratch(path)


@pytest.fixture
def rt_parent():
    """Return the (parent, dataset) pair the dialog writes into.

    The dialog always exports next to the dataset folder, so a test
    needs a scratch folder that holds the source folder itself: the
    zip then lands in the returned parent folder, next to the
    ``data`` source folder.
    """

    path = _make_scratch()
    data = os.path.join(path, "data")
    os.makedirs(data)
    try:
        yield path, data
    finally:
        _release_scratch(path)


def write_image(root, name, payload=None):
    """Write a tiny stand in image and return its path."""

    path = os.path.join(root, name)
    data = payload if payload is not None else ("image:" + name).encode()
    with open(path, "wb") as handle:
        handle.write(data)
    return path


def write_json(root, name, shapes=None, image_path=None, extra=None):
    """Write a label file and return its path."""

    path = os.path.join(root, name)
    data = {
        "version": "1.0",
        "flags": {},
        "shapes": list(shapes or []),
        "imagePath": image_path if image_path is not None else name,
        "imageData": None,
    }
    if extra:
        data.update(extra)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    return path


def make_pair(root, stem, label, ext=".jpg", shapes=None):
    """Create one image/json pair whose dominant label is label."""

    write_image(root, stem + ext)
    if shapes is None:
        shapes = [
            {"label": label, "points": [[0, 0]], "shape_type": "rectangle"}
        ]
    write_json(root, stem + ".json", shapes=shapes, image_path=stem + ext)
    return stem


def snapshot(root):
    """Return {name: (size, mtime_ns, sha256)} of every top level file."""

    result = {}
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as handle:
            payload = handle.read()
        info = os.stat(path)
        result[name] = (
            info.st_size,
            info.st_mtime_ns,
            hashlib.sha256(payload).hexdigest(),
        )
    return result


def read_zip(path):
    """Return {entry name: bytes} of an archive."""

    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def zip_doc(path, name):
    """Return the parsed json entry of an archive."""

    with zipfile.ZipFile(path) as archive:
        return json.loads(archive.read(name).decode("utf-8"))


class FakeWidget(SimpleNamespace):
    """Labeling widget stand in that only owns a Tool menu."""

    def __init__(self, menu=None):
        super().__init__()
        if menu is not None:
            self.menus = SimpleNamespace(tool=menu)


@pytest.fixture(scope="session")
def qapp():
    """Return the shared offscreen QApplication."""

    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])
    return app


@pytest.fixture(scope="session", autouse=True)
def _qt_application(qapp):
    """Give every test of this package a QApplication to build widgets."""

    return qapp
