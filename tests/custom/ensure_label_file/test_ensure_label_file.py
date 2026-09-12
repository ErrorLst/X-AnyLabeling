"""The automatic empty label file of an image without annotation.

The tests cover three layers. The install tests drive the wrapper: the
helper runs exactly for an exact True of the original load_file, and a
real LabelingWidget shows that its constructor queues the wrapped
method. The creation tests call the helper with a fake widget built from
the real widget methods and check the file that lands on disk, field by
field. The guard tests pin down that nothing is written for a widget
that does not prove a freshly loaded image without annotation, and that
a save which fails raises neither in the load nor in front of the user.
"""

import functools
import importlib
import json
import logging
import os
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from PyQt6.QtCore import Qt

import anylabeling.custom.ensure_label_file as ensure_package
from anylabeling.app_info import __version__
from anylabeling.custom.ensure_label_file import (
    ensure_label_file,
    install_ensure_label_file,
)
from anylabeling.views.labeling.label_file import LabelFile
from anylabeling.views.labeling.label_widget import LabelingWidget
from anylabeling.views.labeling.schema import XLABEL_BASIC_FIELDS
from anylabeling.views.labeling.shape import Shape
from anylabeling.views.labeling.widgets.label_list_widget import (
    LabelListWidgetItem,
)
from conftest import write_image

LABEL_FILE_MODULE = importlib.import_module(
    "anylabeling.custom.ensure_label_file.label_file"
)

#: A label file that exists before the test and must stay untouched.
MARKER_DESCRIPTION = "keep me"

#: The bytes of a file another process wrote, which must survive.
MARKER_BYTES = b"written by the other process"


def write_valid_label_file(path, image_name, width=320, height=200):
    "Write a real label file with a marker and return its bytes."

    LabelFile().save(
        filename=path,
        shapes=[],
        image_path=image_name,
        image_height=height,
        image_width=width,
        image_data=None,
        other_data={"description": MARKER_DESCRIPTION},
        flags={},
    )
    return load_bytes(path)


def write_bytes(path, data):
    "Write raw bytes to a path and return them."

    with open(path, "wb") as handle:
        handle.write(data)
    return data


def load_label_file(path):
    "Return the parsed content of a label file."

    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def load_bytes(path):
    "Return the raw bytes of a file."

    with open(path, "rb") as handle:
        return handle.read()


def warning_messages(caplog):
    "Return the warnings the package under test has logged."

    return [
        record.getMessage()
        for record in caplog.records
        if record.name == LABEL_FILE_MODULE.LOGGER.name
        and record.levelno >= logging.WARNING
    ]


def install(widget):
    "Install the wrapper on the fake widget and return it."

    return install_ensure_label_file(widget)


def bind_load_file(widget, handler):
    "Give the widget an instance level load_file and wrap it."

    widget.load_file = handler
    return install(widget)


def image_filename(scratch, name="a.png"):
    "Write the image of the given name and return its path."

    return write_image(os.path.join(scratch, name))


def set_attribute(name, value):
    "Return a guard mutator that sets an attribute of the widget."

    def mutate(widget, scratch):
        setattr(widget, name, value)

    return mutate


def drop_attribute(name):
    "Return a guard mutator that removes an attribute of the widget."

    def mutate(widget, scratch):
        delattr(widget, name)

    return mutate


def json_in_output_dir(widget, scratch):
    "Open a label file as the current file while an output dir is set."

    widget.filename = os.path.join(scratch, "labels.json")
    widget.output_dir = os.path.join(scratch, "out")


def test_package_exposes_the_installed_helpers():
    "The package re-exports both helpers of the module."

    assert ensure_package.ensure_label_file is ensure_label_file
    assert (
        ensure_package.install_ensure_label_file is install_ensure_label_file
    )
    assert "ensure_label_file" in ensure_package.__all__
    assert "install_ensure_label_file" in ensure_package.__all__


def test_install_wraps_load_file_and_passes_its_result_through():
    "The wrapper is installed and returns the original result."

    calls = []

    def original(filename=None):
        calls.append(filename)
        return "sentinel"

    widget = SimpleNamespace(filename="a.png")
    wrapper = bind_load_file(widget, original)

    assert callable(wrapper)
    assert widget.load_file is wrapper
    assert wrapper("a.png") == "sentinel"
    assert calls == ["a.png"]


def test_install_is_idempotent():
    "A second installation returns the same wrapper and changes nothing."

    widget = SimpleNamespace(filename="a.png")
    widget.load_file = lambda filename=None: True

    first = install(widget)
    second = install(widget)

    assert first is second
    assert widget.load_file is first
    assert widget._ensure_label_file_wrapper is first
    assert widget._ensure_label_file_installed is True


