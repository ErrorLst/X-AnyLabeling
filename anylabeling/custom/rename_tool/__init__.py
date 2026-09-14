"""Batch rename a flat dataset folder by dominant label, into a zip.

The tool is a project specific addition upstream does not offer, so it
lives inside anylabeling/custom and is attached to the labeling widget
from two mount lines in LabelingWidget.__init__, one import and one
call to install_rename_tool. The Tool menu entry it adds opens a dialog
that never writes next to the dataset: the source folder stays strictly
read only, the plan is a pure in memory scan, and the result is one zip
written next to the dataset. The dialog shows a read only text display:
picking or dropping a folder parses it at once and prints the counters,
the ignored json count, the blockers or the <source> -> <target> lines
of the files that have to be renamed; a blocked folder disables the
main button instead of opening a popup.

Modules:

* rename_core - the rules, the plan and the archive writer, no Qt;
* launcher - lazy import of the dialog, one instance per widget;
* dialog - the window, the text display and the progress report.

Entry name rule: every top level file of the source folder becomes
exactly one zip entry under its computed name (src name, target stem
plus extension, or its identical current name). A name must be non
empty, slash free, free of two dots and relative; the target of a
renamed file may not equal the name of a file that is mirrored as is.

Blocking conditions: an image without its json, an unparsable json, a
sub directory, two files sharing a stem, and a target name colliding
with a mirrored entry name. A json without its image is not a blocker:
it is ignored - left out of the plan and out of the zip - and only its
count is reported. A blocked plan writes nothing at all - not even the
.part file of the archive.

No thread is used. The scan and the archiving run on the main thread
with QCoreApplication.processEvents driving the progress bar,
while the dialog is disabled so no reentry is possible; there is no
cancel button either, which keeps the archive and the plan consistent.
"""

from .dialog import RenameDialog, install_rename_tool
from .launcher import launch_rename_tool

__all__ = [
    "RenameDialog",
    "install_rename_tool",
    "launch_rename_tool",
]
