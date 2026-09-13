"""Tests of the way the smudge tool owns the canvas while it is on.

The canvas is in its create mode from the moment the mode is entered,
and the upstream drawing actions are taken over rather than disabled:
a click on one of them, or its shortcut, leaves the mode first and
then the activation the user started runs the action, which puts the
canvas into the mode of that action. One user activation runs the
upstream handler of the action exactly once: the tool never triggers
the action a second time. Every other way out -- the button, the
escape key and the canvas switch of another mode -- brings the
editing mode back.
"""

from types import SimpleNamespace

import pytest
from PyQt6 import QtCore, QtGui, QtWidgets

from anylabeling.custom.smudge_tool import smudge_filter
from anylabeling.views.labeling.shape import Shape

from conftest import press, send_key

DRAW_NAMES = smudge_filter.DRAW_ACTION_NAMES


class _Action:
    """A stand in for one upstream toolbar action.

    ``trigger`` does what a real *enabled* ``QAction`` does: it emits
    ``triggered`` and then runs the handler the upstream widget
    connected to that signal, which is what switches the canvas.
    The handler is a plain attribute, not a connection, so the
    number of slots of ``triggered`` measures exactly one thing: the
    handover of the tool, and ``triggers`` counts the dialogs the
    user (and the tool) had with Qt. One user activation is one
    ``trigger`` call.
    """

    def __init__(self, shortcut="", handler=None):
        self.triggered = _Signal(self)
        self.calls = []
        self.triggers = 0
        self.handler = handler
        self._shortcut = QtGui.QKeySequence(shortcut)

    def setEnabled(self, value):
        """Record a call, so a test can prove there is none."""
        self.calls.append(bool(value))

    def isEnabled(self):
        """Return the state of the last call, enabled before any call."""
        if not self.calls:
            return True
        return self.calls[-1]

    def shortcut(self):
        """Return the keyboard shortcut of the action."""
        return self._shortcut

    def trigger(self):
        """Fire the action, like a real enabled ``QAction`` does.

        One call is one dialog of the caller with Qt: the counter is
        bumped here, and the handover of the tool may trigger the
        action once more, so the count is what the user asked for
        plus what the tool ran on their behalf.
        """
        self.triggered.emit()
        if self.handler is not None:
            self.handler()


class _Signal:
    """A minimal stand in for a Qt signal of one argument."""

    def __init__(self, owner=None):
        self.slots = []
        self._owner = owner

    def connect(self, slot):
        """Remember a slot, the way ``QObject.connect`` does."""
        self.slots.append(slot)

    def disconnect(self, slot):
        """Drop one exact connection, the way PyQt does."""
        self.slots.remove(slot)

    def emit(self, *args):
        """Call every slot, on a copy of the list."""
        if self._owner is not None:
            self._owner.triggers += 1
        for slot in list(self.slots):
            slot(*args)

    def receivers(self):
        """Return how many slots are connected."""
        return len(self.slots)


def _rectangle_actions(canvas):
    """Return the three actions a handover test needs, wired to canvas."""

    def to_rectangle():
        canvas.create_mode = "rectangle"
        canvas.set_editing(False)

    def to_edit():
        canvas.set_editing(True)

    return {
        "create_mode": _Action(),
        "create_rectangle_mode": _Action("R", handler=to_rectangle),
        "edit_mode": _Action("", handler=to_edit),
        "edit_brush_mode": _Action(),
    }


def _with_actions(widget, canvas):
    """Give the stand in widget the drawing actions, return the object."""
    fields = {name: _Action() for name in DRAW_NAMES}
    fields.update(_rectangle_actions(canvas))
    actions = SimpleNamespace(**fields)
    widget.actions = actions
    return actions