def test_install_without_load_file_returns_none_and_sets_no_flag():
    "A widget without a callable load_file is left alone."

    widget = SimpleNamespace(filename="a.png")

    assert install(widget) is None
    assert getattr(widget, "_ensure_label_file_installed", False) is False

    widget.load_file = None
    assert install(widget) is None
    assert getattr(widget, "_ensure_label_file_installed", False) is False


def test_install_returns_none_for_a_non_callable_load_file():
    "A non callable attribute leads to no wrapper and no flag."

    widget = SimpleNamespace(filename="a.png", load_file="a.json")

    assert install(widget) is None
    assert widget.load_file == "a.json"
    assert getattr(widget, "_ensure_label_file_installed", False) is False


@pytest.mark.parametrize(
    "loaded",
    [
        pytest.param(True, id="exact-true"),
        pytest.param("sentinel", id="truthy-string"),
        pytest.param(1, id="truthy-number"),
        pytest.param(None, id="none"),
        pytest.param(False, id="false"),
        pytest.param("", id="empty-string"),
    ],
)
def test_helper_runs_only_for_an_exact_true(loaded):
    "A result that is only truthy does not trigger the helper."

    widget = SimpleNamespace(filename="a.png")
    bind_load_file(widget, lambda filename=None: loaded)

    with patch.object(LABEL_FILE_MODULE, "ensure_label_file") as helper:
        assert widget.load_file("a.png") is loaded

    if loaded is True:
        helper.assert_called_once_with(widget)
    else:
        helper.assert_not_called()


def test_a_real_widget_queues_the_wrapped_load_file(
    elf_scratch, make_real_widget
):
    "The mount point runs before the constructor queues the first load."

    image = image_filename(elf_scratch)
    target = os.path.join(elf_scratch, "a.json")
    real = make_real_widget(image)
    widget = real.widget

    assert getattr(widget, "_ensure_label_file_installed", False) is True
    assert widget.load_file is not LabelingWidget.load_file
    assert len(real.queued) == 1

    queued_load = real.queued[0]
    assert isinstance(queued_load, functools.partial)
    assert queued_load.func is widget.load_file
    assert queued_load.func is not LabelingWidget.load_file
    assert queued_load.args == (image,)
    assert widget.actions.delete_file.isEnabled() is False

    # Model the entry an import leaves in the file list: it mirrors the
    # existence of the label file, so it starts unchecked.
    item = widget._create_file_list_item(image, target)
    widget.file_list_widget.addItem(item)
    widget.fn_to_index[image] = widget.file_list_widget.count() - 1
    assert item.checkState() == Qt.CheckState.Unchecked

    # Selecting the entry is how the application loads a listed image,
    # and the selection goes through the wrapped load_file exactly like
    # the load the constructor queued.
    widget.file_list_widget.setCurrentRow(widget.fn_to_index[image])

    assert os.path.exists(target)
    # The upstream save path wrote the file and left the widget in the
    # state of an image with a label file: the instance and the check
    # state of the list entry are its work, not this package's.
    assert isinstance(widget.label_file, LabelFile)
    assert widget.label_file.filename == target
    assert item.checkState() == Qt.CheckState.Checked
    # The delete command upstream disabled for an image without a label
    # file is enabled again, which is the only state this package fixes.
    assert widget.actions.delete_file.isEnabled() is True
    assert widget.dirty is False

    error_box = Mock()
    with patch.object(widget, "error_message", error_box):
        assert queued_load() is True

    error_box.assert_not_called()
    assert os.path.exists(target)
    assert widget.label_file.filename == target


def test_missing_label_file_is_created_for_a_loaded_image(
    elf_scratch, make_widget
):
    "A successful load of an image without json writes an empty file."

    image = image_filename(elf_scratch)
    widget = make_widget(image)
    calls = []

    def original(filename=None):
        calls.append(filename)
        return True

    bind_load_file(widget, original)
    target = os.path.join(elf_scratch, "a.json")

    assert widget.load_file(image) is True

    assert calls == [image]
    data = load_label_file(target)
    assert set(data) == set(XLABEL_BASIC_FIELDS)
    assert list(data) == XLABEL_BASIC_FIELDS
    assert data["version"] == __version__
    assert data["flags"] == {}
    assert data["checked"] is False
    assert data["shapes"] == []
    assert data["imagePath"] == "a.png"
    assert data["imageData"] is None
    assert data["imageHeight"] == 200
    assert data["imageWidth"] == 320
    # The upstream save path leaves the widget in the state of an image
    # with a label file, so only the delete command is completed here.
    assert isinstance(widget.label_file, LabelFile)
    assert widget.label_file.filename == target
    widget.actions.delete_file.setEnabled.assert_called_once_with(True)
    widget.error_message.assert_not_called()
    assert widget.dirty is False
    assert len(widget.label_list) == 0


