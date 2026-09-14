"""Mount point, state and file list wrapper of the label filter.

The feature never touches an upstream file: the whole behaviour is
attached to an existing labeling widget from the outside. Two things
happen here.

``install_label_filter`` adds one entry to the Tool menu - the only
visible mount, called from the two mount lines of
``LabelingWidget.__init__`` - and installs the file list wrapper. Both
steps are idempotent, so calling the function twice cannot leave a
second menu entry or a second wrapper behind.

``LabelFilterController`` owns the filter state of the widget and the
wrapper itself. The wrapper keeps the upstream method intact and calls
it as a whole, so the file list is still built by upstream (with the
search pattern of the user and everything else it does); it only adds
one pass afterwards that drops the rows the selection does not keep,
and decides what the canvas shows: the file that was already there
stays untouched while the selection keeps it, the first surviving row
is loaded when the selection drops it.
The state lives in widget attributes, never in the configuration, so a
restart leaves no trace:

* ``widget._label_filter_state`` is ``None`` while no filter is active,
  otherwise ``{"dir": <normalized folder>, "selected": frozenset(names)}``;
* ``widget._label_filter_action`` is the menu entry;
* ``widget._label_filter_controller`` is this controller;
* ``widget._label_filter_import_wrapped`` guards the wrapper;
* ``widget._label_filter_apply_failed`` reports, for one folder call,
  that the upstream method did not really run (the user cancelled the
  "unsaved annotations" question), which is what makes the dialog roll
  its selection back.

The filter follows the folder: opening another folder clears it, and
the status bar says so. Selecting no category at all is the same as
clearing the filter: there is no state that would show an empty file
list. A single dropped file is never filtered, because only a folder
call goes through the wrapper.
"""

from __future__ import annotations

import contextlib
import os.path as osp

from anylabeling.views.labeling.utils import qt as qt_utils

from . import core
from .launcher import launch_label_filter

__all__ = [
    "ACTION_NAME",
    "ACTION_TIP",
    "LabelFilterController",
    "install_label_filter",
]

ACTION_NAME = "标签过滤"
ACTION_TIP = "按标签分类过滤文件列表：勾选分类后只显示命中的图片"
ACTIVE_TIP_FORMAT = "%s（生效中：%d 类 / 显示 %d 张）"

NO_DIR_TEXT = "请先打开一个图片目录"
RESET_TEXT = "已切换目录，标签过滤已重置"
CLEAR_TEXT = "已清除标签过滤"
APPLY_FAIL_TEXT = "标签过滤未生效（操作已取消）"
FILTER_TEXT = "标签过滤：显示 %d/%d 张（%d 类）"


def install_label_filter(widget):
    """Attach the label filter to the Tool menu of a widget.

    Installing twice is a no op, so the mount point can be called again
    without adding a second entry to the menu. The wrapper of the file
    list is installed as well; it does nothing at all while no filter
    is active.

    Args:
        widget: The labeling widget to extend. It has to expose
            ``menus.tool``.

    Returns:
        The installed QAction, or ``None`` when the widget has no
        Tool menu.
    """

    menus = getattr(widget, "menus", None)
    menu = getattr(menus, "tool", None)
    if menu is None:
        return None
    existing = getattr(widget, "_label_filter_action", None)
    if existing is not None:
        return existing
    controller = _controller(widget)
    action = qt_utils.new_action(
        menu,
        ACTION_NAME,
        lambda _checked=False: launch_label_filter(widget),
        icon="labels",
        tip=ACTION_TIP,
    )
    menu.addAction(action)
    widget._label_filter_action = action
    controller.wrap_import_image_folder()
    return action


def _controller(widget):
    """Return the controller of a widget, creating it once."""

    controller = getattr(widget, "_label_filter_controller", None)
    if controller is None:
        controller = LabelFilterController(widget)
        widget._label_filter_controller = controller
    return controller