class _RealActions:
    """The drawing actions as real ``QAction`` objects.

    The menu tests already build real actions, because a popup is the
    one object of these tests that cannot be faked; these actions
    cover the other activation paths -- a programmatic ``trigger()``,
    the way a shortcut or a toolbar button runs an action -- with the
    same fidelity. ``handlers`` holds the slot upstream connects in the
    constructor of the widget, and ``runs`` counts how often that slot
    ran: exactly one run per user activation.

    The handlers are *not* connected here: the tests do it themselves
    with :meth:`connect_handlers`, and where that call sits picks the
    order of the slots. Before the mode is entered, the tool takes the
    actions over afterwards and the upstream handler runs first -- the
    order of the real window, whose constructor wires its handlers and
    calls ``install_smudge_tool`` at the end. After the entry, the
    handover of the tool is the first slot, the hardest order for the
    rule "one activation runs the action once". Both orders are
    pinned by the tests.
    """

    def __init__(self, canvas, handler=None):
        self.canvas = canvas
        self.handlers = {}
        self.runs = []
        for name in DRAW_NAMES:
            if name == "create_rectangle_mode":
                slot = self._rectangle
            elif name == "edit_mode":
                slot = self._edit
            elif name == "create_circle_mode":
                slot = self._circle
            else:
                slot = self._plain(name)
            self.handlers[name] = slot
            setattr(self, name, QtGui.QAction(name, None))
        self._handler = handler
        if handler is not None:
            self.create_rectangle_mode.triggered.connect(
                lambda checked=False: handler()
            )
        self._connected = False

    def connect_handlers(self):
        """Connect every upstream handler of the widget, once.

        Where the call sits decides who runs first: call it before the
        mode is entered and the handover of the tool is connected last
        (the upstream handler runs first, the order of the real
        window); call it after the entry and the handover is the first
        slot. Both are pinned by the tests. The call is idempotent, so
        a test may also leave it to the constructor path above.
        """
        if self._connected:
            return
        for name, slot in self.handlers.items():
            getattr(self, name).triggered.connect(slot)
        self._connected = True

    def _plain(self, name):
        def run(checked=False):
            self.runs.append(name)
        return run

    def _rectangle(self, checked=False):
        self.runs.append("create_rectangle_mode")
        self.canvas.create_mode = "rectangle"
        self.canvas.set_editing(False)

    def _circle(self, checked=False):
        self.runs.append("create_circle_mode")
        self.canvas.create_mode = "circle"
        self.canvas.set_editing(False)

    def _edit(self, checked=False):
        self.runs.append("edit_mode")
        self.canvas.set_editing(True)


def _real_tool(st_tool, handler=None):
    """Give the widget real drawing actions, the way upstream builds them.

    The controller of the fixture is already built, so ``_with_actions``
    cannot be used here: the actions are installed on the widget and
    the mode is entered, which is what takes them over. The caller
    connects the handlers of the widget afterwards, with
    :meth:`_RealActions.connect_handlers`, once the handover of the
    tool is in place.
    """
    actions = _RealActions(st_tool.widget.canvas, handler)
    st_tool.widget.actions = actions
    return actions


def _real_edit_mode(widget, canvas):
    """Give the widget a real ``set_edit_mode`` and a real edit action.

    What the upstream method does is what matters for the handover:
    the canvas goes back to its editing mode through the widget, once
    per exit.
    """
    calls = []

    def set_edit_mode():
        calls.append(canvas.editing())
        canvas.set_editing(True)

    widget.set_edit_mode = set_edit_mode
    return calls


def _menu_actions(widget, handler):
    """Return the drawing actions of a menu test, one per taken name.

    A menu holds real QActions, so these are real QActions as well,
    exactly like upstream: the menu is the one object of these tests
    that cannot be faked. ``handler`` runs after the triggered signal
    of each of them, the way the handler upstream connected to the
    action does. What the tool sees -- the objects of the actions
    object, reached through ``getattr`` -- is the same either way,
    and ``widget.actions`` points at them: the menu is built from
    that object.
    """
    fields = {}
    for name in DRAW_NAMES:
        action = QtGui.QAction(name, None)
        action.triggered.connect(
            lambda checked=False, run=handler: run()
        )
        fields[name] = action
    fields["create_rectangle_mode"] = _menu_rectangle(
        widget.canvas, handler
    )
    fields["edit_mode"] = _menu_edit_mode(widget.canvas)
    actions = SimpleNamespace(**fields)
    widget.actions = actions
    return actions


def _menu_rectangle(canvas, handler):
    """Return the rectangle action of a menu test, wired to canvas."""

    def to_rectangle():
        handler()
        canvas.create_mode = "rectangle"
        canvas.set_editing(False)

    action = QtGui.QAction("Create Rectangle", None)
    action.triggered.connect(lambda checked=False: to_rectangle())
    return action


def _menu_edit_mode(canvas):
    """Return the editing action of a menu test, wired to canvas."""
    action = QtGui.QAction("Edit Object", None)
    action.triggered.connect(
        lambda checked=False: canvas.set_editing(True)
    )
    return action


def _menu_entry(actions):
    """Return a real QMenu holding the drawing actions it is given.

    The entries are the very QActions of ``actions``, the way the Edit
    menu of the labeling widget holds its own actions: the rectangle
    action, whose click the tests aim at, and the editing one.
    """
    menu = QtWidgets.QMenu("Edit")
    menu.addAction(actions.create_rectangle_mode)
    menu.addAction(actions.edit_mode)
    return menu, actions.create_rectangle_mode


def _plain_menu(*labels):
    """Return a real QMenu holding entries that stand for no action."""
    menu = QtWidgets.QMenu("Edit")
    for label in labels:
        menu.addAction(QtGui.QAction(label, menu))
    return menu


