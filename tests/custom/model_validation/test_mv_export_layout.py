"""The layout of the export archive: classes.txt and images/ only.

The archive of this revision writes the class table at its root and
every exported pair of the run into one folder, images/: the picture
under the relative path it carries inside the source dataset, and the
one xlabel json of the very same name right next to it. There is no
original / augmented split any more and no second, LabelMe json per
picture, and the validation report stays out of the archive as well.

Everything below is asserted on zipfile.namelist(), the list the user
sees after unpacking the archive: the root holds classes.txt alone,
every other entry sits under images/, every picture is joined by its
json of the same name and folder, and no name of a previous revision
survives anywhere.
"""

import json
import os
import os.path as osp
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import cv2
import numpy as np

from anylabeling.custom.model_validation import dataset
from anylabeling.custom.model_validation import records as records_module
from anylabeling.custom.model_validation.exporter import (
    IMAGES_DIRNAME,
    export_zip,
    images_path,
    unique_zip_names,
    with_suffix,
)

CLASSES = ["car", "van"]
IMAGE_SIZE = (12, 10)


def write_image(path: str, value: int = 20) -> None:
    "Write a tiny, deterministic png file."

    os.makedirs(osp.dirname(path), exist_ok=True)
    image = np.full((IMAGE_SIZE[1], IMAGE_SIZE[0], 3), int(value), np.uint8)
    ok, buffer = cv2.imencode(".png", image)
    assert ok
    buffer.tofile(path)


def write_label(path: str, image_name: str) -> None:
    "Write the xlabel json of one staged pair."

    dataset.write_json(
        path,
        {
            "version": "3.0.0",
            "flags": {},
            "checked": False,
            "shapes": [
                {
                    "label": "car",
                    "shape_type": "rectangle",
                    "points": [[1, 1], [5, 1], [5, 5], [1, 5]],
                    "group_id": None,
                    "description": "",
                    "flags": {},
                }
            ],
            "imagePath": image_name,
            "imageData": None,
            "imageHeight": IMAGE_SIZE[1],
            "imageWidth": IMAGE_SIZE[0],
        },
    )


def staged(
    staging: str, kind: str, relpath: str, parent_record_id=None
) -> object:
    "Stage one pair of (image, xlabel json) and return its record."

    paths = dataset.staging_paths(staging, kind, relpath)
    write_image(paths["image"])
    write_label(paths["label"], osp.basename(relpath))
    return records_module.make_record(
        kind,
        relpath,
        paths["image"],
        paths["label"],
        parent_record_id=parent_record_id,
    )


def export(tmp_path, staging: str, records, suffix: str = "export"):
    "Export the records and return (summary, names, archive path)."

    zip_path = str(tmp_path / (suffix + ".zip"))
    summary = export_zip(records, staging, zip_path, list(CLASSES))
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
    return summary, names, zip_path


def names_of_pairs(names) -> dict:
    "Return {picture: json} of every picture / json pair of a namelist."

    pictures = [name for name in names if name.endswith(".png")]
    return {
        picture: osp.splitext(picture)[0] + ".json" for picture in pictures
    }


def test_the_root_holds_the_class_table_alone(tmp_path):
    "classes.txt is the one entry outside images/."

    staging = str(tmp_path / "staging")
    original = staged(staging, records_module.KIND_ORIGINAL, "a.png")
    summary, names, _zip = export(tmp_path, staging, [original])

    assert names[0] == "classes.txt"
    assert [name for name in names if "/" not in name] == ["classes.txt"]
    assert all(
        name.startswith(IMAGES_DIRNAME + "/")
        for name in names
        if name != "classes.txt"
    )
    assert summary["zip_entries"] == 2
    assert summary["renamed_entries"] == 0
    assert summary["renames"] == []


def test_no_name_of_a_previous_revision_survives(tmp_path):
    "Neither LabelMe json, nor the two folders, nor the report."

    staging = str(tmp_path / "staging")
    original = staged(staging, records_module.KIND_ORIGINAL, "a.png")
    child = staged(
        staging,
        records_module.KIND_AUGMENTED,
        "a_aug1.png",
        original.record_id,
    )
    child.include_in_export = True
    _summary, names, _zip = export(tmp_path, staging, [original, child])

    forbidden = (
        "labelme",
        "original/",
        "augmented/",
        "labels_",
        "validation_report",
    )
    for name in names:
        lowered = name.lower()
        for token in forbidden:
            assert token not in lowered, (name, token)
    assert names == [
        "classes.txt",
        "images/a.png",
        "images/a.json",
        "images/a_aug1.png",
        "images/a_aug1.json",
    ]


