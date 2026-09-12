"""Export the selected staging files into a zip archive.

Only the staging folder is read here. The source dataset directory must
never appear in this module.
"""

from __future__ import annotations

import os
import os.path as osp
import zipfile
from collections.abc import Mapping
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import records as records_module
from .app_config import ValidationConfigError
from .json_safe import as_list
from .records import ValidationRecord

PROGRESS_INTERVAL = 8

# The one folder of the archive. Every picture is written into it under
# the relative path it carries inside the source dataset, and every
# picture is joined there by the one xlabel json that belongs to it.
IMAGES_DIRNAME = "images"

# The suffix a colliding name is made unique with: the picture *and* its
# json are renamed together, never one of the two alone.
RENAME_SEPARATOR = "_"

# The one user visible message of this module: the dialog shows it in the
# state line of a cancelled export. It is Chinese like the rest of the
# model validation user interface; the progress lines of write_zip stay
# English, they are development output.
CANCELLED_MESSAGE = "用户已取消导出"


class ExportCancelled(Exception):
    """Raised when the user cancels a running export."""


def read_image_size(image_path: str):
    """Return (height, width) of a staging image or (None, None).

    Kept as the documented reader of a staging picture size. The archive
    no longer builds a LabelMe document, which is what used to ask for
    the two numbers, so nothing inside this module calls it any more.
    """

    import cv2
    import numpy as np

    if not osp.isfile(image_path):
        return None, None
    try:
        buffer = np.fromfile(image_path, dtype=np.uint8)
        image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    except (OSError, ValueError, cv2.error):
        return None, None
    if image is None:
        return None, None
    return int(image.shape[0]), int(image.shape[1])


def appended_path(prefix: str, relpath: str) -> str:
    """Join a zip prefix with a normalised relative path."""

    rel = relpath.replace(os.sep, "/").lstrip("/")
    return prefix + "/" + rel


def zip_relative_path(relpath: str) -> str:
    """Return the path of one record inside images/ of the archive.

    The relative path of a record is the path the file carries inside
    the source dataset - a staged original keeps it as it was collected,
    a staged augmented copy mirrors the folder of its parent (see
    augment.augmented_relpath) - so the archive mirrors the structure of
    the user's own dataset instead of flattening it: front/a.png and
    back/a.png stay two different entries of the same folder. Every
    separator is written as "/", which is what zip uses whatever the
    platform of the writer.
    """

    rel = str(relpath or "").replace(os.sep, "/").replace("\\", "/")
    return rel.lstrip("/")


def images_path(relpath: str) -> str:
    """Return the archive path of one picture, under images/."""

    return IMAGES_DIRNAME + "/" + zip_relative_path(relpath)


def with_suffix(relpath: str, suffix: str) -> str:
    """Return a path whose name carries one more suffix before its type.

    "a_aug1.png" with "_1" is "a_aug1_1.png"; the extension - whatever
    its case, and a name without any - is kept as it is, and the folder
    of the path never takes part in the rename.
    """

    folder, base = osp.split(str(relpath or ""))
    stem, extension = osp.splitext(base)
    name = stem + str(suffix) + extension
    return folder + "/" + name if folder else name


def unique_zip_names(
    wanted: Sequence[Dict[str, str]],
) -> List[Dict[str, str]]:
    """Make the archive names of a list of entries unique.

    Every entry is a mapping with an "image" name and the "json" name
    that belongs to it; both are renamed *together* when a name is
    already taken, first with "_1", then "_2" and so on. Nothing is
    overwritten and nothing is dropped: a name that is already taken -
    the very pathological dataset of this tool, say a source file
    "a_aug1.png" next to the augmented copy of "a.png" - costs one
    suffix and is counted in the summary of the export.
    """

    taken: set = set()
    result: List[Dict[str, str]] = []
    for entry in wanted:
        image = str(entry.get("image", ""))
        label = str(entry.get("json", ""))
        counter = 0
        while image in taken or label in taken:
            counter += 1
            suffix = RENAME_SEPARATOR + str(counter)
            image = with_suffix(str(entry.get("image", "")), suffix)
            label = with_suffix(str(entry.get("json", "")), suffix)
        taken.add(image)
        taken.add(label)
        result.append(
            {
                "image": image,
                "json": label,
                "renamed": image != str(entry.get("image", "")),
            }
        )
    return result


