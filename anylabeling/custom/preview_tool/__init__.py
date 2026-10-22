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
* worker - the folder tree scan and the pixmap preloader;
* pick_worker - the background copy, remove and detect jobs;
* viewer - the image view with its zoom and pan;
* overlay - the annotation layer and the rectangle expansion;
* list_panel - the directory tree with the picked mark;
* category_filter - the multi select category menu;
* dialog - the window itself;
* installer / launcher - the Tool menu entry, one window per widget.

Key rules:

* a folder is scanned recursively, depth first: a directory lists its
  files before its subdirectories and every group is sorted naturally,
  so image_2 comes before image_10; the flat scan order is the visual
  order of the tree and the order every navigation command follows;
* the tree shows one node per directory that holds a visible image and
  one leaf per image, and only a leaf selects an image;
* the master filter switch starts off; when it is on, the score and the
  size axis are joined with "and" and may be satisfied by two different
  shapes, while the category axis stays independent and carries the
  background pseudo category of the images without annotations;
* the button of the current image is a two way switch: it copies the
  image and its side car into <output>/picked/<rel_dir>/, keeping the
  relative directory below the scanned root, and moves every copy of
  that (rel_dir, stem) family into <temp>/dsh-trash once picked;
* a successful copy or removal of the current image jumps to the next
  leaf of the flat order, across directory borders and without
  wrapping at the end; "全部拷贝" never jumps;
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
