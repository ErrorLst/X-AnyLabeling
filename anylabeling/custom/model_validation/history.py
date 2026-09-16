"""Run history: discovery, state.json persistence and restoration.

Every model validation run owns one staging folder inside the system
temporary directory (see dataset.create_staging_root). This module
turns those folders into a browsable history:

* list_runs scans the temporary directory and summarises every folder
  whose name starts with dataset.STAGING_PREFIX;
* save_restore_state / read_restore_state keep the verdicts and the
  marks of a run inside that run's own state.json - nothing is copied
  and no second index is created;
* restore_records rebuilds the record list of a finished run, and
  refresh_from_roots reconciles it with the files still on disk.

The module is pure standard library: it imports neither PyQt6 nor the
numeric stack, so a history can be browsed without loading a model.
"""

from __future__ import annotations

import json
import os
import os.path as osp
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import dataset
from . import json_safe
from . import records as records_module
from .labelme_io import IMAGE_EXTENSIONS

STATE_FILENAME = "state.json"
STATE_VERSION = 1
DEFAULT_SCAN_LIMIT = 400

# meta.json is read from its head only: the counts and the display name
# of the source sit in the first lines, while the originals list grows
# far beyond this cap (1714 entries, about 525 KB on a real run).
META_HEAD_BYTES = 64 * 1024

# The reason column of the history page is driven by these five values
# and by nothing else: "" is a run whose images can be restored, every
# other value names the single reason it cannot.
REASON_MISSING = "missing"
REASON_MISSING_META = "missing_meta"
REASON_BAD_META = "bad_meta"
REASON_NO_LABELS = "no_labels"

NO_STATE_NOTE = "该历史没有判定数据（仅图片与标签）"
MISMATCH_NOTE = "state.json 记录的运行目录与当前目录不一致，已忽略该键"
NEWER_SCHEMA_NOTE = "state.json 的版本比当前程序新，已按可读字段恢复"

HEAD_KEYS = ("staging_root", "source_display", "counts")


@dataclass
class RunSummary:
    """One staging folder as it appears in the history list."""

    staging_root: str
    mtime: float
    staged_originals: int
    label_count: int
    augmented_count: int
    source_display: str
    meta_readable: bool
    state_readable: bool
    judged: int
    skipped: int
    verdict_counts: Dict[str, int]
    marked: int
    reason: str
    truncated: bool = False

    @property
    def restorable(self) -> bool:
        """Return True when the images of this run can be restored."""

        return self.reason == "" and self.label_count > 0


def _as_int(value: Any, default: int = 0) -> int:
    """Return value as an int, falling back to default."""

    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _value(item: Any, name: str, default: Any = None) -> Any:
    """Read one field of a record or of its JSON representation."""

    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _summary_of_items(items: Sequence[Any]) -> Dict[str, Any]:
    """Return the counters state.json stores for a record list.

    The same helper serves the records in memory and the raw records
    read back from a file, so the written summary and the restored one
    are computed by one rule.
    """

    judged = 0
    skipped = 0
    marked = 0
    verdicts: Dict[str, int] = {}
    for item in items:
        if _value(item, "judged"):
            judged += 1
        verdict = str(
            _value(item, "verdict", "") or records_module.PENDING
        )
        verdicts[verdict] = verdicts.get(verdict, 0) + 1
        if verdict == records_module.SKIPPED:
            skipped += 1
        if _value(item, "deleted") or _value(item, "include_in_export"):
            marked += 1
    return {
        "judged": judged,
        "skipped": skipped,
        "marked": marked,
        "verdicts": verdicts,
    }


def _normalize_summary(value: Any) -> Optional[Dict[str, Any]]:
    """Return a well formed summary read from a state file, or None."""

    if not isinstance(value, dict):
        return None
    verdicts = value.get("verdicts")
    if not isinstance(verdicts, dict):
        verdicts = {}
    return {
        "judged": _as_int(value.get("judged"), 0),
        "skipped": _as_int(value.get("skipped"), 0),
        "marked": _as_int(value.get("marked"), 0),
        "verdicts": {
            str(key): _as_int(count, 0) for key, count in verdicts.items()
        },
    }


