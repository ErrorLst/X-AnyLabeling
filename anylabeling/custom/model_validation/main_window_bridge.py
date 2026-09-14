"""Bridge between the validation window and the main labeling window.

The validation window shows its own copy of a dataset: staging keeps
the pictures in ``images/`` and the labels in ``labels/``, while the
main window finds the annotation of a picture in the sibling json next
to it, under the very same name. The bridge closes that gap for one
record at a time.

* :class:`MainWindowBridge` opens a single record in the main window
  (``LabelingWidget.load_file``) behind a short series of refusals, so a
  record the main window cannot load never reaches it.
* :class:`StagingSync` watches the staging folder and copies the sibling
  json the main window saves back into the canonical staging label, so
  an edit made in the main window survives the jump.

Nothing here touches the source dataset: every path is a staging path,
and the only file the bridge creates is the sibling json inside the
staging folder.

The sibling json is only the mirror the main window reads: every jump
starts from the canonical staging label, the file an export and the
results page read. An absent sibling is created from it, a sibling an
earlier jump left behind is refreshed from it, and a sibling that
already carries the canonical bytes is left untouched. The write back
goes the other way only: the save of the main window travels from the
sibling into the canonical label (see StagingSync), never the reverse.
"""

from __future__ import annotations

import json
import os
import os.path as osp
import shutil

from PyQt6 import QtCore, QtWidgets

from . import dataset

DEBOUNCE_MS = 300


def sibling_label_path(staging_image_path: str) -> str:
    """Return the sibling json path the main window looks for.

    The main window derives the label file from the picture path alone
    (same name, json extension), so the staging picture of a record
    decides where its sibling json has to live.
    """

    if not staging_image_path:
        return ""
    return osp.splitext(str(staging_image_path))[0] + ".json"


def _normalise(path) -> str:
    """Return the comparable form of a path, or "" when unusable."""

    try:
        return osp.normcase(osp.abspath(str(path)))
    except (TypeError, ValueError):
        return ""


