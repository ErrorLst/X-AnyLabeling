"""Shared fixtures for the yolo_to_labelme tests.

Every scratch directory lives in the system temporary folder: the suite
never creates anything inside the repository and it never deletes
anything. A finished scratch directory is moved into
<temp>/dsh-trash/<timestamp>-<name> so that it stays recoverable.

The scratch directories are built with os.makedirs, not with
tempfile.mkdtemp: the local sandbox refuses writes inside a mkdtemp
directory on this host, while an ordinary os.makedirs directory stays
writable.
"""

from __future__ import annotations

import datetime
import os
import os.path as osp
import shutil
import tempfile
import uuid
from types import SimpleNamespace

import PIL.Image
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtWidgets  # noqa: E402

SCRATCH_PREFIX = "xal_y2l_test_"
TRASH_DIRNAME = "dsh-trash"


def _stamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def _make_scratch() -> str:
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
    if not path or not osp.isdir(path):
        return
    trash = osp.join(tempfile.gettempdir(), TRASH_DIRNAME)
    os.makedirs(trash, exist_ok=True)
    target = osp.join(trash, _stamp() + "-" + osp.basename(path))
    try:
        shutil.move(path, target)
    except OSError:
        # The sandbox may refuse the move: the directory then stays in
        # place on purpose, this module never deletes anything.
        pass


@pytest.fixture
def scratch():
    path = _make_scratch()
    try:
        yield path
    finally:
        _release_scratch(path)


@pytest.fixture
def scratch_factory():
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


def _make_parents(path: str) -> None:
    parent = osp.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def write_image(root, name, size=(64, 48), color=(0, 0, 0)):
    """Write a real tiny image and return its path."""

    path = osp.join(root, name)
    _make_parents(path)
    PIL.Image.new("RGB", size, color).save(path)
    return path


def write_text(root, name, text):
    path = osp.join(root, name)
    _make_parents(path)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return path


def write_label(root, name, lines):
    body = "\n".join(lines)
    if body:
        body += "\n"
    return write_text(root, name, body)


def write_classes(root, names, name="classes.txt"):
    return write_text(root, name, "\n".join(names) + "\n")


class FakeWidget(SimpleNamespace):
    """Labeling widget stand in that only owns a Tool menu."""

    def __init__(self, menu=None):
        super().__init__()
        if menu is not None:
            self.menus = SimpleNamespace(tool=menu)


@pytest.fixture(scope="session")
def qapp():
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])
    return app


@pytest.fixture(scope="session", autouse=True)
def _qt_application(qapp):
    return qapp
