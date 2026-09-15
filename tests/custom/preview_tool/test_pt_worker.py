"""Tests of the background scan, the preloader and the pick jobs.

Every job is driven by a direct call of run(), on the main thread, so a
test sees the signals of one job in a deterministic order. The scratch
directories come from the shared conftest and end up in the temp trash
folder; no test writes anything inside the repository.
"""

import os
import time

from PyQt6 import QtWidgets

from anylabeling.custom.preview_tool import (
    core,
    pick_core,
    pick_worker,
    worker,
)

from conftest import make_shape, trash_listing, write_image, write_json


def _settle(loader, rounds=200):
    """Wait for every decode of a preloader and deliver its result.

    The preloader decodes in a real thread pool, so a test must not
    guess when a single image has come back: this barrier waits for
    the pool itself, pumps the events that carry the results and only
    returns when nothing is in flight any more. The cache of the
    preloader is therefore in its final state, whatever the load of
    the machine is.
    """

    app = QtWidgets.QApplication.instance()
    assert loader.wait_for_decodes(10000) is True
    for _round in range(rounds):
        if app is not None:
            app.processEvents()
        if not loader.pending():
            break
        time.sleep(0.005)
    assert loader.pending() == ()


def _images(directory, count, size=(8, 8), ext=".png"):
    """Write count deterministic images and return their paths."""

    return [
        write_image(directory, "img_%d%s" % (index, ext), size=size)
        for index in range(count)
    ]


# --------------------------------------------------------------- scanning


def test_scan_returns_natural_order_and_tuple_points(pt_dir):
    write_image(pt_dir, "img_10.jpg", size=(6, 4))
    write_image(pt_dir, "img_2.jpg", size=(6, 4))
    write_json(
        pt_dir, "img_2.json", shapes=[make_shape(score=0.9)]
    )
    write_json(pt_dir, "img_10.json", shapes=[])

    scan = worker.PreviewWorker(pt_dir)
    seen = []
    scan.scanned.connect(seen.append)
    scan.run()

    assert len(seen) == 1
    snapshot = seen[0]
    assert snapshot.directory == pt_dir
    assert [entry.name for entry in snapshot.entries] == [
        "img_2.jpg",
        "img_10.jpg",
    ]
    assert snapshot.total == 2
    assert snapshot.stopped is False

    entry = snapshot.entries[0]
    assert entry.json_path == os.path.join(pt_dir, "img_2.json")
    assert entry.labels == frozenset({"cat"})
    assert len(entry.shapes) == 1
    shape = entry.shapes[0]
    assert isinstance(shape.points, tuple)
    for point in shape.points:
        assert isinstance(point, tuple)
        assert len(point) == 2
        assert isinstance(point[0], float)
        assert isinstance(point[1], float)


def test_scan_reports_progress_from_zero_to_total(pt_dir):
    _images(pt_dir, 3, ext=".jpg")
    scan = worker.PreviewWorker(pt_dir)
    progress = []
    scan.progress.connect(
        lambda done, total: progress.append((done, total))
    )
    scan.run()
    assert progress[0] == (0, 3)
    assert progress[-1] == (3, 3)


def test_scan_stops_early_when_it_is_interrupted(pt_dir):
    _images(pt_dir, 4, ext=".jpg")
    scan = worker.PreviewWorker(pt_dir)
    seen = []
    scan.scanned.connect(seen.append)
    # Qt only arms the flag of a running thread, so the answer of the
    # worker is stubbed instead of racing the thread.
    scan.isInterruptionRequested = lambda: True
    scan.run()
    assert len(seen) == 1
    assert seen[0].stopped is True
    assert seen[0].entries == []
    assert seen[0].total == 4


def test_scan_of_a_missing_directory_is_empty(pt_dir):
    scan = worker.PreviewWorker(os.path.join(pt_dir, "no-such-folder"))
    seen = []
    scan.scanned.connect(seen.append)
    scan.run()
    assert seen[0].entries == []
    assert seen[0].total == 0
    assert seen[0].stopped is False


def test_scan_ignores_a_broken_side_car(pt_dir):
    write_image(pt_dir, "a.png", size=(4, 4))
    with open(os.path.join(pt_dir, "a.json"), "w", encoding="utf-8") as fh:
        fh.write("{not json")
    scan = worker.PreviewWorker(pt_dir)
    seen = []
    scan.scanned.connect(seen.append)
    scan.run()
    assert [entry.name for entry in seen[0].entries] == ["a.png"]
    assert seen[0].entries[0].shapes == ()


