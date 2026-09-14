"""Rename a flat dataset folder by dominant label, output as one zip.

The tool never touches the source directory. It reads the top level of the
folder the user picked, pairs every image with the json of the same stem,
derives the dominant label of that json, computes the name each file
should carry inside the archive, and finally writes a single zip whose
entry names *are* those computed names. The source folder keeps its
bytes, its mtimes and even its file list: no rename, no write, no
temporary file and no staging folder is ever created next to it.

Naming rules:

* the target stem of an ordinary item is <label>_<n> with n a canonical
  decimal number starting at 1 (no leading zero);
* an item whose computed target equals its current file name is
  compliant already: it is left alone and merely reserves its number;
* an _aug<x> item follows its parent: it keeps its augment suffix chain
  verbatim and only inherits the number of the parent item;
* an _aug<x> item whose parent is not in the folder is an orphan: it is
  mirrored under its current name and reserves nothing;
* labels are sanitized (file name illegal characters collapse into a
  single underscore) before the occurrence count is taken;
* a json without the image of the same stem is ignored: it is not part
  of the plan at all - not an item, not a mirrored entry, not a zip
  entry, not counted by total_files() - and it never blocks. It is only
  reported as a number by RenamePlan.stats() and RenamePlan.ignored.

Blocking conditions - any of them disables execution, no zip is written
and no .part file is created:

* an image without the json of the same stem;
* a json that cannot be parsed, or whose top level is not an object;
* a sub directory (only top level files are handled);
* one stem carrying several images or several json files;
* a computed target name that collides with a mirrored entry name;
* a file name that is not a legal archive entry name;
* a plan that does not carry every top level file exactly once.

Archive entry names are validated before anything is opened and by the
preview too: a name must be non empty, must not contain a slash or two
dots and must not be an absolute path, and every source file appears
exactly once.

Writing is atomic per archive: the data goes to <name>.zip.part first
and is moved over the final name with os.replace once the archive is
closed. A failure leaves the .part file in place on purpose (the
exception carries its full path) and never deletes anything.
"""

from __future__ import annotations

import json
import os
import os.path as osp
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

__all__ = [
    "ACTION_ALREADY",
    "ACTION_ORPHAN",
    "ACTION_RENAME",
    "COMPLIANT",
    "IMAGE_EXTS",
    "METHOD_IMAGE",
    "METHOD_OTHER",
    "PART_SUFFIX",
    "RenameError",
    "RenameItem",
    "RenamePlan",
    "SUFFIX",
    "ZIP_DEFAULT_SUFFIX",
    "ZIP_FALLBACK_FORMAT",
    "dominant_label",
    "json_with_image_path",
    "natural_key",
    "plan_directory",
    "resolve_output_path",
    "resolve_targets",
    "sanitize_label",
    "split_aug_suffix",
    "write_zip",
]

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".webp")
PART_SUFFIX = ".part"
ZIP_DEFAULT_SUFFIX = "_renamed"
ZIP_FALLBACK_FORMAT = "{stem}_{index}{suffix}"
METHOD_IMAGE = zipfile.ZIP_STORED
METHOD_OTHER = zipfile.ZIP_DEFLATED

SUFFIX = re.compile(r"^(?P<base>.+)_aug(?P<x>[0-9]*)$")
COMPLIANT = re.compile(r"^(?P<label>.+)_(?P<n>[1-9][0-9]*)$")

ACTION_RENAME = "rename"
ACTION_ALREADY = "already"
ACTION_ORPHAN = "orphan"

_WINDOWS_SEP = chr(92)


class RenameError(Exception):
    """Raised when a rename plan cannot be turned into a zip archive."""


