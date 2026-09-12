"""Tests of the pure texture fill algorithm."""

import numpy as np
import pytest

from anylabeling.custom.smudge_tool import texture_fill as tf


def _texture(height=160, width=200, seed=7):
    "Return a deterministic colour texture with fine detail."

    rng = np.random.default_rng(seed)
    grid = np.indices((height, width))
    data = np.zeros((height, width, 3), np.uint8)
    data[:, :, 0] = (grid[1] * 3 + grid[0]) % 256
    data[:, :, 1] = (grid[0] * 5) % 256
    data[:, :, 2] = ((grid[0] * grid[1]) % 251).astype(np.uint8)
    noise = rng.integers(0, 12, size=data.shape, dtype=np.uint8)
    return (data + noise).astype(np.uint8)


def _defect(image, box, seed=3):
    "Paint a bright blob inside the region so a fill really changes it."

    rng = np.random.default_rng(seed)
    out = image.copy()
    x0, y0, x1, y1 = box
    noise = rng.normal(0, 40, size=out[y0:y1, x0:x1].shape)
    out[y0:y1, x0:x1] = np.clip(
        out[y0:y1, x0:x1].astype(np.float32) + noise, 0, 255
    ).astype(np.uint8)
    out[y0:y1, x0:x1] = 240
    return out


def _outside_mask(shape, box):
    "Return the mask of the pixels outside a region."

    x0, y0, x1, y1 = box
    mask = np.ones(shape[:2], bool)
    mask[y0:y1, x0:x1] = False
    return mask


def test_feather_alpha_is_a_raised_cosine():
    alpha = tf.feather_alpha(24, 24, 6)
    assert alpha.shape == (24, 24, 1)
    assert alpha.dtype == np.float32
    assert alpha.min() > 0.0 and alpha.max() == 1.0
    assert alpha[0, 0, 0] == alpha[0, 0, 0] < 0.06
    assert alpha[23, 23, 0] < 0.06
    assert alpha[12, 12, 0] == 1.0
    # the ramp rises from the border towards the middle
    steps = [float(alpha[index, 12, 0]) for index in range(6)]
    assert steps == sorted(steps)


def test_feather_alpha_clamps_a_small_block():
    alpha = tf.feather_alpha(4, 4, 6)
    assert alpha.shape == (4, 4, 1)
    # a ramp of six cannot fit into four pixels: it is clamped to two
    assert alpha[0, 0, 0] == pytest.approx(0.1464, abs=1e-3)
    assert alpha[1, 1, 0] == pytest.approx(0.8536, abs=1e-3)
    assert alpha.max() < 1.0


def test_ssd_map_finds_the_exact_position():
    image = _texture(60, 80)
    target = image[10:30, 20:40].astype(np.float32)
    weight = np.ones(target.shape[:2], np.float32)
    cost = tf.ssd_map(target, weight, image.astype(np.float32))
    assert cost.shape == (60 - 20 + 1, 80 - 20 + 1)
    index = np.unravel_index(np.argmin(cost), cost.shape)
    assert index == (10, 20)
    assert cost[index] == pytest.approx(0.0, abs=1e-3)


def test_ssd_map_supports_one_channel_data():
    image = _texture(40, 40)[:, :, 0]
    target = image[5:15, 5:15].astype(np.float32)
    weight = np.ones(target.shape, np.float32)
    cost = tf.ssd_map(target, weight, image.astype(np.float32))
    assert cost.shape == (31, 31)
    assert np.unravel_index(np.argmin(cost), cost.shape) == (5, 5)


def test_ssd_map_refuses_a_search_smaller_than_the_window():
    target = np.zeros((10, 10), np.float32)
    weight = np.ones((10, 10), np.float32)
    assert tf.ssd_map(target, weight, np.zeros((8, 40), np.float32)) is None


@pytest.mark.parametrize("dtype", [np.uint8, np.uint16])
@pytest.mark.parametrize("channels", [1, 3, 4])
def test_fill_roi_keeps_everything_outside_the_region(dtype, channels):
    base = _texture().astype(dtype)
    if dtype == np.uint16:
        base = (base * 257).astype(np.uint16)
    if channels == 1:
        image = base[:, :, 0].copy()
    elif channels == 3:
        image = base[:, :, :3].copy()
    else:
        alpha = np.full(base.shape[:2] + (1,), 200, dtype)
        image = np.concatenate([base[:, :, :3], alpha], axis=2)
    box = (60, 40, 140, 120)
    source = (60, 130, 140, 210)
    defect = _defect(
        image.astype(np.uint8).reshape(image.shape[:2] + (-1,)), box
    )
    if channels == 1:
        defect = defect[:, :, 0]
    elif channels == 4:
        defect = np.concatenate([defect[:, :, :3], image[:, :, 3:4]], axis=2)
    if dtype == np.uint16:
        defect = (defect.astype(np.uint16) * 257).astype(np.uint16)
    out = tf.fill_roi(defect, box, source)
    mask = _outside_mask(defect.shape, box)
    assert out.shape == defect.shape
    assert out.dtype == dtype
    assert np.array_equal(out[mask], defect[mask])
    x0, y0, x1, y1 = box
    inside = out[y0:y1, x0:x1]
    before = defect[y0:y1, x0:x1]
    assert not np.array_equal(inside, before)


