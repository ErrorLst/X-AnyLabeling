"""Scanning and image preloading of the preview tool.

Both helpers of this module run outside the main thread and never build
a widget:

* PreviewWorker reads one directory in a QThread. The images are listed
  on the thread itself, the json side cars are parsed by a small pool,
  and every point of an entry stays a plain tuple, so no QPointF is
  ever born outside the main thread.
* PreviewPreloader decodes QImage objects in a QThreadPool and keeps
  the most recent ones inside one pixel budget. Only the main thread
  turns an image into a QPixmap.

The scan is asynchronous, so a dialog stays responsive on a folder with
thousands of images, and the preloader hides the decode time of the two
neighbours of the image on screen.
"""

from __future__ import annotations

import os
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from PyQt6 import QtCore, QtGui

from . import core, pick_core

__all__ = [
    "CACHE_PIXEL_BUDGET",
    "DEFAULT_SCAN_WORKERS",
    "PRELOAD_AHEAD",
    "PRELOAD_BEHIND",
    "PreviewPreloader",
    "PreviewWorker",
    "SourceSnapshot",
    "image_extensions",
    "image_names",
]

#: How many images before and after the current one are decoded ahead.
PRELOAD_AHEAD = 2
PRELOAD_BEHIND = 2

#: Pixel budget of the QImage cache, in pixels.
CACHE_PIXEL_BUDGET = 64_000_000

#: Fallback number of threads of one directory scan.
DEFAULT_SCAN_WORKERS = 4

#: Number of threads the preloader decodes with.
PRELOAD_THREADS = 2


def image_extensions() -> Tuple[str, ...]:
    """Return the lower case extensions the preview reads as images.

    core owns the scan, so its own list wins when it publishes one; the
    list of the pick core is the fallback, both describe the same set of
    files.
    """

    extensions = getattr(core, "IMAGE_EXTS", None)
    if isinstance(extensions, (tuple, list, set, frozenset)):
        if extensions:
            return tuple(str(item).lower() for item in extensions)
    return tuple(str(item).lower() for item in pick_core.PICK_IMAGE_EXTS)


def image_names(directory: str) -> List[str]:
    """Return the image file names of one directory, naturally sorted.

    Only the top level of the directory is read. A missing, unreadable
    or unlistable directory yields an empty list instead of raising.
    """

    if not directory:
        return []
    try:
        extensions = image_extensions()
        names = []
        with os.scandir(directory) as entries:
            for entry in entries:
                try:
                    if not entry.is_file():
                        continue
                except OSError:
                    continue
                name = entry.name
                if os.path.splitext(name)[1].lower() in extensions:
                    names.append(name)
    except OSError:
        return []
    except Exception:
        return []
    try:
        names.sort(key=core.natural_key)
    except Exception:
        names.sort()
    return names


def read_entry(directory: str, name: str):
    """Return one core.ImageEntry for a file of a directory.

    core.localize reads and parses the json side car, here on a worker
    thread; the points stay tuples because shapes are built by core,
    which never knows about Qt. A broken side car only leaves the entry
    without shapes.
    """

    path = os.path.join(directory, name)
    json_path: Optional[str] = None
    shapes: Tuple = ()
    try:
        json_path, shapes = core.localize(directory, name)
        shapes = tuple(shapes or ())
    except Exception:
        json_path, shapes = None, ()
    labels = frozenset(str(shape.label) for shape in shapes)
    return core.ImageEntry(
        path=path,
        name=name,
        json_path=json_path,
        labels=labels,
        shapes=shapes,
    )


@dataclass
class SourceSnapshot:
    """The result of one directory scan, handed to the main thread."""

    directory: str = ""
    entries: List = field(default_factory=list)
    total: int = 0
    stopped: bool = False