@dataclass
class RenameItem:
    """One image/json pair of the source folder and its target name.

    suffixes is the augment suffix chain in peel order, so a_aug1_aug2
    peels to the pure stem a with the chain ("_aug1", "_aug2").
    parent_stem is the stem of the item this one follows; it is empty
    for an orphan. action is one of ACTION_RENAME, ACTION_ALREADY or
    ACTION_ORPHAN; error carries the scan problem of a blocked item
    and is empty for a healthy one.
    """

    stem: str
    image_name: str
    json_name: str
    label: str = ""
    pure_stem: str = ""
    suffixes: Tuple[str, ...] = ()
    parent_stem: str = ""
    target_stem: str = ""
    action: str = ""
    error: str = ""

    def target_image_name(self) -> str:
        """Return the entry name of the image inside the zip."""

        return self.target_stem + osp.splitext(self.image_name)[1]

    def target_json_name(self) -> str:
        """Return the entry name of the json inside the zip."""

        return self.target_stem + ".json"

    def image_entry_name(self) -> str:
        """Return the image entry name of this item.

        A plan that was never resolved (a blocked folder) has no target
        stem yet: the file then keeps its source name.
        """

        if not self.target_stem:
            return self.image_name
        return self.target_image_name()

    def json_entry_name(self) -> str:
        """Return the json entry name of this item, source name if none."""

        if not self.target_stem:
            return self.json_name
        return self.target_json_name()

    def is_renamed(self) -> bool:
        """Return True when this item carries a new name."""

        return self.action == ACTION_RENAME


@dataclass
class RenamePlan:
    """Scan result of one source folder plus the computed targets.

    items holds every image/json pair of the folder in natural order,
    mirrored holds every top level file that no item refers to: a
    classes.txt, a hidden file, the second image of a stem collision
    (B5), any binary file. Those files are what entries() mirrors
    verbatim, under their current name.
    ignored holds the json files of the top level whose stem carries no
    image: such a file is not an item and not a mirrored entry either,
    it is simply left out of the plan and never blocks. Only stats()
    and the dialog report it as a number.
    blockers lists the Chinese reasons that forbid execution;
    plan_directory fills it for a malformed folder, resolve_targets and
    check_entry_names may append more.
    """

    directory: str
    items: List[RenameItem] = field(default_factory=list)
    mirrored: List[str] = field(default_factory=list)
    ignored: List[str] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)

    def blocked(self) -> bool:
        """Return True when execution has to stay disabled."""

        return bool(self.blockers)

    def valid_items(self) -> List[RenameItem]:
        """Return the items that carry a new name."""

        return [item for item in self.items if item.is_renamed()]

    def unchanged_items(self) -> List[RenameItem]:
        """Return the items that keep their current name."""

        return [item for item in self.items if not item.is_renamed()]

    def entries(self) -> List[Tuple[str, str]]:
        """Return (source name, entry name) pairs in natural order.

        An item whose json is missing from the folder (the B1 blocker)
        contributes its image only: the phantom json name is never part
        of the archive, and never counted either.
        """

        pairs: List[Tuple[str, str]] = []
        for name in self.mirrored:
            pairs.append((name, name))
        for item in self.items:
            pairs.append((item.image_name, item.image_entry_name()))
            if osp.isfile(osp.join(self.directory, item.json_name)):
                pairs.append((item.json_name, item.json_entry_name()))
        return pairs

    def total_files(self) -> int:
        """Return the number of top level files mirrored into the zip."""

        return len(self.entries())

    def stats(self) -> Dict[str, int]:
        """Return the file counters of the source folder.

        total counts every top level file, that is every entry plus
        every ignored json; the four other keys are mutually exclusive
        and always add up to total, and total - ignored is the entry
        count of the archive. The buckets are filled from the listing
        itself - images, json paired with an image, json without one
        (ignored) and everything else - so no file can escape them even
        when a stem carries variant json spellings. A folder that
        cannot be listed at all yields all zeroes instead of raising:
        the scan already refuses such a folder, but stats() may be
        asked for a plan that was built by hand.
        """

        names = _top_level_files(self.directory)
        if names is None:
            return {
                "total": 0,
                "images": 0,
                "json": 0,
                "ignored": 0,
                "other": 0,
            }
        image_stems = set()
        for name in names:
            stem, ext = osp.splitext(name)
            if ext.lower() in IMAGE_EXTS:
                image_stems.add(stem)
        images = 0
        paired = 0
        ignored = 0
        other = 0
        for name in names:
            stem, ext = osp.splitext(name)
            lower = ext.lower()
            if lower in IMAGE_EXTS:
                images += 1
            elif lower == ".json" and stem in image_stems:
                paired += 1
            elif lower == ".json":
                ignored += 1
            else:
                other += 1
        return {
            "total": len(names),
            "images": images,
            "json": paired,
            "ignored": ignored,
            "other": other,
        }

    def item_by_filename(self, name: str) -> Optional[RenameItem]:
        """Return the item owning name, or None."""

        for item in self.items:
            if name in (item.image_name, item.json_name):
                return item
        return None

    def unchanged_names(self) -> List[str]:
        """Return the current file names of every non renamed item."""

        names: List[str] = []
        for item in self.items:
            if not item.is_renamed():
                names.append(item.image_name)
                names.append(item.json_name)
        names.extend(self.mirrored)
        return names


