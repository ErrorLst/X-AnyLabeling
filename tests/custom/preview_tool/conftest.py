"""Shared fixtures of the preview tool tests.

Every scratch directory lives in the system temporary folder: the suite
never creates anything inside the repository and it never deletes
anything either - a finished directory is moved into the temp trash
folder (<temp>/dsh-trash/<timestamp>-<name>) so that it stays
recoverable. The settings wrapper is always handed an INI file of its
own, so no test can touch the store of the user.

The factories of this module are plain functions, so a test file can
import them (from conftest import write_image). The fixtures of the
same name return these factories, so a test that prefers injection can
simply ask for them as an argument instead.
"""

import datetime
import json
import os
import shutil
import tempfile
import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtCore, QtGui, QtWidgets  # noqa: E402

from anylabeling.custom.preview_tool.settings import (  # noqa: E402
    PreviewSettings,
)

try:  # the image helpers fall back to a QImage when PIL is absent
    from PIL import Image as PILImage
except ImportError:  # pragma: no cover
    PILImage = None

SCRATCH_PREFIX = "xal_pt_test_"
TRASH_DIRNAME = "dsh-trash"


def _stamp() -> str:
    """Return a unique, sortable timestamp."""

    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def _make_scratch() -> str:
    """Create a directory in the system temp folder, never in the repo."""

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


def release_scratch(path: str) -> None:
    """Move a finished scratch directory into the temp trash folder."""

    if not path or not os.path.isdir(path):
        return
    trash = os.path.join(tempfile.gettempdir(), TRASH_DIRNAME)
    try:
        os.makedirs(trash, exist_ok=True)
        target = os.path.join(
            trash, _stamp() + "-" + os.path.basename(path)
        )
        shutil.move(path, target)
    except OSError:
        # The sandbox may refuse the move: the directory is then left
        # in place on purpose, this suite never deletes anything.
        pass


def _probe_writable(path: str) -> bool:
    """Return True when this process may create a file inside path."""

    return os.access(path, os.W_OK)


def _is_root() -> bool:
    """Return True when the process runs as root."""

    geteuid = getattr(os, "geteuid", None)
    return bool(geteuid is not None and geteuid() == 0)


def make_image(size=(8, 8), mode="RGB"):
    """Return a deterministic gradient image in the requested mode.

    A PIL image is returned whenever PIL is importable, otherwise a
    plain QImage, so the helpers work in a PyQt6 only environment.
    """

    width, height = size
    if PILImage is not None:
        gray = PILImage.new("L", size)
        values = [
            (x * 7 + y * 13) % 250 + 1
            for y in range(height)
            for x in range(width)
        ]
        gray.putdata(values)
        if mode == "L":
            return gray
        return gray.convert(mode)
    image = QtGui.QImage(width, height, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor(255, 255, 255))
    for y in range(height):
        for x in range(width):
            tone = (x * 7 + y * 13) % 250 + 1
            image.setPixelColor(x, y, QtGui.QColor(tone, tone, tone))
    return image


def _resolve_path(root, name):
    """Return the full path of a (root, name) or (path,) call."""

    if name is None:
        return str(root)
    return os.path.join(str(root), str(name))


def write_image(root, name=None, size=(8, 8), mode="RGB", **save_kwargs):
    """Write a deterministic image and return its path.

    Called either as write_image(directory, name) or as
    write_image(path) - the name may stay out.
    """

    path = _resolve_path(root, name)
    image = make_image(size, mode)
    if PILImage is not None and isinstance(image, PILImage.Image):
        image.save(path, **save_kwargs)
        return path
    if not image.save(path):
        raise RuntimeError("cannot write " + path)
    return path


def make_shape(
    label="cat",
    score=None,
    shape_type="rectangle",
    points=None,
    **extra,
):
    """Return one LabelMe shape dictionary."""

    if points is None:
        points = ((1.0, 2.0), (11.0, 12.0))
    shape = {
        "label": label,
        "shape_type": shape_type,
        "points": [list(point) for point in points],
    }
    if score is not None:
        shape["score"] = score
    shape.update(extra)
    return shape


def write_json(
    root,
    name=None,
    data=None,
    shapes=None,
    image_path=None,
    **extra,
):
    """Write a LabelMe side car and return its path.

    Called either as write_json(directory, name, data) or as
    write_json(path, shapes=[...]); without data the payload is a
    well formed empty document plus whatever extra keys are given.
    """

    path = _resolve_path(root, name)
    if data is None:
        payload = {
            "version": "5.5.0",
            "flags": {},
            "shapes": list(shapes or ()),
            "imagePath": image_path or "",
            "imageData": None,
        }
        payload.update(extra)
        data = payload
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)
    return path


def make_settings(ini_path):
    """Return a PreviewSettings wrapper backed by one INI file."""

    store = QtCore.QSettings(ini_path, QtCore.QSettings.Format.IniFormat)
    return PreviewSettings(store)


