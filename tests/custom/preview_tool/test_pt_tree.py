"""Tests of the directory tree widget of the preview tool.

The tree is built offscreen from synthetic core.ImageEntry records, so
nothing is written to the disk. The flat order of the leaves, the
pruning of directories without an image, the chosen marker and the
rebuild that keeps the folded state are the contracts pinned here.
"""

import os

from anylabeling.custom.preview_tool import core, list_panel

ROOT = os.path.join("/data", "preview")


def _entry(name, rel_dir="", path=None):
    """Build one synthetic entry, below ROOT unless a path is given."""

    if path is None:
        if rel_dir:
            path = os.path.join(ROOT, rel_dir, name)
        else:
            path = os.path.join(ROOT, name)
    return core.ImageEntry(path=path, name=name, rel_dir=rel_dir)


def _panel(entries, root=ROOT):
    """Build a tree from synthetic entries."""

    panel = list_panel.PreviewFileTree()
    panel.set_entries(entries, root)
    return panel


def _child_texts(item):
    """Return the labels of the direct children of a tree item."""

    return [item.child(index).text(0) for index in range(item.childCount())]


class TestTreeStructure:
    """The root is on top, directories only appear for visible images."""

    def test_root_node_shows_the_folder_name(self):
        panel = _panel([_entry("a.jpg")], "/data/photos")
        try:
            assert panel.root_text() == "photos"
            assert panel.root_item().toolTip(0) == "/data/photos"
        finally:
            panel.deleteLater()

    def test_empty_entries_keep_the_root_only(self):
        panel = _panel([], ROOT)
        try:
            assert panel.row_count() == 0
            assert panel.root_item() is not None
            assert panel.root_text() == "preview"
            assert panel.root_item().childCount() == 0
        finally:
            panel.deleteLater()

    def test_flat_rows_follow_the_visual_order(self):
        entries = [
            _entry("01.png"),
            _entry("02.png", "a"),
            _entry("03.png", os.path.join("a", "deep")),
            _entry("04.png", "b"),
        ]
        panel = _panel(entries)
        try:
            assert panel.row_count() == 4
            assert [panel.name_at(row) for row in range(4)] == [
                "01.png",
                "02.png",
                "03.png",
                "04.png",
            ]
            assert [panel.entries()[row].rel_dir for row in range(4)] == [
                "",
                "a",
                os.path.join("a", "deep"),
                "b",
            ]
            assert panel.entry_at(4) is None
            assert panel.file_item(-1) is None
        finally:
            panel.deleteLater()

    def test_directories_are_nested_like_the_relative_paths(self):
        entries = [
            _entry("01.png"),
            _entry("02.png", "a"),
            _entry("03.png", os.path.join("a", "deep")),
            _entry("04.png", "b"),
        ]
        panel = _panel(entries)
        try:
            assert _child_texts(panel.root_item()) == ["01.png", "a", "b"]
            a_item = panel.directory_item("a")
            deep_item = panel.directory_item(
                os.path.join("a", "deep")
            )
            assert a_item is not None
            assert deep_item is not None
            assert deep_item.parent() is a_item
            assert _child_texts(a_item) == ["02.png", "deep"]
        finally:
            panel.deleteLater()

    def test_parent_chain_of_a_deep_directory_is_kept(self):
        entries = [_entry("deep.png", os.path.join("one", "two", "three"))]
        panel = _panel(entries)
        try:
            assert panel.row_count() == 1
            assert _child_texts(panel.root_item()) == ["one"]
            one = panel.directory_item("one")
            two = panel.directory_item(os.path.join("one", "two"))
            three = panel.directory_item(
                os.path.join("one", "two", "three")
            )
            assert _child_texts(one) == ["two"]
            assert _child_texts(two) == ["three"]
            assert panel.file_item(0).parent() is three
        finally:
            panel.deleteLater()

    def test_a_directory_without_a_visible_image_is_not_built(self):
        entries = [_entry("a.png", "keep")]
        panel = _panel(entries)
        try:
            assert panel.directory_item("keep") is not None
            assert panel.directory_item("dropped") is None
            assert panel.root_item().childCount() == 1
        finally:
            panel.deleteLater()

    def test_directories_start_expanded(self):
        entries = [_entry("a.png", os.path.join("one", "two"))]
        panel = _panel(entries)
        try:
            assert panel.directory_item("one").isExpanded() is True
            assert panel.directory_item(
                os.path.join("one", "two")
            ).isExpanded() is True
        finally:
            panel.deleteLater()


