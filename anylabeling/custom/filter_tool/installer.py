"""Tool menu entry and display hook installation of the filter tool.

The feature never touches an upstream file: the whole behaviour hangs
off the two mount lines of LabelingWidget.__init__, one import and one
call to install_filter_tool. All this module does is append one entry
to the Tool menu, create the display controller once and install the
paint hook on the canvas; calling it twice cannot add a second entry or
a second wrapper.

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
    "install_filter_tool",
]

ACTION_TEXT = "滤镜工具"
ACTION_ICON = "image"
ACTION_ICON_EXT = "svg"
ACTION_TIP = (
    "为所有显示的图片叠加滤镜（仅影响显示，不改原图）；"
    "列表从上到下依次执行"
)


def install_filter_tool(widget):
    """Attach the filter tool to the Tool menu of a widget.

    Installing twice is a no op. The display controller is created on
    the first call and attached to the current canvas; the window is
    imported lazily by the launcher.

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
    return action
