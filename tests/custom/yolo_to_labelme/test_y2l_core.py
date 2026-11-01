"""Tests of the scanner: layout, pairing, classes and validation."""

import os
import os.path as osp

import pytest

from anylabeling.custom.yolo_to_labelme import yolo_core as core

from conftest import (
    write_classes,
    write_image,
    write_label,
    write_text,
)


def _scan(root, names=("a0",), mode=core.MODE_AUTO):
    return core.scan_dataset(root, write_classes(root, names), mode)


class TestClasses:
    def test_bom_and_crlf(self, scratch):
        path = osp.join(scratch, "classes.txt")
        with open(path, "wb") as handle:
            handle.write(b"\xef\xbb\xbfa0\r\na1\r\n")
        assert core.load_class_names(path) == ["a0", "a1"]

    def test_blank_and_whitespace(self, scratch):
        path = write_text(scratch, "classes.txt", " a0 \n\n a1\n")
        assert core.load_class_names(path) == ["a0", "a1"]

    def test_duplicates_are_allowed(self, scratch):
        path = write_classes(scratch, ["a0", "a0", "a1"])
        assert core.load_class_names(path) == ["a0", "a0", "a1"]

    def test_missing_file(self, scratch):
        with pytest.raises(core.YoloCoreError):
            core.load_class_names(osp.join(scratch, "missing.txt"))

    def test_empty_file(self, scratch):
        path = write_text(scratch, "classes.txt", " \n\n")
        with pytest.raises(core.YoloCoreError):
            core.load_class_names(path)


class TestLayout:
    def test_flat(self, scratch):
        layout, image_root, label_root = core.detect_layout(scratch)
        assert layout == core.LAYOUT_FLAT
        assert image_root == osp.abspath(scratch)
        assert label_root is None

    def test_split(self, scratch):
        os.makedirs(osp.join(scratch, "images"))
        os.makedirs(osp.join(scratch, "labels"))
        layout, image_root, label_root = core.detect_layout(scratch)
        assert layout == core.LAYOUT_SPLIT
        assert image_root == osp.abspath(osp.join(scratch, "images"))
        assert label_root == osp.abspath(osp.join(scratch, "labels"))

    def test_selected_images_directory(self, scratch):
        os.makedirs(osp.join(scratch, "images"))
        os.makedirs(osp.join(scratch, "labels"))
        layout, image_root, label_root = core.detect_layout(
            osp.join(scratch, "images")
        )
        assert layout == core.LAYOUT_SPLIT
        assert image_root == osp.abspath(osp.join(scratch, "images"))
        assert label_root == osp.abspath(osp.join(scratch, "labels"))


class TestFlatScan:
    def test_same_directory_pair(self, scratch):
        write_image(scratch, "a.jpg")
        write_label(scratch, "a.txt", ["0 0.5 0.5 0.5 0.5"])
        result = _scan(scratch)
        assert result.layout == core.LAYOUT_FLAT
        assert len(result.pairs) == 1
        pair = result.pairs[0]
        assert pair.image_path == osp.join(scratch, "a.jpg")
        assert pair.label_path == osp.join(scratch, "a.txt")
        assert pair.output_path == osp.join(scratch, "a.json")
        assert pair.mode == core.MODE_RECTANGLE
        assert pair.line_count == 1
        assert pair.issue == ""
        assert result.mode_counts == {core.MODE_RECTANGLE: 1}
        assert result.ok is True

    def test_missing_label(self, scratch):
        write_image(scratch, "a.jpg")
        write_image(scratch, "b.jpg")
        write_label(scratch, "a.txt", ["0 0.5 0.5 0.5 0.5"])
        result = _scan(scratch)
        assert len(result.pairs) == 1
        assert result.images_without_label == 1

    def test_empty_label_file(self, scratch):
        write_image(scratch, "a.jpg")
        write_label(scratch, "a.txt", [])
        result = _scan(scratch)
        assert len(result.pairs) == 1
        assert result.pairs[0].mode == ""
        assert result.pairs[0].line_count == 0
        assert result.mode_counts == {"empty": 1}

    def test_recursive_pair(self, scratch):
        write_image(scratch, osp.join("sub", "a.png"))
        write_label(scratch, osp.join("sub", "a.txt"), ["0 0.5 0.5 0.2 0.2"])
        result = _scan(scratch)
        assert len(result.pairs) == 1
        assert result.pairs[0].output_path == osp.join(
            scratch, "sub", "a.json"
        )

    def test_images_segment_is_replaced(self, scratch):
        write_image(scratch, osp.join("sub", "images", "a.jpg"))
        write_label(
            scratch,
            osp.join("sub", "labels", "a.txt"),
            ["0 0.5 0.5 0.5 0.5"],
        )
        result = _scan(scratch)
        assert result.layout == core.LAYOUT_FLAT
        assert len(result.pairs) == 1
        assert result.pairs[0].label_path == osp.join(
            scratch, "sub", "labels", "a.txt"
        )

    def test_orphan_label_is_counted(self, scratch):
        write_image(scratch, "a.jpg")
        write_label(scratch, "a.txt", ["0 0.5 0.5 0.5 0.5"])
        write_text(scratch, "orphan.txt", "0 0.5 0.5 0.5 0.5\n")
        result = _scan(scratch)
        assert result.orphan_labels == 1

    def test_class_file_is_not_an_orphan(self, scratch):
        write_image(scratch, "a.jpg")
        write_label(scratch, "a.txt", ["0 0.5 0.5 0.5 0.5"])
        result = _scan(scratch)
        assert result.orphan_labels == 0