def _empty_state(staging_root: str) -> Dict[str, Any]:
    """Return the report of a run that carries no usable state file."""

    return {
        "readable": False,
        "schema_version": 0,
        "saved_at": 0.0,
        "staging_root": staging_root,
        "source_display": "",
        "classes": [],
        "record_count": 0,
        "shown_record_id": "",
        "summary": _summary_of_items([]),
        "records": [],
        "note": NO_STATE_NOTE,
    }


def _head_value(text: str, key: str) -> Any:
    """Decode one top level value out of a truncated JSON head.

    json.loads needs the whole document and raw_decode needs the whole
    object it starts at, so neither of them can read the counts of a
    meta.json that is larger than the head read. This helper locates
    the key and decodes the single value that follows it, which always
    fits in the head because it is small.
    """

    token = '"' + key + '"'
    decoder = json.JSONDecoder()
    start = 0
    while True:
        index = text.find(token, start)
        if index == -1:
            return None
        rest = text[index + len(token):].lstrip()
        if not rest.startswith(":"):
            start = index + len(token)
            continue
        try:
            value, _end = decoder.raw_decode(rest[1:].lstrip())
        except ValueError:
            return None
        return value


def _read_json_capped(
    path: str, limit: int = META_HEAD_BYTES
) -> Optional[Dict[str, Any]]:
    """Read a JSON document from its head, degrading to a partial dict.

    A complete document is returned as it is. A document longer than
    the cap is read as far as the cap reaches and rebuilt from the
    values of HEAD_KEYS that still fit, so the counts of a huge
    meta.json stay readable without parsing half a megabyte.
    """

    try:
        with open(path, "rb") as handle:
            raw = handle.read(limit)
    except OSError:
        return None
    if not raw:
        return None
    text = raw.decode("utf-8", "replace")
    try:
        value = json.loads(text)
    except ValueError:
        value = None
    if isinstance(value, dict):
        return value
    try:
        value, _end = json.JSONDecoder().raw_decode(text.lstrip())
    except ValueError:
        value = None
    if isinstance(value, dict):
        return value
    partial: Dict[str, Any] = {}
    for key in HEAD_KEYS:
        found = _head_value(text, key)
        if found is not None:
            partial[key] = found
    return partial or None


def _count_entries(directory: str) -> int:
    """Count the entries of a folder with a single readdir, no stat."""

    try:
        with os.scandir(directory) as iterator:
            return sum(1 for _ in iterator)
    except OSError:
        return 0


def _meta_reason(meta_path: str) -> str:
    """Return why a meta.json that did not parse is unusable."""

    try:
        size = os.path.getsize(meta_path)
    except OSError:
        return REASON_MISSING_META
    if size <= 0:
        return REASON_MISSING_META
    return REASON_BAD_META