def _click(qapp, menu, qaction):
    """Show a menu and click one entry of it, the way a user does."""
    menu.popup(QtCore.QPoint(20, 20))
    qapp.processEvents()
    point = menu.actionGeometry(qaction).center()
    for kind, buttons in (
        (
            QtCore.QEvent.Type.MouseButtonPress,
            QtCore.Qt.MouseButton.LeftButton,
        ),
        (
            QtCore.QEvent.Type.MouseButtonRelease,
            QtCore.Qt.MouseButton.NoButton,
        ),
    ):
        event = QtGui.QMouseEvent(
            kind,
            QtCore.QPointF(point),
            QtCore.QPointF(point),
            QtCore.Qt.MouseButton.LeftButton,
            buttons,
            QtCore.Qt.KeyboardModifier.NoModifier,
        )
        QtWidgets.QApplication.sendEvent(menu, event)
    qapp.processEvents()
    return event


def _override_shape():
    """Return the shape of the application override cursor, or None."""
    cursor = QtWidgets.QApplication.overrideCursor()
    return None if cursor is None else cursor.shape()


@pytest.fixture(autouse=True)
def _clean_override_cursors():
    """Drop the cursor a test leaves when it stays in the mode."""
    yield
    while QtWidgets.QApplication.overrideCursor() is not None:
        QtWidgets.QApplication.restoreOverrideCursor()


def test_the_entry_takes_the_canvas_to_its_drawing_mode(st_tool):
    controller = st_tool.controller
    canvas = st_tool.widget.canvas
    assert canvas.editing() is True
    controller._action.trigger()
    assert controller._is_active() is True
    assert controller._action.isChecked() is True
    assert controller._mode_switched is True
    assert canvas.drawing() is True
    assert canvas.editing() is False


def test_the_entry_takes_over_every_action_without_disabling_it(st_tool):
    widget = st_tool.widget
    actions = _with_actions(widget, widget.canvas)
    controller = st_tool.controller
    controller._action.trigger()
    # Disabling an action would take its shortcut down with it, so a
    # drawing action has to stay enabled while the mode is on.
    for name in DRAW_NAMES:
        assert getattr(actions, name).calls == []
        assert getattr(actions, name).isEnabled() is True
    # Every one of the thirteen carries the handover of the tool.
    handover = [
        getattr(actions, name) for name in DRAW_NAMES
        if getattr(actions, name).triggered.receivers() == 1
    ]
    assert len(handover) == len(DRAW_NAMES)
    controller.set_mode(False)


def test_the_button_gives_the_editing_mode_back(st_tool):
    widget = st_tool.widget
    canvas = widget.canvas
    actions = _with_actions(widget, canvas)
    controller = st_tool.controller
    controller._action.trigger()
    assert canvas.drawing() is True
    controller._action.trigger()
    assert controller._is_active() is False
    assert controller._action.isChecked() is False
    assert controller._mode_switched is False
    assert canvas.editing() is True
    assert _override_shape() is None
    assert controller._cursor_overridden is False
    assert actions.edit_mode.triggers == 1
    # The handover is gone with the mode, so nothing of the tool runs
    # any more when an upstream action fires.
    assert actions.create_rectangle_mode.triggered.receivers() == 0


def test_escape_gives_the_editing_mode_back(st_tool):
    controller = st_tool.controller
    canvas = st_tool.widget.canvas
    controller._action.trigger()
    event = send_key(canvas, QtCore.Qt.Key.Key_Escape)
    assert event.isAccepted() is True
    assert controller._is_active() is False
    assert controller._mode_switched is False
    assert canvas.editing() is True
    assert _override_shape() is None


def test_a_canvas_switch_hands_the_editing_mode_over_first(st_tool):
    controller = st_tool.controller
    canvas = st_tool.widget.canvas
    seen = []
    real = controller.set_mode

    def spy(enabled, use_action=True):
        result = real(enabled, use_action)
        if not enabled:
            seen.append((use_action, canvas.editing()))
        return result

    controller.set_mode = spy
    controller._action.trigger()
    canvas.set_editing(False)
    # The tool put the canvas back in its editing mode before the mode
    # it makes room for runs: the state the upstream switch finds is
    # the safe one. The switch itself still ends in create mode.
    # The switch is the upstream action of the user running its own
    # handler, so the tool hands the canvas back without running the
    # editing action again (use_action is False): one gesture, one
    # side effect.
    assert seen == [(False, True)]
    assert canvas.drawing() is True
    assert controller._is_active() is False
    assert controller._action.isChecked() is False
    assert controller._mode_switched is False