def looks_like_path(value: Any) -> bool:
    """Return True for a value naming a file (a string or a path)."""

    return isinstance(value, (str, os.PathLike))


def resolve_export_arguments(
    zip_path: Any,
    classes: Any,
    report: Any,
    progress: Optional[Callable[[int, int, str], None]],
    is_cancelled: Any,
) -> Dict[str, Any]:
    """Return the arguments of both documented call shapes.

    The tool documents

        export_zip(records, staging_root, classes_file, zip_path,
                   progress_cb, cancel_flag)

    while the results page calls

        export_zip(records, staging_root, zip_path, classes, report,
                   progress=..., is_cancelled=...)

    The report tells the two apart: a report document is always a
    mapping, whereas the short shape puts the progress callback in that
    position. Reading the callback as if it were the report is what made
    json.dumps raise "Object of type function is not JSON serializable"
    and left the user without an archive, so the shift is applied
    explicitly here instead.
    """

    if isinstance(report, Mapping):
        return {
            "zip_path": zip_path,
            "classes": classes,
            "classes_file": None,
            "report": report,
            "progress": progress,
            "is_cancelled": is_cancelled,
        }
    if looks_like_path(zip_path) and looks_like_path(classes):
        # the classes file shape: everything after the staging root
        # moved one position to the left
        return {
            "zip_path": classes,
            "classes": None,
            "classes_file": zip_path,
            "report": None,
            "progress": report if callable(report) else progress,
            "is_cancelled": progress if progress is not None else is_cancelled,
        }
    # no report at all: the export chain of this revision needs none,
    # because the archive holds no validation_report.json any more (see
    # write_zip). Nothing is staged, read back or assembled here - the
    # field is answered with None and stays the ignored parameter of
    # the export it always was.
    return {
        "zip_path": zip_path,
        "classes": classes,
        "classes_file": None,
        "report": None,
        "progress": progress,
        "is_cancelled": is_cancelled,
    }


def cancellation_requested(is_cancelled: Any) -> bool:
    """Return True when a cancelling callback or flag asks to stop.

    A caller hands over either a callable (the wasCanceled signal of the
    progress dialog, a lambda reading a flag) or a flag object such as a
    threading.Event or a plain boolean: both are accepted so that an
    unexpected shape can never break a running export.
    """

    if is_cancelled is None:
        return False
    if callable(is_cancelled):
        return bool(is_cancelled())
    for name in ("is_set", "is_cancelled", "isCanceled", "wasCanceled"):
        getter = getattr(is_cancelled, name, None)
        if callable(getter):
            return bool(getter())
    return bool(is_cancelled)


def load_export_classes(classes: Any, classes_file: Any) -> List[str]:
    """Return the class table of the export as a list of strings.

    An explicit class list wins; otherwise the classes file is loaded
    with the very same validation as the configuration page, so a
    duplicated or empty class table is still refused.
    """

    if classes is not None:
        return [str(name) for name in as_list(classes)]
    if classes_file is None:
        raise ValidationConfigError(
            "export needs a class table: pass the classes or a classes file"
        )
    from .app_config import load_classes_file

    names = load_classes_file(os.fspath(classes_file))
    return [str(name) for name in names]


