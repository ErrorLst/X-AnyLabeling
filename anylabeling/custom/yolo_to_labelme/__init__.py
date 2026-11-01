"""Restore a YOLO export back into LabelMe annotations.

The tool is a project specific addition upstream does not offer, so it
lives inside anylabeling/custom and is attached to the labeling widget
from two mount lines in LabelingWidget.__init__, one import and one call
to install_yolo_to_labelme. The Tool menu entry opens a non modal window
that asks for one YOLO dataset directory and one classes file, scans the
folder and writes one LabelMe json next to every image whose same stem
.txt file was found.

Modules:

* yolo_core - layout detection, scan, pairing and label validation, no
  Qt and no third party import;
* conversion - dispatch of the ready pairs through the upstream
  LabelConverter, with per file failure isolation;
* installer / launcher - the Tool menu entry and one window per widget;
* dialog - the window, the summary and the progress report.

Key rules:

* only a YOLO export is understood: image plus same stem .txt, or an
  images/ and labels/ split directory; a COCO json is not converted;
* the classes file is mandatory; the line number is the YOLO class id;
* the json is written next to the image, an existing json is skipped
  unless the user checks the overwrite box;
* the source images are read only, nothing is deleted or moved;
* the conversion runs in the main thread with processEvents between
  files, the window is disabled while it runs and refuses to close;
* nothing is persisted: no QSettings, no output directory, no toolbar
  button.
"""

from .installer import install_yolo_to_labelme
from .launcher import launch_yolo_to_labelme

__all__ = [
    "install_yolo_to_labelme",
    "launch_yolo_to_labelme",
]
