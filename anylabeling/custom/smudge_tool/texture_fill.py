"""Texture fill used by the smudge tool.

The module is pure numpy/OpenCV: no Qt, no disk access, so every value
it produces can be unit tested directly. It is a port of the quilting
variant that was accepted during the experiments (the ``quilt()`` of
``exp5.py`` with ``gain=False`` and ``post_seamless=False``), whose
helpers ``_feather_alpha`` and ``_ssd_map`` come from ``common.py``.

The port keeps the numerical behaviour of the experiment line by line:
the onion peel order of the candidate blocks, the source window
restriction, the raised cosine feathering, the exclusion of the
candidates that overlap the region of interest and the ``np.where``
blending. Three deviations are deliberate, and only the first one can
change a value:

* aligned patch: a block whose matching window holds almost no known
  pixel is not matched but covered by the texture of the source window
  at the offset the block has inside the region, which repairs a region
  smaller than one block, and only such a region;
* channels: the experiment always used three channel BGR images, here
  the channel count is ``1`` for a two dimensional array and
  ``image.shape[2]`` otherwise, and a channel is addressed with
  ``arr[..., c]``;
* dtype: the experiment clipped to ``0..255`` and wrote ``uint8``, here
  the clip bound is ``np.iinfo(image.dtype).max`` so ``uint16`` works as
  well.

A block is always matched against the *original* image, never against
the blocks that were already written, exactly like the experiment.
Outside the region of interest the returned array equals the input
array byte for byte.
"""

import cv2
import numpy as np

#: Side of one texture block, in pixels.
BLOCK = 24

#: Distance between two candidate blocks, in pixels.
STEP = 12

#: Width of the raised cosine feather, in pixels.
RAMP = 6

#: Context margin added around a block, in pixels.
MARGIN = 8

#: Weight of a pixel that is already known inside the matching window.
WEIGHT_KNOWN = 2.0

#: A block has to be at least this many pixels wide and tall.
MIN_SIDE = 6

#: A block whose weighted window sums below this is not matched: it is
#: covered by the aligned texture of the source window.
MIN_WEIGHT = 20.0


def channels_of(array):
    """Return the channel count of a two or three dimensional array."""
    return 1 if array.ndim == 2 else array.shape[2]


def channel(array, index):
    """Return one channel of an array as a two dimensional view."""
    if array.ndim == 2:
        return array
    return array[..., index]


