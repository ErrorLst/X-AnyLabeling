"""Pure file layer of the pick switch of the preview tool.

The module imports neither Qt nor a third party library, so a plain
script can drive it. No function ever raises: a missing source, a
folder that cannot be created or a move that is refused all come back
as a status of their own.

The only write this module performs is a copy into <output>/ or a
move into <temp>/dsh-trash. It never removes a file, so a wrong
choice can always be undone by running the opposite operation again -
the source image is never touched and every copy stays recoverable in
the temp trash. The copies land flat in the output folder itself: the
subdirectories of the source are not mirrored.
"""

from __future__ import annotations

import datetime
import os
import shutil
import tempfile
from dataclasses import dataclass
from typing import FrozenSet, List, Optional, Tuple

__all__ = [
    "PICK_IMAGE_EXTS",
    "PickOutcome",
    "UnpickStemResult",
    "detect_picked",
    "entry_key",
    "entry_path",
    "entry_rel_dir",
    "normalize_rel_dir",
    "output_conflicts",
    "output_skip_dirs",
    "pick_all",
    "pick_one",
    "pick_state",
    "trash_dir",
    "unique_target",
    "unpick_one",
    "unpick_stem",
]

#: Extensions a copied image may carry, lower case.
PICK_IMAGE_EXTS = (
    ".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif", ".tif", ".tiff",
)

#: First sequence suffix of a target whose plain name is taken.
PICK_SEQ_START = 1

#: Folder of the temp directory every removed file is moved into.
TRASH_DIRNAME = "dsh-trash"

_WINDOWS_SEP = chr(92)
_MAX_SEQ = 9999


def trash_dir() -> str:
    """Return the temp trash folder; it is never created here."""

    return os.path.join(tempfile.gettempdir(), TRASH_DIRNAME)


def _stamp() -> str:
    """Return a unique, sortable timestamp for a trash name."""

    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def _trash_target(name: str) -> str:
    """Return an unused path inside the temp trash folder."""

    trash = trash_dir()
    os.makedirs(trash, exist_ok=True)
    target = os.path.join(trash, _stamp() + "-" + name)
    counter = 0
    while os.path.exists(target):
        counter += 1
        target = os.path.join(
            trash, _stamp() + "-" + str(counter) + "-" + name
        )
    return target


def unique_target(directory: str, name: str, exists=None) -> str:
    """Return a free path for name inside directory.

    The plain name wins; when it is taken the sequence suffix moves one
    step further, so nothing is ever overwritten. The predicate
    "exists" may be injected, which keeps the helper testable without
    touching the disk.
    """

    probe = os.path.exists if exists is None else exists
    if not name:
        return ""
    candidate = os.path.join(directory, name)
    if not probe(candidate):
        return candidate
    stem, ext = os.path.splitext(name)
    seq = PICK_SEQ_START
    while seq <= _MAX_SEQ:
        candidate = os.path.join(directory, "{}_{}{}".format(stem, seq, ext))
        if not probe(candidate):
            return candidate
        seq += 1
    return os.path.join(directory, "{}_{}{}".format(stem, _stamp(), ext))


def _is_safe_name(name: str) -> bool:
    """Return True when name is a plain file name without a path."""

    if not name or name in (".", ".."):
        return False
    if name != os.path.basename(name):
        return False
    if "/" in name or _WINDOWS_SEP in name:
        return False
    return not os.path.isabs(name)


def normalize_rel_dir(value) -> str:
    """Return a safe relative directory, '' for the root or garbage.

    The separators are unified to the native one, empty and "." parts
    are dropped and any ".." part (or an absolute path) makes the whole
    value unusable, so a relative directory can never climb out of the
    picked folder. An unusable value is reported as the root.
    """

    if value is None:
        return ""
    try:
        text = str(value).strip()
    except (TypeError, ValueError):
        return ""
    if not text or text == ".":
        return ""
    try:
        if os.path.isabs(text) or os.path.splitdrive(text)[0]:
            return ""
    except (OSError, TypeError, ValueError):
        return ""
    unified = text.replace("/", os.sep).replace(_WINDOWS_SEP, os.sep)
    parts: List[str] = []
    for part in unified.split(os.sep):
        if part in ("", "."):
            continue
        if part == "..":
            return ""
        parts.append(part)
    if not parts:
        return ""
    return os.path.join(*parts)


