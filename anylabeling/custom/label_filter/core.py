"""Pure logic behind the label filter: which label each image carries.

The module owns every rule of the feature and imports no Qt at its top
level, so the classification can be unit tested without a QApplication.
The file list of the labeling widget is a list of image paths and the
labels of an image live in the json file next to it (or in the
annotation directory the widget is configured with), so the whole
feature boils down to three questions:

* which json file belongs to an image - ``resolve_label_file`` mirrors
  the rule of ``LabelingWidget.import_image_folder`` exactly;
* which labels an image carries - ``LabelScanCache`` reads them once
  and keeps them until the json is touched again, reported by the pair
  (mtime_ns, size) of the file;
* which of those images a selection of categories keeps - ``is_hit``.

An image without a readable annotation is not a special case the user
has to know about: it is the category ``BACKGROUND_LABEL`` ("背景"),
the picture that carries no annotation at all. A json that is missing,
unreadable, broken, not an object, or free of shapes, and a shape whose
label is the empty string, all lead to the same empty label set, hence
to that one category. A dataset that already uses "背景" as a real
label produces the same category name, so both kinds of image merge
into one entry of the counts; nothing here has to special case it.

``classify`` reports a scan as a ``ScanResult``. A scan that the caller
stops (``should_stop``) returns ``None`` instead: a cancelled scan has no
result, and inventing an empty one would look like an empty folder.

The module is the only reader of the json files of the feature; the
widget itself is never imported, which keeps the import cheap and the
tests free of the whole application.
"""

from __future__ import annotations

import json
import os
import os.path as osp

__all__ = [
    "BACKGROUND_LABEL",
    "LabelScanCache",
    "ScanResult",
    "classify",
    "collect_files",
    "is_hit",
    "resolve_label_file",
]

#: Category of every image that carries no annotation: a missing or
#: unreadable json, a json without shapes, or shapes without a label.
BACKGROUND_LABEL = "背景"


def resolve_label_file(image_path, output_dir=None):
    """Return the json file that holds the labels of one image.

    The rule is the one ``LabelingWidget.import_image_folder`` applies
    when it builds its file list: the extension of the image is
    replaced by ``.json``, and the file is looked up in ``output_dir``
    (the annotation directory the user configured) as soon as one is
    set, never next to the image.

    Args:
        image_path: Path of the image file.
        output_dir: Annotation directory, or ``None`` for "next to the
            image".

    Returns:
        The path of the json file, whether it exists or not.
    """

    label_file = osp.splitext(str(image_path))[0] + ".json"
    if output_dir:
        label_file = output_dir + "/" + osp.basename(label_file)
    return label_file


class ScanResult:
    """Counters of one scan of a folder.

    Attributes:
        counts: ``{category name: number of images}``. Every name is a
            label that really appears in the folder, and
            ``BACKGROUND_LABEL`` is always present, even with a count of
            zero, because it is a category of the tool rather than a
            label of the dataset.
        files: The image paths of the scan, in the order they were
            given.
        total: Number of images.
        unlabeled: Images without any label (they are the images of the
            "背景" category).
        unreadable: Images whose json exists but could not be used as
            an annotation; they are unlabeled as well, this counter
            only says why.
        labels: The set of the real label names that appeared.
    """

    def __init__(self, counts, files, total, unlabeled, unreadable, labels):
        self.counts = counts
        self.files = files
        self.total = total
        self.unlabeled = unlabeled
        self.unreadable = unreadable
        self.labels = labels

    def categories(self):
        """Return the categories to show, most useful order first.

        The real labels come first, sorted by name (a plain, case
        sensitive sort, which is stable whatever the platform locale
        is), and the background category comes last: it is the one
        entry that is always there and the only one that is not a label
        of the dataset.
        """

        names = sorted(self.labels)
        if BACKGROUND_LABEL in self.counts and (
            BACKGROUND_LABEL not in self.labels
        ):
            names.append(BACKGROUND_LABEL)
        return names

    def __repr__(self):  # pragma: no cover - debugging aid
        return (
            "ScanResult(total=%d, unlabeled=%d, unreadable=%d, counts=%r)"
            % (self.total, self.unlabeled, self.unreadable, self.counts)
        )


def _entry_stat(path):
    """Return the (mtime_ns, size) pair that identifies a file version."""

    try:
        info = os.stat(path)
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size)