class PreviewWorker(QtCore.QThread):
    """Scan one directory and emit its entries.

    Signals:
        progress(int, int) - (done, total) while the side cars are read.
        scanned(object) - one SourceSnapshot, always emitted exactly
            once per run, also when the scan was interrupted.
    """

    progress = QtCore.pyqtSignal(int, int)
    scanned = QtCore.pyqtSignal(object)

    def __init__(self, directory: str = "", parent=None):
        """Build an idle worker; set_directory decides what it reads."""

        super().__init__(parent)
        self._directory = str(directory or "")
        self._snapshot: Optional[SourceSnapshot] = None

    def set_directory(self, directory) -> None:
        """Set the directory of the next run."""

        self._directory = str(directory or "")

    @property
    def directory(self) -> str:
        """Return the directory of the next run."""

        return self._directory

    @property
    def snapshot(self) -> Optional[SourceSnapshot]:
        """Return the snapshot of the last run, or None."""

        return self._snapshot

    def _workers(self) -> int:
        """Return the number of threads of the side car pool."""

        try:
            return max(1, int(getattr(core, "SCAN_WORKERS",
                                      DEFAULT_SCAN_WORKERS)))
        except (TypeError, ValueError):
            return DEFAULT_SCAN_WORKERS

    def run(self) -> None:
        """Read the directory and emit one SourceSnapshot."""

        directory = self._directory
        names = image_names(directory)
        total = len(names)
        entries: List = []
        stopped = False
        self.progress.emit(0, total)
        pool = ThreadPoolExecutor(max_workers=self._workers())
        try:
            futures = {}
            for name in names:
                if self.isInterruptionRequested():
                    stopped = True
                    break
                futures[pool.submit(read_entry, directory, name)] = name
            done = 0
            for future in as_completed(futures):
                if self.isInterruptionRequested():
                    stopped = True
                    break
                try:
                    entry = future.result()
                except Exception:
                    entry = None
                done += 1
                if entry is not None:
                    entries.append(entry)
                self.progress.emit(done, total)
        finally:
            try:
                pool.shutdown(wait=not stopped, cancel_futures=stopped)
            except TypeError:
                pool.shutdown(wait=not stopped)
        if not stopped:
            try:
                entries.sort(key=lambda item: core.natural_key(item.name))
            except Exception:
                pass
        snapshot = SourceSnapshot(
            directory=directory,
            entries=entries,
            total=total,
            stopped=stopped,
        )
        self._snapshot = snapshot
        self.scanned.emit(snapshot)


class _DecodeBridge(QtCore.QObject):
    """Carries one decoded image from a pool thread to the main thread."""

    decoded = QtCore.pyqtSignal(str, int, object)


class _DecodeTask(QtCore.QRunnable):
    """Decode one image inside the thread pool of the preloader."""

    def __init__(self, path: str, token: int, bridge: _DecodeBridge):
        super().__init__()
        self._path = path
        self._token = token
        self._bridge = bridge

    def run(self) -> None:
        """Decode the image and hand it to the bridge."""

        image = None
        try:
            image = QtGui.QImage(self._path)
        except Exception:
            image = None
        try:
            self._bridge.decoded.emit(self._path, self._token, image)
        except RuntimeError:
            # The preloader is gone: drop the result silently.
            pass


