"""Tool menu and toolbar entry plus display hook of the filter tool.

The feature never touches an upstream file: the whole behaviour hangs
off the two mount lines of LabelingWidget.__init__, one import and one
call to install_filter_tool. All this module does is append the filter
entry to the Tool menu, install a checkable "显示原图" action there and
in the Tools toolbar, create the display controller once and install
the paint hook on the canvas; calling it twice cannot add a second
entry, a second action or a second wrapper.

The display controller lives on the widget for the whole session, so
the chain survives image switches and only a restart clears it. The
dialog and its Qt imports are loaded lazily by the launcher.
"""

from __future__ import annotations

from PyQt6 import QtGui

from anylabeling.views.labeling.utils import qt as qt_utils

from .launcher import launch_filter_tool

__all__ = [
    "ACTION_ICON",
    "ACTION_ICON_EXT",
    "ACTION_TEXT",
    "ACTION_TIP",
    "VIEW_ACTION_ICON",
    "VIEW_ACTION_ICON_EXT",
    "VIEW_ACTION_TEXT",
    "VIEW_ACTION_TIP",
    "install_filter_tool",
]

ACTION_TEXT = "滤镜工具"
ACTION_ICON = "image"
ACTION_ICON_EXT = "svg"
ACTION_TIP = (
    "为所有显示的图片叠加滤镜（仅影响显示，不改原图）；"
    "列表从上到下依次执行"
)

VIEW_ACTION_TEXT = "显示原图"
VIEW_ACTION_ICON = "eye"
VIEW_ACTION_ICON_EXT = "svg"
VIEW_ACTION_TIP = (
    "显示原图：点击切换原图与滤镜显示（滤镜生效时可用）"
)


def install_filter_tool(widget):
    """Attach the filter tool to the Tool menu of a widget.

    Installing twice is a no op. The display controller is created on
    the first call and attached to the current canvas; the window is
    imported lazily by the launcher. The checkable raw-view action is
    appended to the Tool menu and to the Tools toolbar when present.

    Args:
        widget: The labeling widget to extend. It has to expose
            menus.tool.

    Returns:
        The installed QAction, or None when the widget has no Tool
        menu.
    """

    menus = getattr(widget, "menus", None)
    menu = getattr(menus, "tool", None)
    if menu is None:
        return None
    existing = getattr(widget, "_filter_tool_action", None)
    if existing is not None:
        controller = getattr(widget, "_filter_tool_controller", None)
        if controller is not None:
            controller.attach()
            _install_view_action(widget, menu, controller)
        return existing

    from .display import FilterDisplayController

    action = qt_utils.new_action(
        menu,
        ACTION_TEXT,
        lambda _checked=False: launch_filter_tool(widget),
        tip=ACTION_TIP,
    )
    action.setIcon(
        QtGui.QIcon(qt_utils.new_icon_path(ACTION_ICON, ACTION_ICON_EXT))
    )
    menu.addAction(action)
    widget._filter_tool_action = action

    controller = getattr(widget, "_filter_tool_controller", None)
    if controller is None:
        controller = FilterDisplayController(widget)
        widget._filter_tool_controller = controller
    else:
        controller.attach()
    controller.bind_action(action, ACTION_TIP)
    _install_view_action(widget, menu, controller)
    return action


def _install_view_action(widget, menu, controller):
    """Create and bind the checkable raw-view action once."""

    action = getattr(widget, "_filter_tool_view_action", None)
    if action is None:
        action = qt_utils.new_action(
            menu,
            VIEW_ACTION_TEXT,
            controller.set_bypass,
            tip=VIEW_ACTION_TIP,
            checkable=True,
            enabled=False,
        )
        action.setIcon(
            QtGui.QIcon(
                qt_utils.new_icon_path(
                    VIEW_ACTION_ICON, VIEW_ACTION_ICON_EXT
                )
            )
        )
        widget._filter_tool_view_action = action
    if action not in menu.actions():
        menu.addAction(action)
    controller.bind_bypass_action(action, VIEW_ACTION_TIP)
    _add_to_toolbar(widget, action)
    return action


def _add_to_toolbar(widget, action):
    """Append the view action to the vertical Tools toolbar."""

    tools = getattr(widget, "tools", None)
    if tools is None:
        return
    if action not in tools.actions():
        tools.addAction(action)
        tools.setMinimumHeight(tools.sizeHint().height())
