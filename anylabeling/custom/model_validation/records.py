"""Validation records stored inside the staging folder.

A record never stores a source dataset path: source_display is for
display and reporting only and MUST NOT be used for file access. Every
file operation below targets the staging folder.
"""

from __future__ import annotations

import math
import os.path as osp
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from . import dataset
from .labelme_io import REGION_SHAPE_TYPES

KIND_ORIGINAL = "original"
KIND_AUGMENTED = "augmented"

PENDING = "PENDING"
OK = "OK"
NG = "NG"
SKIPPED = "SKIPPED"
NOT_JUDGED = "NOT_JUDGED"

VERDICT_ORDER = (NG, PENDING, OK, SKIPPED, NOT_JUDGED)


@dataclass
class ValidationRecord:
    """One validated image: an original or one augmented child."""

    record_id: str
    kind: str
    relpath: str
    staging_image_path: str
    staging_label_path: str = ""
    verdict: str = PENDING
    reasons: List[str] = field(default_factory=list)
    detail: Dict[str, Any] = field(default_factory=dict)
    deleted: bool = False
    include_in_export: bool = False
    parent_record_id: Optional[str] = None
    edited: bool = False
    aug_detail: Dict[str, Any] = field(default_factory=dict)
    judged: bool = False
    source_display: str = ""

    @property
    def image_exists(self) -> bool:
        """Return True when the staging image file exists."""

        return osp.isfile(self.staging_image_path)

    @property
    def label_exists(self) -> bool:
        """Return True when the staging label file exists."""

        if not self.staging_label_path:
            return False
        return osp.isfile(self.staging_label_path)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON friendly representation of the record."""

        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]):
        """Rebuild a record from its JSON representation."""

        known = set(cls.__dataclass_fields__)
        payload = {k: v for k, v in data.items() if k in known}
        return cls(**payload)


def make_record(
    kind: str,
    relpath: str,
    staging_image_path: str,
    staging_label_path: str = "",
    source_display: str = "",
    parent_record_id: Optional[str] = None,
) -> ValidationRecord:
    """Create a record with a deterministic record id."""

    return ValidationRecord(
        record_id=f"{kind}::{relpath}",
        kind=kind,
        relpath=relpath,
        staging_image_path=staging_image_path,
        staging_label_path=staging_label_path,
        parent_record_id=parent_record_id,
        source_display=source_display,
    )


def records_from_staging(
    staging_root: str, classes: Optional[List[str]] = None
) -> List[ValidationRecord]:
    """Build the original records from the staged meta.json."""

    meta_path = osp.join(staging_root, dataset.META_FILENAME)
    if not osp.isfile(meta_path):
        return []
    meta = dataset.read_json(meta_path)
    if not isinstance(meta, dict):
        return []
    source_display = str(meta.get("source_display", ""))
    records: List[ValidationRecord] = []
    for item in meta.get("originals", []) or []:
        records.append(
            make_record(
                KIND_ORIGINAL,
                str(item["relpath"]),
                str(item["staging_image_path"]),
                str(item.get("staging_label_path", "")),
                source_display=source_display,
            )
        )
    for item in meta.get("skipped", []) or []:
        record = make_record(
            KIND_ORIGINAL,
            str(item["relpath"]),
            str(item["staging_image_path"]),
            "",
            source_display=source_display,
        )
        record.verdict = SKIPPED
        record.reasons = [str(item.get("reason", "NO_LABEL"))]
        record.detail = {"skipped": True}
        records.append(record)
    if classes is not None:
        for record in records:
            record.detail.setdefault("classes", list(classes))
    return records


def record_lookup(
    records: List[ValidationRecord],
) -> Dict[str, ValidationRecord]:
    """Index records by record id."""

    return {record.record_id: record for record in records}


def children_of(
    records: List[ValidationRecord], parent_record_id: str
) -> List[ValidationRecord]:
    """Return the augmented children of one original record."""

    return [
        record
        for record in records
        if record.parent_record_id == parent_record_id
    ]


def read_staging_label(record: ValidationRecord):
    """Read the staging xlabel json of one record, staging path only."""

    if not record.label_exists:
        return None
    try:
        data = dataset.read_json(record.staging_label_path)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    data.setdefault("shapes", [])
    return data


def write_staging_label(
    record: ValidationRecord, data: Dict[str, Any]
) -> None:
    """Write the staging xlabel json of one record, staging path only."""

    dataset.write_json(record.staging_label_path, data)


def update_shape(
    record: ValidationRecord,
    index: int,
    labels: Optional[str] = None,
    shape_type: Optional[str] = None,
) -> bool:
    """Update one shape of a staging label and mark the record edited.

    A rename that writes the very values the shape already carries is
    answered False without touching the file: the row of the record
    keeps the note it had, and a double click that confirms the current
    name costs no byte of the staging folder. The caller that skipped
    the call altogether is the page (see ResultsPage.on_shape_rename);
    this is the second wall, for every other caller.

    A write the file system refuses answers False instead of raising,
    the very contract of update_shape_points: the rename of a box
    travels through a Qt event handler as well (the double click of the
    canvas), and an OSError escaping one of those aborts the window
    instead of leaving the staging json as it was found.

    The very same inputs are refused as well: a boolean index - True is
    the integer 1 and would rename the wrong shape - an index that is no
    number at all, one outside the shape list and a shape entry that is
    not the dict the writer expects, whose label cannot be read. The
    rename has no error path of its own, so the two writers of a staging
    label share one.
    """

    data = read_staging_label(record)
    if data is None:
        return False
    shapes = data.get("shapes") or []
    # a bool is an int in python, but it is never an index of a shape
    # list: True would silently be read as 1 and rewrite the *second*
    # shape of the label, which is not what the caller asked for
    if isinstance(index, bool):
        return False
    try:
        position = int(index)
    except (TypeError, ValueError):
        return False
    if position < 0 or position >= len(shapes):
        return False
    shape = shapes[position]
    if not isinstance(shape, dict):
        # an entry of another revision: a string, a list, a number. It
        # carries no label to rewrite and reading one would raise an
        # AttributeError out of a Qt event handler
        return False
    changed = False
    if labels is not None:
        changed = changed or str(shape.get("label", "")) != str(labels)
        shape["label"] = labels
    if shape_type is not None:
        changed = changed or str(shape.get("shape_type", "")) != str(
            shape_type
        )
        shape["shape_type"] = shape_type
    if not changed:
        return False
    try:
        write_staging_label(record, data)
    except OSError:
        return False
    record.edited = True
    return True


def _shape_folder(points: Any) -> List[List[float]]:
    """Normalise any accepted point representation to float pairs.

    None, a non finite coordinate and an entry that carries no usable
    pair are dropped instead of raising, so a caller handing over a
    point list of another revision gets a shorter list back rather than
    an exception. Only the entries that survive are ever written.
    """

    result: List[List[float]] = []
    for item in points or []:
        if hasattr(item, "x") and hasattr(item, "y"):
            pair = (item.x(), item.y())
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            pair = (item[0], item[1])
        else:
            continue
        try:
            x = float(pair[0])
            y = float(pair[1])
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(x) and math.isfinite(y)):
            continue
        result.append([x, y])
    return result


def _image_bounds(data: Dict[str, Any]):
    """Return the (width, height) a point set has to stay inside.

    The two numbers come from the staging json itself. A missing or
    unusable one is read as "no limit on this axis" and never raises:
    an old label without the two keys keeps being editable, it just
    cannot be clamped against a size nobody wrote down.
    """

    bounds: List[Optional[float]] = []
    for key in ("imageWidth", "imageHeight"):
        value = data.get(key)
        if isinstance(value, bool) or value is None:
            bounds.append(None)
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            bounds.append(None)
            continue
        bounds.append(number if math.isfinite(number) and number > 0 else None)
    return bounds[0], bounds[1]


def _stored_points(data: Dict[str, Any], points: Any):
    """Return the point list one shape would carry after a write.

    This is the one place the clamp and the rounding of a point update
    are read from: the value it answers for a new point set is exactly
    the value the writer stores, so "did this edit change anything" can
    be answered against the points already on disk without writing a
    file to find out.
    """

    folder = _shape_folder(points)
    width, height = _image_bounds(data)
    for point in folder:
        if width is not None:
            point[0] = min(max(point[0], 0.0), width)
        if height is not None:
            point[1] = min(max(point[1], 0.0), height)
        point[0] = round(point[0], 2)
        point[1] = round(point[1], 2)
    return folder


def update_shape_points(
    record: ValidationRecord,
    index: int,
    points: Any,
) -> bool:
    """Rewrite the point set of one shape of a staging label.

    The point drag of the results page lands here: the edits are written
    into the *staging* json of the record - the source dataset is never
    touched - and the record is marked edited, which is what the
    "(edited)" note of its row reads (see
    results_page._fill_row).

    Every coordinate is clamped into the picture before it is written
    and rounded to two decimals, so a drag over the border of the image
    can never store a point outside it. The clamp reads the size the
    staging json carries; a label without it is written unclamped
    instead of raising.

    A point set that is already the one of the shape - read through
    that very clamp and rounding - is not a write at all: no file is
    rewritten, the record is not marked edited and no row gains an
    "(edited)" note. The caller of a finished gesture is what skips the
    call (see ImageCanvas._finish_drag); this is the second wall, so a
    gesture the canvas could not tell apart from a click still costs no
    byte of the user's disk and no false "(edited)" mark.

    A write that the file system refuses - a full disk, a staging
    folder that is gone, a missing right - answers False as well and is
    never raised at the caller: this function is reached from a Qt
    event handler (the release of a drag), and an exception travelling
    out of one of those aborts the whole window instead of showing the
    user a line of status.

    The function answers False instead of raising for every input it
    cannot use: a record without a readable label, an index outside the
    shape list, a boolean index - True is the integer 1 and would
    rewrite the wrong shape - an empty point set, a shape type this
    tool does not edit (see labelme_io.REGION_SHAPE_TYPES), a label
    whose shapes entry is not the dict the writer expects, a point set
    that does not change anything and a failed write. The same contract
    as records.update_shape, so the two callers share one error path.
    """

    data = read_staging_label(record)
    if data is None:
        return False
    shapes = data.get("shapes") or []
    # a bool is an int in python, but it is never an index of a shape
    # list: True would silently be read as 1 and rewrite the *second*
    # shape of the label, which is not what the caller asked for
    if isinstance(index, bool):
        return False
    try:
        position = int(index)
    except (TypeError, ValueError):
        return False
    if position < 0 or position >= len(shapes):
        return False
    shape = shapes[position]
    if not isinstance(shape, dict):
        return False
    if str(shape.get("shape_type") or "") not in REGION_SHAPE_TYPES:
        return False
    if not _shape_folder(points):
        return False
    folder = _stored_points(data, points)
    if folder == _stored_points(data, shape.get("points")):
        return False
    shape["points"] = folder
    try:
        write_staging_label(record, data)
    except OSError:
        # the staging file is left exactly as it was found: the writer
        # either replaced it or wrote nothing at all
        return False
    record.edited = True
    return True


def label_shape_types(record: ValidationRecord) -> List[str]:
    """Return the shape types of a staging label in order."""

    data = read_staging_label(record)
    if data is None:
        return []
    return [
        str(shape.get("shape_type", "")) for shape in data.get("shapes", [])
    ]


def set_deleted(
    records: List[ValidationRecord],
    record_ids: List[str],
    deleted: bool = True,
) -> List[ValidationRecord]:
    """Soft delete or restore one or more original records."""

    wanted = set(record_ids)
    changed: List[ValidationRecord] = []
    for record in records:
        if record.record_id in wanted and record.kind == KIND_ORIGINAL:
            record.deleted = bool(deleted)
            changed.append(record)
    return changed


def set_include_in_export(
    records: List[ValidationRecord],
    record_ids: List[str],
    include: bool = True,
) -> List[ValidationRecord]:
    """Toggle the export flag of one or more augmented records."""

    wanted = set(record_ids)
    changed: List[ValidationRecord] = []
    for record in records:
        if record.record_id in wanted and record.kind == KIND_AUGMENTED:
            record.include_in_export = bool(include)
            changed.append(record)
    return changed


def affected_record_ids(
    records: List[ValidationRecord], record_ids: List[str]
) -> List[str]:
    """Return the records a mark toggle can change on screen.

    A delete mark of an original travels to its augmented children:
    their export verdict and their own row change with it, so the rows
    that have to be rewritten are the given records plus the children of
    the given originals. The set is collected in one pass over the
    record list - the caller repaints exactly those rows and never
    rebuilds the whole list.
    """

    affected = [str(item) for item in record_ids]
    known = set(affected)
    for record in records:
        parent = record.parent_record_id
        if parent and parent in known and record.record_id not in known:
            known.add(record.record_id)
            affected.append(record.record_id)
    return affected


def parent_deleted(
    records: List[ValidationRecord], record: ValidationRecord
) -> bool:
    """Return True when the parent original of an augmented record is gone."""

    if not record.parent_record_id:
        return False
    for candidate in records:
        if candidate.record_id == record.parent_record_id:
            return bool(candidate.deleted)
    return False


def export_selection(
    records: List[ValidationRecord],
) -> Dict[str, Any]:
    """Apply the export formula to the current records.

    The delete mark of the parents is collected once, so the formula
    stays linear in the number of records: it is asked for on every mark
    toggle of the window, and a scan per augmented record would turn one
    click on a long run into a quadratic pass.
    """

    originals = [r for r in records if r.kind == KIND_ORIGINAL]
    augmented = [r for r in records if r.kind == KIND_AUGMENTED]
    deleted_parents = {r.record_id for r in records if r.deleted}

    # An image without a label is SKIPPED: it is never exported, exactly
    # like the zip builder excludes it, so the preview, the report and
    # the archive always agree.
    selected_originals = [
        r for r in originals if not r.deleted and r.verdict != SKIPPED
    ]
    excluded_skipped_originals = [r for r in originals if r.verdict == SKIPPED]
    excluded_deleted_originals = [
        r for r in originals if r.deleted and r.verdict != SKIPPED
    ]

    selected_augmented: List[ValidationRecord] = []
    excluded_unselected: List[ValidationRecord] = []
    excluded_deleted_parent: List[ValidationRecord] = []
    for record in augmented:
        if record.parent_record_id and record.parent_record_id in (
            deleted_parents
        ):
            excluded_deleted_parent.append(record)
            continue
        if record.include_in_export:
            selected_augmented.append(record)
        else:
            excluded_unselected.append(record)

    return {
        "originals": selected_originals,
        "augmented": selected_augmented,
        "excluded_deleted_originals": excluded_deleted_originals,
        "excluded_skipped_originals": excluded_skipped_originals,
        "excluded_unselected_augmented": excluded_unselected,
        "excluded_deleted_parent_augmented": excluded_deleted_parent,
    }


def export_summary(records: List[ValidationRecord]) -> Dict[str, int]:
    """Return the counters shown on the results page and in the report."""

    selection = export_selection(records)
    return {
        "originals": len(selection["originals"]),
        "augmented": len(selection["augmented"]),
        "excluded_deleted_originals": len(
            selection["excluded_deleted_originals"]
        ),
        "excluded_unselected_augmented": len(
            selection["excluded_unselected_augmented"]
        ),
        "excluded_deleted_parent_augmented": len(
            selection["excluded_deleted_parent_augmented"]
        ),
        "skipped_no_label": len(selection["excluded_skipped_originals"]),
    }


def verdict_counts(records: List[ValidationRecord]) -> Dict[str, int]:
    """Count the records per verdict."""

    counts: Dict[str, int] = {}
    for record in records:
        counts[record.verdict] = counts.get(record.verdict, 0) + 1
    return counts


__all__ = [
    "KIND_AUGMENTED",
    "affected_record_ids",
    "KIND_ORIGINAL",
    "NOT_JUDGED",
    "NG",
    "OK",
    "PENDING",
    "SKIPPED",
    "VERDICT_ORDER",
    "ValidationRecord",
    "children_of",
    "export_selection",
    "export_summary",
    "label_shape_types",
    "make_record",
    "parent_deleted",
    "read_staging_label",
    "record_lookup",
    "records_from_staging",
    "set_deleted",
    "set_include_in_export",
    "update_shape",
    "update_shape_points",
    "verdict_counts",
    "write_staging_label",
]
