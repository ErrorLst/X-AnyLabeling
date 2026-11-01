"""End-to-end tests of the conversion through the upstream converter."""

import json
import os.path as osp

import pytest

from anylabeling.custom.yolo_to_labelme import conversion
from anylabeling.custom.yolo_to_labelme import yolo_core as core

from conftest import (
    write_classes,
    write_image,
    write_label,
    write_text,
)


def _scan(root, names=("a0",), mode=core.MODE_AUTO):
    return core.scan_dataset(root, write_classes(root, names), mode)


def _read(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


class TestDetection:
    def test_rectangle(self, scratch):
        write_image(scratch, "a.jpg", size=(64, 48))
        write_label(scratch, "a.txt", ["0 0.5 0.5 0.5 0.25"])
        scan = _scan(scratch)
        result = conversion.convert_pairs(scan.pairs, scan.class_names)
        assert result.converted == 1
        assert result.empty == 0
        assert result.failed == []
        data = _read(osp.join(scratch, "a.json"))
        assert data["imagePath"] == "a.jpg"
        assert data["imageWidth"] == 64
        assert data["imageHeight"] == 48
        assert len(data["shapes"]) == 1
        shape = data["shapes"][0]
        assert shape["label"] == "a0"
        assert shape["shape_type"] == "rectangle"
        points = shape["points"]
        assert points[0][0] == pytest.approx(16.0)
        assert points[0][1] == pytest.approx(18.0)
        assert points[1][0] == pytest.approx(48.0)
        assert points[2][1] == pytest.approx(30.0)

    def test_polygon(self, scratch):
        write_image(scratch, "a.jpg", size=(100, 50))
        write_label(
            scratch, "a.txt", ["0 0.1 0.2 0.5 0.2 0.5 0.6"]
        )
        scan = _scan(scratch)
        result = conversion.convert_pairs(scan.pairs, scan.class_names)
        assert result.converted == 1
        data = _read(osp.join(scratch, "a.json"))
        shape = data["shapes"][0]
        assert shape["shape_type"] == "polygon"
        points = shape["points"]
        assert points[0] == pytest.approx([10.0, 10.0])
        assert points[1] == pytest.approx([50.0, 10.0])
        assert points[2] == pytest.approx([50.0, 30.0])

    def test_obb(self, scratch):
        write_image(scratch, "a.jpg", size=(100, 100))
        write_label(
            scratch,
            "a.txt",
            ["0 0.1 0.1 0.9 0.1 0.9 0.9 0.1 0.9"],
        )
        scan = _scan(scratch, mode=core.MODE_OBB)
        result = conversion.convert_pairs(scan.pairs, scan.class_names)
        assert result.converted == 1
        data = _read(osp.join(scratch, "a.json"))
        shape = data["shapes"][0]
        assert shape["shape_type"] == "rotation"
        assert len(shape["points"]) == 4
        assert "direction" in shape

    def test_empty_label_file(self, scratch):
        write_image(scratch, "a.jpg")
        write_label(scratch, "a.txt", [])
        scan = _scan(scratch)
        result = conversion.convert_pairs(scan.pairs, scan.class_names)
        assert result.converted == 1
        assert result.empty == 1
        data = _read(osp.join(scratch, "a.json"))
        assert data["shapes"] == []


class TestExistingOutput:
    def test_existing_json_is_skipped(self, scratch):
        write_image(scratch, "a.jpg")
        write_label(scratch, "a.txt", ["0 0.5 0.5 0.5 0.5"])
        write_text(scratch, "a.json", '{"sentinel": true}\n')
        scan = _scan(scratch)
        assert scan.existing_outputs == 1
        result = conversion.convert_pairs(scan.pairs, scan.class_names)
        assert result.skipped_existing == 1
        assert result.converted == 0
        assert _read(osp.join(scratch, "a.json")) == {"sentinel": True}

    def test_overwrite_replaces_json(self, scratch):
        write_image(scratch, "a.jpg")
        write_label(scratch, "a.txt", ["0 0.5 0.5 0.5 0.5"])
        write_text(scratch, "a.json", '{"sentinel": true}\n')
        scan = _scan(scratch)
        result = conversion.convert_pairs(
            scan.pairs, scan.class_names, overwrite=True
        )
        assert result.converted == 1
        data = _read(osp.join(scratch, "a.json"))
        assert data["imagePath"] == "a.jpg"
        assert len(data["shapes"]) == 1


class TestFailures:
    def test_invalid_class_index_writes_nothing(self, scratch):
        write_image(scratch, "a.jpg")
        write_label(scratch, "a.txt", ["1 0.5 0.5 0.5 0.5"])
        scan = _scan(scratch, names=("a0",))
        result = conversion.convert_pairs(scan.pairs, scan.class_names)
        assert result.converted == 0
        assert not osp.exists(osp.join(scratch, "a.json"))
        assert scan.invalid

    def test_broken_image_does_not_stop_the_run(self, scratch):
        write_image(scratch, "a.jpg")
        write_label(scratch, "a.txt", ["0 0.5 0.5 0.5 0.5"])
        write_text(scratch, "b.jpg", "not an image")
        write_label(scratch, "b.txt", ["0 0.5 0.5 0.5 0.5"])
        scan = _scan(scratch)
        result = conversion.convert_pairs(scan.pairs, scan.class_names)
        assert result.converted == 1
        assert len(result.failed) == 1
        assert result.failed[0][0].endswith("b.jpg")
        assert osp.exists(osp.join(scratch, "a.json"))
        assert not osp.exists(osp.join(scratch, "b.json"))


class TestCallbacks:
    def test_progress_is_reported_per_pair(self, scratch):
        write_image(scratch, "a.jpg")
        write_label(scratch, "a.txt", ["0 0.5 0.5 0.5 0.5"])
        write_image(scratch, "b.jpg")
        write_label(scratch, "b.txt", ["0 0.5 0.5 0.5 0.5"])
        scan = _scan(scratch)
        calls = []
        conversion.convert_pairs(
            scan.pairs,
            scan.class_names,
            on_progress=lambda done, total, image: calls.append(
                (done, total, osp.basename(image))
            ),
        )
        assert calls == [
            (1, 2, "a.jpg"),
            (2, 2, "b.jpg"),
        ]

    def test_cancel_stops_after_current_pair(self, scratch):
        write_image(scratch, "a.jpg")
        write_label(scratch, "a.txt", ["0 0.5 0.5 0.5 0.5"])
        write_image(scratch, "b.jpg")
        write_label(scratch, "b.txt", ["0 0.5 0.5 0.5 0.5"])
        scan = _scan(scratch)
        state = {"done": 0}

        def progress(_done, _total, _image):
            state["done"] += 1

        result = conversion.convert_pairs(
            scan.pairs,
            scan.class_names,
            on_progress=progress,
            should_cancel=lambda: state["done"] >= 1,
        )
        assert result.cancelled is True
        assert result.converted == 1
        assert state["done"] == 1

    def test_empty_plan_returns_without_importing(self):
        result = conversion.convert_pairs([], [])
        assert result.converted == 0
        assert result.failed == []