class PreviewPreloader(QtCore.QObject):
    """Decode images ahead of time and cache them by pixel budget.

    Only the main thread calls get() and turns the result into a
    QPixmap; the pool threads only build QImage objects.
    """

    ready = QtCore.pyqtSignal(str)

    def __init__(self, parent=None, budget: Optional[int] = None,
                 threads: int = PRELOAD_THREADS):
        """Build an empty preloader.

        Args:
            parent: The owning QObject, usually the dialog.
            budget: Pixel budget of the cache. None reads the module
                constant CACHE_PIXEL_BUDGET at call time, so a test can
                lower it and be honoured.
            threads: Number of decoding threads.
        """

        super().__init__(parent)
        self._budget = budget
        self._cache: "OrderedDict[str, QtGui.QImage]" = OrderedDict()
        self._pixels = 0
        self._pending = set()
        self._paths: List[str] = []
        self._token = 0
        self._pool = QtCore.QThreadPool(self)
        try:
            self._pool.setMaxThreadCount(max(1, int(threads)))
        except (TypeError, ValueError):
            self._pool.setMaxThreadCount(PRELOAD_THREADS)
        self._bridge = _DecodeBridge()
        self._bridge.decoded.connect(self._on_decoded)

    # ------------------------------------------------------------ cache

    def budget(self) -> int:
        """Return the pixel budget of the cache."""

        if self._budget is None:
            return int(CACHE_PIXEL_BUDGET)
        try:
            return max(0, int(self._budget))
        except (TypeError, ValueError):
            return int(CACHE_PIXEL_BUDGET)

    def get(self, path: str) -> Optional[QtGui.QImage]:
        """Return the decoded image of a path, or None when not cached."""

        if not path:
            return None
        image = self._cache.get(path, None)
        if image is None:
            return None
        self._cache.move_to_end(path)
        return image

    def cached_paths(self) -> Tuple[str, ...]:
        """Return the cached paths, oldest first."""

        return tuple(self._cache.keys())

    def pixel_total(self) -> int:
        """Return the number of pixels currently held in the cache."""

        return int(self._pixels)

    def pending(self) -> Tuple[str, ...]:
        """Return the paths whose decode has not come back yet."""

        return tuple(sorted(self._pending))

    def wait_for_decodes(self, timeout_ms: int = 10000) -> bool:
        """Block until the pool has finished every queued decode.

        The results travel through the event loop, so a caller that
        waits for the whole generation also pumps events afterwards
        (see the tests of this module). This method never runs an event
        loop of its own, which keeps it safe to call from the main
        thread; it reports whether the pool became idle in time.
        """

        try:
            return bool(self._pool.waitForDone(int(timeout_ms)))
        except TypeError:
            self._pool.waitForDone()
            return True
        except Exception:
            return False

    def invalidate(self) -> None:
        """Drop the cache and forget every result still in flight."""

        self._token += 1
        self._pending.clear()
        self._cache.clear()
        self._paths = []
        self._pixels = 0
        try:
            self._pool.clear()
        except Exception:
            pass

    def clear(self) -> None:
        """Alias of invalidate, kept for readability."""

        self.invalidate()

    # --------------------------------------------------------- requests

    def request(self, index, entries) -> None:
        """Decode the images around a row, and that row itself.

        Args:
            index: Row of the image on screen inside entries.
            entries: Sequence of paths, or of objects carrying a path
                attribute (the filtered core.ImageEntry list).
        """

        paths = [self._path_of(entry) for entry in (entries or ())]
        self._paths = paths
        try:
            row = int(index)
        except (TypeError, ValueError):
            return
        if not paths:
            return
        wanted: List[int] = []
        first = max(0, row - PRELOAD_BEHIND)
        last = min(len(paths), row + PRELOAD_AHEAD + 1)
        for other in range(first, last):
            wanted.append(other)
        if 0 <= row < len(paths):
            wanted.append(row)
        for other in wanted:
            if 0 <= other < len(paths):
                self._submit(paths[other])

    @staticmethod
    def _path_of(entry) -> str:
        """Return the path of one entry of the request list."""

        if isinstance(entry, str):
            return entry
        path = getattr(entry, "path", None)
        if isinstance(path, str):
            return path
        return ""

    def _submit(self, path: str) -> None:
        """Queue the decode of one path when it is worth doing."""

        if not path or path in self._cache or path in self._pending:
            return
        self._pending.add(path)
        try:
            self._pool.start(_DecodeTask(path, self._token, self._bridge))
        except Exception:
            self._pending.discard(path)

    def _on_decoded(self, path: str, token: int, image) -> None:
        """Store one decoded image, on the main thread."""

        self._pending.discard(path)
        if token != self._token:
            return
        if image is None or image.isNull():
            return
        width = int(image.width())
        height = int(image.height())
        if width <= 0 or height <= 0:
            return
        old = self._cache.pop(path, None)
        if old is not None:
            self._pixels -= self._pixels_of(old)
        self._cache[path] = image
        self._pixels += width * height
        self._evict()
        self.ready.emit(path)

    def _evict(self) -> None:
        """Drop the oldest images until the budget is respected."""

        limit = self.budget()
        while self._pixels > limit and len(self._cache) > 1:
            _old_path, image = self._cache.popitem(last=False)
            self._pixels -= self._pixels_of(image)

    @staticmethod
    def _pixels_of(image) -> int:
        """Return the pixel count of one cached image."""

        try:
            return int(image.width()) * int(image.height())
        except Exception:
            return 0