def test_fill_roi_is_deterministic():
    image = _defect(_texture(), (60, 40, 140, 120))
    box = (60, 40, 140, 120)
    source = (60, 130, 140, 210)
    first = tf.fill_roi(image, box, source)
    second = tf.fill_roi(image, box, source)
    assert np.array_equal(first, second)


def test_fill_roi_never_copies_the_region_onto_itself():
    "A candidate that overlaps the region is excluded."

    image = np.full((80, 80, 3), 30, np.uint8)
    image[35:45, 20:30] = 200
    box = (30, 30, 50, 50)
    # The marker sits outside the window but inside the region rows, so a
    # candidate covering the region would drag it in: no copied candidate,
    # no marker.
    out = tf.fill_roi(image, box, (20, 60, 60, 100))
    assert out[box[1] : box[3], box[0] : box[2]].max() < 200
    # a window equal to the region leaves every candidate forbidden
    same = tf.fill_roi(image, box, box)
    assert np.array_equal(same, image)



def test_forbid_roi_refuses_every_candidate_over_the_region():
    """The costs of the candidates that would copy the region are infinite.

    The window is laid over the region, which is the situation the user
    meets when the source point sits inside the box: the candidates that
    overlap the box have to be refused, and the others have to stay.
    """

    box = (30, 30, 50, 50)
    window = (20, 30, 60, 70)
    image = np.zeros((80, 80, 3), np.float32)
    state = {
        "work": image,
        "srcwin": image[window[1] : window[3], window[0] : window[2]],
        "src_origin": (window[0], window[1]),
        "alpha": None,
        "margin": 8,
        "weight_known": 2.0,
    }
    # Geometry of the first block of the region, as fill_roi computes it.
    height, width = image.shape[:2]
    bh = bw = 20
    margin = 8
    my0, mx0 = box[1] - margin, box[0] - margin
    my1, mx1 = box[3] + margin, box[2] + margin
    dy, dx = box[1] - my0, box[0] - mx0
    assert 0 <= my0 and 0 <= mx0 and my1 <= height and mx1 <= width
    cost = np.zeros((my1 - my0, mx1 - mx0), np.float32)
    out = tf._forbid_roi(
        cost, box, (dy, dx, bh, bw), True, state
    )
    allowed = np.isfinite(out)
    assert allowed.any()
    assert not allowed.all()
    ys, xs = np.where(~allowed)
    ys = ys + dy + window[1]
    xs = xs + dx + window[0]
    # every refused candidate really does cover the region
    assert ((ys < box[3]) & (ys + bh > box[1])).all()
    assert ((xs < box[2]) & (xs + bw > box[0])).all()
    ys, xs = np.where(allowed)
    ys = ys + dy + window[1]
    xs = xs + dx + window[0]
    # and no candidate that covers the region is left in
    assert not ((ys < box[3]) & (ys + bh > box[1]) & (xs < box[2])).any()


def test_an_intersecting_window_cannot_carry_the_neighbour_in():
    "A window that overlaps the region still yields a clean fill."

    image = np.full((80, 80, 3), 30, np.uint8)
    # Above the region, inside the window below: a candidate that is
    # not refused would drag this marker into the filled rows.
    image[15:25, 40:50] = 200
    box = (30, 30, 50, 50)
    window = (30, 20, 70, 60)
    assert box[0] < window[2] and window[0] < box[2]
    assert box[1] < window[3] and window[1] < box[3]
    assert not (box[0] < 50 and 40 < box[2] and box[1] < 25 and 15 < box[3])
    out = tf.fill_roi(image, box, window)
    inside = out[box[1] : box[3], box[0] : box[2]]
    assert inside.max() < 200
    outside = np.ones(image.shape[:2], bool)
    outside[box[1] : box[3], box[0] : box[2]] = False
    assert np.array_equal(out[outside], image[outside])


def test_fill_roi_leaves_an_empty_region_alone():
    image = _texture()
    empty = (10, 10, 10, 10)
    out = tf.fill_roi(image, empty, (40, 40, 60, 60))
    assert np.array_equal(out, image)


def test_fill_roi_returns_a_new_array():
    image = _defect(_texture(), (60, 40, 140, 120))
    original = image.copy()
    out = tf.fill_roi(image, (60, 40, 140, 120), (60, 130, 140, 210))
    assert out is not image
    assert np.array_equal(image, original)


def test_fill_roi_falls_back_to_the_whole_image_without_a_source():
    image = _defect(_texture(), (60, 40, 140, 120))
    box = (60, 40, 140, 120)
    out = tf.fill_roi(image, box, None)
    mask = _outside_mask(image.shape, box)
    assert np.array_equal(out[mask], image[mask])
    assert not np.array_equal(out[40:120, 60:140], image[40:120, 60:140])


def test_fill_roi_handles_a_region_at_the_border():
    image = _texture()
    box = (0, 0, 40, 40)
    source = (100, 100, 140, 140)
    out = tf.fill_roi(image, box, source)
    mask = _outside_mask(image.shape, box)
    assert np.array_equal(out[mask], image[mask])
    assert not np.array_equal(out[0:40, 0:40], image[0:40, 0:40])


def test_candidate_positions_are_ordered_outside_in():
    positions = tf._candidate_positions(0, 0, 48, 48, 24, 12)
    keys = [
        min(by, bx, 48 - (by + 24), 48 - (bx + 24)) for by, bx in positions
    ]
    assert keys == sorted(keys)
    assert len(positions) == 16