def test_a_canvas_switch_does_not_run_the_editing_action(st_tool):
    widget = st_tool.widget
    canvas = widget.canvas
    actions = _real_tool(st_tool)
    calls = _real_edit_mode(widget, canvas)
    controller = st_tool.controller
    controller._action.trigger()
    actions.connect_handlers()
    # A switch the upstream path makes itself -- a digit shortcut, the
    # brush polygon or the magic wand: toggle_draw_mode calls
    # Canvas.set_editing(False) -- reaches the wrapper of the tool with
    # the target of the new mode. The tool hands the canvas back
    # without running the upstream editing action: the bookkeeping of
    # that switch (button states, text editing, auto labeling marks)
    # belongs to the path that started it, and running the action here
    # would give one gesture two side effects.
    canvas.set_editing(False)
    assert actions.runs == []
    assert calls == []
    assert controller._is_active() is False
    assert controller._action.isChecked() is False
    assert controller._mode_switched is False
    assert canvas.drawing() is True
    assert canvas.editing() is False


def test_the_key_of_a_drawing_action_leaves_the_mode_and_runs_it(st_tool):
    widget = st_tool.widget
    canvas = widget.canvas
    actions = _with_actions(widget, canvas)
    controller = st_tool.controller
    controller._action.trigger()
    assert controller._is_active() is True
    # Qt asks the canvas owner first with a ShortcutOverride. The tool
    # leaves the combination to the shortcut system -- the drawing
    # actions keep their shortcuts -- and the system then fires the
    # action, which the handover catches.
    override = QtGui.QKeyEvent(
        QtCore.QEvent.Type.ShortcutOverride,
        QtCore.Qt.Key.Key_R,
        QtCore.Qt.KeyboardModifier.NoModifier,
    )
    QtWidgets.QApplication.sendEvent(canvas, override)
    assert override.isAccepted() is False
    actions.create_rectangle_mode.trigger()
    assert controller._is_active() is False
    assert controller._action.isChecked() is False
    # One dialog with Qt: the one the user had. The tool leaves the
    # mode and lets that very activation run the action; it never
    # triggers the action a second time.
    assert actions.create_rectangle_mode.triggers == 1
    assert canvas.create_mode == "rectangle"
    assert canvas.drawing() is True
    assert canvas.editing() is False


def test_the_toolbar_button_of_a_drawing_action_is_taken_over_too(st_tool):
    widget = st_tool.widget
    canvas = widget.canvas
    actions = _with_actions(widget, canvas)
    controller = st_tool.controller
    controller._action.trigger()
    actions.create_rectangle_mode.trigger()
    assert controller._is_active() is False
    assert controller._mode_switched is False
    # A toolbar click is one dialog with Qt as well, so the handler
    # of the action runs exactly once.
    assert actions.create_rectangle_mode.triggers == 1
    assert canvas.create_mode == "rectangle"
    assert canvas.drawing() is True
    assert canvas.editing() is False


def test_the_handover_does_not_loop_and_leaves_the_mode_once(st_tool):
    widget = st_tool.widget
    canvas = widget.canvas
    actions = _with_actions(widget, canvas)
    controller = st_tool.controller
    exits = []
    real = controller.set_mode

    def spy(enabled, use_action=True):
        if not enabled:
            exits.append(use_action)
        return real(enabled, use_action)

    controller.set_mode = spy
    controller._action.trigger()
    assert exits == []
    actions.create_rectangle_mode.trigger()
    # One exit for the whole handover, and that exit is the handover
    # kind: the activation the user started runs the action, so the
    # tool must not run the editing action on their behalf. The action
    # is triggered once, so its handler runs once.
    assert exits == [False]
    assert actions.create_rectangle_mode.triggers == 1
    actions.create_rectangle_mode.trigger()
    assert actions.create_rectangle_mode.triggers == 2
    assert exits == [False]
    assert controller._is_active() is False
    assert canvas.create_mode == "rectangle"


def test_a_widget_without_actions_enters_and_leaves_quietly(st_tool):
    controller = st_tool.controller
    widget = st_tool.widget
    assert getattr(widget, "actions", None) is None
    controller._action.trigger()
    assert controller._is_active() is True
    assert controller._draw_connections == {}
    controller._action.trigger()
    assert controller._is_active() is False
    assert controller._draw_connections == {}


def test_a_second_entry_connects_the_handover_exactly_once(st_tool):
    widget = st_tool.widget
    canvas = widget.canvas
    actions = _with_actions(widget, canvas)
    controller = st_tool.controller
    action = actions.create_rectangle_mode
    controller._action.trigger()
    assert action.triggered.receivers() == 1
    controller._action.trigger()
    assert action.triggered.receivers() == 0
    controller._action.trigger()
    assert action.triggered.receivers() == 1
    # Leaving the mode a second time drops that one connection again.
    controller.set_mode(False)
    assert action.triggered.receivers() == 0
    # With the handover gone, the action is the upstream one again:
    # it fires -- the counter shows it -- and the tool stays off.
    canvas.set_editing(True)
    before = action.triggers
    action.trigger()
    assert action.triggers == before + 1
    assert controller._is_active() is False
    assert controller._mode_switched is False


