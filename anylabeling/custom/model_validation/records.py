"""Validation records stored inside the staging folder.

A record never stores a source dataset path: source_display is for
display and reporting only and MUST NOT be used for file access. Every
file operation below targets the staging folder.
"""

from __future__ import annotations

import os.path as osp
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from . import dataset

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
    "make_record",
    "parent_deleted",
    "read_staging_label",
    "record_lookup",
    "records_from_staging",
    "set_deleted",
    "set_include_in_export",
    "verdict_counts",
    "write_staging_label",
]