def _read_labels(path):
    """Return the labels of one json file, and whether it was readable.

    Returns:
        ``(labels, ok)``: ``labels`` is a frozenset of the non empty
        label strings of every shape (empty when the file is missing,
        unreadable, broken, not an object, or without shapes), and
        ``ok`` is False only when the file exists but could not be used
        as an annotation.
    """

    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return frozenset(), True
    except (OSError, ValueError):
        # ValueError covers JSONDecodeError and a file that is not
        # valid UTF-8; either way there is no annotation to read.
        return frozenset(), False
    if not isinstance(data, dict):
        return frozenset(), False
    shapes = data.get("shapes")
    if not isinstance(shapes, list):
        return frozenset(), True
    labels = set()
    for shape in shapes:
        if not isinstance(shape, dict):
            continue
        label = shape.get("label")
        if isinstance(label, str) and label:
            labels.add(label)
    return frozenset(labels), True


class LabelScanCache:
    """Labels of the json files, remembered per file version.

    A json is read once and stays in memory until its (mtime_ns, size)
    pair changes: the annotation of an image is written by the same
    application, so that pair is enough to notice a new save. A file
    that disappears is remembered as empty, not as unknown, which is
    what an image without its json needs to be.
    """

    def __init__(self):
        self._entries = {}

    def clear(self):
        """Forget every remembered json."""

        self._entries.clear()

    def __len__(self):
        return len(self._entries)

    def entry(self, path):
        """Return ``(labels, ok)`` for one json path, reading it once."""

        path = str(path)
        stat = _entry_stat(path)
        cached = self._entries.get(path)
        if cached is not None and cached[0] == stat:
            return cached[1], cached[2]
        labels, ok = _read_labels(path)
        self._entries[path] = (stat, labels, ok)
        return labels, ok

    def labels_of(self, image_path, output_dir=None):
        """Return the labels of one image as a frozenset."""

        labels, _ok = self.entry(resolve_label_file(image_path, output_dir))
        return labels


def collect_files(dirpath):
    """Return every image of a folder, the way the file list does it.

    The scan of the widget is reused rather than reimplemented: it
    walks the folder recursively and sorts the paths naturally, so the
    enumeration of the dialog matches the file list of the widget.

    The import happens inside the function on purpose: the scanner
    lives in the Qt part of the application, this module must not.
    """

    from anylabeling.views.labeling.utils.qt import scan_all_images

    return scan_all_images(dirpath)


def classify(
    files,
    output_dir=None,
    cache=None,
    progress_cb=None,
    should_stop=None,
):
    """Count the categories of a folder and return a ``ScanResult``.

    Args:
        files: The image paths to classify.
        output_dir: Annotation directory, or ``None``.
        cache: A ``LabelScanCache`` to reuse; a private one is built
            when none is given.
        progress_cb: Called as ``progress_cb(done, total)`` after every
            image.
        should_stop: Called before every image; when it returns a true
            value the scan stops and the function returns ``None``.

    Returns:
        A ``ScanResult``, or ``None`` when the scan was stopped.
    """

    files = list(files)
    if cache is None:
        cache = LabelScanCache()
    counts = {BACKGROUND_LABEL: 0}
    labels = set()
    unlabeled = 0
    unreadable = 0
    total = len(files)
    for index, image_path in enumerate(files):
        if should_stop is not None and should_stop():
            return None
        found, ok = cache.entry(
            resolve_label_file(image_path, output_dir)
        )
        if not ok:
            unreadable += 1
        for label in found:
            labels.add(label)
            counts[label] = counts.get(label, 0) + 1
        if not found:
            unlabeled += 1
            counts[BACKGROUND_LABEL] += 1
        if progress_cb is not None:
            progress_cb(index + 1, total)
    return ScanResult(
        counts=counts,
        files=files,
        total=total,
        unlabeled=unlabeled,
        unreadable=unreadable,
        labels=labels,
    )


def is_hit(image_path, output_dir, selected, cache):
    """Return whether one image is kept by a selection of categories.

    The selection is a set of category names; a name only matches a
    label exactly (no case folding), because the dialog enumerates the
    names as they are written in the json files. The categories are
    combined with "or": an image is kept as soon as one of its labels
    is selected. An image without any label belongs to
    ``BACKGROUND_LABEL``, so it is kept when - and only when - that
    category is selected.
    """

    labels = cache.labels_of(image_path, output_dir)
    if not labels:
        return BACKGROUND_LABEL in selected
    return bool(labels & set(selected))
