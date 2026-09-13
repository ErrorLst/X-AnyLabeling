"""Batch rename a flat dataset folder by dominant label, into a zip.

The tool is a project specific addition upstream does not offer, so it
lives inside anylabeling/custom and is attached to the labeling widget
from two mount lines in LabelingWidget.__init__, one import and one
call to install_rename_tool. The Tool menu entry it adds opens a dialog
that never writes next to the dataset: the source folder stays strictly
read only, the preview is a pure in memory plan, and the result is one
zip written to a directory the user picks.

Modules:

* rename_core - the rules, the plan and the archive writer, no Qt;
* launcher - lazy import of the dialog, one instance per widget;
* dialog - the window, the preview table and the progress report.

Entry name rule: every top level file of the source folder becomes
exactly one zip entry under its computed name (src name, target stem
plus extension, or its identical current name). A name must be non
empty, slash free, free of two dots and relative; the target of a
renamed file may not equal the name of a file that is mirrored as is.

Blocking conditions: an image without its json, a json without its
image, an unparsable json, a sub directory, two files sharing a stem,
and a target name colliding with a mirrored entry name. A blocked plan
writes nothing at all - not even the .part file of the archive.

No thread is used. The scan and the archiving run on the main thread
with QCoreApplication.processEvents driving the progress dialogs,
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