def staged_or_built_report(
    staging_root: str,
    records: Sequence[ValidationRecord],
    classes: Sequence[str],
) -> Dict[str, Any]:
    """Return the report of an export that was given none.

    Kept as the compatible API of the previous revision: the export
    chain of this revision does not call it any more (see
    resolve_export_arguments and write_zip), and no export archive holds
    a validation_report.json since this revision - the report parameter
    of write_zip and export_zip is accepted and ignored. The function
    still answers what a caller of the previous revision asks it for:
    the report already written into the staging folder is reused when it
    is there - it carries the model section and the staging meta of the
    run - otherwise a plain report is assembled from the records.
    """

    from . import dataset
    from .report import STAGING_REPORT_FILENAME

    stored: Any = None
    for name in (dataset.REPORT_FILENAME, STAGING_REPORT_FILENAME):
        path = osp.join(staging_root, name)
        if not osp.isfile(path):
            continue
        try:
            stored = dataset.read_json(path)
        except (OSError, ValueError):
            stored = None
        if isinstance(stored, dict):
            return stored
    from .report import build_report

    return build_report(staging_root, list(records), classes=list(classes))


def build_export_entries(
    records: Sequence[ValidationRecord],
) -> Dict[str, Any]:
    """Compute the zip entries implied by the export formula.

    One entry per exported record: the record itself and the two names
    it takes inside the archive - its picture under images/ and the
    xlabel json of the very same name and folder next to it. There is
    no LabelMe document any more: the archive carries one json per
    picture, the annotation the tool itself reads back.
    """

    from . import dataset

    selection = records_module.export_selection(list(records))
    entries: List[Dict[str, Any]] = []
    # An image without a label never reaches this point: export_selection
    # already excluded it. The list below only collects selected records
    # whose staging files disappeared or cannot be read.
    missing_files: List[str] = []

    def add(record: ValidationRecord) -> None:
        if not record.label_exists or not record.image_exists:
            missing_files.append(record.relpath)
            return
        label = dataset.read_json(record.staging_label_path)
        if not isinstance(label, dict):
            missing_files.append(record.relpath)
            return
        image_name = images_path(record.relpath)
        entries.append(
            {
                "record": record,
                "image_name": image_name,
                "json_name": osp.splitext(image_name)[0] + ".json",
            }
        )

    for record in selection["originals"]:
        add(record)
    for record in selection["augmented"]:
        add(record)

    names = unique_zip_names(
        [
            {"image": entry["image_name"], "json": entry["json_name"]}
            for entry in entries
        ]
    )
    renames: List[Dict[str, str]] = []
    for entry, name in zip(entries, names):
        if name["renamed"]:
            renames.append(
                {
                    "relpath": str(entry["record"].relpath),
                    "from": str(entry["image_name"]),
                    "to": str(name["image"]),
                }
            )
        entry["image_name"] = name["image"]
        entry["json_name"] = name["json"]

    return {
        "entries": entries,
        "excluded_missing_files": missing_files,
        "selection": selection,
        "renames": renames,
    }


