"""Tool menu entry of the YOLO to LabelMe tool.

The feature never touches an upstream file: the whole behaviour hangs
off the two mount lines of LabelingWidget.__init__, one import and one
call to install_yolo_to_labelme. All this module does is append the menu
entry; the window is loaded lazily by the launcher. Calling install
twice cannot add a second entry.
"""

from __future__ import annotations

from anylabeling.views.labeling.utils import qt as qt_utils

from .launcher import launch_yolo_to_labelme

__all__ = [
    "ACTION_ICON",
    "ACTION_TEXT",
    "ACTION_TIP",
    "install_yolo_to_labelme",
]

ACTION_TEXT = "YOLO 转 LabelMe"
ACTION_ICON = "convert"
ACTION_TIP = (
    "把导出的 YOLO 数据集（图片 + 同名 txt，或 images/ + labels/）"
    "按 class.txt 还原成图片旁的 labelme json；已有 json 默认跳过"
)

_ACTION_ATTR = "_yolo_to_labelme_action"


def install_yolo_to_labelme(widget):
    """Attach the YOLO to LabelMe action to the Tool menu.

    Installing twice is a no op, so the mount point can be called again
    without adding a second entry to the menu.

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
    existing = getattr(widget, _ACTION_ATTR, None)
    if existing is not None:
        return existing
    action = qt_utils.new_action(
        menu,
        ACTION_TEXT,
        lambda _checked=False: launch_yolo_to_labelme(widget),
        icon=ACTION_ICON,
        tip=ACTION_TIP,
    )
    menu.addAction(action)
    setattr(widget, _ACTION_ATTR, action)
    return action
