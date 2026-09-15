"""Crop a fixed size rectangle out of every image of a folder.

The tool is a project specific addition upstream does not offer, so it
lives inside anylabeling/custom and is attached to the labeling widget
from two mount lines in LabelingWidget.__init__, one import and one
call to install_crop_tool. The Tool menu entry it adds opens a non
modal window that never writes next to the images: the source folder
stays strictly read only, no json side car is read or written, and the
only files the tool ever creates or removes are its own crops inside
the output folder. The crop file name is the record, so a region that
was cropped in an earlier session is visible again as soon as the
output folder is read.

Modules:

* crop_core - scan, naming, parsing and deletion rules, no Qt;
* settings - the QSettings backed parameters of the window;
* viewer - the image view: zoom, pan, crop box and crop marks;
* dialog - the window, the file list and the status bar;
* launcher - lazy import of the dialog, one instance per widget.

Key rules:

* A and D switch images, Left and Right are aliases; Escape closes
  the window;
* the right mouse button is the only crop trigger, Return and Enter
  have no crop meaning at all;
* Delete removes the crops of the current image only, after a
  confirmation whose default answer is no, and only the files whose
  name matches the crop template inside the output folder;
* the fill value is a fixed zero, it is neither shown nor stored;
* a crop name never collides: an existing name only moves the
  sequence suffix one step further, so nothing is ever overwritten;
* padding is the width of the bars painted on the four sides of a
  crop, clamped to half of its width and height, and it is part of
  the file name.
"""

from .dialog import CropDialog, install_crop_tool
from .launcher import launch_crop_tool

__all__ = [
    "CropDialog",
    "install_crop_tool",
    "launch_crop_tool",
]