def test_created_file_is_reloaded_as_a_label_file(elf_scratch, make_widget):
    "The produced json is a label file without any shape."

    image = image_filename(elf_scratch)
    widget = make_widget(image)
    created = ensure_label_file(widget)
    target = os.path.join(elf_scratch, "a.json")

    assert created == target
    assert LabelFile.is_label_file(target)
    reloaded = LabelFile(target)
    assert reloaded.shapes == []
    assert reloaded.image_path == "a.png"


def test_output_dir_receives_the_created_file(elf_scratch, make_widget):
    "With an output directory the json lands there, not next to the image."

    image = image_filename(elf_scratch)
    widget = make_widget(
        image,
        output_dir=os.path.join(elf_scratch, "labels"),
    )

    ensure_label_file(widget)

    target = os.path.join(elf_scratch, "labels", "a.json")
    assert os.path.exists(target)
    assert not os.path.exists(os.path.join(elf_scratch, "a.json"))
    assert widget.label_file.filename == target
    # The path is relative to the directory of the label file.
    expected = os.path.join("..", "a.png")
    assert load_label_file(target)["imagePath"] == expected


def test_missing_output_dir_is_created(elf_scratch, make_widget):
    "An output directory that does not exist yet is created upstream."

    image = image_filename(elf_scratch)
    widget = make_widget(
        image,
        output_dir=os.path.join(elf_scratch, "new"),
    )

    ensure_label_file(widget)

    target = os.path.join(elf_scratch, "new", "a.json")
    assert os.path.isdir(os.path.join(elf_scratch, "new"))
    assert os.path.exists(target)
    assert not os.path.exists(os.path.join(elf_scratch, "a.json"))


def test_existing_label_file_is_left_byte_for_byte(elf_scratch, make_widget):
    "An image that already has a json keeps that json untouched."

    image = image_filename(elf_scratch, "b.png")
    target = os.path.join(elf_scratch, "b.json")
    before = write_valid_label_file(target, "b.png")
    widget = make_widget(image, label_file=LabelFile(target))

    ensure_label_file(widget)

    assert load_bytes(target) == before
    assert load_label_file(target)["description"] == MARKER_DESCRIPTION
    widget.error_message.assert_not_called()
    assert widget.dirty is False
    assert len(widget.label_list) == 0


def test_load_of_a_file_with_labels_creates_nothing(elf_scratch, make_widget):
    "The load of an image that has a label file stays a plain load."

    image = image_filename(elf_scratch, "b.png")
    target = os.path.join(elf_scratch, "b.json")
    before = write_valid_label_file(target, "b.png")
    widget = make_widget(image)

    def original(filename=None):
        # What the upstream load does when the label file exists.
        widget.label_file = LabelFile(target)
        return True

    bind_load_file(widget, original)

    assert widget.load_file(image) is True
    assert load_bytes(target) == before
    assert len(widget.label_list) == 0


def test_failed_load_creates_nothing(elf_scratch, make_widget):
    "A load that reports False does not create a label file."

    image = image_filename(elf_scratch)
    widget = make_widget(image)
    calls = []

    def original(filename=None):
        calls.append(filename)
        return False

    bind_load_file(widget, original)

    assert widget.load_file(image) is False
    assert calls == [image]
    assert not os.path.exists(os.path.join(elf_scratch, "a.json"))


def test_existing_target_without_a_label_file_is_left_alone(
    elf_scratch, make_widget
):
    "A json that exists while label_file is None is not replaced."

    image = image_filename(elf_scratch)
    target = os.path.join(elf_scratch, "a.json")
    before = write_bytes(target, MARKER_BYTES)
    widget = make_widget(image)

    with patch.object(widget, "save_labels") as save:
        assert ensure_label_file(widget) is None

    save.assert_not_called()
    assert load_bytes(target) == before
    assert widget.label_file is None


def test_loaded_label_file_without_a_target_is_not_written(
    elf_scratch, make_widget
):
    "A widget that already loaded a label file gets no second file."

    image = image_filename(elf_scratch, "b.png")
    other = os.path.join(elf_scratch, "other.json")
    write_valid_label_file(other, "b.png")
    widget = make_widget(image, label_file=LabelFile(other))

    with patch.object(widget, "save_labels") as save:
        assert ensure_label_file(widget) is None

    save.assert_not_called()
    assert not os.path.exists(os.path.join(elf_scratch, "b.json"))


