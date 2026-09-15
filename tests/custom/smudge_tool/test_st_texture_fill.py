"""Tests of the pure texture fill algorithm."""

import warnings

import numpy as np
import pytest

from anylabeling.custom.smudge_tool import operations
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


def _fill_state(image, window, margin=8, patch=24):
    "Return the shared state of a fill, like fill_roi builds it."

    x0, y0, x1, y1 = window
    return {
        "work": image,
        "srcwin": image[y0:y1, x0:x1],
        "src_origin": (x0, y0),
        "alpha": tf.feather_alpha(patch, patch, tf.RAMP),
        "margin": margin,
        "weight_known": tf.WEIGHT_KNOWN,
    }


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


@pytest.mark.parametrize(
    "size", [(6, 6), (9, 12), (10, 10), (17, 17), (6, 17), (17, 6)]
)
def test_a_region_below_one_block_becomes_the_source_window(size):
    "A region smaller than a block is covered by the aligned window."

    width, height = size
    box = (20, 20, 20 + width, 20 + height)
    window = (20, 100, 20 + width, 100 + height)
    image = _defect(_texture(), box)
    out = tf.fill_roi(image, box, window)
    assert out.shape == image.shape
    assert out.dtype == image.dtype
    mask = _outside_mask(image.shape, box)
    assert np.array_equal(out[mask], image[mask])
    # The window has the size of the region, so the aligned patch is the
    # whole window: the region holds exactly those pixels instead of the
    # defect that was painted into it.
    assert np.array_equal(
        out[box[1] : box[3], box[0] : box[2]],
        image[window[1] : window[3], window[0] : window[2]],
    )
    assert not np.array_equal(
        out[box[1] : box[3], box[0] : box[2]],
        image[box[1] : box[3], box[0] : box[2]],
    )


@pytest.mark.parametrize(
    "size", [(18, 18), (20, 20), (24, 24), (10, 30), (40, 40)]
)
def test_a_small_region_is_repaired_without_touching_the_outside(size):
    "A region around one block is repaired from edge to edge."

    width, height = size
    box = (20, 20, 20 + width, 20 + height)
    window = (20, 100, 20 + width, 100 + height)
    image = _defect(_texture(), box)
    out = tf.fill_roi(image, box, window)
    mask = _outside_mask(image.shape, box)
    assert np.array_equal(out[mask], image[mask])
    assert not np.array_equal(
        out[box[1] : box[3], box[0] : box[2]],
        image[box[1] : box[3], box[0] : box[2]],
    )


def test_a_region_over_its_own_window_is_left_alone():
    "A window equal to the region cannot be copied onto itself."

    image = _defect(_texture(), (30, 30, 50, 50))
    box = (30, 30, 50, 50)
    assert np.array_equal(tf.fill_roi(image, box, box), image)
    # A region smaller than one block has an aligned patch as well, and
    # that patch is the region itself: it is refused too.
    small = (20, 20, 30, 30)
    defect = _defect(_texture(), small)
    assert np.array_equal(tf.fill_roi(defect, small, small), defect)


@pytest.mark.parametrize(
    "window",
    [
        (65, 40, 75, 50),
        (55, 40, 65, 50),
        (60, 45, 70, 55),
        (60, 35, 70, 45),
    ],
)
def test_a_window_that_overlaps_the_region_is_refused(window):
    "A window that touches the region is never copied into it."

    box = (60, 40, 70, 50)
    image = _defect(_texture(), box)
    assert np.array_equal(tf.fill_roi(image, box, window), image)


def test_cover_block_writes_the_aligned_texture_and_marks_it_known():
    "The aligned patch is written and recorded as final texture."

    image = _texture().astype(np.float32)
    window = (100, 60, 106, 66)
    state = _fill_state(image, window)
    filled = np.ones(image.shape[:2], bool)
    filled[0:6, 0:6] = False
    assert tf._cover_block(state, filled, (0, 0), (0, 0, 6, 6), 24) is True
    assert np.array_equal(state["work"][0:6, 0:6], image[60:66, 100:106])
    assert filled[0:6, 0:6].all()