def write_zip(
    entries: Sequence[Dict[str, Any]],
    zip_path: str,
    classes: Sequence[str],
    report: Any = None,
    progress: Optional[Callable[[int, int, str], None]] = None,
    is_cancelled: Any = None,
) -> Dict[str, Any]:
    """Write every entry into the zip archive and return its summary.

    The layout of the archive is exactly two things: the class table at
    its root and one folder "images" holding every exported pair of the
    run - the picture under the relative path of the record, and the
    xlabel json of the very same name right next to it. There is no
    original / augmented split any more and no second json per picture:
    the two kinds live side by side, told apart by the file name the
    augmentation gave the copy, and every picture carries the one
    annotation this tool reads back.

    The summary holds "entries" (the pairs really written), "zip_entries"
    (the files really written: two per pair) and "renames" (the pairs the
    uniqueness pass had to rename, see unique_zip_names).

    The report parameter is accepted and *ignored*: the archive carries
    no validation_report.json any more. It stays in the signature so a
    caller of the previous revision keeps working, and the report of the
    run is left where it always was - in the staging folder and in the
    hands of its caller - instead of being copied into the archive.

    Only staging paths are written: the picture and its json come
    straight from the staging folder, so the source dataset is never
    read here and never written anywhere.
    """

    def hit_cancel() -> None:
        if cancellation_requested(is_cancelled):
            raise ExportCancelled(CANCELLED_MESSAGE)

    def emit(done: int, total: int, message: str) -> None:
        if progress is not None:
            progress(done, total, message)

    total = len(entries)
    text = "\n".join(str(name) for name in as_list(classes)) + "\n"
    class_bytes = text.encode("utf-8")

    written = 0
    renames: List[Dict[str, str]] = []
    with zipfile.ZipFile(
        zip_path, "w", compression=zipfile.ZIP_DEFLATED
    ) as archive:
        archive.writestr("classes.txt", class_bytes)
        for index, entry in enumerate(entries):
            hit_cancel()
            record = entry["record"]
            image_entry = str(entry["image_name"])
            json_entry = str(entry["json_name"])
            if image_entry != images_path(record.relpath):
                renames.append(
                    {
                        "relpath": str(record.relpath),
                        "from": images_path(record.relpath),
                        "to": image_entry,
                    }
                )
            archive.write(record.staging_image_path, image_entry)
            archive.write(record.staging_label_path, json_entry)
            written += 2
            if index % PROGRESS_INTERVAL == 0:
                emit(index, total, f"Packaging {index + 1}/{total}")
        emit(total, total, "Done")
    return {
        "entries": total,
        "zip_entries": written,
        "renames": renames,
        "renamed_entries": len(renames),
    }


def export_zip(
    records: Sequence[ValidationRecord],
    staging_root: str,
    zip_path: Any,
    classes: Any = None,
    report: Any = None,
    progress: Optional[Callable[[int, int, str], None]] = None,
    is_cancelled: Any = None,
) -> Dict[str, Any]:
    """Write the export zip and return the export summary.

    Both documented call shapes are accepted, see
    resolve_export_arguments for how they are told apart.

    The archive is built from the staging folder alone and holds the
    class table plus the images/ folder of the export (see write_zip).
    A report the caller hands over is neither written into the archive
    nor onto the disk any more: the report of the run is the business of
    whoever asked for an export, and the export itself stays a read only
    pass over the staging folder. The parameter is kept for the call
    shapes of the previous revision.
    """

    from . import dataset

    arguments = resolve_export_arguments(
        zip_path, classes, report, progress, is_cancelled
    )
    target = os.fspath(arguments["zip_path"])
    names = load_export_classes(
        arguments["classes"], arguments["classes_file"]
    )

    payload = build_export_entries(records)
    entries = payload["entries"]
    written = write_zip(
        entries,
        target,
        names,
        arguments["report"],
        progress=arguments["progress"],
        is_cancelled=arguments["is_cancelled"],
    )

    summary = records_module.export_summary(list(records))
    summary["excluded_missing_staging_files"] = len(
        payload["excluded_missing_files"]
    )
    meta_path = osp.join(staging_root, dataset.META_FILENAME)
    orphan_labels = []
    if osp.isfile(meta_path):
        meta = dataset.read_json(meta_path)
        if isinstance(meta, dict):
            orphan_labels = meta.get("orphan_labels", [])
    summary["orphan_labels"] = len(orphan_labels)
    summary["renamed_entries"] = int(written["renamed_entries"])
    summary["renames"] = list(written["renames"])
    summary["zip_entries"] = int(written["zip_entries"])
    summary["zip_path"] = target
    return summary


__all__ = [
    "CANCELLED_MESSAGE",
    "ExportCancelled",
    "IMAGES_DIRNAME",
    "PROGRESS_INTERVAL",
    "RENAME_SEPARATOR",
    "appended_path",
    "build_export_entries",
    "cancellation_requested",
    "export_zip",
    "images_path",
    "load_export_classes",
    "looks_like_path",
    "read_image_size",
    "resolve_export_arguments",
    "staged_or_built_report",
    "unique_zip_names",
    "with_suffix",
    "write_zip",
    "zip_relative_path",
]
