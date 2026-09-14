"""Asynchronous scanning of the source dataset directory.

Selecting a source directory used to enumerate the whole dataset inside
the Qt slot that watches the path line edit: a large dataset froze the
window, and typing a path scanned once per keystroke. This module moves
that enumeration behind a worker thread and a restarting debounce, so
the window keeps painting while the directory is walked.

The scheduler owns exactly one scan thread. Requests arriving while that
thread is busy do not queue up behind the older ones: only the newest
request is kept, and it starts as soon as the thread is free. The token
travels through untouched - the scheduler never decides which answer is
stale, the caller compares the tokens and drops what it no longer wants.
"""

from __future__ import annotations

import os.path as osp
from typing import List, Optional, Tuple

from PyQt6 import QtCore

from . import dataset

SCAN_ERROR_TEMPLATE = "扫描数据目录失败：{}"


class DirectoryScanWorker(QtCore.QThread):
    """Count the image/label pairs of one directory in a worker thread."""

    scanned = QtCore.pyqtSignal(int, str, int)
    failed = QtCore.pyqtSignal(int, str, str)

    def __init__(self, directory: str, token: int, parent=None) -> None:
        super().__init__(parent)
        self.directory = directory
        self.token = int(token)

    def run(self) -> None:
        """Enumerate the directory; no exception may escape this method.

        run() executes inside a thread of its own: an exception that
        leaves it would abort the whole application instead of failing
        the single scan, so every failure - a permission error, a broken
        json, anything - is reported on the failed signal.
        """

        directory = self.directory
        token = self.token
        try:
            scan = dataset.collect_pairs(directory)
            count = len(scan.pairs)
        except Exception as error:  # noqa: BLE001
            message = SCAN_ERROR_TEMPLATE.format(error)
            self.failed.emit(token, directory, message)
            return
        self.scanned.emit(token, directory, int(count))


class DirectoryScanScheduler(QtCore.QObject):
    """Debounce directory scans behind a single worker thread.

    schedule() replaces whatever was waiting: a new request inside the
    debounce window restarts the timer, and a new request while the
    worker runs replaces the pending one. At most one scan thread exists
    at any moment, a directory that is not a folder is answered without
    starting one, and shutdown() makes the scheduler permanently mute.
    """

    scan_ready = QtCore.pyqtSignal(int, str, int)
    scan_failed = QtCore.pyqtSignal(int, str, str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._start_pending)
        self._pending: Optional[Tuple[str, int]] = None
        self._worker: Optional[DirectoryScanWorker] = None
        # workers that outlived shutdown(timeout_ms): they keep running
        # here, because destroying a live QThread aborts the process
        self._detached_workers: List[DirectoryScanWorker] = []
        self._shutdown = False

    # ------------------------------------------------------------ public
    def schedule(
        self, directory: str, token: int, delay_ms: int = 0
    ) -> None:
        """Ask for one scan of directory, debounced by delay_ms.

        A second call inside the debounce window drops the previous
        request instead of adding a scan behind it. A call while the
        worker is busy replaces the pending request the same way: the
        newest request always wins.
        """

        if self._shutdown:
            return
        self._pending = (directory, int(token))
        delay = int(delay_ms)
        if delay > 0:
            # a restarting single shot timer: every new request pushes
            # the deadline back, only the last one of a burst survives
            self._timer.start(delay)
            return
        self._timer.stop()
        self._start_pending()

    def shutdown(self, timeout_ms: int = 1000) -> None:
        """Stop the timer and the worker; emit nothing afterwards."""

        if self._shutdown:
            return
        self._shutdown = True
        self._timer.stop()
        self._pending = None
        worker = self._worker
        self._worker = None
        if worker is None:
            return
        worker.quit()
        if worker.wait(int(timeout_ms)):
            return
        # the worker is still walking a huge directory: hold it until it
        # finishes, its late signals are dropped by the flag above
        self._detached_workers.append(worker)
        worker.finished.connect(self._on_detached_finished)

    # ---------------------------------------------------------- internal
    def _start_pending(self) -> None:
        """Start the newest pending request, unless a worker is busy."""

        if self._shutdown or self._worker is not None:
            return
        request = self._pending
        self._pending = None
        if request is None:
            return
        directory, token = request
        if not directory or not osp.isdir(directory):
            # nothing to walk: answer at once, without a thread
            self.scan_ready.emit(token, directory, 0)
            return
        worker = DirectoryScanWorker(directory, token)
        worker.scanned.connect(self._on_worker_scanned)
        worker.failed.connect(self._on_worker_failed)
        worker.finished.connect(self._on_worker_finished)
        self._worker = worker
        worker.start()

    def _on_worker_scanned(
        self, token: int, directory: str, count: int
    ) -> None:
        """Relay a finished scan, token untouched."""

        if self._shutdown:
            return
        self.scan_ready.emit(int(token), directory, int(count))

    def _on_worker_failed(
        self, token: int, directory: str, message: str
    ) -> None:
        """Relay a failed scan, token untouched."""

        if self._shutdown:
            return
        self.scan_failed.emit(int(token), directory, message)

    def _on_worker_finished(self) -> None:
        """Reclaim the finished thread and start the newest pending."""

        worker = self._worker
        if worker is not None:
            worker.wait()
            self._worker = None
        if self._shutdown:
            return
        self._start_pending()

    def _on_detached_finished(self) -> None:
        """Release a worker that outlived shutdown(timeout_ms)."""

        worker = self.sender()
        if worker in self._detached_workers:
            self._detached_workers.remove(worker)
            worker.wait()


__all__ = ["DirectoryScanScheduler", "DirectoryScanWorker"]