def entry_name_ok(name: str) -> bool:
    """Return True when name is a legal archive entry name."""

    if not name or name in (".", ".."):
        return False
    if "/" in name or _WINDOWS_SEP in name:
        return False
    if ".." in name:
        return False
    return not osp.isabs(name)


def natural_key(value: str) -> List[object]:
    """Sort key that keeps image_2 before image_10.

    Only decimal digits open a numeric run. A superscript such as the
    two of a² passes str.isdigit() but int() rejects it with a
    ValueError, so it is compared as the ordinary character it is.
    That matters because plan_directory and check_entry_names run
    inside a Qt slot: an uncaught exception in a slot makes PyQt
    abort the whole application.
    """

    chunks: List[object] = []
    buffer = ""
    for char in value:
        if char.isdecimal():
            buffer += char
        else:
            if buffer:
                chunks.append(int(buffer))
                buffer = ""
            chunks.append(char.lower())
    if buffer:
        chunks.append(int(buffer))
    return chunks


def sanitize_label(raw: str) -> str:
    """Turn a raw label into a safe file name component."""

    name = re.sub(r'[\\/:*?"<>|]+', "_", str(raw))
    name = re.sub(r"_+", "_", name).strip("._ ")
    return name or "unnamed"


def dominant_label(json_path: str) -> Tuple[str, str]:
    """Return the dominant label and the error of one annotation file.

    The label that occurs most often wins, a tie goes to the one seen
    first. Shapes are counted after sanitizing, so the count compares
    the names that would end up on disk. An empty shape list, or shapes
    carrying no label at all, is background. A json that cannot be read
    or parsed, or whose top level is not an object, yields an empty
    label and a Chinese error text.
    """

    try:
        with open(json_path, "rb") as handle:
            data = json.loads(handle.read().decode("utf-8"))
    except (OSError, UnicodeDecodeError) as error:
        return "", "json 读取失败：%s" % error
    except ValueError as error:
        return "", "json 解析失败：%s" % error
    if not isinstance(data, dict):
        return "", "json 顶层不是对象"
    shapes = data.get("shapes")
    if not isinstance(shapes, list) or not shapes:
        return "background", ""
    counts: Dict[str, int] = {}
    order: List[str] = []
    for shape in shapes:
        if not isinstance(shape, dict):
            continue
        raw = str(shape.get("label", "") or "").strip()
        if not raw:
            continue
        clean = sanitize_label(raw)
        if clean not in counts:
            counts[clean] = 0
            order.append(clean)
        counts[clean] += 1
    if not order:
        return "background", ""
    best = order[0]
    for name in order:
        if counts[name] > counts[best]:
            best = name
    return best, ""