def test_every_picture_is_joined_by_its_json_in_its_own_folder(tmp_path):
    "A picture and its annotation are one pair of the same folder."

    staging = str(tmp_path / "staging")
    original = staged(staging, records_module.KIND_ORIGINAL, "a.png")
    child = staged(
        staging,
        records_module.KIND_AUGMENTED,
        "a_aug1.png",
        original.record_id,
    )
    child.include_in_export = True
    _summary, names, zip_path = export(tmp_path, staging, [original, child])

    pairs = names_of_pairs(names)
    assert len(pairs) == 2
    with zipfile.ZipFile(zip_path) as archive:
        for picture, label in pairs.items():
            assert label in names, picture
            assert osp.dirname(picture) == osp.dirname(label)
            payload = json.loads(archive.read(label).decode("utf-8"))
            assert payload["shapes"][0]["label"] == "car"
            assert payload["imagePath"] == osp.basename(picture)


def test_the_sub_folders_of_the_source_dataset_are_mirrored(tmp_path):
    "front/a.png and back/a.png stay two entries, neither overwrites one."

    staging = str(tmp_path / "staging")
    front = staged(staging, records_module.KIND_ORIGINAL, "front/a.png")
    back = staged(staging, records_module.KIND_ORIGINAL, "back/a.png")
    _summary, names, _zip = export(tmp_path, staging, [front, back])

    assert "images/front/a.png" in names
    assert "images/back/a.png" in names
    assert "images/front/a.json" in names
    assert "images/back/a.json" in names
    assert "images/a.png" not in names
    # two pictures, two annotations: nothing was flattened onto one name
    assert len([name for name in names if name.endswith(".png")]) == 2
    assert len(names_of_pairs(names)) == 2


def test_a_colliding_name_is_renamed_with_its_json(tmp_path):
    "A source file wearing the name of a copy costs one suffix, no more."

    staging = str(tmp_path / "staging")
    original = staged(staging, records_module.KIND_ORIGINAL, "a.png")
    # the pathological dataset: the source itself carries a_aug1.png, so
    # the augmented copy of a.png lands on a name that is already taken
    lookalike = staged(staging, records_module.KIND_ORIGINAL, "a_aug1.png")
    child = staged(
        staging,
        records_module.KIND_AUGMENTED,
        "a_aug1.png",
        original.record_id,
    )
    child.include_in_export = True
    summary, names, _zip = export(
        tmp_path, staging, [original, lookalike, child]
    )

    assert len(names_of_pairs(names)) == 3
    assert "images/a.png" in names
    assert "images/a.json" in names
    assert "images/a_aug1.png" in names
    assert "images/a_aug1.json" in names
    renamed = "images/a_aug1_1.png"
    assert renamed in names
    assert "images/a_aug1_1.json" in names
    assert summary["renamed_entries"] == 1
    assert summary["renames"][0]["relpath"] == "a_aug1.png"
    assert summary["renames"][0]["from"] == "images/a_aug1.png"
    assert summary["renames"][0]["to"] == renamed
    # every picture is still joined by its own json
    for picture, label in names_of_pairs(names).items():
        assert label in names, picture


def test_the_uniqueness_pass_never_overwrites_a_name():
    "The name maker answers distinct pairs for the same wanted names."

    wanted = [
        {"image": "images/a.png", "json": "images/a.json"},
        {"image": "images/a.png", "json": "images/a.json"},
        {"image": "images/a.png", "json": "images/a.json"},
    ]
    made = unique_zip_names(wanted)
    assert [entry["image"] for entry in made] == [
        "images/a.png",
        "images/a_1.png",
        "images/a_2.png",
    ]
    assert [entry["json"] for entry in made] == [
        "images/a.json",
        "images/a_1.json",
        "images/a_2.json",
    ]
    assert [entry["renamed"] for entry in made] == [False, True, True]
    assert len({entry["image"] for entry in made}) == 3
    assert len({entry["json"] for entry in made}) == 3


def test_the_helpers_of_a_name_keep_the_folder_and_the_type():
    "with_suffix and images_path only ever touch the file name."

    assert with_suffix("a.png", "_1") == "a_1.png"
    assert with_suffix("front/a.png", "_1") == "front/a_1.png"
    assert with_suffix("a_aug1.PNG", "_2") == "a_aug1_2.PNG"
    assert with_suffix("a", "_1") == "a_1"
    assert images_path("a.png") == "images/a.png"
    assert images_path("front/a.png") == "images/front/a.png"
    assert images_path("/front/a.png") == "images/front/a.png"
    assert images_path("front" + os.sep + "a.png") == "images/front/a.png"


def test_the_export_of_an_empty_selection_is_an_empty_folder(tmp_path):
    "Nothing selected still writes a readable archive, never a crash."

    staging = str(tmp_path / "staging")
    os.makedirs(osp.join(staging, dataset.IMAGES_DIRNAME), exist_ok=True)
    summary, names, _zip = export(tmp_path, staging, [])

    assert names == ["classes.txt"]
    assert summary["zip_entries"] == 0
    assert summary["originals"] == 0
    assert summary["augmented"] == 0