def _norm_real(path) -> str:
    """Return the normalized real path of a path, '' when unknown."""

    try:
        text = os.fspath(path)
    except TypeError:
        return ""
    if isinstance(text, bytes) or not text:
        return ""
    try:
        return os.path.normcase(os.path.realpath(text))
    except (OSError, TypeError, ValueError):
        return ""


def _is_inside(child: str, root: str) -> bool:
    """Return True when child is root or below it, both real paths."""

    if not child or not root:
        return False
    if child == root:
        return True
    try:
        return os.path.commonpath([child, root]) == root
    except (OSError, ValueError):
        return False


def output_skip_dirs(directory, output_dir) -> Tuple[str, ...]:
    """Return the subtrees a recursive input scan has to leave out.

    The tool writes its copies into the output folder itself. When
    that folder is a strict subtree of the scanned root, the whole
    subtree is skipped, because every image inside it is a product of
    the tool and not an input. A folder that is the scanned root
    itself needs no skip: its copies cannot be told apart from their
    sources, which is why the window refuses that choice; the scan
    still lists the folder as the input it is. An output folder
    outside the input tree needs no skip either.
    """

    root = _norm_real(directory)
    out = _norm_real(output_dir)
    if not root or not out:
        return ()
    if out == root:
        return ()
    if _is_inside(out, root):
        return (str(output_dir),)
    return ()


def output_conflicts(directory, output_dir) -> bool:
    """Return True when the output folder is the input folder itself.

    A copy into the input folder cannot be told apart from its source,
    so the window refuses that choice before it copies anything. Two
    unknown paths never conflict; the caller only guards a real
    directory it has opened.
    """

    root = _norm_real(directory)
    out = _norm_real(output_dir)
    if not root or not out:
        return False
    return root == out


def _matches_stem(file_stem: str, stem: str) -> bool:
    """Return True when a file belongs to the family of one stem.

    A copy that was made when the plain name was taken carries a
    sequence suffix (a.jpg, a_1.jpg, a_2.jpg); all of them belong to
    the same choice, so removing one stem has to remove them all.
    """

    if file_stem == stem:
        return True
    if not file_stem.startswith(stem + "_"):
        return False
    return file_stem[len(stem) + 1:].isdecimal()


def _entry_name(entry) -> str:
    """Return the file name of an entry of any supported shape."""

    if isinstance(entry, str):
        return os.path.basename(entry)
    name = getattr(entry, "name", None)
    if isinstance(name, str) and name:
        return os.path.basename(name)
    path = getattr(entry, "path", None)
    if isinstance(path, str) and path:
        return os.path.basename(path)
    return ""


def _entry_path(entry) -> str:
    """Return the file path of an entry of any supported shape."""

    if isinstance(entry, str):
        return entry
    path = getattr(entry, "path", None)
    if isinstance(path, str) and path:
        return path
    name = getattr(entry, "name", None)
    if isinstance(name, str):
        return name
    return ""


def entry_rel_dir(entry) -> str:
    """Return the relative directory of an entry, '' at the root."""

    if isinstance(entry, (tuple, list)):
        if len(entry) == 2:
            return normalize_rel_dir(entry[0])
        return ""
    return normalize_rel_dir(getattr(entry, "rel_dir", ""))


def entry_path(entry) -> str:
    """Return the source file path of an entry of any supported shape."""

    return _entry_path(entry)


def entry_key(entry) -> Tuple[str, str]:
    """Return the (relative directory, stem) identity of one image.

    The key stays the identity the window uses: two images of
    different subdirectories may share a file name and still count and
    behave as two different choices, even though every copy lands in
    the same flat output folder. A plain string is a root image, an
    object carries its rel_dir attribute and a two item tuple is the
    key itself, so every caller can hand over what it has.
    """

    if isinstance(entry, (tuple, list)) and len(entry) == 2:
        rel_dir = normalize_rel_dir(entry[0])
        name = os.path.basename(str(entry[1]))
        return rel_dir, os.path.splitext(name)[0]
    name = _entry_name(entry)
    stem = os.path.splitext(name)[0] if name else ""
    return entry_rel_dir(entry), stem


def _output_names(output_dir) -> List[str]:
    """Return the names inside the output folder, empty when absent."""

    try:
        return sorted(os.listdir(str(output_dir or ".")))
    except (OSError, TypeError, ValueError):
        return []


@dataclass(frozen=True, slots=True)
class PickOutcome:
    """Result of copying or removing a single image."""

    status: str
    source: str = ""
    target: str = ""
    sidecar: str = ""
    message: str = ""
    error: Optional[str] = None