def split_aug_suffix(stem: str) -> Tuple[str, Tuple[str, ...]]:
    """Split stem into its pure stem and its augment suffix chain.

    The chain comes back in peel order: a_aug1_aug2 gives the pure
    stem a and the chain ("_aug1", "_aug2").
    """

    suffixes: List[str] = []
    base = stem
    while True:
        match = SUFFIX.match(base)
        if match is None:
            break
        suffixes.append("_aug" + match.group("x"))
        base = match.group("base")
    suffixes.reverse()
    return base, tuple(suffixes)


def resolve_output_path(directory: str, filename: str) -> str:
    """Return a zip path inside directory that does not exist yet.

    The requested name is used as is when it is free, otherwise _2, _3
    ... is inserted before the extension until an unused name shows up.
    An empty file name falls back to dataset_renamed.zip.
    """

    name = (filename or "").strip() or "dataset_renamed.zip"
    root, ext = osp.splitext(name)
    if not ext:
        ext = ".zip"
    candidate = osp.join(directory, name)
    index = 2
    while osp.exists(candidate):
        candidate = osp.join(directory, ZIP_FALLBACK_FORMAT.format(
            stem=root, index=index, suffix=ext
        ))
        index += 1
    return candidate


def _is_dir(path: str) -> bool:
    """Return True when path is a directory, False on any OS error."""

    try:
        return osp.isdir(path)
    except OSError:
        return False


def _read_dir(directory: str) -> List[str]:
    """Return the top level entries in natural order."""

    try:
        names = list(os.listdir(directory))
    except OSError as error:
        raise RenameError("无法读取目录：%s（%s）" % (directory, error))
    names.sort(key=natural_key)
    return names


def _split_into(item: RenameItem) -> None:
    """Fill the pure stem and the suffix chain of item."""

    pure, suffixes = split_aug_suffix(item.stem)
    item.pure_stem = pure
    item.suffixes = suffixes
    item.parent_stem = ""


def _distinct_conflicts(
    conflicts: List[Tuple[str, str]]
) -> List[Tuple[str, str]]:
    """Return one conflict pair per conflicted stem."""

    seen: Dict[str, str] = {}
    result: List[Tuple[str, str]] = []
    for stem, other in conflicts:
        if stem in seen:
            continue
        seen[stem] = other
        result.append((stem, other))
    return result