class TestSplitScan:
    def test_split_pair(self, scratch):
        write_image(scratch, osp.join("images", "sub", "a.jpg"))
        write_label(
            scratch,
            osp.join("labels", "sub", "a.txt"),
            ["0 0.5 0.5 0.5 0.5"],
        )
        result = _scan(scratch)
        assert result.layout == core.LAYOUT_SPLIT
        assert len(result.pairs) == 1
        assert result.pairs[0].output_path == osp.join(
            scratch, "images", "sub", "a.json"
        )

    def test_flat_label_tree(self, scratch):
        write_image(scratch, osp.join("images", "sub", "a.jpg"))
        write_label(
            scratch, osp.join("labels", "a.txt"), ["0 0.5 0.5 0.5 0.5"]
        )
        result = _scan(scratch)
        assert len(result.pairs) == 1

    def test_selected_images_root(self, scratch):
        write_image(scratch, osp.join("images", "a.jpg"))
        write_label(
            scratch, osp.join("labels", "a.txt"), ["0 0.5 0.5 0.5 0.5"]
        )
        result = core.scan_dataset(
            osp.join(scratch, "images"),
            write_classes(scratch, ["a0"]),
            core.MODE_AUTO,
        )
        assert result.layout == core.LAYOUT_SPLIT
        assert len(result.pairs) == 1

    def test_labels_images_are_ignored(self, scratch):
        write_image(scratch, osp.join("images", "a.jpg"))
        write_label(
            scratch, osp.join("labels", "a.txt"), ["0 0.5 0.5 0.5 0.5"]
        )
        write_image(scratch, osp.join("labels", "b.jpg"))
        write_label(
            scratch, osp.join("labels", "b.txt"), ["0 0.5 0.5 0.5 0.5"]
        )
        result = _scan(scratch)
        assert len(result.pairs) == 1
        assert result.pairs[0].image_path == osp.join(
            scratch, "images", "a.jpg"
        )

    def test_empty_image_tree_falls_back_to_flat(self, scratch):
        os.makedirs(osp.join(scratch, "images"))
        os.makedirs(osp.join(scratch, "labels"))
        write_image(scratch, "a.jpg")
        write_label(scratch, "a.txt", ["0 0.5 0.5 0.5 0.5"])
        result = _scan(scratch)
        assert result.layout == core.LAYOUT_FLAT
        assert len(result.pairs) == 1