@dataclass(frozen=True, slots=True)
class UnpickItem:
    """One file that was moved out of the output folder."""

    source: str
    target: str
    name: str = ""


@dataclass(frozen=True, slots=True)
class UnpickStemResult:
    """Result of removing every copy of one stem."""

    status: str
    moved: Tuple[UnpickItem, ...] = ()
    failed: Tuple[Tuple[str, str], ...] = ()
    message: str = ""
    error: Optional[str] = None


def pick_state(entry, output_dir) -> bool:
    """Return True when the output folder holds this image already.

    The lookup only uses the stem family of the file name, because
    every copy lands flat in the output folder: the same name in two
    subdirectories of the input is one choice, not two. Only the image
    extensions count: a side car without its image does not make an
    image picked. A missing or unreadable output folder simply means
    "not picked", and every copy of the family counts.
    """

    name = _entry_name(entry)
    if not name:
        return False
    stem = os.path.splitext(name)[0]
    for file_name in _output_names(output_dir):
        if os.path.splitext(file_name)[1].lower() not in PICK_IMAGE_EXTS:
            continue
        if _matches_stem(os.path.splitext(file_name)[0], stem):
            return True
    return False


def pick_one(source_path, output_dir) -> PickOutcome:
    """Copy one image and its side car into the output folder.

    The copy keeps the file name of its source and lands directly in
    the output folder. A taken name only moves the sequence suffix of
    the copy, so nothing is ever overwritten. The target name is
    claimed atomically with mode "xb" before the copy, so two workers
    can race for the same name without ever overwriting a file and
    without leaving a placeholder behind.
    """

    source = str(source_path)
    try:
        if not os.path.isfile(source):
            return PickOutcome(
                status="missing",
                source=source,
                message="找不到源文件："
                + source,
            )
        directory = str(output_dir or ".")
        os.makedirs(directory, exist_ok=True)
        target = unique_target(directory, os.path.basename(source))
        with open(target, "xb"):
            pass
        try:
            shutil.copy2(source, target)
        except OSError as error:
            return PickOutcome(
                status="error",
                source=source,
                target=target,
                message="拷贝失败：{}".format(error),
                error=repr(error),
            )
        sidecar = ""
        side_source = os.path.splitext(source)[0] + ".json"
        if os.path.isfile(side_source):
            side_target = os.path.splitext(target)[0] + ".json"
            try:
                with open(side_target, "xb"):
                    pass
                shutil.copy2(side_source, side_target)
                sidecar = side_target
            except FileExistsError:
                sidecar = ""
            except OSError as error:
                return PickOutcome(
                    status="error",
                    source=source,
                    target=target,
                    message="边车拷贝失败：{}".format(
                        error
                    ),
                    error=repr(error),
                )
        return PickOutcome(
            status="ok",
            source=source,
            target=target,
            sidecar=sidecar,
            message="已拷贝到 {}".format(target),
        )
    except OSError as error:
        return PickOutcome(
            status="error",
            source=source,
            message="操作失败：{}".format(error),
            error=repr(error),
        )
    except (TypeError, ValueError) as error:
        return PickOutcome(
            status="invalid",
            source=source,
            message="非法路径：{}".format(error),
            error=repr(error),
        )


def _move_to_trash(source: str, name: str) -> str:
    """Move one file into the temp trash, return its new path."""

    target = _trash_target(name)
    shutil.move(source, target)
    return target


def unpick_one(output_dir, name) -> PickOutcome:
    """Move one copied file and its side car into the temp trash."""

    name = str(name)
    if not _is_safe_name(name):
        return PickOutcome(
            status="invalid",
            source=name,
            message="非法文件名：" + name,
        )
    source = os.path.join(str(output_dir or "."), name)
    try:
        if not os.path.isfile(source):
            return PickOutcome(
                status="missing",
                source=source,
                message="输出目录里没有这个文件：" + source,
            )
        target = _move_to_trash(source, name)
        sidecar = ""
        side_source = os.path.splitext(source)[0] + ".json"
        if os.path.isfile(side_source):
            side_name = os.path.basename(side_source)
            try:
                sidecar = _move_to_trash(side_source, side_name)
            except OSError as error:
                return PickOutcome(
                    status="error",
                    source=source,
                    target=target,
                    message="边车移动失败：{}".format(
                        error
                    ),
                    error=repr(error),
                )
        return PickOutcome(
            status="ok",
            source=source,
            target=target,
            sidecar=sidecar,
            message="已移入 {}".format(target),
        )
    except OSError as error:
        return PickOutcome(
            status="error",
            source=source,
            message="移动失败：{}".format(error),
            error=repr(error),
        )


