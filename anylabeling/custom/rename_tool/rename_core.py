"""Rename a flat dataset folder by dominant label, in place after a backup.

The tool works on the top level of one folder. It pairs every image with
the json of the same stem, derives the dominant label of that json and
computes the name each pair should carry. Execution is two steps:

1. write_backup: every top level file of the folder is written into one
   zip under its current name. Images are stored, everything else is
   deflated; the archive is built as <backup>.part and moved over the
   final name with os.replace once it is closed, so a failure never
   leaves a half written archive under the final name.
2. apply_renames: the image/json pairs whose target name differs are
   renamed inside the folder itself. A renamed json is rewritten as well
   so its imagePath names the new image file. Every source file is first
   moved to a private staging name, then to its target, which keeps a
   target free even when it equals the source name of another item. A
   failure triggers a best effort rollback of the moves made so far; the
   backup from step 1 stays the final safety net.

Naming rules:

* the target stem of an item is <label>_<n> with n a canonical
  decimal number starting at 1 (no leading zero);
* an item whose computed target equals its current file name is
  compliant already: it is left alone and merely reserves its number;
* labels are sanitized (file name illegal characters collapse into a
  single underscore) before the occurrence count is taken;
* a json without the image of the same stem is ignored: it is not part
  of the plan at all - not an item, not a renamed file, not counted by
  total_files() - and it never blocks. It is only reported as a number
  by RenamePlan.stats() and RenamePlan.ignored. The backup still keeps
  it, because the backup mirrors the folder as it is.

Blocking conditions - any of them disables execution, no backup is
written and no file is renamed:

* an image without the json of the same stem;
* a json that cannot be parsed, or whose top level is not an object;
* a sub directory (only top level files are handled);
* one stem carrying several images or several json files;
* a computed target name that collides with a mirrored entry name;
* a file name that is not a legal target name;
* a plan that does not carry every top level file exactly once.

Target names are validated before anything is opened and by the preview
too: a name must be non empty, must not contain a slash or two dots and
must not be an absolute path, and every source file appears exactly
once.
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
    "ACTION_RENAME",
    "BACKUP_DEFAULT_SUFFIX",
    "BACKUP_FALLBACK_FORMAT",
    "COMPLIANT",
    "IMAGE_EXTS",
    "METHOD_IMAGE",
    "METHOD_OTHER",
    "PART_SUFFIX",
    "STAGING_PREFIX",
    "RenameError",
    "RenameItem",
    "RenamePlan",
    "apply_renames",
    "dominant_label",
    "json_with_image_path",
    "natural_key",
    "plan_directory",
    "resolve_output_path",
    "resolve_targets",
    "sanitize_label",
    "write_backup",
]

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".webp")
PART_SUFFIX = ".part"
BACKUP_DEFAULT_SUFFIX = "_backup"
BACKUP_FALLBACK_FORMAT = "{stem}_{index}{suffix}"
STAGING_PREFIX = ".xal-rename-"
METHOD_IMAGE = zipfile.ZIP_STORED
METHOD_OTHER = zipfile.ZIP_DEFLATED

COMPLIANT = re.compile(r"^(?P<label>.+)_(?P<n>[1-9][0-9]*)$")

ACTION_RENAME = "rename"
ACTION_ALREADY = "already"

_WINDOWS_SEP = chr(92)


class RenameError(Exception):
    """Raised when a rename plan cannot be backed up or applied."""


@dataclass
class RenameItem:
    """One image/json pair of the source folder and its target name.

    action is one of ACTION_RENAME or ACTION_ALREADY; error carries
    the scan problem of a blocked item and is empty for a healthy one.
    """

    stem: str
    image_name: str
    json_name: str
    label: str = ""
    target_stem: str = ""
    action: str = ""
    error: str = ""

    def target_image_name(self) -> str:
        """Return the target file name of the image."""

        return self.target_stem + osp.splitext(self.image_name)[1]

    def target_json_name(self) -> str:
        """Return the target file name of the json."""

        return self.target_stem + ".json"

    def image_entry_name(self) -> str:
        """Return the planned file name of the image.

        A plan that was never resolved (a blocked folder) has no target
        stem yet: the file then keeps its source name.
        """

        if not self.target_stem:
            return self.image_name
        return self.target_image_name()

    def json_entry_name(self) -> str:
        """Return the planned file name of the json, source name if none."""

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
        """Return (source name, target name) pairs in natural order.

        An item whose json is missing from the folder (the B1 blocker)
        contributes its image only: the phantom json name is never part
        of the plan, and never counted either.
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
        """Return the number of plan entries, ignored json excluded."""

        return len(self.entries())

    def stats(self) -> Dict[str, int]:
        """Return the file counters of the source folder.

        total counts every top level file, that is every entry plus
        every ignored json; the four other keys are mutually exclusive
        and always add up to total, and total - ignored is the entry
        count of the plan. The buckets are filled from the listing
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
    """Return True when name is a legal target file name."""

    if not name or name in (".", ".."):
        return False
    if "/" in name or _WINDOWS_SEP in name:
        return False
    if ".." in name:
        return False
    return not osp.isabs(name)


def natural_key(value: str) -> List[Tuple[int, object]]:
    """Sort key that keeps image_2 before image_10.

    Every chunk carries its kind in front of its value: 0 for a run of
    digits, 1 for a folded character. Two chunks of the same kind
    compare by number or by text, two chunks of a different kind
    compare by kind first, so a folder that mixes names such as 1.jpg
    and a.jpg, or a.jpg and a2.jpg, sorts instead of raising a
    TypeError.

    Only decimal digits open a numeric run. A superscript such as the
    two of a² passes str.isdigit() but int() rejects it with a
    ValueError, so it is compared as the ordinary character it is.
    That matters because plan_directory and check_entry_names run
    inside a Qt slot: an uncaught exception in a slot makes PyQt
    abort the whole application.
    """

    chunks: List[Tuple[int, object]] = []
    buffer = ""
    for char in value:
        if char.isdecimal():
            buffer += char
        else:
            if buffer:
                chunks.append((0, int(buffer)))
                buffer = ""
            chunks.append((1, char.lower()))
    if buffer:
        chunks.append((0, int(buffer)))
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


def resolve_output_path(directory: str, filename: str) -> str:
    """Return a backup path inside directory that does not exist yet.

    The requested name is used as is when it is free, otherwise _2, _3
    ... is inserted before the extension until an unused name shows up.
    An empty file name falls back to dataset_backup.zip.
    """

    name = (filename or "").strip() or "dataset_backup.zip"
    root, ext = osp.splitext(name)
    if not ext:
        ext = ".zip"
    candidate = osp.join(directory, name)
    index = 2
    while osp.exists(candidate):
        candidate = osp.join(directory, BACKUP_FALLBACK_FORMAT.format(
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
    of its label. A target name colliding with a kept name appends one
    blocker.
    """

    reserved: Dict[str, set] = {}
    for item in plan.items:
        if not item.label or item.error:
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
        if not item.label or item.error:
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
    """Return the target name problems of plan, empty when it is clean.

    Beyond the shape of every target name, the plan has to carry every
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
    image, keeping the directory prefix of the original value when it
    had one. Nothing is written to disk here.
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


def _rename_file(source: str, target: str) -> None:
    """Move one file, refusing to overwrite an existing target."""

    if osp.exists(target):
        raise RenameError("目标文件已存在，拒绝覆盖：%s" % target)
    os.rename(source, target)


def _read_bytes(path: str) -> bytes:
    """Return the whole payload of one file."""

    with open(path, "rb") as handle:
        return handle.read()


def _write_bytes(path: str, payload: bytes) -> None:
    """Replace the whole payload of one file."""

    with open(path, "wb") as handle:
        handle.write(payload)


def _allocate_staging(
    directory: str, name: str, used: set, counter: int
) -> Tuple[str, int]:
    """Return a free staging path for one file plus the next counter.

    The staging name keeps the extension of the source file, so a
    reader that opens the folder during the rename still sees a known
    file type, and it never collides with an existing file or with a
    staging name handed out earlier in the same run.
    """

    ext = osp.splitext(name)[1]
    while True:
        candidate = osp.join(
            directory, "%s%d%s" % (STAGING_PREFIX, counter, ext)
        )
        counter += 1
        if candidate in used:
            continue
        if osp.exists(candidate):
            continue
        used.add(candidate)
        return candidate, counter


def _rollback(
    moves: List[Tuple[str, str]], restore: List[Tuple[str, bytes]]
) -> str:
    """Undo the moves and json rewrites of a failed apply_renames.

    Every step is attempted even when an earlier one fails. Returns the
    empty string when the folder is back to its original listing, or a
    Chinese note that names the first problem otherwise.
    """

    problems: List[str] = []
    for source, target in reversed(moves):
        try:
            if osp.exists(target) and not osp.exists(source):
                os.rename(target, source)
        except OSError as error:
            problems.append("%s -> %s：%s" % (target, source, error))
    for path, payload in reversed(restore):
        try:
            _write_bytes(path, payload)
        except OSError as error:
            problems.append("%s：%s" % (path, error))
    if problems:
        return "；回滚不完整：%s" % "；".join(problems)
    return "；已回滚"


def write_backup(
    plan: RenamePlan,
    zip_path: str,
    progress: Optional[Callable[[int, int, str], None]] = None,
) -> Dict[str, object]:
    """Write every top level file of the source folder into zip_path.

    The archive is a faithful copy taken before apply_renames: every
    file keeps its current name and its current bytes, including the
    ignored json files that are not part of the plan. The archive is
    built as <zip_path>.part and moved over the final name with
    os.replace once it is closed, so a failure never leaves a half
    written archive under the final name. The .part file is kept on
    purpose when something goes wrong and its path is part of the
    raised RenameError message. Images are stored, everything else is
    deflated.

    progress(done, total, name) is called once per entry, without
    throttling, and once more as progress(total, total, "Done") when
    the archive is closed.
    """

    if plan.blocked():
        raise RenameError(
            "计划存在阻塞项，拒绝备份：%s" % "；".join(plan.blockers)
        )
    problems = check_entry_names(plan)
    if problems:
        raise RenameError("；".join(problems))
    if osp.exists(zip_path):
        raise RenameError("备份文件已存在，拒绝覆盖：%s" % zip_path)
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
            "备份 zip 不能放在源目录内：%s" % osp.realpath(zip_path)
        )
    part = zip_path + PART_SUFFIX
    if osp.exists(part):
        raise RenameError(
            "临时文件已存在，拒绝覆盖：%s（请自行确认后处理）" % part
        )
    parent = osp.dirname(zip_path)
    if parent and not osp.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    files = _top_level_files(directory)
    if files is None:
        raise RenameError("无法读取目录：%s" % directory)
    files.sort(key=natural_key)
    total = len(files)
    done = 0
    try:
        with zipfile.ZipFile(
            part, "w", compression=METHOD_OTHER, allowZip64=True
        ) as archive:
            for name in files:
                full = osp.join(directory, name)
                lower = osp.splitext(name)[1].lower()
                method = (
                    METHOD_IMAGE if lower in IMAGE_EXTS
                    else METHOD_OTHER
                )
                archive.write(full, name, compress_type=method)
                done += 1
                if progress is not None:
                    progress(done, total, name)
            if progress is not None:
                progress(total, total, "Done")
        os.replace(part, zip_path)
    except Exception as error:  # noqa: BLE001
        raise RenameError(
            "%s：%s（半成品已保留：%s）"
            % (type(error).__name__, error, part)
        )
    return {
        "backup_path": zip_path,
        "files": total,
        "entries": list(files),
        "ignored": len(plan.ignored),
        "mirrored": len(plan.mirrored),
        "source_untouched": True,
    }


def apply_renames(
    plan: RenamePlan,
    progress: Optional[Callable[[int, int, str, str], None]] = None,
) -> Dict[str, object]:
    """Rename the planned files inside the source folder itself.

    Only the items whose action is ACTION_RENAME are touched: their
    image and their json are renamed to the computed target names and
    the imagePath of the json is rewritten to the new image name. An
    item that is compliant already, a mirrored file and an ignored json
    keep their name and their bytes.

    Every source file first goes to a private staging name, then the
    images go to their target names, then the jsons are rewritten and
    moved to theirs. The staging step keeps a target free even when it
    equals the source name of another item (a swap or a cycle). A
    failure triggers a best effort rollback and raises RenameError; the
    backup written by write_backup is the final safety net.

    progress(done, total, source_name, target_name) is called once per
    renamed file, then once more as progress(total, total, "Done", "").
    """

    if plan.blocked():
        raise RenameError(
            "计划存在阻塞项，拒绝重命名：%s" % "；".join(plan.blockers)
        )
    problems = check_entry_names(plan)
    if problems:
        raise RenameError("；".join(problems))
    directory = plan.directory
    items = plan.valid_items()
    moves: List[Tuple[str, str]] = []
    restore: List[Tuple[str, bytes]] = []

    def _move(source: str, target: str) -> None:
        _rename_file(source, target)
        moves.append((source, target))

    try:
        staged_images: Dict[str, str] = {}
        staged_jsons: Dict[str, str] = {}
        used: set = set()
        counter = 0
        for item in items:
            for source_name, table in (
                (item.image_name, staged_images),
                (item.json_name, staged_jsons),
            ):
                source = osp.join(directory, source_name)
                staging, counter = _allocate_staging(
                    directory, source_name, used, counter
                )
                _move(source, staging)
                table[source_name] = staging
        total = 2 * len(items)
        done = 0
        for item in items:
            image_staging = staged_images[item.image_name]
            image_target = osp.join(directory, item.target_image_name())
            _move(image_staging, image_target)
            done += 1
            if progress is not None:
                progress(
                    done, total, item.image_name,
                    item.target_image_name(),
                )
            json_staging = staged_jsons[item.json_name]
            payload = _read_bytes(json_staging)
            restore.append(
                (osp.join(directory, item.json_name), payload)
            )
            _write_bytes(
                json_staging,
                json_with_image_path(
                    json_staging, item.target_image_name()
                ),
            )
            json_target = osp.join(directory, item.target_json_name())
            _move(json_staging, json_target)
            done += 1
            if progress is not None:
                progress(
                    done, total, item.json_name,
                    item.target_json_name(),
                )
        if progress is not None and items:
            progress(total, total, "Done", "")
    except Exception as error:  # noqa: BLE001
        rollback = _rollback(moves, restore)
        raise RenameError(
            "%s：%s%s" % (type(error).__name__, error, rollback)
        )
    renamed = 2 * len(items)
    entries: List[Tuple[str, str]] = []
    for item in items:
        entries.append((item.image_name, item.target_image_name()))
        entries.append((item.json_name, item.target_json_name()))
    return {
        "directory": directory,
        "renamed": renamed,
        "renamed_items": len(items),
        "kept": 2 * len(plan.unchanged_items()),
        "mirrored": len(plan.mirrored),
        "ignored": len(plan.ignored),
        "moves": entries,
    }