def plan_directory(
    directory: str,
    progress: Optional[Callable[[int, int, str], None]] = None,
) -> RenamePlan:
    """Scan the top level of directory and build the rename plan.

    Nothing is written and nothing is renamed: this is a read only
    pass that enumerates the folder, pairs the files and reads the
    labels. progress(done, total, message) is called once per pair and
    once more with message "Done" when the scan finished.
    """

    plan = RenamePlan(directory=directory)
    names = _read_dir(directory)

    subdirectories: List[str] = []
    images: Dict[str, str] = {}
    labels: Dict[str, str] = {}
    image_conflicts: List[Tuple[str, str]] = []
    label_conflicts: List[Tuple[str, str]] = []

    for name in names:
        full = osp.join(directory, name)
        if _is_dir(full):
            subdirectories.append(name)
            continue
        stem, ext = osp.splitext(name)
        lower = ext.lower()
        if lower in IMAGE_EXTS:
            if stem in images:
                image_conflicts.append((stem, images[stem]))
                image_conflicts.append((stem, name))
            else:
                images[stem] = name
            continue
        if lower == ".json":
            if stem in labels:
                label_conflicts.append((stem, labels[stem]))
                label_conflicts.append((stem, name))
            else:
                labels[stem] = name
            continue

    if subdirectories:
        plan.blockers.append(
            "%d 个子目录不支持（只处理顶层文件）：%s"
            % (len(subdirectories), "、".join(subdirectories))
        )
    all_names = {}
    for name in names:
        stem, _ext = osp.splitext(name)
        all_names.setdefault(stem, []).append(name)
    for groups in (
        _distinct_conflicts(image_conflicts),
        _distinct_conflicts(label_conflicts),
    ):
        if not groups:
            continue
        texts = []
        for stem, _other in groups:
            texts.append(
                "%s 同名" % " 与 ".join(all_names.get(stem) or [stem])
            )
        plan.blockers.append(
            "%d 组同名冲突：%s" % (len(groups), "、".join(texts))
        )

    total = len(images)
    done = 0
    bad_json: List[str] = []
    items: List[RenameItem] = []
    missing: List[str] = []
    for stem in sorted(images, key=natural_key):
        image_name = images[stem]
        json_name = labels.get(stem)
        done += 1
        if progress is not None:
            progress(done, total, image_name)
        if json_name is None:
            item = RenameItem(
                stem=stem,
                image_name=image_name,
                json_name=stem + ".json",
                error="缺少同名 json",
            )
            _split_into(item)
            items.append(item)
            missing.append(image_name)
            continue
        label, error = dominant_label(osp.join(directory, json_name))
        item = RenameItem(
            stem=stem,
            image_name=image_name,
            json_name=json_name,
            label=label,
            error=error,
        )
        _split_into(item)
        items.append(item)
        if error:
            bad_json.append(json_name)

    item_names = set()
    for item in items:
        item_names.add(item.image_name)
        item_names.add(item.json_name)
    orphan_json: List[str] = []
    for stem in sorted(labels, key=natural_key):
        if stem in images:
            continue
        orphan_json.append(labels[stem])

    plan.ignored = sorted(orphan_json, key=natural_key)
    ignored_set = set(plan.ignored)
    mirrored_kept: List[str] = []
    for name in names:
        if name in item_names:
            continue
        if name in ignored_set:
            continue
        if _is_dir(osp.join(directory, name)):
            continue
        mirrored_kept.append(name)

    plan.items = items
    plan.mirrored = mirrored_kept

    if missing:
        plan.blockers.append(
            "%d 张图片缺少同名 json：%s"
            % (len(missing), "、".join(missing))
        )
    if bad_json:
        plan.blockers.append(
            "%d 个 json 无法解析：%s"
            % (len(bad_json), "、".join(bad_json))
        )

    if progress is not None:
        progress(total, total, "Done")
    return plan


def resolve_targets(plan: RenamePlan) -> RenamePlan:
    """Compute the target stem and the action of every item.

    A compliant item (<label>_<n> matching its own dominant label)
    keeps its number, everything else takes the smallest free number
    of its label. An augment item follows its parent and never
    reserves a number; an augment item without a parent is an orphan
    that keeps its current name. Entry names colliding with a kept
    name append one blocker.
    """

    items_by_pure_stem: Dict[str, RenameItem] = {}
    for item in plan.items:
        items_by_pure_stem.setdefault(item.pure_stem, item)

    reserved: Dict[str, set] = {}
    for item in plan.items:
        if item.suffixes or not item.label or item.error:
            continue
        match = COMPLIANT.match(item.stem)
        if match is None:
            continue
        if sanitize_label(match.group("label")) != item.label:
            continue
        number = int(match.group("n"))
        numbers = reserved.setdefault(item.label, set())
        if number in numbers:
            continue
        numbers.add(number)
        item.target_stem = item.stem
        item.action = ACTION_ALREADY

    next_number: Dict[str, int] = {}
    for item in sorted(plan.items, key=_item_key):
        if item.suffixes or not item.label or item.error:
            continue
        if item.action == ACTION_ALREADY:
            continue
        numbers = reserved.get(item.label) or set()
        number = next_number.get(item.label, 1)
        while number in numbers:
            number += 1
        numbers.add(number)
        reserved[item.label] = numbers
        next_number[item.label] = number + 1
        item.target_stem = "%s_%d" % (item.label, number)
        item.action = ACTION_RENAME

    memo: Dict[int, Optional[str]] = {}

    def resolve(item: RenameItem) -> Optional[str]:
        if not item.suffixes:
            return item.target_stem or None
        key = id(item)
        if key in memo:
            return memo[key]
        memo[key] = None
        parent = items_by_pure_stem.get(item.pure_stem)
        if parent is None or parent is item:
            return None
        parent_target = resolve(parent)
        if not parent_target:
            return None
        found = parent_target + "".join(item.suffixes)
        memo[key] = found
        return found

    for item in sorted(plan.items, key=_item_key):
        if not item.suffixes:
            continue
        found = resolve(item)
        if found is None:
            item.action = ACTION_ORPHAN
            item.target_stem = item.stem
            continue
        item.target_stem = found
        item.action = (
            ACTION_ALREADY if found == item.stem else ACTION_RENAME
        )

    kept = set(plan.unchanged_names())
    collisions: List[str] = []
    for item in plan.items:
        if not item.is_renamed():
            continue
        for name in (item.target_image_name(), item.target_json_name()):
            if name in kept and name not in collisions:
                collisions.append(name)
    if collisions:
        plan.blockers.append(
            "%d 个改名目标与保持原名的文件同名：%s"
            % (len(collisions), "、".join(collisions))
        )
    return plan