def _collect_family(output_dir, stem: str) -> List[str]:
    """Return every copied image of one stem in the output folder."""

    names: List[str] = []
    for name in _output_names(output_dir):
        base, ext = os.path.splitext(name)
        if ext.lower() not in PICK_IMAGE_EXTS:
            continue
        if _matches_stem(base, stem):
            names.append(name)
    return names


def _move_one(directory, name, moved: List[UnpickItem], failed) -> None:
    """Move one named file of the output folder into the trash."""

    source = os.path.join(directory, name)
    try:
        target = _move_to_trash(source, name)
    except OSError as error:
        failed.append((name, str(error)))
        return
    moved.append(UnpickItem(source=source, target=target, name=name))


def _describe(moved) -> str:
    """Return the "name -> trash path" list of the moved files."""

    parts = []
    for item in moved:
        parts.append(
            "{} → {}".format(os.path.basename(item.source), item.target)
        )
    return ", ".join(parts)


def unpick_stem(output_dir, stem) -> UnpickStemResult:
    """Move every copy of one stem out of the output folder.

    The family is every copy of the stem inside the flat output
    folder, whatever subdirectory of the input the copies came from.
    Every image of the family is moved into the temp trash together
    with its own side car, each file under an independent timestamp.
    A single refused move is collected in the failed list and never
    stops the other files.
    """

    stem = str(stem)
    if not _is_safe_name(stem):
        return UnpickStemResult(
            status="invalid",
            message="非法文件名：" + stem,
        )
    directory = str(output_dir or ".")
    if not os.path.isdir(directory):
        return UnpickStemResult(
            status="missing",
            message="找不到目录：" + directory,
        )
    images = _collect_family(directory, stem)
    if not images:
        return UnpickStemResult(
            status="missing",
            message="输出目录里没有 {} 的图片".format(
                stem
            ),
        )
    moved: List[UnpickItem] = []
    failed: List[Tuple[str, str]] = []
    for name in images:
        _move_one(directory, name, moved, failed)
        side_name = os.path.splitext(name)[0] + ".json"
        if os.path.isfile(os.path.join(directory, side_name)):
            _move_one(directory, side_name, moved, failed)
    if failed:
        message = (
            "已移除 {} 个文件，{} 个失败：[{}]".format(
                len(moved), len(failed), _describe(moved)
            )
        )
        return UnpickStemResult(
            status="error",
            moved=tuple(moved),
            failed=tuple(failed),
            message=message,
        )
    message = "已移除 {} 个文件：[{}]".format(
        len(moved), _describe(moved)
    )
    return UnpickStemResult(status="ok", moved=tuple(moved), message=message)


def pick_all(entries, output_dir, progress_cb=None, should_stop=None):
    """Copy every entry that is not picked yet.

    Returns (copied, skipped, keys) with one (rel_dir, stem) key per
    copied image. The key stays the identity of the entry inside the
    input tree, while every copy itself lands flat in the output
    folder. The progress callback is called once per entry, the stop
    callback is asked before every entry and stops the run
    immediately, keeping what was copied.
    """

    items = list(entries or ())
    total = len(items)
    copied = 0
    skipped = 0
    keys: List[Tuple[str, str]] = []
    for index, entry in enumerate(items):
        if should_stop is not None and _should_stop(should_stop):
            break
        if pick_state(entry, output_dir):
            skipped += 1
        else:
            outcome = pick_one(_entry_path(entry), output_dir)
            if outcome.status == "ok":
                copied += 1
                keys.append(entry_key(entry))
        _notify(progress_cb, index + 1, total)
    return copied, skipped, tuple(keys)


def _should_stop(should_stop) -> bool:
    """Return the answer of a cancellation callback, False on error."""

    try:
        return bool(should_stop())
    except (TypeError, ValueError, OSError, RuntimeError):
        return False


def _notify(progress_cb, done: int, total: int) -> None:
    """Report one finished entry, ignoring a broken callback."""

    if progress_cb is None:
        return
    try:
        progress_cb(done, total)
    except (TypeError, ValueError, RuntimeError):
        return


def detect_picked(entries, output_dir) -> FrozenSet[Tuple[str, str]]:
    """Return the (rel_dir, stem) keys of the entries already copied."""

    keys = set()
    for entry in entries or ():
        try:
            if pick_state(entry, output_dir):
                keys.add(entry_key(entry))
        except (OSError, TypeError, ValueError):
            continue
    return frozenset(keys)
