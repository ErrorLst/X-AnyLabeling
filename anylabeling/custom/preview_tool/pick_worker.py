"""Background pick and remove jobs of the preview tool.

One PickJob is one QThread around the pure functions of pick_core. It
never builds a widget and never talks to the dialog directly: every
result leaves through a signal, so the main thread stays free while a
thousand files are copied or moved.

Four kinds exist, PickJob.KINDS:

* "pick" - targets is a list of core.ImageEntry images (a plain source
  path works too), each one goes through pick_core.pick_one;
* "all" - entries is the visible list, every image that is not
  copied yet goes through pick_core.pick_one, the copied ones are
  skipped;
* "remove" - targets is a list of (rel_dir, stem) keys (a bare stem
  works too), each one goes through pick_core.unpick_stem, which moves
  every copy of that family and its side cars into the trash folder;
  the copies live flat in the output folder, so the rel_dir of the
  key is only the identity of the row;
* "detect" - entries is the visible list, the keys of the copied
  images come back through detect_finished.

Nothing in this module deletes a file: a removal is a move into
pick_core.trash_dir(), exactly like the pure core does it.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Sequence, Tuple

from PyQt6 import QtCore

from . import pick_core

__all__ = ["PickJob", "moved_path"]

#: Number of threads used by the parallel kinds.
JOB_WORKERS = 4


def moved_path(item) -> str:
    """Return the trash path of one entry of UnpickStemResult.moved.

    The item is a small record of pick_core; the attribute names are
    read defensively so that a rename inside the core cannot make a
    removal invisible.
    """

    if isinstance(item, str):
        return item
    for name in ("target", "dest", "moved", "path", "name"):
        value = getattr(item, name, None)
        if isinstance(value, str) and value:
            return value
    if isinstance(item, (tuple, list)) and item:
        last = item[-1]
        if isinstance(last, str):
            return last
    return ""


def _stem(value) -> str:
    """Return the stem of a path, a name or a core entry."""

    if value is None:
        return ""
    if not isinstance(value, str):
        value = getattr(value, "name", None) or getattr(value, "path", "")
    return os.path.splitext(os.path.basename(str(value)))[0]


def _target_key(value):
    """Return the (rel_dir, stem) key of a remove target.

    A target is either a key tuple or a bare stem, which belongs to
    the root of the input tree. The rel_dir part is only the identity
    of the row: the copies of a stem live flat in the output folder,
    so the removal of one family covers every copy of that stem.
    """

    try:
        return pick_core.entry_key(value)
    except Exception:
        return "", _stem(value)


class PickJob(QtCore.QThread):
    """Copy, remove or detect the copies of one output directory.

    Signals:
        progress(int, int) - (done, total) of the running kind.
        item_picked(object, str) - ((rel_dir, stem), dest) of a copy.
        item_removed(object, str) - ((rel_dir, stem), moved path).
        item_failed(object, str) - ((rel_dir, stem), message).
        detect_finished(object) - frozenset of the copied keys.
        job_done(str, int, int) - (kind, ok, total).
    """

    progress = QtCore.pyqtSignal(int, int)
    item_picked = QtCore.pyqtSignal(object, str)
    item_removed = QtCore.pyqtSignal(object, str)
    item_failed = QtCore.pyqtSignal(object, str)
    detect_finished = QtCore.pyqtSignal(object)
    job_done = QtCore.pyqtSignal(str, int, int)

    #: Every kind the dialog may ask for.
    KINDS = ("pick", "all", "remove", "detect")

    def __init__(self, kind, entries=(), output_dir="", targets=(),
                 parent=None):
        """Build a job.

        Args:
            kind: One of KINDS.
            entries: The visible core.ImageEntry list, used by "all"
                and "detect".
            output_dir: The directory the copies go into.
            targets: Images (core.ImageEntry or a source path) for
                "pick", (rel_dir, stem) keys for "remove".
            parent: The owning QObject.
        """

        super().__init__(parent)
        self._kind = str(kind or "")
        self._entries = list(entries or ())
        self._output_dir = str(output_dir or "")
        self._targets = list(targets or ())
        self._cancelled = False

    # ------------------------------------------------------------ state

    @property
    def kind(self) -> str:
        """Return the kind of this job."""

        return self._kind

    @property
    def entries(self) -> List:
        """Return the entries of this job."""

        return list(self._entries)

    @property
    def targets(self) -> List:
        """Return the targets of this job."""

        return list(self._targets)

    def cancel(self) -> None:
        """Ask the job to stop as soon as the running file is done."""

        self._cancelled = True
        self.requestInterruption()

    def _stop(self) -> bool:
        """Return True when the job was asked to stop."""

        if self._cancelled:
            return True
        return bool(self.isInterruptionRequested())

    # -------------------------------------------------------------- run

    def run(self) -> None:
        """Run the job; a broken future never escapes this method."""

        kind = self._kind
        try:
            if kind == "detect":
                self._run_detect()
            elif kind == "remove":
                self._run_remove()
            elif kind == "all":
                self._run_all()
            elif kind == "pick":
                self._run_pick()
            else:
                self.job_done.emit(kind, 0, 0)
        except Exception as error:  # never kill the thread
            self.item_failed.emit("", str(error))
            self.job_done.emit(kind, 0, 0)

    def _run_pick(self) -> None:
        """Copy every target image through pick_core.pick_one."""

        targets = list(self._targets)
        total = len(targets)
        ok = 0
        done = 0
        self.progress.emit(0, total)
        pool = ThreadPoolExecutor(max_workers=JOB_WORKERS)
        futures = {}
        try:
            for target in targets:
                if self._stop():
                    break
                path = str(pick_core.entry_path(target))
                future = pool.submit(
                    pick_core.pick_one, path, self._output_dir
                )
                futures[future] = pick_core.entry_key(target)
            for future in as_completed(futures):
                key = futures[future]
                done += 1
                outcome, error = self._result(future)
                if outcome is not None and outcome.status == "ok":
                    ok += 1
                    self.item_picked.emit(key, outcome.target)
                else:
                    self.item_failed.emit(
                        key, self._message(outcome, error)
                    )
                self.progress.emit(done, total)
        finally:
            self._shutdown(pool)
        self.job_done.emit("pick", ok, total)

    def _run_all(self) -> None:
        """Copy the images of the visible list that are not copied yet."""

        entries = list(self._entries)
        total = len(entries)
        picked = self._detected(entries)
        work = []
        ok = 0
        for entry in entries:
            path = str(getattr(entry, "path", "") or "")
            key = pick_core.entry_key(entry)
            if key in picked or not path:
                continue
            work.append((key, path))
        done = total - len(work)
        self.progress.emit(done, total)
        pool = ThreadPoolExecutor(max_workers=JOB_WORKERS)
        futures = {}
        try:
            for key, path in work:
                if self._stop():
                    break
                future = pool.submit(
                    pick_core.pick_one, path, self._output_dir
                )
                futures[future] = key
            for future in as_completed(futures):
                key = futures[future]
                done += 1
                outcome, error = self._result(future)
                if outcome is not None and outcome.status == "ok":
                    ok += 1
                    self.item_picked.emit(key, outcome.target)
                else:
                    self.item_failed.emit(
                        key, self._message(outcome, error)
                    )
                self.progress.emit(done, total)
        finally:
            self._shutdown(pool)
        self.job_done.emit("all", ok, total)

    def _run_remove(self) -> None:
        """Move every copy of every target key into the trash folder."""

        targets = [_target_key(item) for item in self._targets]
        ok = 0
        seen = 0
        for key in targets:
            if self._stop():
                break
            stem = key[1]
            try:
                result = pick_core.unpick_stem(self._output_dir, stem)
            except Exception as error:
                self.item_failed.emit(key, str(error))
                continue
            moved = tuple(
                path for path in (
                    moved_path(item)
                    for item in (getattr(result, "moved", ()) or ())
                ) if path
            )
            failed = tuple(getattr(result, "failed", ()) or ())
            seen += len(moved) + len(failed)
            ok += len(moved)
            for path in moved:
                self.item_removed.emit(key, path)
            for _name, message in failed:
                self.item_failed.emit(key, str(message))
            if not moved and not failed:
                self.item_failed.emit(
                    key,
                    str(getattr(result, "message", "")
                        or getattr(result, "status", "")),
                )
        self.job_done.emit("remove", ok, seen)

    def _run_detect(self) -> None:
        """Report the (rel_dir, stem) keys already copied."""

        stems = self._detected(self._entries)
        self.detect_finished.emit(frozenset(stems))
        self.job_done.emit("detect", len(stems), len(self._entries))

    # ----------------------------------------------------------- helpers

    def _detected(self, entries: Sequence) -> frozenset:
        """Return the copied keys of a list, never raising."""

        if not entries:
            return frozenset()
        try:
            return frozenset(pick_core.detect_picked(
                entries, self._output_dir
            ))
        except Exception:
            return frozenset()

    @staticmethod
    def _result(future) -> Tuple[object, str]:
        """Return (outcome, error message) of one future."""

        try:
            return future.result(), ""
        except Exception as error:
            return None, str(error)

    @staticmethod
    def _message(outcome, error: str) -> str:
        """Return the message of a failed outcome."""

        if error:
            return error
        if outcome is None:
            return ""
        for name in ("message", "error", "status"):
            value = getattr(outcome, name, None)
            if value:
                return str(value)
        return ""

    @staticmethod
    def _shutdown(pool: ThreadPoolExecutor) -> None:
        """Release the pool, cancelling what did not start."""

        try:
            pool.shutdown(wait=True, cancel_futures=True)
        except TypeError:
            pool.shutdown(wait=True)