# ------------------------------------------------------------- preloading


def test_preloader_loads_the_neighbourhood_only(pt_dir):
    paths = _images(pt_dir, 6)
    loader = worker.PreviewPreloader()
    try:
        loader.request(0, paths)
        assert loader.get(paths[5]) is None
        _settle(loader)
        cached = set(loader.cached_paths())
        assert cached == set(paths[:3])
        assert loader.pixel_total() == 3 * 64
        assert loader.get(paths[5]) is None
        image = loader.get(paths[0])
        assert image.width() == 8
        assert image.height() == 8
    finally:
        loader.invalidate()


def test_preloader_invalidate_clears_the_cache(pt_dir):
    paths = _images(pt_dir, 4)
    loader = worker.PreviewPreloader()
    try:
        loader.request(0, paths)
        _settle(loader)
        assert loader.pixel_total() == 3 * 64
        assert len(loader.cached_paths()) == 3
        assert loader.get(paths[0]) is not None
        loader.invalidate()
        assert loader.get(paths[0]) is None
        assert loader.cached_paths() == ()
        assert loader.pixel_total() == 0
        assert loader.pending() == ()
    finally:
        loader.invalidate()


def test_preloader_invalidate_drops_the_decodes_in_flight(pt_dir):
    """A result of an invalidated generation is never written back."""

    paths = _images(pt_dir, 8)
    loader = worker.PreviewPreloader(threads=1)
    try:
        loader.request(0, paths)
        loader.invalidate()
        _settle(loader)
        assert loader.cached_paths() == ()
        assert loader.pixel_total() == 0
        assert loader.get(paths[0]) is None
        assert loader.get(paths[2]) is None

        # The next generation still works after the stale one.
        loader.request(0, paths)
        _settle(loader)
        assert set(loader.cached_paths()) == set(paths[:3])
        assert loader.pixel_total() == 3 * 64
    finally:
        loader.invalidate()


def test_preloader_drops_the_oldest_image_over_the_budget(pt_dir):
    paths = _images(pt_dir, 3, size=(20, 20))
    loader = worker.PreviewPreloader(budget=800, threads=1)
    try:
        loader.request(0, paths)
        _settle(loader)
        assert loader.pixel_total() <= 800
        assert len(loader.cached_paths()) == 2
        assert set(loader.cached_paths()) == {paths[1], paths[2]}
        assert loader.get(paths[0]) is None
        assert loader.get(paths[2]) is not None
    finally:
        loader.invalidate()


def test_preloader_returns_none_for_an_unknown_path(pt_dir):
    loader = worker.PreviewPreloader()
    assert loader.get("") is None
    assert loader.get(os.path.join(pt_dir, "nope.png")) is None


# --------------------------------------------------------------- picking


def test_pick_job_copies_the_image_and_its_side_car(pt_dir, pt_out):
    source = write_image(pt_dir, "a.jpg")
    write_json(pt_dir, "a.json", shapes=[make_shape()])

    picked = []
    failures = []
    done = []
    job = pick_worker.PickJob("pick", output_dir=pt_out, targets=(source,))
    job.item_picked.connect(
        lambda stem, dest: picked.append((stem, dest))
    )
    job.item_failed.connect(
        lambda stem, message: failures.append((stem, message))
    )
    job.job_done.connect(
        lambda kind, ok, total: done.append((kind, ok, total))
    )
    job.run()

    assert failures == []
    assert [stem for stem, _dest in picked] == ["a"]
    dest = picked[0][1]
    assert os.path.isfile(dest)
    assert dest == os.path.join(pt_out, "picked", "a.jpg")
    assert os.path.isfile(os.path.join(pt_out, "picked", "a.json"))
    assert os.path.isfile(source)
    assert done == [("pick", 1, 1)]


def test_pick_job_reports_a_missing_source(pt_dir, pt_out):
    failures = []
    job = pick_worker.PickJob(
        "pick",
        output_dir=pt_out,
        targets=(os.path.join(pt_dir, "gone.jpg"),),
    )
    job.item_failed.connect(
        lambda stem, message: failures.append((stem, message))
    )
    job.run()
    assert len(failures) == 1
    assert failures[0][0] == "gone"
    assert failures[0][1]