def test_a_real_action_runs_its_upstream_handler_exactly_once(st_tool):
    widget = st_tool.widget
    canvas = widget.canvas
    runs = []
    actions = _real_tool(st_tool, lambda: runs.append(1))
    controller = st_tool.controller
    controller._action.trigger()
    assert controller._is_active() is True
    assert canvas.drawing() is True
    actions.connect_handlers()
    actions.create_rectangle_mode.trigger()
    # One activation, one run: the handover slot of the tool runs
    # first, leaves the mode, and lets the upstream handler that is
    # already queued run the action. The tool must not trigger it a
    # second time.
    assert runs == [1]
    assert actions.runs == ["create_rectangle_mode"]
    assert controller._is_active() is False
    assert controller._action.isChecked() is False
    assert canvas.create_mode == "rectangle"
    assert canvas.drawing() is True
    assert canvas.editing() is False


def test_a_real_action_runs_once_in_the_order_of_the_widget(st_tool):
    widget = st_tool.widget
    canvas = widget.canvas
    runs = []
    actions = _real_tool(st_tool, lambda: runs.append(1))
    # The real widget wires its own handler in the constructor and the
    # tool is installed after the whole window was built, so the
    # handover of the tool is the *last* slot of triggered and the
    # upstream handler runs first. That order is the one the window
    # really has, so it is pinned here as well.
    actions.connect_handlers()
    controller = st_tool.controller
    controller._action.trigger()
    actions.create_rectangle_mode.trigger()
    assert runs == [1]
    assert actions.runs == ["create_rectangle_mode"]
    assert controller._is_active() is False
    assert canvas.create_mode == "rectangle"
    assert canvas.drawing() is True
    assert canvas.editing() is False


def test_the_edit_action_runs_once_in_the_order_of_the_widget(st_tool):
    widget = st_tool.widget
    canvas = widget.canvas
    actions = _real_tool(st_tool)
    actions.connect_handlers()
    controller = st_tool.controller
    controller._action.trigger()
    actions.edit_mode.trigger()
    # One activation of the editing action of the user runs its own
    # handler once. The canvas switch it makes reaches the tool, which
    # hands the canvas back without running the editing action again:
    # the handler count stays one instead of two.
    assert actions.runs == ["edit_mode"]
    assert controller._is_active() is False
    assert controller._mode_switched is False
    assert canvas.editing() is True
    assert canvas.drawing() is False


def test_a_real_action_runs_once_for_a_shortcut_of_another_mode(st_tool):
    widget = st_tool.widget
    canvas = widget.canvas
    actions = _real_tool(st_tool)
    controller = st_tool.controller
    controller._action.trigger()
    actions.connect_handlers()
    # The circle action is a second drawing action: the handover has
    # to leave the mode for it as well, and its upstream handler has
    # to run once -- no matter how many actions of the widget were
    # taken over.
    actions.create_circle_mode.trigger()
    assert actions.runs == ["create_circle_mode"]
    assert controller._is_active() is False
    assert canvas.create_mode == "circle"
    assert canvas.drawing() is True
    assert canvas.editing() is False


def test_the_edit_action_runs_its_upstream_handler_exactly_once(st_tool):
    widget = st_tool.widget
    canvas = widget.canvas
    actions = _real_tool(st_tool)
    calls = _real_edit_mode(widget, canvas)
    controller = st_tool.controller
    controller._action.trigger()
    assert canvas.drawing() is True
    actions.connect_handlers()
    actions.edit_mode.trigger()
    # One activation, one run of the upstream handler. The handover
    # puts the canvas back in its editing mode by hand and lets the
    # activation that is under way put it where the user asked: the
    # editing method of the widget is not run at all, or the widget
    # would fire the editing action of the user a second time.
    assert actions.runs == ["edit_mode"]
    assert calls == []
    assert controller._is_active() is False
    assert controller._action.isChecked() is False
    assert controller._mode_switched is False
    assert canvas.editing() is True
    assert canvas.drawing() is False


def test_the_edit_action_of_a_menu_runs_exactly_once(st_tool, qapp):
    widget = st_tool.widget
    canvas = widget.canvas
    actions = _real_tool(st_tool)
    menu = QtWidgets.QMenu("Edit")
    menu.addAction(actions.create_rectangle_mode)
    menu.addAction(actions.edit_mode)
    widget.menus = SimpleNamespace(edit=menu)
    controller = st_tool.controller
    controller._action.trigger()
    actions.connect_handlers()
    _click(qapp, menu, actions.edit_mode)
    # The entry of a popup is run by Qt, once: the filter of the tool
    # leaves the mode and lets the entry run, it does not run the
    # editing action itself.
    assert actions.runs == ["edit_mode"]
    assert controller._is_active() is False
    assert canvas.editing() is True
    assert canvas.drawing() is False


