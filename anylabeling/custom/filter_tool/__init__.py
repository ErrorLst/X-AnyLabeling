"""Display-only filter chain for every image shown by the canvas.

The tool is a project specific addition upstream does not offer, so it
lives inside anylabeling/custom and is attached to the labeling widget
from two mount lines in LabelingWidget.__init__, one import and one
call to install_filter_tool. The Tool menu entry opens a non modal
window with an ordered filter list on the right and the original image
plus its filtered preview on the left. Confirming the chain makes the
canvas draw every current and future image through the same chain.

Modules:

* core - the three filters, no Qt, no file IO;
* display - the QImage conversion helpers and the canvas paint hook;
* viewer - the two read only image panes;
* dialog - the window and its filter editor;
* installer / launcher - the Tool menu entry and one window per widget.

Key rules:

* the chain is display only: Canvas.pixmap, image_data, widget.image,
  the label json, exports and auto labeling keep the raw pixels;
* the paint hook swaps the filtered pixmap in for the duration of one
  paintEvent and restores it in a finally block;
* the list order is the execution order; an unchecked row is skipped
  but stays in the list; an empty chain means no filter at all;
* pixel filter is per channel, equalization works on luminance, the
  Gaussian blur is an ordinary cv2 GaussianBlur; alpha is preserved;
* the chain lives for the session only, nothing is persisted;
* a new image needs no extra work: it replaces Canvas.pixmap, the
  cache key changes and the next paint filters it automatically.
"""

from .installer import install_filter_tool
from .launcher import launch_filter_tool

__all__ = [
    "install_filter_tool",
    "launch_filter_tool",
]