def feather_alpha(height, width, ramp=RAMP):
    """Return the raised cosine feather of one block.

    Args:
        height: Height of the block, in pixels.
        width: Width of the block, in pixels.
        ramp: Width of the feather, in pixels. It is clamped to half of
            the smaller side, so a small block gets a smaller feather.

    Returns:
        A ``(height, width, 1)`` float32 array holding the smaller of the
        horizontal and the vertical raised cosine ramp, in ``0..1``.
    """
    ay = np.ones(height, np.float32)
    ax = np.ones(width, np.float32)
    r = int(min(ramp, height // 2, width // 2))
    if r > 0:
        t = (np.arange(r, dtype=np.float32) + 0.5) / r
        vals = 0.5 - 0.5 * np.cos(np.pi * t)
        ay[:r] = vals
        ay[-r:] = vals[::-1]
        ax[:r] = vals
        ax[-r:] = vals[::-1]
    return np.minimum(ay[:, None], ax[None, :])[:, :, None]


def ssd_map(target, weight, search):
    """Return the weighted SSD cost of every placement of a window.

    Args:
        target: The window to match, ``(h, w)`` or ``(h, w, channels)``.
        weight: Per pixel weight of ``target``, ``(h, w)`` float32.
        search: The image to search in, same channel count as
            ``target``.

    Returns:
        A float32 array holding the cost of every pixel aligned
        placement of ``target`` inside ``search``, or ``None`` when
        ``search`` is smaller than ``target``.
    """
    wy, wx = target.shape[:2]
    sh, sw = search.shape[:2]
    if sh < wy or sw < wx:
        return None
    t1 = float((_weighted(weight, target) * (target**2)).sum())
    t2 = cv2.filter2D(
        _squared_channel_sum(search), cv2.CV_32F, weight, anchor=(0, 0)
    )[: sh - wy + 1, : sw - wx + 1]
    t3 = np.zeros((sh - wy + 1, sw - wx + 1), np.float32)
    for c in range(channels_of(search)):
        conv = cv2.filter2D(
            channel(search, c),
            cv2.CV_32F,
            weight * channel(target, c),
            anchor=(0, 0),
        )
        t3 -= 2.0 * conv[: sh - wy + 1, : sw - wx + 1]
    return t1 + t2 + t3


def fill_roi(
    image,
    roi,
    source_window=None,
    patch=BLOCK,
    step=STEP,
    ramp=RAMP,
    margin=MARGIN,
    weight_known=WEIGHT_KNOWN,
):
    """Replace the content of a region with texture taken elsewhere.

    The region is filled block by block, from its border inwards. For
    every block the best placement of the window around it is searched
    inside ``source_window`` with the weighted sum of squared
    differences, where a pixel that is already known outweighs a pixel
    that is still to be filled, and the texture found is blended into
    the block with the raised cosine feather. A block that still has no
    known pixel to match is covered by the texture of the source window
    at the place that has the same offset inside the window as the block
    has inside the region, which repairs a region smaller than one
    block; without a ``source_window`` such a block is left untouched. A
    candidate that overlaps the region itself is excluded, so the region
    is never copied onto itself.

    Args:
        image: The image to take the texture from, ``uint8`` or
            ``uint16``, ``(h, w)`` or ``(h, w, channels)``.
        roi: The region to replace, ``(x0, y0, x1, y1)``, half open.
        source_window: The window to search in, same format as ``roi``,
            or ``None`` to search the whole image.
        patch: Side of one block, in pixels.
        step: Distance between two candidate blocks, in pixels.
        ramp: Width of the feather, in pixels.
        margin: Context margin around a block, in pixels.
        weight_known: Weight of a pixel that is already known.

    Returns:
        A new array of the same shape and dtype as ``image``. Outside
        ``roi`` it is byte for byte identical to ``image``.
    """
    x0, y0, x1, y1 = roi
    height, width = image.shape[:2]
    work = image.astype(np.float32, copy=True)
    filled = np.ones((height, width), bool)
    filled[y0:y1, x0:x1] = False
    srcwin = None
    src_origin = (0, 0)
    if source_window is not None:
        sx0, sy0, sx1, sy1 = source_window
        srcwin = work[sy0:sy1, sx0:sx1]
        src_origin = (sx0, sy0)
    state = {
        "work": work,
        "srcwin": srcwin,
        "src_origin": src_origin,
        "alpha": feather_alpha(patch, patch, ramp),
        "margin": margin,
        "weight_known": weight_known,
    }
    for by, bx in _candidate_positions(x0, y0, x1, y1, patch, step):
        _fill_block(state, filled, (by, bx), (x0, y0, x1, y1), patch)
    out = image.copy()
    if x1 > x0 and y1 > y0:
        out[y0:y1, x0:x1] = _clip_to_dtype(work[y0:y1, x0:x1], image.dtype)
    return out


def _candidate_positions(x0, y0, x1, y1, patch, step):
    """Return the candidate block origins, the outermost ones first.

    This is the onion peel order of the experiment: a block close to the
    border of the region is matched before a block that sits deeper
    inside, so every block sees as much already filled context as it can.
    Python sorts stably, which keeps the ties in raster order, again like
    the experiment.
    """
    positions = [
        (by, bx) for by in range(y0, y1, step) for bx in range(x0, x1, step)
    ]
    positions.sort(
        key=lambda t: min(
            t[0] - y0, t[1] - x0, y1 - (t[0] + patch), x1 - (t[1] + patch)
        )
    )
    return positions


def _fill_block(state, filled, origin, roi, patch):
    """Match and blend the texture of one block of the region.

    A block whose matching window holds almost no known pixel is not
    matched but covered by the aligned texture of the source window, see
    :func:`_cover_block`.

    Args:
        state: The shared inputs of the fill.
        filled: The map of the pixels that already hold final texture.
        origin: ``(by, bx)`` origin of the block.
        roi: ``(x0, y0, x1, y1)`` region being filled.
        patch: Side of one block, in pixels.
    """
    by, bx = origin
    x0, y0, x1, y1 = roi
    work = state["work"]
    srcwin = state["srcwin"]
    height, width = work.shape[:2]
    bey, bex = min(by + patch, y1), min(bx + patch, x1)
    bh, bw = bey - by, bex - bx
    if bh < MIN_SIDE or bw < MIN_SIDE:
        return
    m = state["margin"]
    while m > 0 and srcwin is not None:
        if srcwin.shape[0] >= bh + 2 * m and srcwin.shape[1] >= bw + 2 * m:
            break
        m -= 2
    my0, mx0 = max(0, by - m), max(0, bx - m)
    my1, mx1 = min(height, bey + m), min(width, bex + m)
    weight = np.zeros((my1 - my0, mx1 - mx0), np.float32)
    weight[by - my0 : bey - my0, bx - mx0 : bex - mx0] = filled[
        by:bey, bx:bex
    ].astype(np.float32)
    weight[filled[my0:my1, mx0:mx1] & (weight == 0)] = state["weight_known"]
    if weight.sum() < MIN_WEIGHT:
        # Almost no pixel of the window is known yet: the weighted cost
        # would be flat and its minimum would sit in the corner of the
        # search area, so the block cannot be matched. Skipping it here
        # is what used to leave a region smaller than one block wholly
        # untouched; the aligned texture of the source window repairs it
        # instead, because that is the texture the user picked.
        _cover_block(state, filled, origin, roi, patch)
        return
    target = work[my0:my1, mx0:mx1]
    in_source = srcwin is not None and _fits(srcwin, weight)
    search = srcwin if in_source else work
    cost = ssd_map(target, weight, search)
    if cost is None:
        return
    dy, dx = by - my0, bx - mx0
    cost = _forbid_roi(cost, roi, (dy, dx, bh, bw), in_source, state)
    if not np.isfinite(cost).any():
        return
    oy, ox = np.unravel_index(np.argmin(cost), cost.shape)
    sy, sx = int(oy) + dy, int(ox) + dx
    if in_source:
        sx += state["src_origin"][0]
        sy += state["src_origin"][1]
    sy = int(np.clip(sy, 0, height - bh))
    sx = int(np.clip(sx, 0, width - bw))
    _blend_block(state, filled, (by, bx, bey, bex, bh, bw), (sy, sx))


def _cover_block(state, filled, origin, roi, patch):
    """Cover one block with the aligned texture of the source window.

    A block whose matching window holds almost no known pixel - a region
    smaller than one block has no other block to learn from - cannot be
    matched, and leaving it out would keep the defect of the region in
    place. The block is then taken from the place of the source window
    that has the same offset inside the window as the block has inside
    the region: the window is as large as the region, so for a region
    smaller than one block this is the region sized texture around the
    source point, which is what the tool promises the user.

    Two cases are refused, and the block keeps its pixels, the way a
    block whose every candidate was refused does: a window so small, or
    so far outside the image, that it cannot hold the aligned block, and
    an aligned block that lands on the region itself, because the region
    is never copied onto itself.

    Args:
        state: The shared inputs of the fill.
        filled: The map of the pixels that already hold final texture.
        origin: ``(by, bx)`` origin of the block.
        roi: ``(x0, y0, x1, y1)`` region being filled.
        patch: Side of one block, in pixels.

    Returns:
        ``True`` when the block was written.
    """
    srcwin = state["srcwin"]
    if srcwin is None:
        return False
    by, bx = origin
    x0, y0, x1, y1 = roi
    bey, bex = min(by + patch, y1), min(bx + patch, x1)
    bh, bw = bey - by, bex - bx
    off_y, off_x = by - y0, bx - x0
    if off_y + bh > srcwin.shape[0] or off_x + bw > srcwin.shape[1]:
        return False
    sx0, sy0 = state["src_origin"]
    sy, sx = sy0 + off_y, sx0 + off_x
    if sy < y1 and sy + bh > y0 and sx < x1 and sx + bw > x0:
        return False
    _blend_block(state, filled, (by, bx, bey, bex, bh, bw), (sy, sx))
    return True


def _squared_channel_sum(array):
    """Return the sum of the squares of every pixel of an array.

    The experiment summed over the channel axis of a color image; a two
    dimensional image has no channel axis and is squared as it is.
    """
    if array.ndim == 2:
        return array**2
    return (array**2).sum(axis=2)


def _weighted(weight, target):
    """Shape the per pixel weights like ``target`` for broadcasting.

    A color image gets the weights as a trailing axis, which is how the
    experiment multiplied them; a two dimensional image, a grayscale
    image, keeps the plain weight map, because a trailing axis of size
    one would broadcast against the width instead of the channels.
    """
    if target.ndim == 2:
        return weight
    return weight[:, :, None]


def _fits(search, window):
    """Return ``True`` when ``window`` fits inside ``search``."""
    return (
        search.shape[0] >= window.shape[0]
        and search.shape[1] >= window.shape[1]
    )


def _forbid_roi(cost, roi, block, in_source, state):
    """Set the cost of every candidate overlapping the region to infinity.

    A candidate tile that covers a part of the region would copy the
    defect onto itself, so it can never be a source. The candidate is
    first translated back to image coordinates, which needs the origin of
    the source window when the search happened inside that window.

    Args:
        cost: The cost map of one block.
        roi: ``(x0, y0, x1, y1)`` region that must not be copied.
        block: ``(dy, dx, bh, bw)`` position of the block inside the
            coordinate system of the search.
        in_source: ``True`` when the search happened inside the source
            window.
        state: The shared inputs of the fill, used for the window origin.

    Returns:
        The cost map with the overlapping candidates at infinity.
    """
    x0, y0, x1, y1 = roi
    dy, dx, bh, bw = block
    oy_grid, ox_grid = np.mgrid[0 : cost.shape[0], 0 : cost.shape[1]]
    off_x, off_y = state["src_origin"] if in_source else (0, 0)
    abs_y = oy_grid + dy + off_y
    abs_x = ox_grid + dx + off_x
    bad = (abs_y < y1) & (abs_y + bh > y0) & (abs_x < x1) & (abs_x + bw > x0)
    return np.where(bad, np.inf, cost)


def _blend_block(state, filled, block, source):
    """Blend one matched texture block into the region.

    A pixel of the block that is already known is cross faded with the
    feather, so the block joins its neighbours without a seam, while a
    pixel that is still empty takes the texture value untouched.
    """
    work = state["work"]
    by, bx, bey, bex, bh, bw = block
    sy, sx = source
    inside = filled[by:bey, bx:bex]
    al = state["alpha"][:bh, :bw]
    patch = work[sy : sy + bh, sx : sx + bw]
    cur = work[by:bey, bx:bex]
    if work.ndim == 2:
        al = al[:, :, 0]
    else:
        inside = inside[:, :, None]
    work[by:bey, bx:bex] = np.where(
        inside, cur * (1.0 - al) + patch * al, patch
    )
    filled[by:bey, bx:bex] = True


def _clip_to_dtype(array, dtype):
    """Clip a float array to the range of ``dtype`` and cast it."""
    if np.issubdtype(dtype, np.integer):
        max_val = np.iinfo(dtype).max
    else:
        max_val = 1.0
    return np.clip(array, 0, max_val).astype(dtype)