def test_a_real_menu_click_runs_the_handler_exactly_once(st_tool, qapp):
    widget = st_tool.widget
    canvas = widget.canvas
    runs = []
    actions = _real_tool(st_tool, lambda: runs.append(1))
    menu = QtWidgets.QMenu("Edit")
    menu.addAction(actions.create_rectangle_mode)
    menu.addAction(actions.edit_mode)
    widget.menus = SimpleNamespace(edit=menu)
    controller = st_tool.controller
    controller._action.trigger()
    actions.connect_handlers()
    _click(qapp, menu, actions.create_rectangle_mode)
    assert runs == [1]
    assert actions.runs == ["create_rectangle_mode"]
    assert controller._is_active() is False
    assert canvas.create_mode == "rectangle"
    assert canvas.drawing() is True


def test_leaving_with_the_button_fires_the_edit_action_once(st_tool):
    widget = st_tool.widget
    canvas = widget.canvas
    actions = _real_tool(st_tool)
    controller = st_tool.controller
    controller._action.trigger()
    actions.connect_handlers()
    # Two slots on the real QAction while the mode owns the canvas: the
    # handover of the tool and the upstream handler of the widget.
    assert actions.create_rectangle_mode.receivers(
        actions.create_rectangle_mode.triggered
    ) == 2
    controller._action.trigger()
    # The button is not a drawing action: the tool takes the editing
    # action back, once, and its upstream handler runs once.
    assert actions.runs == ["edit_mode"]
    # And the slot of the tool is really gone from the real QAction:
    # only the upstream handler is left, so the action cannot reach the
    # mode any more.
    assert actions.create_rectangle_mode.receivers(
        actions.create_rectangle_mode.triggered
    ) == 1
    assert controller._is_active() is False
    assert canvas.editing() is True


def test_a_widget_without_edit_mode_falls_back_to_set_edit_mode(st_tool):
    widget = st_tool.widget
    canvas = widget.canvas
    calls = []
    widget.actions = SimpleNamespace()
    widget.set_edit_mode = lambda: calls.append(canvas.editing())
    controller = st_tool.controller
    controller._action.trigger()
    assert canvas.drawing() is True
    controller._action.trigger()
    # The second level of the fallback reaches the canvas through the
    # widget, and it runs while the canvas is still drawing.
    assert calls == [False]
    assert canvas.editing() is True
    assert controller._mode_switched is False


def test_a_disabled_editing_action_is_not_taken_as_fired(st_tool):
    widget = st_tool.widget
    canvas = widget.canvas
    action = _Action()
    action.setEnabled(False)
    widget.actions = SimpleNamespace(edit_mode=action)
    calls = []
    widget.set_edit_mode = lambda: calls.append(canvas.editing())
    controller = st_tool.controller
    controller._action.trigger()
    assert canvas.drawing() is True
    controller._action.trigger()
    # Upstream disables the editing action while the canvas edits, and
    # triggering a disabled QAction is a silent no-op: the tool must
    # not count it as the way back and has to reach the widget method,
    # which is what really takes the canvas to editing.
    assert action.triggers == 0
    assert calls == [False]
    assert canvas.editing() is True
    assert controller._mode_switched is False


def test_a_canvas_that_stayed_drawing_is_switched_by_hand(st_tool):
    widget = st_tool.widget
    canvas = widget.canvas
    widget.actions = SimpleNamespace()
    controller = st_tool.controller
    controller._action.trigger()
    # The canvas came back to editing through the last level: the
    # third one is the hand switch, canvas.set_editing(True).
    controller._action.trigger()
    assert canvas.editing() is True
    assert controller._mode_switched is False
    assert controller._is_active() is False


def test_ctrl_z_still_wins_over_a_matching_action(st_tool):
    widget = st_tool.widget
    actions = _with_actions(widget, widget.canvas)
    actions.create_mode = _Action("Ctrl+Z")
    controller = st_tool.controller
    canvas = widget.canvas
    controller._action.trigger()
    event = send_key(
        canvas,
        QtCore.Qt.Key.Key_Z,
        QtCore.Qt.KeyboardModifier.ControlModifier,
    )
    assert event.isAccepted() is True
    assert controller._is_active() is True


def test_escape_still_leaves_over_a_matching_action(st_tool):
    widget = st_tool.widget
    controller = st_tool.controller
    canvas = widget.canvas
    controller._action.trigger()
    event = send_key(canvas, QtCore.Qt.Key.Key_Escape)
    assert event.isAccepted() is True
    assert controller._is_active() is False
    assert canvas.editing() is True


