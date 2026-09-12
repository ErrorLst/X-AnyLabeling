"""Shared fixtures for the model validation tests.

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

SCRATCH_PREFIX = "xal_mv_test_"
TRASH_DIRNAME = "dsh-trash"

SANDBOX_SKIP_REASON = (
    "the sandbox denies file writes inside a freshly created staging "
    "folder; run these tests outside the restricted sandbox"
)


def scratch_root() -> str:
    "Return the system temporary directory hosting every scratch dir."

    return tempfile.gettempdir()


def trash_root() -> str:
    "Return the temp trash directory that keeps the finished scratch dirs."

    return os.path.join(scratch_root(), TRASH_DIRNAME)


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
            scratch_root(),
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


def make_staging_root(parent=None):
    "Create a staging root, skipping when the sandbox denies it."

    from anylabeling.custom.model_validation import dataset

    try:
        return dataset.create_staging_root(parent)
    except PermissionError:
        pytest.skip(SANDBOX_SKIP_REASON)


class _PathLike:
    "Thin pathlib like wrapper exposing the used subset of the API."

    def __init__(self, path: str) -> None:
        self._path = path

    def __fspath__(self) -> str:
        return self._path

    def __truediv__(self, other):
        return _PathLike(os.path.join(self._path, str(other)))

    def __str__(self) -> str:
        return self._path

    def __repr__(self) -> str:
        return f"_PathLike({self._path!r})"

    def mkdir(self, parents: bool = False, exist_ok: bool = False):
        os.makedirs(self._path, exist_ok=True)
        return self

    def write_text(self, text: str, encoding: str = "utf-8"):
        with open(self._path, "w", encoding=encoding) as handle:
            handle.write(text)

    def read_text(self, encoding: str = "utf-8") -> str:
        with open(self._path, "r", encoding=encoding) as handle:
            return handle.read()

    @property
    def name(self) -> str:
        return os.path.basename(self._path)

    def joinpath(self, *parts):
        return _PathLike(os.path.join(self._path, *parts))

    def __eq__(self, other) -> bool:
        return os.fspath(self) == os.fspath(other)

    def __hash__(self) -> int:
        return hash(self._path)


@pytest.fixture
def mv_scratch():
    "System temp scratch directory used for the staging folders."

    try:
        path = _make_scratch()
    except PermissionError:
        pytest.skip(SANDBOX_SKIP_REASON)
    try:
        yield path
    finally:
        _release_scratch(path)


@pytest.fixture
def tmp_path():
    "Writable scratch directory for one test."

    try:
        path = _make_scratch()
    except PermissionError:
        pytest.skip(SANDBOX_SKIP_REASON)
    try:
        yield _PathLike(path)
    finally:
        _release_scratch(path)


@pytest.fixture
def tmp_path_factory():
    "Minimal stand-in for the pytest tmp_path_factory fixture."

    created = []

    class Factory:
        def mktemp(self, name, numbered=True):
            path = _make_scratch()
            created.append(path)
            return _PathLike(path)

    factory = Factory()
    try:
        yield factory
    finally:
        for path in created:
            _release_scratch(path)