def test_cover_block_refuses_the_region_and_a_window_that_is_too_small():
    "A self copy and a window that cannot hold the block are refused."

    image = _texture().astype(np.float32)
    filled = np.ones(image.shape[:2], bool)
    filled[30:50, 30:50] = False
    before = filled.copy()
    state = _fill_state(image, (30, 30, 50, 50))
    assert (
        tf._cover_block(state, filled, (30, 30), (30, 30, 50, 50), 24)
        is False
    )
    assert np.array_equal(filled, before)
    small = _fill_state(image, (100, 100, 103, 103))
    assert tf._cover_block(small, filled, (0, 0), (0, 0, 6, 6), 24) is False
    assert np.array_equal(filled, before)


def test_a_small_region_without_a_source_window_is_matched_globally():
    "Without a source window the plain matching still does the work."

    box = (20, 20, 30, 30)
    image = _defect(_texture(), box)
    out = tf.fill_roi(image, box, None)
    mask = _outside_mask(image.shape, box)
    assert np.array_equal(out[mask], image[mask])
    assert not np.array_equal(out[20:30, 20:30], image[20:30, 20:30])


#: The region, the source point and the defect of the seam probes: a box
#: of 50x40 pixels, a source point 60 pixels to its right, and a defect
#: that fills the whole region, so a border that keeps the pixels it
#: replaces shows up as the colour of the defect.
SEAM_BOX = (60, 50, 110, 90)
SEAM_SOURCE = (170.0, 130.0)
SEAM_DEFECT = 20


def _ramp_material(height=160, width=200):
    "A smooth diagonal gradient, 0.43 grey level per pixel."

    rows, cols = np.indices((height, width))
    values = 40.0 + 0.43 * (cols + rows)
    return np.clip(np.round(values), 0, 255).astype(np.uint8)


def _active_material(height=160, width=200):
    "A grainy material whose level drifts."

    rows, cols = np.indices((height, width))
    level = 71.0 + 80 * np.sin(rows / 60.0) + 60 * np.cos(cols / 50.0)
    rng = np.random.default_rng(23)
    noise = rng.integers(-9, 10, (height, width))
    return np.clip(np.round(level + noise), 0, 255).astype(np.uint8)


def _blobbed(image, box, colour=SEAM_DEFECT):
    "Paint the whole region with the colour of a defect."

    out = image.copy()
    out[box[1] : box[3], box[0] : box[2]] = colour
    return out


def _ring(image, box):
    "The outermost ring of a region, as one flat array."

    x0, y0, x1, y1 = box
    values = image.astype(np.float64)
    return np.concatenate(
        [
            values[y0, x0:x1],
            values[y1 - 1, x0:x1],
            values[y0:y1, x0],
            values[y0:y1, x1 - 1],
        ]
    )


def _seam_windows(image):
    "The narrow window of the port and the window the tool asks for."

    size = (SEAM_BOX[2] - SEAM_BOX[0], SEAM_BOX[3] - SEAM_BOX[1])
    narrow = operations.source_window(SEAM_SOURCE, size, image.shape, factor=1)
    wide = operations.source_window(SEAM_SOURCE, size, image.shape)
    return narrow, wide


def _edge_jump(result, box):
    "Mean absolute difference across the four edges of a region."

    x0, y0, x1, y1 = box
    values = result.astype(np.float64)
    jumps = [
        np.abs(values[y0, x0:x1] - values[y0 - 1, x0:x1]),
        np.abs(values[y1 - 1, x0:x1] - values[y1, x0:x1]),
        np.abs(values[y0:y1, x0] - values[y0:y1, x0 - 1]),
        np.abs(values[y0:y1, x1 - 1] - values[y0:y1, x1]),
    ]
    return float(np.mean([jump.mean() for jump in jumps]))


def _material_variation(image, box):
    "Mean absolute difference of neighbouring pixels of the material."

    band = image[box[1] - 30 : box[1] - 10, :].astype(np.float64)
    return float(np.abs(np.diff(band, axis=1)).mean())


@pytest.mark.parametrize(
    "shorter,expected",
    [
        (6, 0),
        (10, 0),
        (12, 1),
        (17, 2),
        (20, 2),
        (30, 3),
        (40, 4),
        (50, 5),
        (80, 8),
        (2000, 8),
    ],
)
def test_the_outer_feather_follows_the_shorter_side(shorter, expected):
    "A tenth of the shorter side, clamped to eight pixels."

    assert tf.edge_ramp_for(shorter, shorter) == expected
    assert tf.edge_ramp_for(shorter, shorter + 40) == expected