def read_restore_state(staging_root: str) -> Dict[str, Any]:
    """Read the state.json of one run into a report dictionary.

    A missing or unreadable file degrades to readable=False: it never
    raises and it never makes the images of the run unreachable. A
    staging_root recorded in the file that does not match the folder it
    was read from is ignored and noted, because the key is never used
    to reach a file; a schema_version newer than this module keeps the
    fields that can be read and notes the difference.
    """

    path = osp.join(staging_root, STATE_FILENAME)
    data: Any = None
    if osp.isfile(path):
        try:
            data = dataset.read_json(path)
        except (OSError, ValueError):
            data = None
    if not isinstance(data, dict):
        return _empty_state(staging_root)

    notes: List[str] = []
    recorded_root = str(data.get("staging_root", "") or "")
    if recorded_root and osp.normpath(recorded_root) != osp.normpath(
        staging_root
    ):
        notes.append(MISMATCH_NOTE)
    version = _as_int(data.get("schema_version"), 0)
    if version > STATE_VERSION:
        notes.append(NEWER_SCHEMA_NOTE)

    raw = data.get("records")
    items = (
        [item for item in raw if isinstance(item, dict)]
        if isinstance(raw, list)
        else []
    )
    summary = _summary_of_items(items)
    if not items:
        stored = _normalize_summary(data.get("summary"))
        if stored is not None:
            summary = stored
    classes = data.get("classes")
    return {
        "readable": True,
        "schema_version": version,
        "saved_at": data.get("saved_at", 0.0),
        "staging_root": staging_root,
        "source_display": str(data.get("source_display", "") or ""),
        "classes": (
            [str(item) for item in classes]
            if isinstance(classes, list)
            else []
        ),
        "record_count": _as_int(data.get("record_count"), len(items)),
        "shown_record_id": str(data.get("shown_record_id", "") or ""),
        "summary": summary,
        "records": items,
        "note": "；".join(notes),
    }


def _safe_read_state(staging_root: str) -> Dict[str, Any]:
    """Read a state file, degrading every failure to unreadable."""

    try:
        return read_restore_state(staging_root)
    except Exception:  # noqa: BLE001 - history must never raise here
        return _empty_state(staging_root)


def save_restore_state(
    staging_root: str,
    records: Sequence[records_module.ValidationRecord],
    *,
    classes: Sequence[str] = (),
    source_display: str = "",
    shown_record_id: str = "",
) -> Dict[str, Any]:
    """Write the state.json of one run and return the written payload.

    The document is compact JSON written to a fixed temporary name next
    to the target and moved over it with os.replace, so a reader either
    sees the previous file or the new one and never half of either. A
    failure raises OSError; the temporary file is deliberately left in
    place (this package deletes nothing) and is overwritten by the next
    save, while the previous state.json stays complete.
    """

    payload: Dict[str, Any] = {
        "schema_version": STATE_VERSION,
        "saved_at": time.time(),
        "staging_root": staging_root,
        "source_display": source_display,
        "classes": [str(item) for item in classes],
        "record_count": len(records),
        "shown_record_id": str(shown_record_id or ""),
        "summary": _summary_of_items(records),
        "records": [
            json_safe.sanitize(record.to_dict()) for record in records
        ],
    }
    text = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    )
    path = osp.join(staging_root, STATE_FILENAME)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp_path, path)
    return payload


def augment_suffix_stem(name: str) -> str:
    """Fold the _augN marker of an augmented name back to its source.

    "a (1)_aug1.jpg" becomes "a (1).jpg" and a name without a trailing
    _augN marker - including one whose only marker carries no index -
    is returned unchanged.
    """

    stem, ext = osp.splitext(str(name))
    marker = stem.rfind("_aug")
    if marker == -1:
        return str(name)
    index = stem[marker + 4:]
    if not index.isdigit():
        return str(name)
    base = stem[:marker]
    if not base or base.endswith("/") or base.endswith(os.sep):
        return str(name)
    return base + ext


def _augment_token(name: str) -> str:
    """Return the _augN marker of a name, or an empty text."""

    stem = osp.splitext(osp.basename(name))[0]
    marker = stem.rfind("_aug")
    if marker == -1:
        return ""
    token = stem[marker:]
    if token[4:] and token[4:].isdigit():
        return token
    return ""


def _original_index(
    records: Sequence[records_module.ValidationRecord],
) -> Dict[str, List[records_module.ValidationRecord]]:
    """Index the original records by relpath, duplicates included."""

    index: Dict[str, List[records_module.ValidationRecord]] = {}
    for record in records:
        if record.kind == records_module.KIND_ORIGINAL:
            index.setdefault(record.relpath, []).append(record)
    return index


