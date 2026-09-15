"""Mount point of the preview tool inside the Tool menu.

The feature never touches an upstream file: the whole behaviour hangs
off an existing labeling widget from the two mount lines of
``LabelingWidget.__init__``, one import and one call to
``install_preview_tool``. All this module does is append one entry to
the Tool menu and remember it on the widget, so calling the installer
twice cannot leave a second entry behind.

The entry owns no shortcut: the preview window handles its own keys and
the main window has to keep the ones it already uses. Its icon is read
through ``utils.qt.new_icon_path`` from a resource that really exists
in ``resources.qrc`` (``images/image.svg``); a resource that is not
registered in the running build only yields an empty icon instead of an
error, which is why the format is passed explicitly - the helper of the
upstream module defaults to ``png``.
"""

from __future__ import annotations

from PyQt6 import QtGui

from anylabeling.views.labeling.utils import qt as qt_utils

from .launcher import launch_preview_tool

__all__ = [
    "ACTION_ICON",
    "ACTION_ICON_EXT",
    "ACTION_NAME",
    "ACTION_TIP",
    "install_preview_tool",
]

ACTION_NAME = "预览工具"
ACTION_TIP = "打开预览窗口：拖入图片目录，按文件名自然排序预览并叠加标注"
ACTION_ICON = "image"
ACTION_ICON_EXT = "svg"


def install_preview_tool(widget):
    """Attach the preview tool to the Tool menu of a widget.

    Installing twice is a no op, so the mount point can be called again
    without adding a second entry to the menu. The window itself is
    imported lazily by the launcher - only the entry is built here.

    Args:
        widget: The labeling widget to extend. It has to expose
            ``menus.tool``.

    Returns:
        The installed QAction, or ``None`` when the widget has no Tool
        menu.
    """

    menus = getattr(widget, "menus", None)
    menu = getattr(menus, "tool", None)
    if menu is None:
        return None
    existing = getattr(widget, "_preview_tool_action", None)
    if existing is not None:
        return existing
    action = qt_utils.new_action(
        menu,
        ACTION_NAME,
        lambda _checked=False: launch_preview_tool(widget),
        tip=ACTION_TIP,
    )
    action.setIcon(
        QtGui.QIcon(qt_utils.new_icon_path(ACTION_ICON, ACTION_ICON_EXT))
    )
    menu.addAction(action)
    widget._preview_tool_action = action
    return action
