"""Automatic empty label file for images that have no annotation.

Creating a json next to a browsed image is a project specific addition
that upstream does not offer, so it lives inside the project custom
package. The behaviour is attached to the labeling widget by wrapping
its load_file method from a single mount point in the constructor of
LabelingWidget; no upstream method body is touched.

The file itself is written by the upstream save path, save_labels: this
package decides when a label file is missing, upstream decides what it
holds and how it is written. Neither module imports `label_widget`, the
widget is passed in and used at runtime, which keeps the mount point in
that module acyclic and the module unit testable.

The feature leans on public widget methods (load_file, save_labels,
get_label_file, has_label_file) and on widget state an upstream upgrade
has to check: filename, label_file, dirty, image, label_list, canvas,
actions.delete_file and, inside save_labels, `_config`.
"""

from .label_file import ensure_label_file, install_ensure_label_file

__all__ = [
    "ensure_label_file",
    "install_ensure_label_file",
]
