"""Filter the file list by the labels of the annotation files.

The tool is a project specific addition upstream does not offer, so it
lives inside anylabeling/custom and is attached to the labeling widget
from two mount lines in LabelingWidget.__init__, one import and one
call to install_label_filter. The Tool menu entry it adds opens a
dialog that lists the categories of the folder that is currently open
- every label of its json files, plus the category "背景" for the
images that carry no annotation at all - and the checked categories
are the ones the file list keeps.

Modules:

* core - the classification rules, no Qt at all;
* installer - the mount point, the state and the file list wrapper;
* dialog - the category picker window, the scan and its progress;
* launcher - lazy import of the dialog, one instance per widget.

Behaviour contract: the selection is a set of "or"ed category names,
matched exactly; checking nothing means "no filter"; opening another
folder clears the filter; a single dropped file is never filtered; and
nothing is ever persisted - the state lives in widget attributes, so a
restart starts from an empty filter.
"""

from .installer import install_label_filter
from .launcher import launch_label_filter

__all__ = [
    "install_label_filter",
    "launch_label_filter",
]