class TestPickedMarker:
    """The chosen marker is a prefix and it is keyed by rel_dir + stem."""

    def test_marker_goes_to_the_matching_leaf_only(self):
        entries = [_entry("a.jpg"), _entry("a.jpg", "sub")]
        panel = _panel(entries)
        try:
            panel.set_picked({("", "a")})
            assert panel.row_text(0) == list_panel.PICK_MARK + "a.jpg"
            assert panel.row_text(1) == "a.jpg"
            panel.set_picked({("sub", "a")})
            assert panel.row_text(0) == "a.jpg"
            assert panel.row_text(1) == list_panel.PICK_MARK + "a.jpg"
            assert panel.picked() == frozenset({("sub", "a")})
        finally:
            panel.deleteLater()

    def test_a_plain_stem_means_the_root(self):
        entries = [_entry("a.jpg"), _entry("a.jpg", "sub")]
        panel = _panel(entries)
        try:
            panel.set_picked({"a"})
            assert panel.row_text(0) == list_panel.PICK_MARK + "a.jpg"
            assert panel.row_text(1) == "a.jpg"
        finally:
            panel.deleteLater()

    def test_clearing_the_set_clears_every_marker(self):
        entries = [_entry("a.jpg", "sub")]
        panel = _panel(entries)
        try:
            panel.set_picked({("sub", "a")})
            assert panel.row_text(0).startswith(list_panel.PICK_MARK)
            panel.set_picked(set())
            assert panel.row_text(0) == "a.jpg"
            assert panel.picked() == frozenset()
        finally:
            panel.deleteLater()


class TestCurrentRow:
    """Only a leaf reports a flat row; a directory node reports -1."""

    def test_set_current_row_does_not_emit(self):
        panel = _panel([_entry("a.jpg"), _entry("b.jpg")])
        try:
            seen = []
            panel.selection_changed.connect(seen.append)
            panel.set_current_row(1)
            assert panel.current_row() == 1
            assert seen == []
            panel.set_current_row(0)
            assert panel.current_row() == 0
            assert seen == []
        finally:
            panel.deleteLater()

    def test_clicking_a_leaf_emits_its_flat_row(self):
        panel = _panel([_entry("a.jpg"), _entry("b.jpg", "sub")])
        try:
            seen = []
            panel.selection_changed.connect(seen.append)
            panel.setCurrentItem(panel.file_item(1))
            assert seen == [1]
        finally:
            panel.deleteLater()

    def test_selecting_a_directory_node_emits_nothing(self):
        panel = _panel([_entry("a.jpg", "sub")])
        try:
            panel.set_current_row(0)
            seen = []
            panel.selection_changed.connect(seen.append)
            panel.setCurrentItem(panel.directory_item("sub"))
            assert seen == []
            assert panel.current_row() == -1
        finally:
            panel.deleteLater()

    def test_clearing_the_selection_gives_minus_one(self):
        panel = _panel([_entry("a.jpg")])
        try:
            panel.set_current_row(0)
            panel.set_current_row(-1)
            assert panel.current_row() == -1
        finally:
            panel.deleteLater()


class TestRebuild:
    """A filter rebuild keeps the folded nodes folded."""

    def test_folded_state_survives_a_rebuild(self):
        entries = [_entry("a.jpg", "sub"), _entry("b.jpg", "other")]
        panel = _panel(entries)
        try:
            panel.directory_item("sub").setExpanded(False)
            assert panel.directory_item("other").isExpanded() is True
            panel.set_entries(entries, ROOT)
            assert panel.directory_item("sub").isExpanded() is False
            assert panel.directory_item("other").isExpanded() is True
        finally:
            panel.deleteLater()

    def test_rebuild_replaces_the_rows(self):
        panel = _panel([_entry("a.jpg"), _entry("b.jpg")])
        try:
            panel.set_entries([_entry("c.jpg", "sub")], ROOT)
            assert panel.row_count() == 1
            assert panel.row_text(0) == "c.jpg"
            assert panel.directory_item("sub") is not None
        finally:
            panel.deleteLater()