def augment_parents(
    records: Sequence[records_module.ValidationRecord],
) -> Dict[str, str]:
    """Map the augmented records to the parent original they belong to.

    A parent already recorded by the run wins, then the parent_relpath
    left by the augmentation stage, then the _augN suffix folded back
    and looked up among the originals. A lookup that finds no original
    - or more than one, which only a duplicated relpath can produce -
    links nothing: the child is reported as unknown instead of being
    attached to a guess.
    """

    originals = _original_index(records)
    siblings = {
        record.record_id
        for record in records
        if record.kind == records_module.KIND_ORIGINAL
    }
    parents: Dict[str, str] = {}
    for record in records:
        if record.kind != records_module.KIND_AUGMENTED:
            continue
        if record.parent_record_id in siblings:
            parents[record.record_id] = str(record.parent_record_id)
            continue
        detail = record.aug_detail if record.aug_detail else {}
        parent_relpath = str(detail.get("parent_relpath", "") or "")
        if parent_relpath:
            found = originals.get(parent_relpath, [])
            if len(found) == 1:
                parents[record.record_id] = found[0].record_id
            continue
        folded = augment_suffix_stem(record.relpath)
        if folded == record.relpath:
            continue
        found = originals.get(folded, [])
        if len(found) == 1:
            parents[record.record_id] = found[0].record_id
    return parents


def clear_augment_parents(
    records: Sequence[records_module.ValidationRecord],
    unknown: Sequence[str],
) -> List[str]:
    """Drop the parent links that could not be confirmed.

    unknown names the augmented children whose parent stayed unknown,
    by record id or by relpath; every link such a child still carries
    is removed and the cleared record ids are returned.
    """

    wanted = {str(item) for item in unknown}
    cleared: List[str] = []
    for record in records:
        if record.kind != records_module.KIND_AUGMENTED:
            continue
        if record.record_id not in wanted and record.relpath not in wanted:
            continue
        if record.parent_record_id:
            record.parent_record_id = None
            cleared.append(record.record_id)
    return cleared


def _list_relpaths(
    directory: str, suffixes: Optional[Sequence[str]] = None
) -> List[str]:
    """List the files of a tree as slash separated relative paths.

    An optional suffix filter keeps a tree to the files it is named
    after: real runs carry stray label docs inside their images
    folders (194 and 379 of them on the two measured runs), and a
    .json is neither an image nor an augmented copy.

    os.walk already hands over the non-directory entries only, so no
    entry is stat'ed here: on the measured Windows mount an extra stat
    per file costs more than the whole walk.
    """

    found: List[str] = []
    if not osp.isdir(directory):
        return found
    wanted = tuple(suffixes) if suffixes else None
    for current, dirs, names in os.walk(directory):
        dirs.sort()
        for name in sorted(names):
            if wanted is not None and not name.lower().endswith(wanted):
                continue
            full = osp.join(current, name)
            relpath = osp.relpath(full, directory).replace(os.sep, "/")
            found.append(relpath)
    return found


def _image_relpaths(directory: str) -> List[str]:
    """List the images of a tree, ignoring anything else it holds."""

    return _list_relpaths(directory, IMAGE_EXTENSIONS)


def _augmented_relpaths(staging_root: str) -> List[str]:
    """Return every augmented relpath found in the two augmented trees.

    The label tree is authoritative and unfiltered - a label may be
    named without any known extension - while the image tree only
    contributes real images, so a copy that lost its label is still
    listed and a stray document is not mistaken for one.
    """

    root = osp.join(staging_root, dataset.AUGMENTED_DIRNAME)
    found = set(
        _image_relpaths(osp.join(root, dataset.IMAGES_DIRNAME))
    )
    found.update(_list_relpaths(osp.join(root, dataset.LABELS_DIRNAME)))
    return sorted(found, key=dataset.natural_key)


