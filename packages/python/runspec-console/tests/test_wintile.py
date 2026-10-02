"""Layout maths for tiling several launched terminal windows (wintile)."""

from __future__ import annotations

import sys

import pytest

from runspec_console import wintile
from runspec_console.wintile import grid_shape, tile_rects

AREA = (0, 0, 1920, 1040)


@pytest.mark.parametrize(
    "n,shape",
    [
        (1, (1, 1)),
        (2, (2, 1)),
        (3, (2, 2)),
        (4, (2, 2)),
        (5, (3, 2)),
        (9, (3, 3)),
        (10, (4, 3)),
    ],
)
def test_grid_shape(n, shape):
    assert grid_shape(n) == shape


def test_zero_windows():
    assert tile_rects(0, AREA) == []


def test_single_window_fills_area():
    assert tile_rects(1, AREA) == [AREA]


def test_grid_covers_area_without_overlap():
    rects = tile_rects(4, AREA)
    assert rects == [
        (0, 0, 960, 520),
        (960, 0, 960, 520),
        (0, 520, 960, 520),
        (960, 520, 960, 520),
    ]


def test_short_last_row_stretches():
    rects = tile_rects(3, AREA)
    assert rects[2] == (0, 520, 1920, 520)


def test_grid_honours_area_offset_and_remainders():
    area = (100, 50, 1001, 701)
    rects = tile_rects(2, area, min_w=100, min_h=100)
    assert rects[0][0] == 100 and rects[0][1] == 50
    # right edge of the last cell lands exactly on the area's right edge
    x, _, w, h = rects[1]
    assert x + w == 1101 and h == 701


def test_too_many_for_grid_cascades():
    rects = tile_rects(30, AREA)
    assert len(rects) == 30
    x0, y0, w, h = rects[0]
    assert (x0, y0) == (0, 0) and w == 960 and h == 520
    assert rects[1][:2] == (wintile.CASCADE_STEP, wintile.CASCADE_STEP)
    # every window stays inside the area
    for x, y, rw, rh in rects:
        assert x + rw <= 1920 and y + rh <= 1040


def test_tiling_is_a_noop_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert wintile.tile_process_windows([1, 2]) == 0


ULTRAWIDE = (0, 0, 5120, 1400)


def test_columns_put_windows_side_by_side():
    rects = tile_rects(4, ULTRAWIDE, layout="columns")
    assert rects == [
        (0, 0, 1280, 1400),
        (1280, 0, 1280, 1400),
        (2560, 0, 1280, 1400),
        (3840, 0, 1280, 1400),
    ]


def test_rows_stack_windows_full_width():
    rects = tile_rects(3, AREA, layout="rows")
    assert [r[1] for r in rects] == [0, 346, 692]
    assert all(r[0] == 0 and r[2] == 1920 for r in rects)
    assert rects[2][1] + rects[2][3] == 1040


def test_columns_too_narrow_fall_back_to_grid():
    assert tile_rects(6, AREA, layout="columns") == tile_rects(6, AREA)


def test_rows_too_short_fall_back_to_grid():
    assert tile_rects(5, AREA, layout="rows") == tile_rects(5, AREA)


def test_cascade_layout_cascades_even_when_a_grid_fits():
    rects = tile_rects(3, AREA, layout="cascade")
    assert [r[:2] for r in rects] == [(0, 0), (32, 32), (64, 64)]


def test_unknown_layout_is_a_grid():
    assert tile_rects(4, AREA, layout="spiral") == tile_rects(4, AREA)


def test_compact_packs_fixed_size_windows_top_left():
    rects = tile_rects(4, ULTRAWIDE, layout="compact")
    w, h = wintile.COMPACT_W, wintile.COMPACT_H
    assert rects == [(0, 0, w, h), (w, 0, w, h), (0, h, w, h), (w, h, w, h)]


def test_compact_is_the_same_size_on_any_monitor_shape():
    assert tile_rects(4, ULTRAWIDE, layout="compact") == tile_rects(
        4, AREA, layout="compact"
    )


def test_compact_scales_with_dpi_and_honours_offset():
    rects = tile_rects(2, (100, 50, 5120, 1400), layout="compact", scale=1.5)
    assert rects[0] == (100, 50, 1230, 780)
    assert rects[1][0] == 100 + 1230


def test_compact_shrinks_to_fit_then_cascades():
    rects = tile_rects(9, AREA, layout="compact")
    assert all(r[2] == 640 and r[3] == 346 for r in rects)
    assert tile_rects(30, AREA, layout="compact") == tile_rects(
        30, AREA, layout="cascade"
    )