def flush_settings(settings):
    """Force the store of a wrapper to write its INI file."""

    settings._settings.sync()


class FakeExplorer(SimpleNamespace):
    """Stand in of the labeling widget the preview is opened from."""

    def __call__(self, **overrides):
        """Allow the fixture instance to build further explorers."""

        return fake_explorer(**overrides)


def fake_explorer(last_open_dir=None, parent=None, **overrides):
    """Build a stand in labeling widget with a real Tool menu.

    The object carries what the preview tool reads from the real
    widget: menus.tool, the QMenu the installer appends its action to,
    and last_open_dir. statusBar() answers a mock whose messages are
    both recorded on the mock and collected in the messages list, so a
    test can assert either way.
    """

    menu = QtWidgets.QMenu("Tool", parent)
    messages = []
    status_bar = Mock()
    status_bar.showMessage.side_effect = (
        lambda text, *args: messages.append(str(text))
    )
    explorer = FakeExplorer(
        menus=SimpleNamespace(tool=menu),
        last_open_dir=last_open_dir,
        messages=messages,
        status_bar=status_bar,
        statusBar=Mock(return_value=status_bar),
        may_continue=Mock(return_value=True),
    )
    for key, value in overrides.items():
        setattr(explorer, key, value)
    return explorer


def _trash_dir() -> str:
    """Return the temp trash folder of the suite."""

    return os.path.join(tempfile.gettempdir(), TRASH_DIRNAME)


def trash_listing(prefix=None):
    """Return the names inside the temp trash folder, sorted.

    The timestamp of a trashed file always precedes the original name,
    so a test can pick its own files with a unique stem as prefix.
    """

    try:
        names = sorted(os.listdir(_trash_dir()))
    except OSError:
        return []
    if prefix:
        names = [name for name in names if str(prefix) in name]
    return names


def trash_paths(prefix=None):
    """Return the full paths inside the temp trash folder, sorted."""

    return [os.path.join(_trash_dir(), name) for name in trash_listing(prefix)]


def snapshot(root):
    """Return the sorted names of every top level entry of a directory."""

    try:
        return sorted(os.listdir(root))
    except OSError:
        return []


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


@pytest.fixture
def pt_dir():
    """Empty scratch directory that stands in for the source folder."""

    path = _make_scratch()
    try:
        if not _probe_writable(path):
            pytest.skip("the scratch directory is not writable")
        yield path
    finally:
        release_scratch(path)


@pytest.fixture
def pt_out():
    """Empty scratch directory that stands in for the output folder."""

    path = _make_scratch()
    try:
        if not _probe_writable(path):
            pytest.skip("the scratch directory is not writable")
        yield path
    finally:
        release_scratch(path)


@pytest.fixture
def pt_make():
    """Factory of scratch directories, all released at the end."""

    paths = []

    def _make():
        path = _make_scratch()
        if not _probe_writable(path):
            pytest.skip("the scratch directory is not writable")
        paths.append(path)
        return path

    try:
        yield _make
    finally:
        for path in paths:
            release_scratch(path)


@pytest.fixture
def pt_store():
    """(PreviewSettings, ini path) pair backed by a scratch INI file."""

    path = _make_scratch()
    try:
        if not _probe_writable(path):
            pytest.skip("the scratch directory is not writable")
        ini = os.path.join(path, "preview_tool.ini")
        yield make_settings(ini), ini
    finally:
        release_scratch(path)


@pytest.fixture
def pt_readonly():
    """Scratch directory the process may not write into."""

    path = _make_scratch()
    os.chmod(path, 0o500)
    try:
        if _is_root() or _probe_writable(path):
            pytest.skip("a read only directory cannot be built here")
        yield path
    finally:
        os.chmod(path, 0o700)
        release_scratch(path)


@pytest.fixture(name="make_image")
def _make_image_fixture():
    """The make_image factory as an injectable fixture."""

    return make_image


@pytest.fixture(name="write_image")
def _write_image_fixture():
    """The write_image factory as an injectable fixture."""

    return write_image


@pytest.fixture(name="write_json")
def _write_json_fixture():
    """The write_json factory as an injectable fixture."""

    return write_json


@pytest.fixture(name="make_shape")
def _make_shape_fixture():
    """The make_shape factory as an injectable fixture."""

    return make_shape


@pytest.fixture(name="make_settings")
def _make_settings_fixture():
    """The make_settings factory as an injectable fixture."""

    return make_settings


@pytest.fixture(name="trash_listing")
def _trash_listing_fixture():
    """The trash_listing factory as an injectable fixture."""

    return trash_listing


@pytest.fixture(name="fake_explorer")
def _fake_explorer_fixture(qapp):
    """A fresh stand in labeling widget, callable for more of them."""

    return fake_explorer()