class StagingSync(QtCore.QObject):
    """Copy what the main window saves back into the canonical label.

    One watcher watches the whole staging folder: every json of the two
    label trees, the four staging folders (so a rename or a new file is
    noticed) and the sibling json the bridge handed to the main window.
    A single restarting timer debounces the burst of events one save
    produces, and every path Qt takes off its watch list when an event
    is delivered is put back on it.

    The write back is a plain rewrite of the canonical json
    (``open(..., "w")``), never a replace: the upstream writer already
    replaced the sibling, and the canonical label of an export has to
    keep its inode for the results page that still holds it.

    The sibling json starts as a hard link of the canonical label, and
    inotify reports an event on a shared inode under the path that was
    watched first - the canonical one. The first save of the main
    window, the very save that breaks the link, is therefore answered
    from the canonical path as well (see _adopt_canonical), and every
    folder event asks the siblings it holds before the debounce closes.

    A sibling is registered by note_sibling() alone, and only for a
    record the bridge really handed over: attach() watches the json trees
    of the staging folder, never a sibling, because a sibling of an
    earlier jump describes a label this run has not written yet.
    """

    record_changed = QtCore.pyqtSignal(str)
    status_message = QtCore.pyqtSignal(str)

    def __init__(self, parent=None, debounce_ms=None):
        super().__init__(parent)
        self._watcher = QtCore.QFileSystemWatcher(self)
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        interval = DEBOUNCE_MS if debounce_ms is None else int(debounce_ms)
        self._timer.setInterval(interval)
        self._timer.timeout.connect(self.flush_pending)
        self._watcher.fileChanged.connect(self.queue_path)
        self._watcher.directoryChanged.connect(self._on_directory_changed)
        self._staging_root = ""
        self._records = {}
        self._records_by_path = {}
        self._siblings = {}
        self._watch_dirs = {}
        self._watched = {}
        self._pending = {}
        self._canonical_text = {}
        # True while detach() runs its final flush: the write back of a
        # pending save still happens, but a run that is over wakes no
        # page any more
        self._silent = False

    @property
    def attached(self) -> bool:
        """Return True while a staging folder is watched."""

        return bool(self._staging_root)

    def attach(self, staging_root: str, records) -> None:
        """Watch the staging folder that holds the given records."""

        self.detach()
        self._staging_root = str(staging_root or "")
        for record in records or []:
            if record is None:
                continue
            self._records[record.record_id] = record
            label_path = str(
                getattr(record, "staging_label_path", "") or ""
            )
            if not label_path:
                continue
            self._records_by_path[_normalise(label_path)] = record
            self._remember_canonical(record, label_path)
        if not self._staging_root:
            return
        if not osp.isdir(self._staging_root):
            self.status_message.emit(
                "暂存目录不存在，主窗口的编辑同步未启用"
            )
            return
        paths = []
        for folder in (dataset.ORIGINAL_DIRNAME, dataset.AUGMENTED_DIRNAME):
            for sub in (dataset.IMAGES_DIRNAME, dataset.LABELS_DIRNAME):
                directory = osp.join(self._staging_root, folder, sub)
                if not osp.isdir(directory):
                    continue
                paths.append(directory)
                self._watch_dirs[_normalise(directory)] = directory
                if sub == dataset.LABELS_DIRNAME:
                    paths.extend(self._json_files(directory))
        self._add_paths(paths, notify=True)

    def detach(self) -> None:
        """Stop watching and forget every record of the run.

        The paths still waiting in the debounce are flushed one last time
        first: a save made inside the debounce window and a window closed
        right after it must not throw that edit away, and detach() is
        also the way of the next run, which replaces the records of this
        one. The flush needs the very state this method is about to drop,
        it writes no signal any more - the run it would report on is
        over - and it is never allowed to raise, because it runs on the
        close path of the tool.
        """

        self._timer.stop()
        self._silent = True
        try:
            self.flush_pending()
        except Exception:  # noqa: BLE001
            # the run is over either way: a write back that failed here
            # is not a reason to abort the close of the window
            pass
        finally:
            self._silent = False
        watched = list(self._watched.values())
        watched.extend(self._watcher.files())
        watched.extend(self._watcher.directories())
        if watched:
            self._watcher.removePaths(watched)
        self._staging_root = ""
        self._records = {}
        self._records_by_path = {}
        self._siblings = {}
        self._watch_dirs = {}
        self._watched = {}
        self._canonical_text = {}

    def note_sibling(self, record) -> None:
        """Watch the sibling json of one record.

        The bridge calls this right after it wrote the sibling, so the
        save that follows lands on a watched path.
        """

        if record is None:
            return
        sibling = sibling_label_path(
            getattr(record, "staging_image_path", "")
        )
        if not sibling:
            return
        self._siblings[record.record_id] = sibling
        if not self._staging_root:
            return
        stored = self._records.get(record.record_id, record)
        self._records.setdefault(record.record_id, stored)
        self._records_by_path[_normalise(sibling)] = stored
        label_path = str(getattr(stored, "staging_label_path", "") or "")
        if label_path:
            self._records_by_path.setdefault(
                _normalise(label_path), stored
            )
        self._add_paths([sibling])

    def queue_path(self, path) -> None:
        """Remember one changed path and restart the debounce timer."""

        key = _normalise(path)
        if not key or not self._staging_root:
            return
        self._pending[key] = str(path)
        self._timer.start()

    def flush_pending(self) -> None:
        """Adopt every path queued since the last flush.

        The sibling of a record is copied back into its canonical
        label; a canonical label changed behind the tool is only
        reported. Both mark the record edited and emit record_changed
        once per record, and every mapped path is watched again.
        """

        if self._timer.isActive():
            self._timer.stop()
        pending = self._pending
        self._pending = {}
        if not pending:
            return
        changed = []
        reported = set()
        for key, path in pending.items():
            record = self._records_by_path.get(key)
            if record is None:
                continue
            sibling = self._siblings.get(record.record_id, "")
            if sibling and _normalise(sibling) == key:
                adopted = self._adopt_sibling(record, path)
            elif _normalise(record.staging_label_path) == key:
                adopted = self._adopt_canonical(record, path)
            else:
                adopted = False
            if not adopted:
                continue
            record.edited = True
            if record.record_id not in reported:
                reported.add(record.record_id)
                changed.append(record.record_id)
        self._add_paths(
            [
                path
                for key, path in pending.items()
                if key in self._records_by_path
            ]
        )
        # getattr: flush_pending stays callable on a synchronization built
        # by an older revision of this class
        if getattr(self, "_silent", False):
            return
        for record_id in changed:
            self.record_changed.emit(record_id)

    def _adopt_sibling(self, record, sibling_path: str) -> bool:
        """Copy one sibling json over the canonical label of a record."""

        try:
            with open(sibling_path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except (OSError, ValueError):
            return False
        try:
            json.loads(text)
        except ValueError:
            # a save in flight: an editor that truncated the file is
            # about to write it, and a half written label must never
            # reach the canonical json an export reads
            return False
        if self._canonical_text.get(record.record_id) == text:
            # the canonical label already carries this text: a second
            # event of one save, or the event our own write back fed
            # the watcher. Nothing is written and nothing is reported,
            # so one save never costs two rows a repaint.
            return False
        canonical = str(getattr(record, "staging_label_path", "") or "")
        if not canonical:
            return False
        try:
            with open(canonical, "w", encoding="utf-8") as handle:
                handle.write(text)
        except OSError:
            self.status_message.emit(
                "无法把主窗口的修改写回暂存标注：%s" % canonical
            )
            return False
        self._canonical_text[record.record_id] = text
        return True

    def _adopt_canonical(self, record, path: str) -> bool:
        """Answer one event reported on the canonical label of a record.

        Two different changes hide behind that path. A canonical label
        the results page rewrote is news and is only reported: the
        change already is on disk and a write would only feed the
        watcher its own event. But while the sibling json still shares
        its inode with the canonical label - the link the bridge made
        is only broken by the first save of the main window - inotify
        reports that save under the canonical path of the record, and
        the text of the canonical label has not moved. A sibling that
        is younger than the canonical label is that save and is copied
        back like any other.
        """

        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except (OSError, ValueError):
            return False
        if self._canonical_text.get(record.record_id) != text:
            self._canonical_text[record.record_id] = text
            return True
        # the write this sync just made itself, or the second event of
        # a change already reported: nothing new to say, unless a
        # younger sibling says the main window saved
        sibling = self._siblings.get(record.record_id, "")
        if not sibling or not osp.isfile(sibling):
            return False
        try:
            if osp.getmtime(sibling) <= osp.getmtime(path):
                return False
        except OSError:
            return False
        return self._adopt_sibling(record, sibling)

    def _remember_canonical(self, record, label_path: str) -> None:
        """Remember the text the canonical label carries right now."""

        try:
            with open(label_path, "r", encoding="utf-8") as handle:
                self._canonical_text[record.record_id] = handle.read()
        except (OSError, ValueError):
            self._canonical_text.pop(record.record_id, None)

    def _on_directory_changed(self, path) -> None:
        """Pick up the json a rename or a new file brought in.

        The upstream writer replaces the sibling json on every save, so
        the folder that holds it changes; asking the sibling itself
        covers the save even when inotify reports it under the
        canonical path its inode is shared with.
        """

        if not self._staging_root:
            return
        directory = str(path)
        if _normalise(directory) not in self._watch_dirs:
            return
        self._add_paths(self._json_files(directory))
        self._add_paths([directory])
        self._queue_siblings_in(directory)

    def _queue_siblings_in(self, directory: str) -> None:
        """Queue every sibling json that lives in a changed folder."""

        key = _normalise(directory)
        for sibling in list(self._siblings.values()):
            if _normalise(osp.dirname(sibling)) == key:
                self.queue_path(sibling)

    def _add_paths(self, paths, notify: bool = False):
        """Watch paths, answering the ones that could not be watched.

        Qt answers a path it already watches exactly like one it cannot
        watch at all, so the current list of the watcher decides what
        still has to be added; and a watch list that hits the inotify
        limit is reported as a line of status instead of raised - the
        page re-reads every label from disk when a record is selected,
        which is what keeps a missed event harmless.
        """

        current = {_normalise(item) for item in self._watcher.files()}
        current.update(
            _normalise(item) for item in self._watcher.directories()
        )
        fresh = {}
        for path in paths or []:
            if not path:
                continue
            key = _normalise(path)
            if not key or key in current or key in fresh:
                continue
            fresh[key] = str(path)
        if not fresh:
            return []
        failed = self._watcher.addPaths(list(fresh.values())) or []
        failed_keys = {_normalise(item) for item in failed}
        for key, path in fresh.items():
            if key in failed_keys:
                continue
            self._watched[key] = path
        if notify and failed:
            self.status_message.emit(
                "暂存目录监听不完整（%d 个路径），"
                "切换记录时会重新读取磁盘上的标注" % len(failed)
            )
        return failed

    @staticmethod
    def _json_files(directory: str):
        """Return every json below a directory, in a stable order."""

        found = []
        for current, _dirs, names in os.walk(directory):
            for name in sorted(names):
                if name.lower().endswith(".json"):
                    found.append(osp.join(current, name))
        return found


class MainWindowBridge(QtCore.QObject):
    """Open one validation record in the main labeling window.

    The main window is a ``LabelingWidget``; ``None`` means the tool was
    started on its own, and every jump is then refused with a line of
    status instead of an exception. The class never writes outside the
    staging folder of its records.
    """

    record_changed = QtCore.pyqtSignal(str)
    status_message = QtCore.pyqtSignal(str)

    def __init__(self, main_window=None, parent=None):
        super().__init__(parent)
        self._main_window = main_window
        self._sync = StagingSync(self)
        # one connection for the whole life of the bridge: attach and
        # detach only move the watcher, they never rewire a signal
        self._sync.record_changed.connect(self.record_changed)
        self._sync.status_message.connect(self.status_message)

    @property
    def main_window(self):
        """Return the main window, ``None`` when the tool runs alone."""

        return self._main_window

    @main_window.setter
    def main_window(self, window):
        self._main_window = window

    @property
    def sync(self):
        """Return the staging synchronization behind the bridge."""

        return self._sync

    def attach(self, staging_root: str, records) -> None:
        """Watch the staging folder of a run."""

        self._sync.attach(staging_root, records)

    def detach(self) -> None:
        """Stop watching the staging folder."""

        self._sync.detach()

    def open_record(self, record) -> bool:
        """Open one record in the main window, or explain the refusal.

        The gates are asked in the order of the damage a jump could do:
        a missing window or record, a record without a picture or a
        label, a label the main window cannot load, a main window whose
        output_dir would take the edits away from the export, the
        unsaved annotations of the user, and finally the sibling json
        the main window reads. True means the record is on screen in
        the main window.

        The jump only switches the current file of the main window: it
        never lifts the main window and never takes the keyboard focus,
        because the user is the one who decides where the focus goes.
        ``load_file`` ends on ``canvas.setFocus()`` upstream, so the
        window that asked for the jump is the one that takes the focus
        back (see ``ModelValidationDialog._restore_validation_focus``);
        a raise or a front is the click of the user alone.

        The sibling is refreshed from the canonical label before it is
        handed to the main window, and a save the debounce has not
        carried back yet is flushed first: refreshing the mirror before
        the last edit of the user reached the canonical label would
        throw that edit away. The may_continue gate stands before the
        flush on purpose, because the save it can trigger is such an
        edit.
        """

        window = self._main_window
        if window is None:
            self.status_message.emit(
                "未连接主窗口，无法在窗口中打开该标注"
            )
            return False
        if record is None:
            self.status_message.emit("没有可打开的记录")
            return False
        image_path = str(
            getattr(record, "staging_image_path", "") or ""
        )
        if not image_path or not osp.isfile(image_path):
            self.status_message.emit(
                "暂存图片不存在，无法打开：%s" % (image_path or "?")
            )
            return False
        label_path = str(
            getattr(record, "staging_label_path", "") or ""
        )
        if not label_path:
            self.status_message.emit(
                "该记录没有暂存标注（无标签项），无法编辑"
            )
            return False
        if not osp.isfile(label_path):
            self.status_message.emit(
                "暂存标注文件不存在：%s" % label_path
            )
            return False
        data = self._read_label(label_path)
        if data is None:
            self.status_message.emit(
                "暂存标注无法读取：%s" % label_path
            )
            return False
        if not isinstance(data, dict) or "shapes" not in data or (
            "imagePath" not in data
        ):
            self.status_message.emit(
                "暂存标注缺少 shapes 或 imagePath，主窗口无法加载"
            )
            return False
        if getattr(window, "output_dir", ""):
            if not self._confirm_output_dir():
                self.status_message.emit(
                    "已取消：标注会写入 output_dir 而非暂存目录"
                )
                return False
        may_continue = getattr(window, "may_continue", None)
        if callable(may_continue) and not may_continue():
            self.status_message.emit(
                "已取消：主窗口还有未保存的标注"
            )
            return False
        if self._sync.attached:
            self._sync.flush_pending()
        if not self._prepare_sibling(record, label_path):
            self.status_message.emit(
                "无法在暂存图片旁创建同名标注文件"
            )
            return False
        self._sync.note_sibling(record)
        window.load_file(image_path)
        self.status_message.emit(
            "已在主窗口中打开：%s" % osp.basename(image_path)
        )
        return True

    def _confirm_output_dir(self) -> bool:
        """Ask the user before a jump the main window would misplace.

        The message box is parented to the validation window on
        purpose: only that window steps aside, the main window is not
        hidden behind a modal dialog of a window that is still closed.
        """

        answer = QtWidgets.QMessageBox.question(
            self.parent(),
            "主窗口已设置输出目录",
            "标注将读写 output_dir 而非暂存目录，"
            "编辑不会进入导出。仍要继续吗？",
            QtWidgets.QMessageBox.StandardButton.Yes
            | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        return answer == QtWidgets.QMessageBox.StandardButton.Yes

    @staticmethod
    def _read_label(path: str):
        """Read a staging label, answering None when it is unusable."""

        try:
            return dataset.read_json(path)
        except (OSError, ValueError):
            return None

    @staticmethod
    def _prepare_sibling(record, label_path: str) -> bool:
        """Make the sibling json carry the canonical label of a record.

        The main window only looks for a json next to the picture while
        staging keeps the labels in their own tree, and the sibling is
        no more than the mirror of the canonical label the export and
        the results page read: it is refreshed on every jump.

        An absent sibling is linked, never moved - the upstream writer
        replaces the file on every save, which breaks the link, and
        StagingSync is what carries that save back into the canonical
        label - with a copy as the fallback of a file system without
        links. A sibling that already carries the canonical bytes is
        left alone, which is the case of a hard link and also the case
        of a jump that follows a save; writing it would only feed the
        watcher an event. A sibling an earlier jump left behind is
        rewritten in place (never replaced, so its inode and any link
        survive) with the canonical bytes, so the main window never
        shows an annotation the export does not have.
        """

        sibling = sibling_label_path(
            getattr(record, "staging_image_path", "")
        )
        if not sibling:
            return False
        try:
            with open(label_path, "rb") as handle:
                canonical = handle.read()
        except OSError:
            return False
        if osp.exists(sibling):
            try:
                with open(sibling, "rb") as handle:
                    current = handle.read()
            except OSError:
                current = None
            if current == canonical:
                return True
            try:
                with open(sibling, "wb") as handle:
                    handle.write(canonical)
            except OSError:
                return False
            return True
        try:
            os.link(label_path, sibling)
            return True
        except OSError:
            pass
        try:
            shutil.copy2(label_path, sibling)
        except OSError:
            return False
        return True


__all__ = [
    "DEBOUNCE_MS",
    "MainWindowBridge",
    "StagingSync",
    "sibling_label_path",
]