def _item_key(item: RenameItem) -> List[object]:
    """Sort key of one plan item: its source stem in natural order."""

    return natural_key(item.stem)


def _top_level_files(directory: str) -> Optional[List[str]]:
    """Return the top level file names of directory, None when unreadable.

    A name counts as a file unless it is a directory, which is the same
    predicate plan_directory uses while it scans.
    """

    try:
        names = os.listdir(directory)
    except OSError:
        return None
    result: List[str] = []
    for name in names:
        if not _is_dir(osp.join(directory, name)):
            result.append(name)
    return result


def check_entry_names(plan: RenamePlan) -> List[str]:
    """Return the entry name problems of plan, empty when it is clean.

    Beyond the shape of every entry name, the plan has to carry every
    top level file of the source folder exactly once: a file the plan
    never mirrors is as wrong as a file it mirrors twice. The ignored
    json files are the one exception - they are deliberately left out
    of the plan - so they are neither demanded nor allowed to show up.
    When the folder cannot be listed again the completeness half is
    skipped, the name shape half still runs.
    """

    counts: Dict[str, int] = {}
    problems: List[str] = []
    for source, name in plan.entries():
        counts[source] = counts.get(source, 0) + 1
        if not entry_name_ok(name):
            problems.append("条目名非法：%s -> %s" % (source, name))
    for source, count in counts.items():
        if count > 1:
            problems.append("源文件在计划里出现 %d 次：%s" % (count, source))
    for name in sorted(
        set(plan.ignored).intersection(counts), key=natural_key
    ):
        problems.append("忽略集合与计划重叠：%s" % name)
    listed = _top_level_files(plan.directory)
    if listed is None:
        return problems
    ignored = set(plan.ignored)
    missing = [
        name for name in listed
        if not counts.get(name) and name not in ignored
    ]
    for name in missing:
        problems.append("源文件没有被计划镜像：%s" % name)
    for name in sorted(set(counts).difference(listed), key=natural_key):
        problems.append("计划里的文件不在源目录：%s" % name)
    return problems


def json_with_image_path(json_path: str, target_image: str) -> bytes:
    """Return the bytes of json_path with imagePath retargeted.

    Only imagePath changes: the value is the base name of the target
    image entry, keeping the directory prefix of the original value
    when it had one. Nothing is written to disk here.
    """

    try:
        with open(json_path, "rb") as handle:
            data = json.loads(handle.read().decode("utf-8"))
    except (OSError, UnicodeDecodeError) as error:
        raise RenameError("json 读取失败：%s（%s）" % (json_path, error))
    except ValueError as error:
        raise RenameError("json 解析失败：%s（%s）" % (json_path, error))
    if not isinstance(data, dict):
        raise RenameError("json 顶层不是对象：%s" % json_path)
    new_name = posixpath.basename(target_image.replace(_WINDOWS_SEP, "/"))
    old = data.get("imagePath")
    if isinstance(old, str) and ("/" in old or _WINDOWS_SEP in old):
        prefix = posixpath.dirname(old.replace(_WINDOWS_SEP, "/"))
        data["imagePath"] = prefix + "/" + new_name
    else:
        data["imagePath"] = new_name
    text = json.dumps(data, ensure_ascii=False, indent=2)
    return text.encode("utf-8")