def test_another_key_is_left_to_the_canvas(st_tool):
    widget = st_tool.widget
    _with_actions(widget, widget.canvas)
    controller = st_tool.controller
    canvas = widget.canvas
    controller._action.trigger()
    event = QtGui.QKeyEvent(
        QtCore.QEvent.Type.KeyPress,
        QtCore.Qt.Key.Key_Q,
        QtCore.Qt.KeyboardModifier.NoModifier,
    )
    assert controller.eventFilter(canvas, event) is False
    assert controller._is_active() is True
    controller.set_mode(False)


def test_a_left_press_outside_the_image_starts_no_shape(st_tool):
    widget = st_tool.widget
    controller = st_tool.controller
    canvas = widget.canvas
    controller._action.trigger()
    canvas.create_mode = "rectangle"
    event = press(canvas, (150.0, 150.0))
    # The canvas is in the create mode the tool asked for: an event
    # left to it would start a rectangle from outside the image.
    assert event.isAccepted() is True
    assert canvas.current is None
    assert canvas.shapes == []
    assert controller._drag_start is None
    assert controller._is_active() is True
    assert canvas.drawing() is True
    assert widget.messages[-1] == "请从图像内部开始框选"


def test_a_double_click_is_swallowed_while_the_mode_is_on(st_tool):
    widget = st_tool.widget
    controller = st_tool.controller
    canvas = widget.canvas
    controller._action.trigger()
    canvas.create_mode = "rectangle"
    # A shape is under way, the way the create mode leaves it while the
    # user draws; closing it is what upstream does on a double click.
    shape = Shape(shape_type="polygon")
    shape.add_point(QtCore.QPointF(10.0, 10.0))
    shape.add_point(QtCore.QPointF(30.0, 10.0))
    shape.add_point(QtCore.QPointF(30.0, 30.0))
    shape.add_point(QtCore.QPointF(10.0, 30.0))
    canvas.current = shape
    canvas.shapes = []
    point = QtCore.QPointF(20.0, 20.0)
    event = QtGui.QMouseEvent(
        QtCore.QEvent.Type.MouseButtonDblClick,
        point,
        point,
        QtCore.Qt.MouseButton.LeftButton,
        QtCore.Qt.MouseButton.LeftButton,
        QtCore.Qt.KeyboardModifier.NoModifier,
    )
    QtWidgets.QApplication.sendEvent(canvas, event)
    assert event.isAccepted() is True
    assert canvas.current is shape
    assert canvas.shapes == []
    assert shape.is_closed() is False


def test_a_menu_entry_is_taken_over_and_runs_exactly_once(st_tool, qapp):
    widget = st_tool.widget
    canvas = widget.canvas
    runs = []
    actions = _menu_actions(widget, lambda: runs.append(1))
    menu, qaction = _menu_entry(actions)
    widget.menus = SimpleNamespace(edit=menu)
    controller = st_tool.controller
    controller._action.trigger()
    event = _click(qapp, menu, qaction)
    # Qt runs the entry of a popup from the release, and that is the
    # very event the tool steps in on: the popup closes, the mode is
    # left, and the entry runs afterwards -- once. Running the action
    # here as well would show up as a second run.
    assert event.isAccepted() is True
    assert menu.isVisible() is False
    assert controller._is_active() is False
    assert controller._action.isChecked() is False
    assert canvas.editing() is False
    assert canvas.drawing() is True
    assert canvas.create_mode == "rectangle"
    assert runs == [1]


def test_a_menu_entry_leaves_the_mode_before_the_action_runs(st_tool, qapp):
    widget = st_tool.widget
    canvas = widget.canvas
    seen = []
    actions = _menu_actions(
        widget, lambda: seen.append(controller._is_active())
    )
    menu, qaction = _menu_entry(actions)
    widget.menus = SimpleNamespace(edit=menu)
    controller = st_tool.controller
    controller._action.trigger()
    assert controller._menu_filters == {menu: True}
    _click(qapp, menu, qaction)
    assert controller._is_active() is False
    # The entry ran once, on a canvas the tool had already handed
    # back: the mode was off when the action behind it ran.
    assert seen == [False]


def test_a_menu_handover_does_not_wedge_the_cursor_of_later_exits(
    st_tool, qapp
):
    widget = st_tool.widget
    actions = _menu_actions(widget, lambda: None)
    menu, qaction = _menu_entry(actions)
    widget.menus = SimpleNamespace(edit=menu)
    controller = st_tool.controller
    controller._action.trigger()
    _click(qapp, menu, qaction)
    # The entry of the menu is a handover: the cursor of the mode is
    # left to the drawing mode the entry switches to. That flag
    # describes that one exit alone, so the next entry and the button
    # exit after it give the cursor back like any other exit.
    assert controller._is_active() is False
    controller._action.trigger()
    assert controller._is_active() is True
    controller._action.trigger()
    assert controller._is_active() is False
    assert controller._cursor_overridden is False
    assert _override_shape() is None