def test_pick_job_all_skips_what_is_picked_already(pt_dir, pt_out):
    first = write_image(pt_dir, "a.jpg")
    second = write_image(pt_dir, "b.jpg")
    pick_core.pick_one(first, pt_out)
    entries = [
        core.ImageEntry(path=first, name="a.jpg"),
        core.ImageEntry(path=second, name="b.jpg"),
    ]
    picked = []
    progress = []
    job = pick_worker.PickJob("all", entries=entries, output_dir=pt_out)
    job.item_picked.connect(lambda stem, dest: picked.append(stem))
    job.progress.connect(
        lambda done, total: progress.append((done, total))
    )
    job.run()
    assert picked == ["b"]
    assert progress[-1] == (2, 2)
    assert os.path.isfile(os.path.join(pt_out, "picked", "b.jpg"))


def test_pick_job_remove_moves_every_copy_into_the_trash(pt_dir, pt_out):
    # The temp trash folder belongs to the whole machine: another test
    # run may write into it at any moment. The stem is therefore unique
    # to this scratch directory, so only the files of this test are
    # counted and the assertion stays true under a parallel run.
    stem = "a_" + os.path.basename(pt_dir).replace("-", "")
    source = write_image(pt_dir, stem + ".jpg")
    write_json(pt_dir, stem + ".json", shapes=[make_shape()])
    pick_core.pick_one(source, pt_out)
    pick_core.pick_one(source, pt_out)

    moved = []
    failures = []
    job = pick_worker.PickJob(
        "remove", output_dir=pt_out, targets=(stem,)
    )
    job.item_removed.connect(
        lambda name, path: moved.append((name, path))
    )
    job.item_failed.connect(
        lambda name, message: failures.append((name, message))
    )
    job.run()

    assert failures == []
    assert len(moved) == 4
    assert all(name == stem for name, _path in moved)
    for _name, path in moved:
        assert path.startswith(pick_core.trash_dir())
        assert os.path.isfile(path)
        assert stem in os.path.basename(path)
    assert len(trash_listing(prefix=stem)) == 4
    picked_dir = os.path.join(pt_out, "picked")
    assert [name for name in os.listdir(picked_dir)] == []
    assert os.path.isfile(source)


def test_pick_job_remove_of_an_unknown_stem_fails_softly(pt_dir, pt_out):
    failures = []
    job = pick_worker.PickJob(
        "remove", output_dir=pt_out, targets=("nothing",)
    )
    job.item_removed.connect(lambda stem, path: failures.append("moved"))
    job.item_failed.connect(
        lambda stem, message: failures.append((stem, message))
    )
    job.run()
    assert len(failures) == 1
    assert failures[0][0] == "nothing"


def test_pick_job_detect_reports_the_picked_stems(pt_dir, pt_out):
    first = write_image(pt_dir, "a.jpg")
    second = write_image(pt_dir, "b.jpg")
    pick_core.pick_one(second, pt_out)
    entries = [
        core.ImageEntry(path=first, name="a.jpg"),
        core.ImageEntry(path=second, name="b.jpg"),
    ]
    stems = []
    done = []
    job = pick_worker.PickJob(
        "detect", entries=entries, output_dir=pt_out
    )
    job.detect_finished.connect(stems.append)
    job.job_done.connect(
        lambda kind, ok, total: done.append((kind, ok, total))
    )
    job.run()
    assert stems == [frozenset({"b"})]
    assert done == [("detect", 1, 2)]


def test_pick_job_cancel_stops_before_copying(pt_dir, pt_out):
    source = write_image(pt_dir, "a.jpg")
    picked = []
    job = pick_worker.PickJob("pick", output_dir=pt_out, targets=(source,))
    job.item_picked.connect(lambda stem, dest: picked.append(stem))
    job.cancel()
    job.run()
    assert picked == []
    assert not os.path.isdir(os.path.join(pt_out, "picked"))


def test_moved_path_reads_the_record_of_the_core(pt_dir, pt_out):
    source = write_image(pt_dir, "a.jpg")
    pick_core.pick_one(source, pt_out)
    result = pick_core.unpick_stem(pt_out, "a")
    assert result.status == "ok"
    for item in result.moved:
        assert pick_worker.moved_path(item) == item.target
    assert pick_worker.moved_path("plain") == "plain"
    assert pick_worker.moved_path(None) == ""
