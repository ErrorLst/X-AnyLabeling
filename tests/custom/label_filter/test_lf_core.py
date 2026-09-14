"""Tests of the classification core: which label an image carries.

The cases of the folder are built on disk in the system temp folder
and scanned through the real scanner of the widget, so the counts are
checked against the same enumeration the file list uses.
"""

from __future__ import annotations

import os
import os.path as osp
import time

from conftest import make_image, write_image, write_json

from anylabeling.custom.label_filter import core


def test_resolve_label_file_next_to_the_image(lf_scratch):
    image = osp.join(lf_scratch, "a.jpg")
    assert core.resolve_label_file(image) == osp.join(
        lf_scratch, "a.json"
    )


def test_resolve_label_file_in_the_output_dir(lf_scratch):
    image = osp.join(lf_scratch, "nested", "a.jpeg")
    out = osp.join(lf_scratch, "labels")
    assert core.resolve_label_file(image, out) == out + "/a.json"


def test_resolve_label_file_ignores_an_empty_output_dir(lf_scratch):
    image = osp.join(lf_scratch, "a.png")
    assert core.resolve_label_file(image, None) == image[:-4] + ".json"
    assert core.resolve_label_file(image, "") == image[:-4] + ".json"


def test_collect_files_uses_the_widget_scanner(lf_scratch):
    write_image(lf_scratch, "b10.jpg")
    write_image(lf_scratch, "b2.jpg")
    write_image(lf_scratch, "notes.txt")
    nested = osp.join(lf_scratch, "deep")
    os.makedirs(nested)
    write_image(nested, "b1.jpg")
    files = core.collect_files(lf_scratch)
    # The scanner sorts the whole path naturally and walks the folder
    # before it descends, so the nested file comes last.
    assert [osp.basename(path) for path in files] == [
        "b2.jpg",
        "b10.jpg",
        "b1.jpg",
    ]
    assert files[-1] == osp.join(nested, "b1.jpg")
    assert osp.isabs(files[0])


def test_classify_counts_every_category(lf_scratch):
    make_image(lf_scratch, "img1", ["person"])
    make_image(lf_scratch, "img2", ["person", "car"])
    make_image(lf_scratch, "img3", ["car"])
    write_image(lf_scratch, "img4.jpg")  # no json at all
    make_image(lf_scratch, "img5", [])  # empty shapes
    write_json(
        lf_scratch,
        "img6.json",
        raw=b"{not json",
    )
    write_image(lf_scratch, "img6.jpg")
    files = core.collect_files(lf_scratch)
    result = core.classify(files)

    assert result.total == 6
    assert result.counts["person"] == 2
    assert result.counts["car"] == 2
    assert result.counts[core.BACKGROUND_LABEL] == 3
    assert result.unlabeled == 3
    assert result.unreadable == 1
    assert result.labels == {"person", "car"}
    assert result.categories() == [
        "car",
        "person",
        core.BACKGROUND_LABEL,
    ]
    assert result.files == files


def test_a_real_background_label_merges_with_the_category(lf_scratch):
    make_image(lf_scratch, "img1", [core.BACKGROUND_LABEL])
    write_image(lf_scratch, "img2.jpg")
    result = core.classify(core.collect_files(lf_scratch))

    assert result.counts == {core.BACKGROUND_LABEL: 2}
    assert result.categories() == [core.BACKGROUND_LABEL]
    assert result.unlabeled == 1
    assert result.labels == {core.BACKGROUND_LABEL}


def test_classify_reports_a_stopped_scan_as_none(lf_scratch):
    for index in range(4):
        make_image(lf_scratch, "img%d" % index, ["person"])
    calls = []
    result = core.classify(
        core.collect_files(lf_scratch),
        progress_cb=lambda done, total: calls.append((done, total)),
        should_stop=lambda: len(calls) >= 2,
    )
    assert result is None
    assert calls == [(1, 4), (2, 4)]


def test_classify_reports_progress_for_every_image(lf_scratch):
    make_image(lf_scratch, "img1", ["person"])
    make_image(lf_scratch, "img2", None)
    calls = []
    core.classify(
        core.collect_files(lf_scratch),
        progress_cb=lambda done, total: calls.append((done, total)),
    )
    assert calls == [(1, 2), (2, 2)]


