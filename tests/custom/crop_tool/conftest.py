"""Shared fixtures of the crop tool tests.

Every scratch directory lives in the system temporary folder: the
suite never creates anything inside the repository and it never
deletes anything either. A finished scratch directory is moved into
the temp trash folder (<temp>/dsh-trash/<timestamp>-<name>) so that
it stays recoverable.

The settings wrapper is always handed an INI file of its own, so no
test can touch the store of the user.
"""

import datetime
import hashlib
import os
import shutil
import tempfile
import uuid

import PIL.Image
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtCore, QtWidgets  # noqa: E402

from anylabeling.custom.crop_tool import crop_core  # noqa: E402

from anylabeling.custom.crop_tool.settings import (  # noqa: E402
    CropSettings,
)

SCRATCH_PREFIX = "xal_ct_test_"
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


def _probe_writable(path: str) -> bool:
    """Return True when this process may create a file inside path.

    The probe stays read only on purpose: this suite never creates a
    file that it would afterwards have to delete.
    """

    return os.access(path, os.W_OK)


def _is_root() -> bool:
    """Return True when the process runs as root."""

    geteuid = getattr(os, "geteuid", None)
    return bool(geteuid is not None and geteuid() == 0)


def make_image(size=(8, 8), mode="RGB"):
    """Return a deterministic gradient image in the requested mode.

    Every pixel carries a value above zero, so a black pixel of a
    saved crop proves the fill value and not the source.
    """

    width, height = size
    gray = PIL.Image.new("L", size)
    values = [
        (x * 7 + y * 13) % 250 + 1
        for y in range(height)
        for x in range(width)
    ]
    gray.putdata(values)
    if mode == "L":
        return gray
    return gray.convert(mode)


def write_pil(root, name, size=(8, 8), mode="RGB", **save_kwargs):
    """Write a deterministic image into root and return its path."""

    path = os.path.join(root, name)
    make_image(size, mode).save(path, **save_kwargs)
    return path


def make_settings(ini_path):
    """Return a CropSettings wrapper backed by one INI file."""

    store = QtCore.QSettings(ini_path, QtCore.QSettings.Format.IniFormat)
    return CropSettings(store)


def flush_settings(settings):
    """Force the store of a wrapper to write its INI file."""

    settings._settings.sync()


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


def crop_names(root):
    """Return the names of the crops of a directory, in natural order."""

    names = []
    for name in os.listdir(root):
        if crop_core.parse_crop_name(name) is None:
            continue
        names.append(name)
    return sorted(names, key=crop_core.natural_key)


@pytest.fixture
def ct_dir():
    """Empty scratch directory that stands in for the source folder."""

    path = _make_scratch()
    try:
        if not _probe_writable(path):
            pytest.skip("the scratch directory is not writable")
        yield path
    finally:
        _release_scratch(path)


@pytest.fixture
def ct_out():
    """Empty scratch directory that stands in for the output folder."""

    path = _make_scratch()
    try:
        if not _probe_writable(path):
            pytest.skip("the scratch directory is not writable")
        yield path
    finally:
        _release_scratch(path)


@pytest.fixture
def ct_make():
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
            _release_scratch(path)


@pytest.fixture
def ct_store():
    """(CropSettings, ini path) pair backed by a scratch INI file."""

    path = _make_scratch()
    try:
        if not _probe_writable(path):
            pytest.skip("the scratch directory is not writable")
        ini = os.path.join(path, "crop_tool.ini")
        yield make_settings(ini), ini
    finally:
        _release_scratch(path)


@pytest.fixture
def ct_readonly():
    """Scratch directory the process may not write into.

    The test is skipped when the process is root or when the write
    probe still succeeds, because then the condition under test cannot
    be built at all.
    """

    path = _make_scratch()
    os.chmod(path, 0o500)
    try:
        if _is_root() or _probe_writable(path):
            pytest.skip("a read only directory cannot be built here")
        yield path
    finally:
        os.chmod(path, 0o700)
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