def test_edge_feather_is_the_complement_of_the_block_feather():
    "The two feathers sum to one: where the one is high the other is low."

    feather = tf.edge_feather(24, 24, 6)
    alpha = tf.feather_alpha(24, 24, 6)
    assert feather.shape == (24, 24, 1)
    assert feather.dtype == np.float32
    assert np.allclose(feather[:, :, 0] + alpha[:, :, 0], 1.0)
    assert feather[12, 12, 0] == 0.0
    assert feather[0, 0, 0] > 0.9
    assert alpha[12, 12, 0] == 1.0
    assert alpha[0, 0, 0] < 0.06


def test_the_adaptive_feather_spares_a_region_below_one_block():
    "A large region gets a border that moves, a small one does not."

    box = (40, 30, 90, 70)
    window = (100, 100, 150, 140)
    image = _defect(_texture(), box)
    assert tf.edge_ramp_for(40, 50) == 4
    zero = tf.fill_roi(image, box, window, edge_ramp=0)
    adaptive = tf.fill_roi(image, box, window)
    bands = (
        (slice(30, 31), slice(40, 90)),
        (slice(69, 70), slice(40, 90)),
        (slice(30, 70), slice(40, 41)),
        (slice(30, 70), slice(89, 90)),
    )
    for rows, cols in bands:
        assert not np.array_equal(adaptive[rows, cols], zero[rows, cols])
    small = (20, 20, 30, 30)
    assert tf.edge_ramp_for(10, 10) == 0
    tiny = _defect(_texture(), small)
    assert np.array_equal(
        tf.fill_roi(tiny, small, (100, 100, 110, 110)),
        tf.fill_roi(tiny, small, (100, 100, 110, 110), edge_ramp=0),
    )


def test_a_zero_ramp_is_the_result_of_the_port():
    "Without a feather the fill only writes the matched texture."

    box = (40, 30, 90, 70)
    window = (100, 100, 150, 140)
    image = _defect(_texture(), box)
    zero = tf.fill_roi(image, box, window, edge_ramp=0)
    adaptive = tf.fill_roi(image, box, window)
    assert not np.array_equal(zero, adaptive)
    mask = _outside_mask(image.shape, box)
    assert np.array_equal(zero[mask], image[mask])
    assert np.array_equal(adaptive[mask], image[mask])


def test_the_enlarged_window_removes_the_seam_of_a_gradient():
    "The jump across the edges drops to the slope of the gradient."

    clean = _ramp_material()
    image = _blobbed(clean, SEAM_BOX)
    narrow, wide = _seam_windows(image)
    # The port searched a window of the size of the region, which is the
    # left column of this one: eleven placements in a single row.
    assert narrow == (145, 110, 195, 150)
    assert wide == (50, 40, 200, 160)
    zero = tf.fill_roi(image, SEAM_BOX, narrow, edge_ramp=0)
    out = tf.fill_roi(image, SEAM_BOX, wide)
    assert _edge_jump(zero, SEAM_BOX) >= 30.0
    assert _edge_jump(out, SEAM_BOX) <= 10.0
    # The border of the region carries the material it was painted with:
    # a feather that kept the pixels the region replaces would leave the
    # ring of the region at the colour of the defect.
    assert _ring(out, SEAM_BOX).mean() == pytest.approx(
        _ring(clean, SEAM_BOX).mean(), abs=10.0
    )
    assert _ring(clean, SEAM_BOX).min() > SEAM_DEFECT + 40


def test_the_enlarged_window_removes_the_seam_of_a_grainy_material():
    "The jump across the edges stays within the activity of the grain."

    clean = _active_material()
    image = _blobbed(clean, SEAM_BOX)
    narrow, wide = _seam_windows(image)
    zero = tf.fill_roi(image, SEAM_BOX, narrow, edge_ramp=0)
    out = tf.fill_roi(image, SEAM_BOX, wide)
    material = _material_variation(clean, SEAM_BOX)
    assert _edge_jump(zero, SEAM_BOX) >= 30.0
    assert _edge_jump(out, SEAM_BOX) <= material + 8.0
    assert _edge_jump(out, SEAM_BOX) < _edge_jump(zero, SEAM_BOX)
    assert _ring(out, SEAM_BOX).mean() == pytest.approx(
        _ring(clean, SEAM_BOX).mean(), abs=10.0
    )


def test_the_border_of_the_region_never_keeps_the_defect():
    "The outermost ring of a region is texture, never the defect."

    image = _blobbed(_ramp_material(), SEAM_BOX)
    _, wide = _seam_windows(image)
    out = tf.fill_roi(image, SEAM_BOX, wide)
    # Every pixel of the ring sits on the material the region replaces,
    # which is a gradient of 87..127 grey levels there: the defect is a
    # colour of its own and cannot hide in that range.
    assert _ring(out, SEAM_BOX).min() > SEAM_DEFECT + 40


