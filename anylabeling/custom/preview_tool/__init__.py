"""Preview an image folder with its LabelMe annotations.

The tool is a project specific addition upstream does not offer, so it
lives inside anylabeling/custom and is attached to the labeling widget
from two mount lines in LabelingWidget.__init__, one import and one
call to install_preview_tool. The Tool menu entry it adds opens a non
modal window that previews a folder without ever changing it: the
source images and their side cars are only read, and the only files
the tool writes are the copies inside <output>/picked/ - a removal
moves every copy of one stem into <temp>/dsh-trash, it never deletes.

Modules:

* core - scan, annotation parsing and the filter, no Qt;
* pick_core - the pick switch and the move to the temp trash, no Qt;
* settings - the QSettings backed parameters of the window;
* worker - the folder scan and the pixmap preloader;
* pick_worker - the background copy, remove and detect jobs;
* viewer - the image view with its zoom and pan;
* overlay - the annotation layer and the rectangle expansion;
* list_panel - the file list with the picked mark;
* category_filter - the multi select category menu;
* dialog - the window itself;
* installer / launcher - the Tool menu entry, one window per widget.

Key rules:

* a folder is scanned on the top level only and sorted by natural file
  name, so image_2 comes before image_10;
* the master filter switch starts off; when it is on, the score and the
  size axis are joined with "and" and may be satisfied by two different
  shapes, while the category axis stays independent and carries the
  background pseudo category of the images without annotations;
* the button of the current image is a two way switch: it copies the
  image and its side car into <output>/picked/, and moves every copy
  of that stem into <temp>/dsh-trash once the image is picked;
* there is no undo stack and no Ctrl+Z: the source image is never
  touched, so the opposite operation can always be run again;
* Space toggles the current image, Delete only removes;
* the eight parameters live under the custom/preview_tool/ prefix of
  the settings store, the category selection of a directory as well.
"""

from .installer import install_preview_tool
from .launcher import launch_preview_tool

__all__ = [
    "install_preview_tool",
    "launch_preview_tool",
]