def test_empty_shapes_and_empty_labels_are_background(lf_scratch):
    make_image(lf_scratch, "img1", [])
    make_image(lf_scratch, "img2", [""])
    write_json(
        lf_scratch,
        "img3.json",
        raw=b"[1, 2, 3]",
    )
    write_image(lf_scratch, "img3.jpg")
    result = core.classify(core.collect_files(lf_scratch))
    assert result.counts == {core.BACKGROUND_LABEL: 3}
    assert result.unreadable == 1


def test_a_renamed_annotation_dir_moves_the_json_lookup(lf_scratch):
    images = osp.join(lf_scratch, "images")
    first = osp.join(lf_scratch, "first")
    second = osp.join(lf_scratch, "second")
    for path in (images, first, second):
        os.makedirs(path)
    write_image(images, "img1.jpg")
    write_json(first, "img1.json", shapes=[{"label": "a"}])
    write_json(second, "img1.json", shapes=[{"label": "b"}])

    files = core.collect_files(images)
    cache = core.LabelScanCache()
    assert core.classify(files, output_dir=first, cache=cache).labels == {
        "a"
    }
    assert core.classify(files, output_dir=second, cache=cache).labels == {
        "b"
    }


def test_the_cache_forgets_a_rewritten_json(lf_scratch):
    image, label = make_image(lf_scratch, "img1", ["person"])
    cache = core.LabelScanCache()
    assert cache.labels_of(image) == frozenset({"person"})
    write_json(
        lf_scratch,
        "img1.json",
        shapes=[{"label": "car"}, {"label": "bike"}],
    )
    assert cache.labels_of(image) == frozenset({"car", "bike"})


def test_the_cache_notices_a_touch_of_the_same_json(lf_scratch):
    image, label = make_image(lf_scratch, "img1", ["person"])
    cache = core.LabelScanCache()
    assert cache.labels_of(image) == frozenset({"person"})
    before = os.stat(label)
    later = 10 ** 9
    os.utime(
        label,
        ns=(before.st_atime_ns + later, before.st_mtime_ns + later),
    )
    assert cache.labels_of(image) == frozenset({"person"})
    assert osp.getsize(label) == before.st_size
    assert os.stat(label).st_mtime_ns != before.st_mtime_ns


def test_a_read_json_is_read_once(lf_scratch):
    image, label = make_image(lf_scratch, "img1", ["person"])
    cache = core.LabelScanCache()
    cache.labels_of(image)
    before = os.stat(label).st_atime_ns
    time.sleep(0.01)
    cache.labels_of(image)
    assert os.stat(label).st_atime_ns == before
    assert len(cache) == 1
    cache.clear()
    assert len(cache) == 0
    assert cache.labels_of(image) == frozenset({"person"})


def test_a_missing_json_is_background_and_never_unreadable(lf_scratch):
    image, label = make_image(lf_scratch, "img1", ["person"])
    os.rename(label, label + ".moved")
    cache = core.LabelScanCache()
    assert cache.labels_of(image) == frozenset()
    result = core.classify([image])
    assert result.counts == {core.BACKGROUND_LABEL: 1}
    assert result.unreadable == 0


def test_is_hit_or_semantics(lf_scratch):
    person, label = make_image(lf_scratch, "person1", ["person"])
    car, label2 = make_image(lf_scratch, "car1", ["car"])
    none, label3 = make_image(lf_scratch, "none1", None)
    cache = core.LabelScanCache()

    assert core.is_hit(person, None, {"person"}, cache)
    assert not core.is_hit(person, None, {"car"}, cache)
    assert core.is_hit(person, None, {"car", "person"}, cache)
    assert core.is_hit(car, None, {"person"}, cache) is False
    assert core.is_hit(car, None, {"person", "car"}, cache)
    assert core.is_hit(none, None, {core.BACKGROUND_LABEL}, cache)
    assert not core.is_hit(none, None, {"person"}, cache)


def test_is_hit_matches_the_name_exactly(lf_scratch):
    image, label = make_image(lf_scratch, "img1", ["Person"])
    cache = core.LabelScanCache()
    assert core.is_hit(image, None, {"Person"}, cache)
    assert not core.is_hit(image, None, {"person"}, cache)
