"""Application close guard of the remote training window (spec §5.1.4).

The dialog owns the only close state machine; the guard is what makes the
two vetoable entry points of the application - the top level window's
`QCloseEvent` and the `QEvent::Quit` posted on the `QApplication` - run
through it:

- veto (`event.ignore()` + `return True`, the only working pair for
  `QEvent::Quit`);
- release (`return False`) when there is no dialog at all, or when the
  dialog has no unfinished worker;
- with an unfinished worker: the same word for word confirmation of step
  0, then `_pending_quit = True` and `dialog.close()`, and finally the
  same single criterion (`all(w.isFinished() for w in dialog.workers)`).
  While it is false the guard keeps a 200 ms timer alive and, once it
  becomes true, has the vetoed top level window itself re-issue
  `close()` (`QApplication.quit()` only when there is no such window).

Three properties the guard has to keep, because both are easy to lose:

- **the dialog is looked up, never captured**: the guard is a process
  wide singleton, so it reads `owner._remote_training_dialog` on every
  request.  A captured lambda would keep pointing at the first dialog
  after `WA_DeleteOnClose` destroyed it, and the second window would be
  unprotected (`launcher._forget` is the single source of truth for
  "there is no dialog"); `install_close_guard` therefore refreshes the
  owner of an already installed guard;
- **the dialog's own close is not filtered**: its `close()` belongs to
  its own steps 0 to 6.  Vetoing it here would re-enter this filter,
  duplicate the confirmation and hide the step 4 wait;
- **a dead wrapper counts as "no dialog"**: `dialog()` probes the object
  once and falls back to the release branch with one WARNING per process
  instead of letting a `RuntimeError` escape an event filter.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, List, Optional

from PyQt6 import QtCore, QtWidgets

__all__ = [
    "ApplicationCloseGuard",
    "install_close_guard",
    "running_workers",
    "workers_finished",
]

_LOGGER = logging.getLogger(__name__)

#: Fixed poll interval of the guard and of the dialog's own step 4.
CLOSE_POLL_MS = 200

_NO_DIALOG_WARNING = (
    "the remote training window has no owning widget; the exit request "
    "is released without waiting (spec §5.1.4)"
)
_DEAD_DIALOG_WARNING = (
    "the remote training window is already destroyed; the exit request "
    "is released without waiting (spec §5.1.4)"
)


def _live_workers(dialog: Any) -> Optional[List[Any]]:
    """The workers of `dialog`; `None` when the wrapper is dead."""

    try:
        workers = list(getattr(dialog, "workers", None) or [])
    except RuntimeError:
        return None
    alive: List[Any] = []
    for worker in workers:
        is_finished = getattr(worker, "isFinished", None)
        try:
            done = bool(is_finished()) if callable(is_finished) else False
        except RuntimeError:  # pragma: no cover - deleted worker wrapper
            continue
        if not done:
            alive.append(worker)
    return alive


def running_workers(dialog: Any) -> List[Any]:
    """Return the dialog's workers that are still running.

    A `None` dialog and a destroyed wrapper both report no worker: the
    caller has nothing left to wait for.
    """

    if dialog is None:
        return []
    return _live_workers(dialog) or []


def workers_finished(dialog: Any) -> bool:
    """The single criterion of both close paths (spec §5.1.4 step 4).

    `all(...)` over an empty list is true; no "non empty set" special
    case is allowed.
    """

    if dialog is None:
        return True
    alive = _live_workers(dialog)
    if alive is None:
        return True
    return not alive


class ApplicationCloseGuard(QtCore.QObject):
    """Vetoes the application exit until every worker has finished."""

    def __init__(
        self,
        owner: Optional[Any] = None,
        *,
        dialog_getter: Optional[Callable[[], Any]] = None,
        confirm: Optional[Callable[[Any], bool]] = None,
        app: Optional[Any] = None,
        interval_ms: int = CLOSE_POLL_MS,
    ) -> None:
        # Deliberately unparented: the guard is installed on the
        # QApplication and has to outlive every window it protects.  A
        # window parent would delete the guard (and its timer) together
        # with the first owner, leaving a deleted filter installed on the
        # application - an event filter that raises aborts the process.
        super().__init__(None)
        self._owner = owner
        self._dialog_getter = dialog_getter
        self._confirm = confirm
        self._app = app
        self._warned = False
        self._dead_warned = False
        self._in_flight = False
        #: The top level object whose exit request was vetoed; the timer
        #: has that window re-issue close() (spec §5.1.4).
        self._vetoed: Any = None
        #: True while a vetoed exit request still has to be re-issued.
        #: It lives on the guard, not only on the dialog: the dialog may
        #: be destroyed by its own timer before this one fires, and the
        #: vetoed request must survive that (spec §5.1.4).
        self._pending_exit = False
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(int(interval_ms))
        self._timer.timeout.connect(self._on_timer)

    # ------------------------------------------------------------ helpers

    def rebind(
        self,
        owner: Optional[Any],
        dialog_getter: Optional[Callable[[], Any]] = None,
    ) -> "ApplicationCloseGuard":
        """Point the guard at the current dialog owner (spec §5.1.2).

        The guard outlives every dialog, so every new dialog refreshes the
        owner it reads the window from; without this the second window
        would be protected by a guard still looking at the first one.
        """

        self._owner = owner
        self._dialog_getter = dialog_getter
        self._warned = False
        return self

    def app(self) -> Any:
        """The QApplication the guard watches."""

        if self._app is not None:
            return self._app
        return QtWidgets.QApplication.instance()

    def _dialog_ref(self) -> Any:
        """The raw dialog reference of the owner (it may be destroyed)."""

        if self._dialog_getter is not None:
            try:
                return self._dialog_getter()
            except RuntimeError:  # pragma: no cover - defensive
                return None
        owner = self._owner
        if owner is None:
            return None
        try:
            return getattr(owner, "_remote_training_dialog", None)
        except RuntimeError:  # pragma: no cover - deleted owner wrapper
            return None

    def dialog(self) -> Any:
        """The dialog this guard protects, or None when there is none."""

        dialog = self._dialog_ref()
        if dialog is None:
            return None
        try:
            dialog.workers  # touch once: a deleted wrapper raises here
        except RuntimeError:
            if not self._dead_warned:
                self._dead_warned = True
                _LOGGER.warning(_DEAD_DIALOG_WARNING)
            return None
        return dialog

    def _guarded_window(self) -> Any:
        """The top level window that carries the dialog (spec §5.1.4).

        The owner is the widget the launcher was handed, and in the real
        application that is a **child widget** (the labeling widget of the
        main window), so the vetoable window is its top level ancestor:
        `QWidget.window()` returns the owner itself when it already is
        the window, and the main window otherwise.  The dialog is never
        its own guarded window - its close belongs to its own steps 0-6.
        """

        owner = self._owner
        if owner is None:
            return None
        try:
            window = owner.window() if hasattr(owner, "window") else owner
        except RuntimeError:  # pragma: no cover - deleted owner wrapper
            return None
        if window is None or window is self._dialog_ref():
            return None
        is_window = getattr(window, "isWindow", None)
        if callable(is_window):
            try:
                if not is_window():
                    return None
            except RuntimeError:  # pragma: no cover - deleted wrapper
                return None
        return window

    def vetoed_window(self) -> Any:
        """The top level window that has to re-issue its close request."""

        target = self._vetoed
        if target is None:
            return None
        try:
            if not target.isWindow():
                return None
        except RuntimeError:
            return None
        return target

    # -------------------------------------------------------------- hooks

    def eventFilter(self, obj: Any, event: Any) -> bool:  # noqa: N802
        """Veto / release one exit request (spec §5.1.4)."""

        etype = event.type()
        if etype not in (
            QtCore.QEvent.Type.Close,
            QtCore.QEvent.Type.Quit,
        ):
            return False
        if etype == QtCore.QEvent.Type.Close:
            # Only the top level window that carries the dialog is a
            # vetoable entry point: the dialog's own close runs its own
            # steps 0 to 6 (filtering it would recurse into this filter
            # from dialog.close()), and every other top level window of
            # the process - a message box, a file dialog, a native handle -
            # must stay closable.  The owner is compared through its top
            # level ancestor: the launcher is handed a child widget.
            if obj is not self._guarded_window():
                return False
        dialog = self.dialog()
        if dialog is None:
            if self._owner is not None and not self._warned:
                self._warned = True
                _LOGGER.warning(_NO_DIALOG_WARNING)
            return False
        if workers_finished(dialog):
            return False
        if self._in_flight:
            # A nested request while the confirmation is up: veto it, the
            # request already being handled will be re-issued.
            event.ignore()
            return True
        self._in_flight = True
        try:
            if not self._confirm_exit(dialog):
                event.ignore()
                return True
            # The object that has to re-issue the request once the workers
            # are done: the vetoed window, or (for a Quit request, whose
            # obj is the application) the window carrying the dialog.
            self._vetoed = obj if self._is_window(obj) else (
                self._guarded_window()
            )
            self._pending_exit = True
            dialog._pending_quit = True
            try:
                dialog.close()
            except Exception:  # pragma: no cover - defensive
                pass
            if workers_finished(dialog):
                dialog._pending_quit = False
                self._vetoed = None
                self._pending_exit = False
                return False
            if not self._timer.isActive():
                self._timer.start()
            event.ignore()
            return True
        finally:
            self._in_flight = False

    def _is_window(self, obj: Any) -> bool:
        is_window = getattr(obj, "isWindow", None)
        try:
            if callable(is_window) and is_window():
                return True
        except RuntimeError:  # pragma: no cover - deleted wrapper
            return False
        return False

    def _confirm_exit(self, dialog: Any) -> bool:
        """Ask the word for word confirmation of step 0."""

        if self._confirm is not None:
            return bool(self._confirm(dialog))
        confirm = getattr(dialog, "confirm_close", None)
        if callable(confirm):
            return bool(confirm())
        return True

    def _on_timer(self) -> None:
        """Re-evaluate the single criterion (spec §5.1.4)."""

        dialog = self.dialog()
        pending = self._pending_exit
        if dialog is not None:
            try:
                pending = pending and bool(dialog._pending_quit)
            except RuntimeError:  # pragma: no cover - defensive
                pending = self._pending_exit
        if not pending:
            self._timer.stop()
            self._vetoed = None
            self._pending_exit = False
            return
        if dialog is not None and not workers_finished(dialog):
            return
        # The criterion is true - or the window that owned the workers is
        # already gone, which implies its workers ended: either way the
        # vetoed exit request is re-issued now.
        self._timer.stop()
        self._pending_exit = False
        if dialog is not None:
            try:
                dialog._pending_quit = False
            except RuntimeError:  # pragma: no cover - defensive
                pass
        target = self.vetoed_window()
        self._vetoed = None
        app = self.app()
        if target is not None:
            # The window that was vetoed re-issues the request itself.
            QtCore.QTimer.singleShot(0, target.close)
        elif app is not None:
            # No top level window to close: the degenerate form.
            QtCore.QTimer.singleShot(0, app.quit)


def install_close_guard(
    owner: Any,
    *,
    dialog_getter: Optional[Callable[[], Any]] = None,
    confirm: Optional[Callable[[Any], bool]] = None,
    app: Optional[Any] = None,
) -> ApplicationCloseGuard:
    """Install the guard once for the whole process (spec §5.1.4).

    The dialog calls this from the end of its constructor; the owner is
    the dialog's parent (the main window), because the guard has to
    outlive the dialog it protects.  A second dialog rebinds the existing
    guard to itself instead of returning a stale one (spec §5.1.2).
    """

    application = app if app is not None else QtWidgets.QApplication.instance()
    existing = (
        getattr(application, "_remote_training_close_guard", None)
        if application is not None
        else None
    )
    if existing is not None:
        try:
            return existing.rebind(owner, dialog_getter)
        except RuntimeError:
            # The cached guard was destroyed with its application parent
            # (only possible if the application itself is going away);
            # fall through and build a fresh one.
            existing = None
    guard = ApplicationCloseGuard(
        owner,
        dialog_getter=dialog_getter,
        confirm=confirm,
        app=application,
    )
    if application is not None and isinstance(application, QtCore.QObject):
        # Owned by the application, so it outlives every window.
        guard.setParent(application)
        application.installEventFilter(guard)
        application._remote_training_close_guard = guard
    return guard