def test_a_region_at_the_image_corner_is_feathered_on_every_edge():
    "The feather follows the region, not one of the image borders."

    box = (0, 0, 40, 40)
    image = _defect(_texture(), box)
    window = (100, 100, 140, 140)
    assert tf.edge_ramp_for(40, 40) == 4
    zero = tf.fill_roi(image, box, window, edge_ramp=0)
    out = tf.fill_roi(image, box, window)
    mask = _outside_mask(image.shape, box)
    assert np.array_equal(out[mask], image[mask])
    bands = (
        (slice(0, 2), slice(0, 40)),
        (slice(38, 40), slice(0, 40)),
        (slice(0, 40), slice(0, 2)),
        (slice(0, 40), slice(38, 40)),
    )
    for rows, cols in bands:
        assert not np.array_equal(out[rows, cols], zero[rows, cols])


@pytest.mark.parametrize("dtype", [np.uint8, np.uint16])
@pytest.mark.parametrize("channels", [1, 3, 4])
def test_the_outer_feather_keeps_the_range_of_the_dtype(dtype, channels):
    "The feathered border stays inside the range of the dtype."

    base = _texture()
    box = (60, 40, 140, 120)
    source = (60, 130, 140, 210)
    if channels == 1:
        image = base[:, :, 0].copy()
    elif channels == 3:
        image = base[:, :, :3].copy()
    else:
        alpha = np.full(base.shape[:2] + (1,), 200, np.uint8)
        image = np.concatenate([base[:, :, :3], alpha], axis=2)
    defect = _defect(image, box)
    if dtype == np.uint16:
        image = (image.astype(np.uint16) * 257).astype(np.uint16)
        defect = (defect.astype(np.uint16) * 257).astype(np.uint16)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out = tf.fill_roi(defect, box, source)
    assert [
        item for item in caught if issubclass(item.category, RuntimeWarning)
    ] == []
    assert out.dtype == dtype
    assert out.shape == defect.shape
    mask = _outside_mask(defect.shape, box)
    assert np.array_equal(out[mask], defect[mask])
    assert not np.array_equal(out[40:120, 60:140], defect[40:120, 60:140])
    assert out.max() <= np.iinfo(dtype).max


def _border_state(image, ramp):
    "Return the state of a fill whose region border carries the feather."

    state = _fill_state(image, (100, 60, 112, 72))
    if ramp:
        state.update(
            {
                "edge_ramp": ramp,
                "edge_h": 12,
                "edge_w": 12,
                "edge_origin": (0, 0),
                "edge_feather": tf.edge_feather(12, 12, ramp),
            }
        )
    return state


def test_the_border_weight_leaves_a_pixel_no_block_has_written_alone():
    "The port already gives an empty pixel the whole texture."

    image = _texture()[:, :, 0].astype(np.float32)
    source = image[60:66, 100:106].copy()
    state = _border_state(image, 4)
    filled = np.ones(image.shape[:2], bool)
    filled[0:6, 0:6] = False
    assert tf._cover_block(state, filled, (0, 0), (0, 0, 6, 6), 24)
    # The border weight of the region cannot lift a pixel that no block
    # has written yet: that pixel takes the texture whole already, so
    # the aligned patch is the source window untouched.
    assert np.array_equal(state["work"][0:6, 0:6], source)


def test_the_border_weight_takes_over_a_pixel_an_earlier_block_wrote():
    "On the border the fresh texture outweighs what is written there."

    image = _texture()[:, :, 0].astype(np.float32)
    source = float(image[60, 100])
    written = np.zeros(2, np.float32)
    for index, ramp in enumerate((0, 4)):
        state = _border_state(image, ramp)
        state["work"][0:6, 0:6] = 7.0
        filled = np.ones(image.shape[:2], bool)
        tf._blend_block(state, filled, (0, 0, 6, 6, 6, 6), (60, 100))
        written[index] = state["work"][0, 0]
    # The port leaves the corner of the block to the pixels it wrote
    # before, see the block feather; the feather of the region hands
    # that corner to the fresh texture instead.
    assert abs(written[0] - source) > abs(written[1] - source)
    assert abs(written[1] - source) < abs(written[1] - 7.0)
