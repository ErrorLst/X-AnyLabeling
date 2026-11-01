"""Tests of the filter export: planning, writing and the dialog flow."""

from __future__ import annotations

import os.path as osp
from types import SimpleNamespace
from pathlib import Path
import shutil
import tempfile
import uuid

import numpy as np
import PIL.Image
import pytest
from PyQt6 import QtWidgets

from anylabeling.custom.filter_tool import core, export
from anylabeling.custom.filter_tool.dialog import FilterDialog

from conftest import FakeWidget, keep, make_image, make_rgb


@pytest.fixture
def export_tmp():
    root = Path(tempfile.gettempdir()) / (
        "filter-export-tests-" + uuid.uuid4().hex
    )
    root.mkdir(parents=True)
    yield root
    trash = Path(tempfile.gettempdir()) / "dsh-trash"
    trash.mkdir(exist_ok=True)
    shutil.move(str(root), str(trash / root.name))


def _write_png(path, array, mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    image = (
        PIL.Image.fromarray(array, mode)
        if mode is not None
        else PIL.Image.fromarray(array)
    )
    image.save(path)


def _black_spec():
    return [
        core.FilterSpec(
            core.KIND_PIXEL,
            True,
            {"low": 0, "high": 0, "fill": 0},
        )
    ]


class TestPlanning:
    def test_collect_prefers_the_file_list_and_deduplicates(self, export_tmp):
        first = str(export_tmp / "a.png")
        second = str(export_tmp / "b.png")
        widget = SimpleNamespace(
            image_list=[first, second, first], filename="ignored.png"
        )
        assert export.collect_image_paths(widget) == [
            osp.abspath(first),
            osp.abspath(second),
        ]

    def test_collect_falls_back_to_the_current_file(self, export_tmp):
        path = str(export_tmp / "a.png")
        widget = SimpleNamespace(image_list=[], filename=path)
        assert export.collect_image_paths(widget) == [osp.abspath(path)]

    def test_plan_preserves_relative_directories(self, export_tmp):
        root = export_tmp / "data"
        first = root / "a.png"
        second = root / "sub" / "b.png"
        targets = export.plan_export(
            [str(first), str(second)], str(export_tmp / "out")
        )
        assert [item.relative for item in targets] == [
            "a.png",
            osp.join("sub", "b.png"),
        ]
        assert targets[1].target == osp.abspath(
            export_tmp / "out" / "sub" / "b.png"
        )

    def test_plan_rejects_overwriting_a_source(self, export_tmp):
        source = export_tmp / "data" / "a.png"
        source.parent.mkdir()
        with pytest.raises(export.ExportError):
            export.plan_export([str(source)], str(source.parent))

    def test_plan_rejects_an_empty_list(self, export_tmp):
        with pytest.raises(export.ExportError):
            export.plan_export([], str(export_tmp))


class TestWriting:
    def test_export_writes_copy_and_keeps_the_source(self, export_tmp):
        source = export_tmp / "source.png"
        rgb = make_rgb(16, 12)
        _write_png(source, rgb)
        before = source.read_bytes()
        targets = export.plan_export(
            [str(source)], str(export_tmp / "out")
        )
        result = export.export_images(targets, _black_spec())
        assert result.failed == []
        assert result.written == [targets[0].target]
        output = np.array(
            PIL.Image.open(targets[0].target).convert("RGB")
        )
        assert int(output.max()) == 0
        assert source.read_bytes() == before
        original = np.array(PIL.Image.open(source).convert("RGB"))
        assert int(original.max()) > 0

    def test_export_keeps_alpha_for_png(self, export_tmp):
        source = export_tmp / "alpha.png"
        rgb = np.full((8, 8, 3), 40, dtype=np.uint8)
        alpha = np.full((8, 8), 64, dtype=np.uint8)
        _write_png(source, np.dstack((rgb, alpha)), "RGBA")
        before = source.read_bytes()
        targets = export.plan_export(
            [str(source)], str(export_tmp / "out")
        )
        result = export.export_images(targets, _black_spec())
        assert result.failed == []
        output = PIL.Image.open(targets[0].target).convert("RGBA")
        assert output.getpixel((1, 1))[3] == 64
        assert source.read_bytes() == before

    def test_export_writes_jpeg_with_its_own_format(self, export_tmp):
        source = export_tmp / "photo.jpg"
        PIL.Image.fromarray(make_rgb(16, 12)).save(source, "JPEG")
        before = source.read_bytes()
        targets = export.plan_export(
            [str(source)], str(export_tmp / "out")
        )
        result = export.export_images(targets, _black_spec())
        assert result.failed == []
        output = PIL.Image.open(targets[0].target)
        assert output.format == "JPEG"
        assert source.read_bytes() == before

    def test_bad_source_is_reported_and_does_not_stop_the_batch(
        self, export_tmp
    ):
        good = export_tmp / "good.png"
        bad = export_tmp / "bad.png"
        _write_png(good, make_rgb(8, 8))
        bad.write_text("not an image", encoding="utf-8")
        targets = export.plan_export(
            [str(good), str(bad)], str(export_tmp / "out")
        )
        result = export.export_images(targets, _black_spec())
        assert len(result.written) == 1
        assert len(result.failed) == 1
        assert result.failed[0][0] == str(bad)

    def test_existing_target_is_not_overwritten_by_default(self, export_tmp):
        source = export_tmp / "source.png"
        _write_png(source, make_rgb(8, 8))
        target = export_tmp / "out" / "source.png"
        target.parent.mkdir()
        target.write_bytes(b"old")
        targets = export.plan_export(
            [str(source)], str(export_tmp / "out")
        )
        result = export.export_images(targets, _black_spec())
        assert result.written == []
        assert len(result.failed) == 1
        assert target.read_bytes() == b"old"

    def test_export_can_be_cancelled(self, export_tmp):
        first = export_tmp / "a.png"
        second = export_tmp / "b.png"
        _write_png(first, make_rgb(8, 8))
        _write_png(second, make_rgb(8, 8))
        targets = export.plan_export(
            [str(first), str(second)], str(export_tmp / "out")
        )
        state = {"done": 0}

        def progress(done, total):
            state["done"] = done

        def cancelled():
            return state["done"] >= 1

        result = export.export_images(
            targets,
            _black_spec(),
            progress=progress,
            cancelled=cancelled,
        )
        assert result.cancelled is True
        assert len(result.written) == 1
        assert result.failed == []


def test_dialog_export_button_runs_the_export(
    export_tmp, monkeypatch
):
    source = export_tmp / "source.png"
    _write_png(source, make_rgb(8, 8))
    widget = keep(FakeWidget(image=make_image(make_rgb())))
    widget.image_list = [str(source)]
    widget.filename = str(source)
    dialog = keep(FilterDialog(widget))
    dialog.add_filter(core.KIND_PIXEL)
    assert dialog.set_current_params(low=0, high=0, fill=0)
    output = export_tmp / "out"
    output.mkdir()
    monkeypatch.setattr(
        QtWidgets.QFileDialog,
        "getExistingDirectory",
        lambda *args, **kwargs: str(output),
    )
    messages = []
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "information",
        lambda *args, **kwargs: messages.append(args[2]),
    )
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "warning",
        lambda *args, **kwargs: messages.append(args[2]),
    )
    before = source.read_bytes()
    assert dialog.export_button.text() == "导出..."
    assert dialog.export_filtered_images() is True
    target = output / "source.png"
    assert target.exists()
    output_pixels = np.array(PIL.Image.open(target).convert("RGB"))
    assert int(output_pixels.max()) == 0
    assert source.read_bytes() == before
    assert messages