class LabelFilterController:
    """Filter state of one widget, and the wrapper that applies it."""

    def __init__(self, widget):
        self.widget = widget
        self._cache = core.LabelScanCache()
        self._gate_passed = False
        self._original = None
        self._original_widget = None

    # ------------------------------------------------------------ state

    def state(self):
        """Return the active filter state, or ``None``."""

        return getattr(self.widget, "_label_filter_state", None)

    def selected(self):
        """Return the selected category names, empty while inactive."""

        state = self.state()
        if not state:
            return frozenset()
        return state.get("selected") or frozenset()

    def action(self):
        """Return the menu entry of the widget, if it has one."""

        return getattr(self.widget, "_label_filter_action", None)

    def _notify(self, message):
        """Write one line into the status bar of the widget."""

        status_bar = getattr(self.widget, "statusBar", None)
        if status_bar is None:
            return
        status_bar().showMessage(message)

    def _update_tip(self, shown=None, categories=None):
        """Rewrite the tooltip of the menu entry from the state."""

        action = self.action()
        if action is None:
            return
        state = self.state()
        if not state or categories is None:
            action.setToolTip(ACTION_TIP)
            return
        action.setToolTip(
            ACTIVE_TIP_FORMAT % (ACTION_TIP, categories, shown or 0)
        )

    # ----------------------------------------------------------- wrapper

    def wrap_import_image_folder(self):
        """Wrap the folder import of the widget, once.

        The upstream method keeps its whole body: the wrapper calls it
        unchanged (its own pattern and load included) and only decides
        what happens around that call. Reading the original from the
        instance, not from the class, makes the wrapper stack with the
        wrapper another custom module may have installed before.
        """

        widget = self.widget
        if getattr(widget, "_label_filter_import_wrapped", False):
            return
        original = getattr(widget, "import_image_folder", None)
        if not callable(original):
            return
        controller = self

        def import_image_folder(dirpath, pattern=None, load=True):
            state = controller.state()
            active = bool(state)
            current = normalized_dir(dirpath)
            if active and current != state.get("dir"):
                # Another folder: the filter belongs to the folder it
                # was built for, so it is dropped - but only once the
                # folder import really happens. The unsaved-annotations
                # question comes first, and a refused answer returns on
                # the spot with the old list in place: the state and
                # the tooltip then have to stay with that list instead
                # of claiming a reset that never happened.
                with controller._gate_probe():
                    result = original(dirpath, pattern=pattern, load=load)
                if not controller._run_happened():
                    return result
                widget._label_filter_state = None
                controller._notify(RESET_TEXT)
                controller._update_tip()
                return result
            if not active:
                return original(dirpath, pattern=pattern, load=load)
            # The filtered folder: let upstream rebuild the whole list
            # first, then drop the rows the selection does not keep.
            widget._label_filter_apply_failed = False
            # The file the canvas shows right now: upstream forgets it
            # as its first side effect (filename = None, list cleared),
            # so the pass below can only be told about it from here.
            current = getattr(widget, "filename", None)
            with controller._gate_probe():
                original(dirpath, pattern=pattern, load=False)
            if not controller._run_happened():
                # The user cancelled the question, so upstream returned
                # on the spot: the filter is stale and the caller (the
                # dialog) has to roll back.
                widget._label_filter_apply_failed = True
                return None
            controller._post_filter(load, current)
            return None

        self._original = original
        self._original_widget = widget
        widget.import_image_folder = import_image_folder
        widget._label_filter_import_wrapped = True

    @contextlib.contextmanager
    def _gate_probe(self):
        """Watch the question the upstream method asks first.

        The wrapper cannot see the return value of the upstream method
        and the folder it was called with is not a reliable signal
        either: a cancelled "unsaved annotations" question leaves the
        remembered folder untouched when the same folder is imported
        again, which is exactly what happens when the dialog is
        confirmed twice in a row.

        What is reliable is the question itself. The upstream folder
        import asks may_continue() before it does anything at all and
        returns on the spot when the answer is no, so the gate is the
        earliest and the most precise evidence that the call ran. At
        most one such call runs at a time - the question is modal and
        the dialog that starts the import is modal as well - so a
        plain flag on this controller is enough. The answer is stored
        and passed on unchanged, and the method of the widget is put
        back before anything else can see it.
        """

        widget = self.widget
        original = getattr(widget, "may_continue", None)
        self._gate_passed = False

        def may_continue():
            answer = original()
            self._gate_passed = bool(answer)
            return answer

        try:
            if callable(original):
                widget.may_continue = may_continue
            yield
        finally:
            if callable(original):
                widget.may_continue = original

    def _run_happened(self):
        """Return whether the upstream import passed its gate.

        The flag starts as False on every probed call and only the
        refused or accepted gate ever sets it, so False means "the
        import returned before it asked", which is the cancelled case
        the caller has to roll back.
        """

        return self._gate_passed

    def _call_original(self, dirpath, load=False):
        """Run the folder import captured when the wrapper was built.

        The wrapper of the instance is skipped on purpose, and with it
        every wrapper another custom module stacked on top of it: its
        only work around the call is the filtering itself, and the
        clear path wants the plain upstream rebuild that shows the
        whole folder. The captured method is still the one upstream
        owns, so the question it asks is seen by the probe of the
        caller.
        """

        target = self._original
        if target is None:
            target = getattr(self.widget, "import_image_folder", None)
        if not callable(target):
            return None
        return target(dirpath, load=load)

    def _post_filter(self, load, current=None):
        """Drop the rows the selection does not keep.

        The work is done from the last row to the first one so that the
        indexes of the remaining rows never move, and the row -> index
        map of the widget is rebuilt afterwards: upstream readers
        (open_next_image, load_file, the context menu) all go through
        ``fn_to_index``, so it has to match the shortened list.

        ``current`` is the file the canvas was showing before the folder
        was rebuilt, read by the wrapper before upstream forgot it.
        While the selection keeps it, it stays on screen untouched and
        only its row is moved; when the selection drops it (or there
        was no current file at all), the widget falls back to the first
        surviving row, and the ``load`` of the caller decides whether
        that one is loaded as well.

        Taking the current file over is the answer to a caller that
        asked for a load, which is the confirm of the dialog. A caller
        that passed ``load=False`` - the search bar, the re-import
        after the annotations folder changed - keeps the upstream
        division of labour: nothing is loaded and the current row
        stays untouched, so a caller that sets that row itself (and
        loads the file with it) still works.
        """

        widget = self.widget
        selected = self.selected()
        state = self.state()
        output_dir = getattr(widget, "output_dir", None)
        shown = 0
        total = 0
        rows = None
        items = getattr(widget, "file_list_widget", None)
        if items is not None:
            total = items.count()
            items.setUpdatesEnabled(False)
            try:
                for row in range(items.count() - 1, -1, -1):
                    item = items.item(row)
                    if core.is_hit(
                        item.text(), output_dir, selected, self._cache
                    ):
                        shown += 1
                        continue
                    items.takeItem(row)
            finally:
                items.setUpdatesEnabled(True)
            widget.fn_to_index = {
                items.item(row).text(): row
                for row in range(items.count())
            }
            rows = widget.fn_to_index
        if not load or not self._keep_shown_file(current, rows, items):
            widget.filename = None
            widget.open_next_image(load=load)
        categories = len(state.get("selected") or ()) if state else 0
        self._notify(FILTER_TEXT % (shown, total, categories))
        self._update_tip(shown, categories)
        return shown

    def _keep_shown_file(self, current, rows, items):
        """Keep the file the canvas shows, when the new list holds it.

        The canvas is not touched at all in that case: the file the
        widget already had is put back and only its row is moved, so
        the image and the annotations on it stay exactly as the user
        left them.

        Returns:
            True when the shown file is the current one again, False
            when the caller has to navigate to another row.
        """

        if not current or not rows or current not in rows:
            return False
        self.widget.filename = current
        self._select_row(items, rows[current])
        return True

    def _select_row(self, items, row):
        """Move the selection of the file list without loading a file.

        Upstream answers a moved selection with a full load of that row
        (itemSelectionChanged -> file_selection_changed -> load_file),
        which would hit the very file the canvas already shows. The
        signals of the list are blocked around the call - the gesture
        the batch runner uses - so that keeping the shown image really
        is a no-op for the canvas.
        """

        if items is None:
            return
        blocked = items.blockSignals(True)
        try:
            items.setCurrentRow(row)
        finally:
            items.blockSignals(blocked)

    # ------------------------------------------------------------ public

    def scan(self, progress_cb=None, should_stop=None):
        """Enumerate the categories of a folder, for the dialog.

        Returns:
            A ``core.ScanResult``, or ``None`` when the scan was
            stopped by ``should_stop`` - the dialog treats that as a
            cancellation and changes nothing.
        """

        dirpath = getattr(self.widget, "last_open_dir", None)
        if not dirpath:
            return None
        files = core.collect_files(dirpath)
        return core.classify(
            files,
            output_dir=getattr(self.widget, "output_dir", None),
            cache=self._cache,
            progress_cb=progress_cb,
            should_stop=should_stop,
        )

    def apply(self, selected):
        """Turn a selection into the active filter of the folder.

        An empty selection is not a filter: it clears the active one,
        so the user never ends up with an empty file list.

        Returns:
            True when the file list now shows the selection, False when
            nothing was applied (no folder open, or the user cancelled
            the "unsaved annotations" question of the widget).
        """

        widget = self.widget
        dirpath = getattr(widget, "last_open_dir", None)
        if not dirpath:
            self._notify(NO_DIR_TEXT)
            return False
        selected = frozenset(selected or ())
        if not selected:
            return self.reset()
        previous = self.state()
        widget._label_filter_state = {
            "dir": osp.normpath(osp.abspath(dirpath)),
            "selected": selected,
        }
        widget._label_filter_apply_failed = False
        # The canvas has to end on a surviving image: the wrapper keeps
        # the one already shown while the selection keeps it, and loads
        # the first surviving one otherwise.
        widget.import_image_folder(dirpath, load=True)
        if getattr(widget, "_label_filter_apply_failed", False):
            widget._label_filter_state = previous
            widget._label_filter_apply_failed = False
            self._notify(APPLY_FAIL_TEXT)
            return False
        return True

    def reset(self):
        """Clear the filter and show the folder in full again.

        The state is dropped only after the folder was really rebuilt:
        the upstream import asks the unsaved-annotations question
        first, and a refused answer returns on the spot with the old,
        still filtered list in place. The upstream method is called
        directly there, in the probe, so the run can be told from a
        cancelled one - the wrapper of the instance would only filter
        the fresh list again.

        The canvas is left on the file it shows: that file survived the
        filter, so the clear only has to make it the current row of the
        rebuilt list again - no reload and no jump to another image.

        Returns:
            True when the filter is gone, False when the user refused
            the question: the list, the state and the tooltip then
            stay as they were, and no "cleared" message is written.
        """

        widget = self.widget
        dirpath = getattr(widget, "last_open_dir", None)
        if dirpath:
            current = getattr(widget, "filename", None)
            with self._gate_probe():
                self._call_original(dirpath, load=False)
            if not self._run_happened():
                return False
            # The rebuild forgets the current file and lands on the
            # first row without loading it, while the canvas still
            # shows the file the filter had kept. That file is part of
            # the full list again, so it is put back where it was
            # instead of leaving the list and the image apart.
            rows = getattr(widget, "fn_to_index", None) or {}
            items = getattr(widget, "file_list_widget", None)
            self._keep_shown_file(current, rows, items)
        widget._label_filter_state = None
        self._notify(CLEAR_TEXT)
        self._update_tip()
        return True


def normalized_dir(dirpath):
    """Return the normalized folder of a folder path, or ``None``."""

    if not dirpath:
        return None
    return osp.normpath(osp.abspath(str(dirpath)))