class TestValidation:
    def test_class_index_out_of_range(self, scratch):
        write_image(scratch, "a.jpg")
        write_label(scratch, "a.txt", ["1 0.5 0.5 0.5 0.5"])
        result = _scan(scratch, names=("a0",))
        assert result.pairs == []
        assert len(result.invalid) == 1
        assert "outside" in result.invalid[0].issue

    def test_multiple_spaces(self, scratch):
        write_image(scratch, "a.jpg")
        write_label(scratch, "a.txt", ["0  0.5 0.5 0.5 0.5"])
        result = _scan(scratch)
        assert result.pairs == []
        assert len(result.invalid) == 1

    def test_mixed_line_kinds(self, scratch):
        write_image(scratch, "a.jpg")
        write_label(
            scratch,
            "a.txt",
            ["0 0.5 0.5 0.5 0.5", "0 0.1 0.1 0.2 0.2 0.3 0.3"],
        )
        result = _scan(scratch)
        assert result.pairs == []
        assert "mixed" in result.invalid[0].issue

    def test_polygon_is_detected(self, scratch):
        write_image(scratch, "a.jpg")
        write_label(
            scratch, "a.txt", ["0 0.1 0.1 0.2 0.2 0.3 0.3"]
        )
        result = _scan(scratch)
        assert result.pairs[0].mode == core.MODE_POLYGON
        assert result.mode_counts == {core.MODE_POLYGON: 1}

    def test_explicit_rectangle_rejects_polygon(self, scratch):
        write_image(scratch, "a.jpg")
        write_label(
            scratch, "a.txt", ["0 0.1 0.1 0.2 0.2 0.3 0.3"]
        )
        result = _scan(scratch, mode=core.MODE_RECTANGLE)
        assert len(result.invalid) == 1

    def test_explicit_polygon_rejects_box(self, scratch):
        write_image(scratch, "a.jpg")
        write_label(scratch, "a.txt", ["0 0.5 0.5 0.5 0.5"])
        result = _scan(scratch, mode=core.MODE_POLYGON)
        assert len(result.invalid) == 1

    def test_obb_explicit_mode(self, scratch):
        write_image(scratch, "a.jpg")
        write_label(
            scratch,
            "a.txt",
            ["0 0.1 0.1 0.2 0.1 0.2 0.2 0.1 0.2"],
        )
        result = _scan(scratch, mode=core.MODE_OBB)
        assert result.pairs[0].mode == core.MODE_OBB
        assert result.mode_counts == {core.MODE_OBB: 1}

    def test_obb_auto_is_polygon(self, scratch):
        write_image(scratch, "a.jpg")
        write_label(
            scratch,
            "a.txt",
            ["0 0.1 0.1 0.2 0.1 0.2 0.2 0.1 0.2"],
        )
        result = _scan(scratch)
        assert result.pairs[0].mode == core.MODE_POLYGON

    def test_output_is_a_directory(self, scratch):
        write_image(scratch, "a.jpg")
        write_label(scratch, "a.txt", ["0 0.5 0.5 0.5 0.5"])
        os.makedirs(osp.join(scratch, "a.json"))
        result = _scan(scratch)
        assert result.pairs == []
        assert len(result.invalid) == 1
        assert "directory" in result.invalid[0].issue

    def test_stem_collision(self, scratch):
        write_image(scratch, "a.jpg")
        write_image(scratch, "a.png")
        write_label(scratch, "a.txt", ["0 0.5 0.5 0.5 0.5"])
        result = _scan(scratch)
        assert len(result.pairs) == 2
        assert len(result.collisions) == 1
        assert result.ok is False


class TestHintsAndErrors:
    def test_coco_hint(self, scratch):
        write_text(scratch, "coco_detection.json", "{}")
        result = _scan(scratch)
        assert result.pairs == []
        assert result.hint_coco is True

    def test_no_coco_hint(self, scratch):
        write_text(scratch, "other.json", "{}")
        result = _scan(scratch)
        assert result.hint_coco is False

    def test_missing_directory(self, scratch):
        with pytest.raises(core.YoloCoreError):
            core.scan_dataset(
                osp.join(scratch, "missing"),
                write_classes(scratch, ["a0"]),
                core.MODE_AUTO,
            )

    def test_unknown_mode(self, scratch):
        with pytest.raises(core.YoloCoreError):
            core.scan_dataset(
                scratch,
                write_classes(scratch, ["a0"]),
                "not-a-mode",
            )