def restore_records(
    run: RunSummary, classes: Optional[Sequence[str]] = None
) -> Tuple[List[records_module.ValidationRecord], Dict[str, Any]]:
    """Rebuild the records of one finished run.

    The originals always come from the staged meta.json. When the state
    file is readable the verdicts, the marks and the augmented children
    come from it, and the originals the state does not mention are
    filled back in; otherwise the augmented children are recomputed
    from the augmented folder and every record starts PENDING. A record
    whose staged image no longer exists is dropped and counted, so a
    partially cleaned run still opens.
    """

    staging_root = run.staging_root
    wanted = [str(item) for item in classes] if classes is not None else []
    base = records_module.records_from_staging(
        staging_root, list(wanted) if classes is not None else None
    )
    source_display = run.source_display
    if not source_display and base:
        source_display = base[0].source_display

    state = _safe_read_state(staging_root)
    state_readable = bool(state.get("readable"))
    dropped = 0
    records: List[records_module.ValidationRecord] = []
    if state_readable:
        if not source_display:
            source_display = str(state.get("source_display", "") or "")
        note = str(state.get("note", "") or "")
        known = set()
        for item in state.get("records", []):
            try:
                record = records_module.ValidationRecord.from_dict(item)
            except (TypeError, ValueError):
                continue
            if not record.record_id or record.record_id in known:
                continue
            if not record.image_exists:
                dropped += 1
                continue
            known.add(record.record_id)
            if not record.source_display:
                record.source_display = source_display
            records.append(record)
        for record in base:
            if record.record_id not in known:
                records.append(record)
    else:
        note = NO_STATE_NOTE
        records = list(base)
        for relpath in _augmented_relpaths(staging_root):
            paths = dataset.staging_paths(
                staging_root, records_module.KIND_AUGMENTED, relpath
            )
            records.append(
                records_module.make_record(
                    records_module.KIND_AUGMENTED,
                    relpath,
                    paths["image"],
                    paths["label"],
                    source_display=source_display,
                )
            )

    augmented_ids = [
        record.record_id
        for record in records
        if record.kind == records_module.KIND_AUGMENTED
    ]
    parents = augment_parents(records)
    unknown = [
        record_id for record_id in augmented_ids if record_id not in parents
    ]
    clear_augment_parents(records, unknown)
    for record in records:
        parent = parents.get(record.record_id)
        if parent:
            record.parent_record_id = parent

    report = {
        "staging_root": staging_root,
        "source_display": source_display,
        "classes": wanted,
        "record_count": len(records),
        "judged": sum(1 for record in records if record.judged),
        "skipped": sum(
            1
            for record in records
            if record.verdict == records_module.SKIPPED
        ),
        "marked": sum(
            1
            for record in records
            if record.deleted or record.include_in_export
        ),
        "verdicts": records_module.verdict_counts(records),
        "dropped_missing_images": dropped,
        "unknown_parents": unknown,
        "state_readable": state_readable,
        "note": note,
    }
    return records, report


