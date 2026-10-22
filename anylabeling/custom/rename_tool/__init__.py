"""Batch rename a flat dataset folder by dominant label, in place.

The tool is a project specific addition upstream does not offer, so it
lives inside anylabeling/custom and is attached to the labeling widget
from two mount lines in LabelingWidget.__init__, one import and one
call to install_rename_tool. The Tool menu entry it adds opens a dialog
that backs the dataset folder up first and then renames the files
inside the folder itself: one click writes <source>_backup.zip next to
the dataset, and only after that archive is closed does the tool rename
the image/json pairs in place and rewrite the imagePath of every
renamed json. The backup is the safety net; the source folder is the
result. The dialog shows a read only text display: picking or dropping
a folder parses it at once and prints the counters, the ignored json
count, the blockers or the <source> -> <target> lines of the files that
have to be renamed; a blocked folder - or a folder without a single
rename - disables the main button instead of opening a popup.

Modules:

* rename_core - the rules, the plan, the backup writer and the in
  place rename, no Qt;
* launcher - lazy import of the dialog, one instance per widget;
* dialog - the window, the text display and the progress report.

Target name rule: the image and the json of a renamed pair become
<label>_<n><ext>. A name must be non empty, slash free, free of two
dots and relative; the target of a renamed file may not equal the name
of a file that is kept as is. The backup keeps the current names of
every top level file, ignored json included, because it mirrors the
folder as it was before the run.

Blocking conditions: an image without its json, an unparsable json, a
sub directory, two files sharing a stem, and a target name colliding
with a kept name. A json without its image is not a blocker: it is
ignored - it stays in the folder untouched and only its count is
reported. A blocked plan writes nothing at all - not even the .part
file of the backup.

No thread is used. The scan, the backup and the rename run on the main
thread with QCoreApplication.processEvents driving the progress bar,
while the dialog is disabled so no reentry is possible; there is no
cancel button either, which keeps the folder and the backup consistent.
"""

from .dialog import RenameDialog, install_rename_tool
from .launcher import launch_rename_tool

__all__ = [
    "RenameDialog",
    "install_rename_tool",
    "launch_rename_tool",
]