def test_an_entry_that_is_not_a_drawing_action_is_left_alone(st_tool, qapp):
    widget = st_tool.widget
    canvas = widget.canvas
    _menu_actions(widget, lambda: None)
    menu = _plain_menu("Duplicate", "Delete")
    widget.menus = SimpleNamespace(edit=menu)
    controller = st_tool.controller
    controller._action.trigger()
    # A menu without a drawing action is not taken over at all, so a
    # release inside it is left to the menu and the mode stays on.
    release = QtGui.QMouseEvent(
        QtCore.QEvent.Type.MouseButtonRelease,
        QtCore.QPointF(0.0, 0.0),
        QtCore.QPointF(0.0, 0.0),
        QtCore.Qt.MouseButton.LeftButton,
        QtCore.Qt.MouseButton.NoButton,
        QtCore.Qt.KeyboardModifier.NoModifier,
    )
    assert controller.eventFilter(menu, release) is False
    assert controller._is_active() is True
    assert canvas.editing() is False
    controller.set_mode(False)


def test_a_plain_entry_of_a_filtered_menu_is_left_to_the_menu(
    st_tool, qapp
):
    widget = st_tool.widget
    canvas = widget.canvas
    runs = []
    actions = _menu_actions(widget, lambda: runs.append(1))
    clicked = []
    menu = QtWidgets.QMenu("Edit")
    menu.addAction(actions.create_rectangle_mode)
    plain = QtGui.QAction("Duplicate", menu)
    plain.triggered.connect(lambda checked=False: clicked.append(1))
    menu.addAction(plain)
    widget.menus = SimpleNamespace(edit=menu)
    controller = st_tool.controller
    controller._action.trigger()
    # The menu holds a drawing action, so the filter of the tool is on
    # it: the plain entry is the one that filter has to leave alone.
    assert controller._menu_filters == {menu: True}
    _click(qapp, menu, plain)
    # The plain entry ran, once, and nothing else moved: only the
    # drawing entries of a filtered menu leave the mode, not every
    # entry of it.
    assert clicked == [1]
    assert runs == []
    assert controller._is_active() is True
    assert controller._action.isChecked() is True
    assert canvas.editing() is False
    assert canvas.drawing() is True
    controller.set_mode(False)


def test_the_menu_filter_is_installed_once(st_tool, qapp):
    widget = st_tool.widget
    canvas = widget.canvas
    actions = _menu_actions(widget, lambda: None)
    menu, qaction = _menu_entry(actions)
    widget.menus = SimpleNamespace(edit=menu)
    controller = st_tool.controller
    controller._action.trigger()
    controller._action.trigger()
    # A second entry must not install a second filter on the same menu:
    # every entry into the mode runs the install step again.
    installed = []
    real_install = menu.installEventFilter
    menu.installEventFilter = installed.append
    controller._action.trigger()
    menu.installEventFilter = real_install
    assert installed == []
    assert controller._is_active() is True
    event = _click(qapp, menu, qaction)
    assert event.isAccepted() is True
    assert controller._is_active() is False
    assert canvas.drawing() is True
    assert canvas.create_mode == "rectangle"
    controller.set_mode(False)


def test_a_menu_rebuilt_while_the_mode_is_on_is_taken_over_too(
    st_tool, qapp
):
    widget = st_tool.widget
    canvas = widget.canvas
    controller = st_tool.controller
    seen = []
    actions = _menu_actions(
        widget, lambda: seen.append(controller._is_active())
    )
    menu, _first = _menu_entry(actions)
    widget.menus = SimpleNamespace(edit=menu)
    controller._action.trigger()
    assert menu in controller._menu_filters
    # This test simulates the rebuild that would need the re-install:
    # upstream currently clears the Edit menu in place and fills the
    # very same QMenu again, so the filter of the tool is still on it.
    # A *new* QMenu takes its place here, the shape upstream would
    # have if it ever stopped reusing the object. A menu the tool
    # never filtered would let its entry run while the mode is still
    # on -- the recording below would then be [True].
    rebuilt, entry = _menu_entry(actions)
    widget.menus.edit = rebuilt
    widget.populate_mode_actions()
    assert rebuilt in controller._menu_filters
    assert controller._menu_filters[rebuilt] is True
    event = _click(qapp, rebuilt, entry)
    assert event.isAccepted() is True
    assert rebuilt.isVisible() is False
    assert seen == [False]
    assert controller._is_active() is False
    assert canvas.editing() is False
    assert canvas.drawing() is True
    assert canvas.create_mode == "rectangle"

