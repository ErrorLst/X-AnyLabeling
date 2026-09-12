"""Assemble the validation_report.json document."""

from __future__ import annotations

import datetime
import os.path as osp
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import records as records_module
from .json_safe import as_list, as_mapping, sanitize
from .labelme_io import LABELME_KNOWN_LIMITATIONS
from .onnx_meta import truncate_diff
from .records import ValidationRecord

REPORT_VERSION = 1

# the copy of the report written next to the zip, inside the staging
# folder: export_zip reuses it when a caller does not hand one over
STAGING_REPORT_FILENAME = "validation_report.json"

AUGMENT_KNOWN_LIMITATIONS: Tuple[str, ...] = (
    "circle is rebuilt from the centroid and the mean radius of the "
    "transformed probes, therefore a circle under Perspective is "
    "approximated instead of becoming an ellipse",
    "a sample whose transform leaves a shape outside the picture is "
    "generated again with a fresh derived seed (augment.retried counts "
    "those samples) and is dropped when no attempt fits "
    "(augment.discarded_unfittable counts those), so a planned copy may "
    "be missing instead of being produced with a clipped defect",
)


def timestamp() -> str:
    """Return the current local time as an ISO-8601 string."""

    return datetime.datetime.now().replace(microsecond=0).isoformat()


def build_record_payload(
    record: ValidationRecord,
    records: Sequence[ValidationRecord],
) -> Dict[str, Any]:
    """Serialise one record for the validation report."""

    entry = record.to_dict()
    entry["source_display"] = (
        record.source_display
        + " (display only - MUST NOT be used for file access)"
    )
    entry["primary_reason"] = record.reasons[0] if record.reasons else ""
    entry["excluded_by_deleted_parent"] = records_module.parent_deleted(
        list(records), record
    )
    entry["in_export"] = in_export(record, records)
    # the entry is assembled from the record only: sanitize is the last
    # line of defence against a foreign object stored in detail or in
    # aug_detail (a numpy scalar, a Path, a callback).
    return sanitize(entry)


def in_export(
    record: ValidationRecord, records: Sequence[ValidationRecord]
) -> bool:
    """Apply the export formula to a single record."""

    if record.kind == records_module.KIND_AUGMENTED:
        return bool(
            record.include_in_export
        ) and not records_module.parent_deleted(list(records), record)
    return not record.deleted and record.verdict != records_module.SKIPPED


def build_report(
    staging_root: str,
    records: Sequence[ValidationRecord],
    model_info: Optional[Dict[str, Any]] = None,
    classes: Optional[Sequence[str]] = None,
    config: Optional[Dict[str, Any]] = None,
    augment_summary: Optional[Dict[str, Any]] = None,
    staging_meta: Optional[Dict[str, Any]] = None,
    warnings: Optional[Sequence[str]] = None,
    previous_staging_roots: Optional[Sequence[str]] = None,
    export_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the full validation report document."""

    selection = records_module.export_selection(list(records))
    summary = records_module.export_summary(list(records))
    verdicts = records_module.verdict_counts(list(records))
    clipped = [
        {
            "record_id": record.record_id,
            "relpath": record.relpath,
            "shapes": record.aug_detail.get("clipped", []),
        }
        for record in records
        if record.aug_detail.get("clipped")
    ]
    edited = [record.record_id for record in records if record.edited]
    judged = [record.record_id for record in records if record.judged]

    # the model section records the embedded names of the ONNX and the
    # effective class table side by side so that a later audit can tell
    # which names produced the labels of the report.
    model_section = as_mapping(model_info)
    model_section.setdefault(
        "names_model", as_list(model_section.get("names"))
    )
    model_section.setdefault("classes", as_list(classes))
    # the per index diff stays truncated in the report, exactly like the
    # status line of the UI.
    model_section["classes_name_diff"] = truncate_diff(
        as_list(model_section.get("classes_name_diff"))
    )

    augment_section = as_mapping(augment_summary)
    staging_section = as_mapping(staging_meta)

    summary = dict(summary)
    summary["verdicts"] = dict(verdicts)
    summary["total_records"] = len(records)
    summary["ng"] = int(verdicts.get(records_module.NG, 0))
    summary["ok"] = int(verdicts.get(records_module.OK, 0))
    summary["not_judged"] = int(verdicts.get(records_module.NOT_JUDGED, 0))

    # Every section holds data collected from a different source, so
    # the whole document is rebuilt with JSON primitives before it is
    # handed over: neither a Qt object nor a callback given by a caller
    # may reach the writer. dumps_safe of the export layer is the second
    # defence, for a document that never went through this function.
    document = {
        "version": REPORT_VERSION,
        "generated_at": timestamp(),
        "staging_root": staging_root,
        "previous_staging_roots": as_list(previous_staging_roots),
        "source_display": (
            "the source dataset is display only - MUST NOT be used for "
            "file access"
        ),
        "model": model_section,
        "classes": as_list(classes),
        "config": as_mapping(config),
        "augment": augment_section,
        "disabled_multi_image_augmentations": as_list(
            augment_section.get("disabled_multi_image_augmentations")
        ),
        "excluded_pairs": {
            "unreadable_label_pairs": as_list(
                staging_section.get("unreadable_label_pairs")
            ),
            "note": (
                "pairs whose json cannot be read as an xlabel file are "
                "excluded: they are neither staged, nor judged, nor "
                "exported"
            ),
        },
        "labelme_known_limitations": list(LABELME_KNOWN_LIMITATIONS),
        "augment_known_limitations": list(AUGMENT_KNOWN_LIMITATIONS),
        "summary": summary,
        "clipped_shapes": clipped,
        "clipped_count": sum(len(item["shapes"]) for item in clipped),
        "edited_records": edited,
        "judged_records": judged,
        "warnings": as_list(warnings),
        "export_path": export_path,
        "export_selection": {
            "originals": [r.record_id for r in selection["originals"]],
            "augmented": [r.record_id for r in selection["augmented"]],
        },
        "records": [
            build_record_payload(record, records) for record in records
        ],
    }
    return sanitize(document)


def write_report(
    staging_root: str,
    report: Dict[str, Any],
    filename: str = STAGING_REPORT_FILENAME,
) -> str:
    """Write the report next to the zip copy into the staging folder."""

    from . import dataset

    path = osp.join(staging_root, filename)
    dataset.write_json(path, sanitize(report))
    return path


__all__ = [
    "AUGMENT_KNOWN_LIMITATIONS",
    "REPORT_VERSION",
    "STAGING_REPORT_FILENAME",
    "build_record_payload",
    "build_report",
    "in_export",
    "timestamp",
    "write_report",
]