def test_json_filename_creates_no_second_json(elf_scratch, make_widget):
    "A label file opened as the current file gets no label file."

    image = image_filename(elf_scratch)
    target = os.path.join(elf_scratch, "a.json")
    before = write_valid_label_file(target, "a.png")
    widget = make_widget(target, image_path=image)

    with patch.object(widget, "save_labels") as save:
        assert ensure_label_file(widget) is None

    save.assert_not_called()
    assert load_bytes(target) == before
    assert not os.path.exists(os.path.join(elf_scratch, "a.json.json"))


@pytest.mark.parametrize(
    "mutate, candidates",
    [
        pytest.param(
            set_attribute("filename", ""), ("a.json",), id="no-filename"
        ),
        pytest.param(
            set_attribute("filename", None), ("a.json",), id="null-filename"
        ),
        pytest.param(set_attribute("dirty", True), ("a.json",), id="dirty"),
        pytest.param(set_attribute("image", None), ("a.json",), id="no-image"),
        pytest.param(
            set_attribute("image", SimpleNamespace()),
            ("a.json",),
            id="image-without-is-null",
        ),
        pytest.param(
            set_attribute("image", SimpleNamespace(isNull=lambda: True)),
            ("a.json",),
            id="null-image",
        ),
        pytest.param(
            set_attribute("label_list", None),
            ("a.json",),
            id="no-label-list",
        ),
        pytest.param(
            set_attribute(
                "label_list",
                [LabelListWidgetItem("object", Shape(label="object"))],
            ),
            ("a.json",),
            id="non-empty-label-list",
        ),
        pytest.param(
            drop_attribute("get_label_file"),
            ("a.json",),
            id="no-get-label-file",
        ),
        pytest.param(
            set_attribute("get_label_file", "a.json"),
            ("a.json",),
            id="non-callable-get-label-file",
        ),
        pytest.param(
            set_attribute("get_label_file", lambda: ""),
            ("a.json",),
            id="empty-target",
        ),
        pytest.param(
            set_attribute("get_label_file", lambda: None),
            ("a.json",),
            id="null-target",
        ),
        pytest.param(
            set_attribute("canvas", SimpleNamespace(shapes=[object()])),
            ("a.json",),
            id="canvas-with-shapes",
        ),
        pytest.param(
            set_attribute("label_file", LabelFile()),
            ("a.json",),
            id="loaded-label-file",
        ),
        pytest.param(
            set_attribute("filename", "labels.json"),
            ("labels.json",),
            id="json-filename",
        ),
        pytest.param(
            json_in_output_dir,
            ("labels.json", os.path.join("out", "labels.json")),
            id="json-filename-with-output-dir",
        ),
    ],
)
def test_guard_keeps_the_helper_from_writing(
    elf_scratch, make_widget, mutate, candidates
):
    "Every guard returns before the save of the widget is reached."

    image = image_filename(elf_scratch)
    widget = make_widget(image)
    mutate(widget, elf_scratch)

    with patch.object(widget, "save_labels") as save:
        assert ensure_label_file(widget) is None

    save.assert_not_called()
    widget.error_message.assert_not_called()
    for name in candidates:
        assert not os.path.exists(os.path.join(elf_scratch, name))


def test_save_that_reports_false_is_swallowed(
    elf_scratch, make_widget, caplog
):
    "A save that reports False breaks neither the load nor the widget."

    image = image_filename(elf_scratch)
    widget = make_widget(image)
    target = os.path.join(elf_scratch, "a.json")
    bind_load_file(widget, lambda filename=None: True)

    with patch.object(widget, "save_labels", Mock(return_value=False)) as save:
        with caplog.at_level(
            logging.WARNING, logger=LABEL_FILE_MODULE.LOGGER.name
        ):
            assert widget.load_file(image) is True

    save.assert_called_once_with(target)
    assert not os.path.exists(target)
    assert widget.label_file is None
    widget.actions.delete_file.setEnabled.assert_not_called()
    widget.error_message.assert_not_called()
    assert any(target in message for message in warning_messages(caplog))


def test_save_that_raises_is_swallowed(elf_scratch, make_widget, caplog):
    "A save that raises is reported to the log and nowhere else."

    image = image_filename(elf_scratch)
    widget = make_widget(image)
    target = os.path.join(elf_scratch, "a.json")
    bind_load_file(widget, lambda filename=None: True)
    error = OSError("volume is read only")

    with patch.object(widget, "save_labels", Mock(side_effect=error)):
        with caplog.at_level(
            logging.WARNING, logger=LABEL_FILE_MODULE.LOGGER.name
        ):
            assert widget.load_file(image) is True

    assert not os.path.exists(target)
    assert widget.label_file is None
    widget.actions.delete_file.setEnabled.assert_not_called()
    widget.error_message.assert_not_called()
    assert any(str(error) in message for message in warning_messages(caplog))