def write_zip(
    plan: RenamePlan,
    zip_path: str,
    progress: Optional[Callable[[int, int, str, str, bool], None]] = None,
) -> Dict[str, object]:
    """Write the mirror of the source folder into zip_path.

    The archive is built as <zip_path>.part and moved over the final
    name with os.replace once it is closed, so a failure never leaves a
    half written archive under the final name. The .part file is kept
    on purpose when something goes wrong and its path is part of the
    raised RenameError message. Images are stored, everything else is
    deflated.

    progress(done, total, entry_name, source_name, changed) is called
    once per entry, without throttling, and once more as
    progress(total, total, "Done", "", False) when the archive is
    closed. done counts entries of the archive; changed tells whether
    that entry carries a new name. The ignored json files of the plan
    are not part of the archive and never reach the callback.
    """

    if plan.blocked():
        raise RenameError(
            "计划存在阻塞项，拒绝写 zip：%s" % "；".join(plan.blockers)
        )
    if osp.exists(zip_path):
        raise RenameError("输出文件已存在，拒绝覆盖：%s" % zip_path)
    problems = check_entry_names(plan)
    if problems:
        raise RenameError("；".join(problems))
    directory = plan.directory
    try:
        root = osp.realpath(directory)
        inside = osp.commonpath(
            [root, osp.realpath(zip_path)]
        ) == root
    except ValueError:
        inside = False
    if inside:
        raise RenameError(
            "输出 zip 不能放在源目录内：%s" % osp.realpath(zip_path)
        )
    part = zip_path + PART_SUFFIX
    if osp.exists(part):
        raise RenameError(
            "临时文件已存在，拒绝覆盖：%s（请自行确认后处理）" % part
        )
    parent = osp.dirname(zip_path)
    if parent and not osp.isdir(parent):
        os.makedirs(parent, exist_ok=True)

    entries = plan.entries()
    entry_map = dict(entries)
    total = len(entries)
    renamed = 0
    done = 0
    try:
        with zipfile.ZipFile(
            part, "w", compression=METHOD_OTHER, allowZip64=True
        ) as archive:
            for source, entry_name in entries:
                full = osp.join(directory, source)
                item = plan.item_by_filename(source)
                lower = osp.splitext(source)[1].lower()
                changed = (
                    item is not None
                    and item.is_renamed()
                    and entry_name != source
                )
                if lower == ".json" and changed:
                    image_entry = entry_map.get(
                        item.image_name, item.image_name
                    )
                    archive.writestr(
                        entry_name,
                        json_with_image_path(full, image_entry),
                        compress_type=METHOD_OTHER,
                    )
                elif lower in IMAGE_EXTS:
                    archive.write(
                        full, entry_name, compress_type=METHOD_IMAGE
                    )
                else:
                    archive.write(
                        full, entry_name, compress_type=METHOD_OTHER
                    )
                done += 1
                if changed:
                    renamed += 1
                if progress is not None:
                    progress(done, total, entry_name, source, changed)
            if progress is not None:
                progress(total, total, "Done", "", False)
        os.replace(part, zip_path)
    except Exception as error:  # noqa: BLE001
        raise RenameError(
            "%s：%s（半成品已保留：%s）"
            % (type(error).__name__, error, part)
        )
    return {
        "zip_path": zip_path,
        "files": total,
        "entries": [name for _source, name in entries],
        "renamed": renamed,
        "ignored": len(plan.ignored),
        "blockers": [],
        "source_untouched": True,
    }