def _rename_candidate(
    old_relpath: str, candidates: Sequence[str]
) -> Optional[str]:
    """Return the one new path that plainly replaces a vanished one.

    A rename keeps the folder, the extension and the _augN copy index
    of the file; only the name in front of them changes. Exactly one
    candidate has to match, otherwise nothing is assumed.
    """

    folder = osp.dirname(old_relpath)
    ext = osp.splitext(old_relpath)[1]
    token = _augment_token(old_relpath)
    matches = [
        item
        for item in candidates
        if osp.dirname(item) == folder
        and osp.splitext(item)[1] == ext
        and _augment_token(item) == token
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def _retarget_augmented(
    record: records_module.ValidationRecord,
    staging_root: str,
    relpath: str,
) -> None:
    """Point an augmented record at a renamed copy, keeping its verdict."""

    paths = dataset.staging_paths(
        staging_root, records_module.KIND_AUGMENTED, relpath
    )
    record.relpath = relpath
    record.record_id = f"{records_module.KIND_AUGMENTED}::{relpath}"
    record.staging_image_path = paths["image"]
    record.staging_label_path = paths["label"]


def refresh_from_roots(
    staging_root: str,
    records: Sequence[records_module.ValidationRecord],
) -> Dict[str, Any]:
    """Reconcile the records in memory with the files still on disk.

    Both trees are listed again. A missing counterpart is reported, an
    augmented record whose files vanished is dropped - unless exactly
    one new copy plainly replaces it (same folder, extension and _augN
    index), in which case the record is retargeted in place so that its
    verdict and its marks survive a rename - and a newly written
    augmented copy becomes a fresh PENDING record. The verdict and the
    marks of every other record are never touched, and the given list
    is updated in place, so it has to be mutable.
    """

    trees: Dict[str, Tuple[List[str], List[str]]] = {}
    for kind, dirname in (
        (records_module.KIND_ORIGINAL, dataset.ORIGINAL_DIRNAME),
        (records_module.KIND_AUGMENTED, dataset.AUGMENTED_DIRNAME),
    ):
        root = osp.join(staging_root, dirname)
        trees[kind] = (
            _image_relpaths(osp.join(root, dataset.IMAGES_DIRNAME)),
            _list_relpaths(osp.join(root, dataset.LABELS_DIRNAME)),
        )

    missing_labels: List[str] = []
    missing_images: List[str] = []
    for kind in (records_module.KIND_ORIGINAL, records_module.KIND_AUGMENTED):
        images, labels = trees[kind]
        image_set = set(images)
        label_set = set(labels)
        for relpath in images:
            if relpath not in label_set:
                missing_labels.append(f"{kind}::{relpath}")
        for relpath in labels:
            if relpath not in image_set:
                missing_images.append(f"{kind}::{relpath}")

    aug_images, aug_labels = trees[records_module.KIND_AUGMENTED]
    on_disk = set(aug_images) | set(aug_labels)
    recorded = {
        record.relpath: record
        for record in records
        if record.kind == records_module.KIND_AUGMENTED
    }
    vanished = sorted(
        (item for item in recorded if item not in on_disk),
        key=dataset.natural_key,
    )
    appeared = sorted(
        (item for item in on_disk if item not in recorded),
        key=dataset.natural_key,
    )

    renamed: List[Dict[str, Any]] = []
    removed: List[str] = []
    remaining = list(appeared)
    for relpath in vanished:
        mate = _rename_candidate(relpath, remaining)
        if mate is None:
            removed.append(relpath)
            continue
        remaining.remove(mate)
        record = recorded[relpath]
        old_record_id = record.record_id
        _retarget_augmented(record, staging_root, mate)
        renamed.append(
            {
                "record_id": record.record_id,
                "old_record_id": old_record_id,
                "old_relpath": relpath,
                "new_relpath": mate,
            }
        )

    if removed:
        gone = {id(recorded[relpath]) for relpath in removed}
        records[:] = [
            record for record in records if id(record) not in gone
        ]

    source_display = ""
    for record in records:
        if record.source_display:
            source_display = record.source_display
            break
    added: List[str] = []
    for relpath in remaining:
        paths = dataset.staging_paths(
            staging_root, records_module.KIND_AUGMENTED, relpath
        )
        records.append(
            records_module.make_record(
                records_module.KIND_AUGMENTED,
                relpath,
                paths["image"],
                paths["label"],
                source_display=source_display,
            )
        )
        added.append(relpath)

    return {
        "missing_labels": missing_labels,
        "missing_images": missing_images,
        "extra_augmented": added,
        "renamed": renamed,
        "removed": removed,
    }


def _summarize(path: str, mtime: float, truncated: bool) -> RunSummary:
    """Summarise one staging folder, never raising for a missing part."""

    state = _safe_read_state(path)
    if not osp.isdir(path):
        return _missing_summary(path, mtime, truncated, REASON_MISSING)

    label_dir = osp.join(
        path, dataset.ORIGINAL_DIRNAME, dataset.LABELS_DIRNAME
    )
    augmented_dir = osp.join(
        path, dataset.AUGMENTED_DIRNAME, dataset.LABELS_DIRNAME
    )
    label_count = _count_entries(label_dir)
    augmented_count = _count_entries(augmented_dir)

    meta_path = osp.join(path, dataset.META_FILENAME)
    meta = _read_json_capped(meta_path)
    staged = 0
    source_display = ""
    if isinstance(meta, dict):
        counts = meta.get("counts")
        if isinstance(counts, dict):
            staged = _as_int(counts.get("original"), 0)
        source_display = str(meta.get("source_display", "") or "")
        reason = "" if label_count > 0 else REASON_NO_LABELS
    else:
        reason = _meta_reason(meta_path)

    summary = state.get("summary") or {}
    verdicts = summary.get("verdicts")
    if not isinstance(verdicts, dict):
        verdicts = {}
    return RunSummary(
        staging_root=path,
        mtime=mtime,
        staged_originals=staged,
        label_count=label_count,
        augmented_count=augmented_count,
        source_display=source_display,
        meta_readable=isinstance(meta, dict),
        state_readable=bool(state.get("readable")),
        judged=_as_int(summary.get("judged"), 0),
        skipped=_as_int(summary.get("skipped"), 0),
        verdict_counts={
            str(key): _as_int(count, 0) for key, count in verdicts.items()
        },
        marked=_as_int(summary.get("marked"), 0),
        reason=reason,
        truncated=truncated,
    )


def _missing_summary(
    path: str, mtime: float, truncated: bool, reason: str
) -> RunSummary:
    """Return the summary of a folder that disappeared or stayed empty."""

    return RunSummary(
        staging_root=path,
        mtime=mtime,
        staged_originals=0,
        label_count=0,
        augmented_count=0,
        source_display="",
        meta_readable=False,
        state_readable=False,
        judged=0,
        skipped=0,
        verdict_counts={},
        marked=0,
        reason=reason,
        truncated=truncated,
    )


def list_runs(
    temp_root: Optional[str] = None, scan_limit: int = DEFAULT_SCAN_LIMIT
) -> List[RunSummary]:
    """List the staging folders found under temp_root, newest first.

    The first pass stats every candidate and keeps only its folder and
    its modification time; the second pass reads the details of the
    first scan_limit folders, so a directory full of old runs costs one
    readdir plus one stat per candidate instead of a full read of every
    run. The selected summaries carry truncated=True when older runs
    were left out. A folder that vanishes between the two passes is
    summarised as missing; a folder whose stat fails is skipped.
    """

    root = temp_root or tempfile.gettempdir()
    entries: List[Tuple[float, str]] = []
    try:
        with os.scandir(root) as iterator:
            for entry in iterator:
                if not entry.name.startswith(dataset.STAGING_PREFIX):
                    continue
                try:
                    if not entry.is_dir():
                        continue
                    mtime = float(entry.stat().st_mtime)
                except OSError:
                    continue
                entries.append((mtime, entry.path))
    except OSError:
        return []

    entries.sort(key=lambda item: (-item[0], item[1]))
    limit = max(0, _as_int(scan_limit, DEFAULT_SCAN_LIMIT))
    selected = entries[:limit]
    truncated = len(entries) > len(selected)

    runs: List[RunSummary] = []
    for mtime, path in selected:
        try:
            runs.append(_summarize(path, mtime, truncated))
        except OSError:
            continue
    return runs


__all__ = [
    "DEFAULT_SCAN_LIMIT",
    "META_HEAD_BYTES",
    "NO_STATE_NOTE",
    "REASON_BAD_META",
    "REASON_MISSING",
    "REASON_MISSING_META",
    "REASON_NO_LABELS",
    "STATE_FILENAME",
    "STATE_VERSION",
    "RunSummary",
    "augment_parents",
    "augment_suffix_stem",
    "clear_augment_parents",
    "list_runs",
    "read_restore_state",
    "refresh_from_roots",
    "restore_records",
    "save_restore_state",
]
