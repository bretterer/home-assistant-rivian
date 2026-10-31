"""Unit tests for the pure-Python road heat map module."""

from __future__ import annotations

import math
import time
import zlib

import pytest

from custom_components.rivian.drive_track import DriveTrack, TrackPoint
from custom_components.rivian.road_heat import (
    BASE_LEVEL,
    CORRIDOR_RADIUS,
    GLITCH_MIN_DISTANCE_M,
    GLITCH_MIN_SPEED_MPS,
    GRID_FORMAT_VERSION,
    HEAT_FORMAT_VERSION,
    MAX_MERCATOR_LAT,
    REPASS_MIN_DISTANCE_M,
    REPASS_MIN_GAP_S,
    TILE_CELL_PX,
    HeatGrid,
    RoadHeat,
    cell_key,
    split_key,
    track_cells,
    track_passes,
    world_xy,
)

_MAX_COORD = 2**BASE_LEVEL - 1


def _two_point_track(
    lat0: float, lon0: float, lat1: float, lon1: float, dt: float = 60.0
) -> DriveTrack:
    """Build a minimal two-point track for exercising a single segment."""
    track = DriveTrack()
    track.append(TrackPoint(t=1_700_000_000.0, lat=lat0, lon=lon0))
    track.append(TrackPoint(t=1_700_000_000.0 + dt, lat=lat1, lon=lon1))
    return track


def _make_realistic_track(n: int, *, start_t: float = 1_700_000_000.0) -> DriveTrack:
    """Build a curved, southward/westward track (mirrors test_drive_track.py)."""
    track = DriveTrack()
    lat, lon = 40.0, -105.0
    for i in range(n):
        lat -= 0.0003 + 0.00005 * math.sin(i / 7.0)
        lon -= 0.0002 + 0.00004 * math.cos(i / 11.0)
        track.append(TrackPoint(t=start_t + i * 5.0, lat=lat, lon=lon))
    return track


def _point_segment_distance(
    px: float, py: float, x0: float, y0: float, x1: float, y1: float
) -> float:
    """Distance (in cell units) from (px, py) to segment (x0,y0)-(x1,y1)."""
    dx, dy = x1 - x0, y1 - y0
    seg_len2 = dx * dx + dy * dy
    if seg_len2 == 0.0:
        return math.hypot(px - x0, py - y0)
    t = max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / seg_len2))
    return math.hypot(px - (x0 + t * dx), py - (y0 + t * dy))


def _brute_force_cells(
    x0: float, y0: float, x1: float, y1: float, steps_per_cell: int = 50
) -> set[tuple[int, int]]:
    """Densely sample the segment and floor each sample to its cell."""
    dist_cells = max(1.0, math.hypot(x1 - x0, y1 - y0))
    n_samples = max(2, int(dist_cells * steps_per_cell))
    cells = set()
    for i in range(n_samples + 1):
        t = i / n_samples
        cells.add((math.floor(x0 + (x1 - x0) * t), math.floor(y0 + (y1 - y0) * t)))
    return cells


def _cells_4connected(
    cells: set[tuple[int, int]], start: tuple[int, int], end: tuple[int, int]
) -> bool:
    """BFS within `cells` using only 4-neighbour moves; True if start reaches end
    and every cell in `cells` is part of that one connected component.
    """
    if start not in cells or end not in cells:
        return False
    seen = {start}
    frontier = [start]
    while frontier:
        cx, cy = frontier.pop()
        for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
            if (nx, ny) in cells and (nx, ny) not in seen:
                seen.add((nx, ny))
                frontier.append((nx, ny))
    return end in seen and seen == cells


def _segment_cells(
    lat0, lon0, lat1, lon1, dt=60.0
) -> tuple[set[tuple[int, int]], tuple, tuple]:
    """Run track_cells on a 2-point track and return (cells, xy0, xy1)."""
    track = _two_point_track(lat0, lon0, lat1, lon1, dt=dt)
    keys = track_cells(track)
    cells = {split_key(k) for k in keys}
    xy0 = world_xy(lat0, lon0)
    xy1 = world_xy(lat1, lon1)
    return cells, xy0, xy1


class TestWorldXY:
    def test_center_of_the_world(self) -> None:
        assert world_xy(0.0, 0.0, level=2) == pytest.approx((2.0, 2.0))

    def test_west_edge(self) -> None:
        x, _ = world_xy(0.0, -180.0, level=5)
        assert x == pytest.approx(0.0, abs=1e-9)

    def test_east_edge(self) -> None:
        x, _ = world_xy(0.0, 180.0, level=5)
        assert x == pytest.approx(2**5, abs=1e-9)

    def test_latitude_is_clamped(self) -> None:
        assert world_xy(90.0, 10.0) == world_xy(MAX_MERCATOR_LAT, 10.0)
        assert world_xy(-90.0, 10.0) == world_xy(-MAX_MERCATOR_LAT, 10.0)

    def test_y_increases_southward(self) -> None:
        _, y_north = world_xy(10.0, 0.0)
        _, y_south = world_xy(-10.0, 0.0)
        assert y_north < y_south

    def test_chicago_matches_standard_xyz_tile_formula(self) -> None:
        x, y = world_xy(41.8781, -87.6298, level=10)
        assert math.floor(x) == 262
        assert math.floor(y) == 380


class TestCellKey:
    @pytest.mark.parametrize(
        "x,y",
        [
            (0, 0),
            (1, 1),
            (12345, 67890),
            (_MAX_COORD, _MAX_COORD),
            (_MAX_COORD, 0),
            (0, _MAX_COORD),
        ],
    )
    def test_round_trip(self, x: int, y: int) -> None:
        assert split_key(cell_key(x, y)) == (x, y)


class TestTrackCells:
    def test_empty_track(self) -> None:
        assert track_cells(DriveTrack()) == set()

    def test_single_point_track(self) -> None:
        track = DriveTrack()
        track.append(TrackPoint(t=1.0, lat=40.0, lon=-105.0))
        x, y = world_xy(40.0, -105.0)
        expected = cell_key(math.floor(x), math.floor(y))
        assert track_cells(track) == {expected}

    def test_zero_length_piece(self) -> None:
        track = DriveTrack()
        track.append(TrackPoint(t=1.0, lat=40.0, lon=-105.0))
        track.append(TrackPoint(t=2.0, lat=40.0, lon=-105.0000001))
        # Effectively zero-length at this precision/level; must not crash and
        # must at least contain the shared cell.
        cells = track_cells(track)
        x, y = world_xy(40.0, -105.0)
        assert cell_key(math.floor(x), math.floor(y)) in cells

    @pytest.mark.parametrize(
        "lat0,lon0,lat1,lon1",
        [
            (40.0, -105.0, 40.0, -104.997),  # horizontal
            (40.0, -105.0, 40.003, -105.0),  # vertical
            (40.0, -105.0, 40.0021, -104.9979),  # diagonal-ish, non-tie ratio
            (40.0, -105.0, 40.0035, -104.9992),  # steep
        ],
    )
    def test_traversal_covers_line_without_straying(
        self, lat0, lon0, lat1, lon1
    ) -> None:
        cells, (x0, y0), (x1, y1) = _segment_cells(lat0, lon0, lat1, lon1)
        brute = _brute_force_cells(x0, y0, x1, y1)
        # Every densely-sampled cell must be present.
        assert brute <= cells
        # No returned cell may be far from the line.
        for cx, cy in cells:
            dist = _point_segment_distance(cx + 0.5, cy + 0.5, x0, y0, x1, y1)
            assert dist <= 2.0
        # The path must be 4-connected end to end (no gaps, no stray corners).
        start = (math.floor(x0), math.floor(y0))
        end = (math.floor(x1), math.floor(y1))
        assert _cells_4connected(cells, start, end)

    def test_150m_jump_at_40n_has_no_gaps(self) -> None:
        # ~150 m eastward at ~40 deg N (~0.00175 deg longitude).
        cells, (x0, y0), (x1, y1) = _segment_cells(40.0, -105.0, 40.0, -104.99825)
        start = (math.floor(x0), math.floor(y0))
        end = (math.floor(x1), math.floor(y1))
        assert start != end
        assert _cells_4connected(cells, start, end)


class TestGlitchHandling:
    def test_large_fast_jump_is_skipped_but_endpoint_kept(self) -> None:
        lat0, lon0 = 40.0, -105.0
        lat1, lon1 = 40.18, -105.0  # ~20 km north
        cells, xy0, xy1 = _segment_cells(lat0, lon0, lat1, lon1, dt=10.0)
        start = (math.floor(xy0[0]), math.floor(xy0[1]))
        end = (math.floor(xy1[0]), math.floor(xy1[1]))
        assert start in cells
        assert end in cells
        # Skipped piece: only the two endpoint cells, no filled-in path.
        assert not _cells_4connected(cells, start, end) or len(cells) == 2
        assert len(cells) == 2

    def test_slow_long_thinned_piece_is_drawn(self) -> None:
        lat0, lon0 = 40.0, -105.0
        lat1, lon1 = 40.09, -105.0  # ~10 km north
        cells, xy0, xy1 = _segment_cells(lat0, lon0, lat1, lon1, dt=360.0)
        start = (math.floor(xy0[0]), math.floor(xy0[1]))
        end = (math.floor(xy1[0]), math.floor(xy1[1]))
        assert len(cells) > 2
        assert _cells_4connected(cells, start, end)

    def test_non_positive_dt_glitch_uses_distance_alone(self) -> None:
        track = DriveTrack()
        # Bypass DriveTrack.append's monotonic-time guard to exercise dt<=0.
        track.points = [
            TrackPoint(t=100.0, lat=40.0, lon=-105.0),
            TrackPoint(t=100.0, lat=40.2, lon=-105.0),  # ~22 km, dt == 0
        ]
        cells = {split_key(k) for k in track_cells(track)}
        x0, y0 = world_xy(40.0, -105.0)
        x1, y1 = world_xy(40.2, -105.0)
        start = (math.floor(x0), math.floor(y0))
        end = (math.floor(x1), math.floor(y1))
        assert cells == {start, end}

    def test_glitch_distance_and_speed_thresholds(self) -> None:
        assert GLITCH_MIN_DISTANCE_M == 5000.0
        assert GLITCH_MIN_SPEED_MPS == 70.0


class TestTrackCellsDeduplication:
    def test_loop_yields_each_cell_once(self) -> None:
        track = DriveTrack()
        lat, lon = 40.0, -105.0
        forward: list[TrackPoint] = []
        t = 1_700_000_000.0
        for i in range(20):
            lat -= 0.0002
            t += 5.0
            forward.append(TrackPoint(t=t, lat=lat, lon=lon))
        for p in forward:
            track.append(p)
        # Retrace the same road back to the start.
        for p in reversed(forward[:-1]):
            t += 5.0
            track.append(TrackPoint(t=t, lat=p.lat, lon=p.lon))
        cells = track_cells(track)
        # A set by construction, but confirm no accounting inflates counts.
        assert len(cells) == len({split_key(k) for k in cells})


class TestHeatGridBasics:
    def test_empty(self) -> None:
        grid = HeatGrid.empty()
        assert len(grid) == 0
        assert grid.to_counts() == {}

    def test_from_counts_drops_non_positive(self) -> None:
        grid = HeatGrid.from_counts({1: 3, 2: 0, 3: -5, 4: 1})
        assert grid.to_counts() == {1: 3, 4: 1}

    def test_to_counts_round_trip(self) -> None:
        counts = {cell_key(5, 5): 2, cell_key(10, 1): 7, cell_key(1, 1): 1}
        grid = HeatGrid.from_counts(counts)
        assert grid.to_counts() == counts
        assert len(grid) == 3

    def test_add_drive_dedups_within_one_call(self) -> None:
        grid = HeatGrid.empty()
        cells = [cell_key(1, 1), cell_key(1, 1), cell_key(2, 2)]
        grid = grid.add_drive(cells)
        assert grid.to_counts() == {cell_key(1, 1): 1, cell_key(2, 2): 1}

    def test_add_drive_twice_gives_two(self) -> None:
        grid = HeatGrid.empty()
        cells = {cell_key(1, 1), cell_key(2, 2)}
        grid = grid.add_drive(cells).add_drive(cells)
        assert grid.to_counts() == {cell_key(1, 1): 2, cell_key(2, 2): 2}

    def test_add_drive_returns_new_grid(self) -> None:
        grid = HeatGrid.empty()
        grid2 = grid.add_drive([cell_key(1, 1)])
        assert len(grid) == 0
        assert len(grid2) == 1


class TestMerge:
    def test_merge_sums_per_cell(self) -> None:
        g1 = HeatGrid.from_counts({1: 3, 2: 5})
        g2 = HeatGrid.from_counts({2: 2, 3: 1})
        merged = HeatGrid.merge([g1, g2])
        assert merged.to_counts() == {1: 3, 2: 7, 3: 1}

    def test_merge_with_empty_is_identity(self) -> None:
        g1 = HeatGrid.from_counts({1: 3, 2: 5})
        merged = HeatGrid.merge([g1, HeatGrid.empty()])
        assert merged.to_counts() == g1.to_counts()

    def test_merge_no_grids(self) -> None:
        assert HeatGrid.merge([]).to_counts() == {}


class TestEncodeDecode:
    def test_round_trip(self) -> None:
        counts = {cell_key(5, 5): 2, cell_key(10, 1): 7, cell_key(1_000_000, 1): 40}
        grid = HeatGrid.from_counts(counts)
        decoded = HeatGrid.decode(grid.encode())
        assert decoded.to_counts() == counts

    def test_round_trip_empty(self) -> None:
        decoded = HeatGrid.decode(HeatGrid.empty().encode())
        assert decoded.to_counts() == {}

    def test_decode_rejects_wrong_version(self) -> None:
        payload = zlib.compress(b'{"v":999,"level":21,"k":[1],"c":[1]}')
        with pytest.raises(ValueError, match="version"):
            HeatGrid.decode(payload)

    def test_decode_rejects_wrong_level(self) -> None:
        payload = zlib.compress(
            f'{{"v":{GRID_FORMAT_VERSION},"level":20,"k":[1],"c":[1]}}'.encode()
        )
        with pytest.raises(ValueError, match="level"):
            HeatGrid.decode(payload)

    def test_decode_rejects_corrupt_bytes(self) -> None:
        with pytest.raises(ValueError):
            HeatGrid.decode(b"not zlib data at all")

    def test_decode_rejects_non_increasing_keys(self) -> None:
        payload = zlib.compress(
            f'{{"v":{GRID_FORMAT_VERSION},"level":{BASE_LEVEL},"k":[100,-5,5],"c":[1,1,1]}}'.encode()
        )
        with pytest.raises(ValueError, match="increasing"):
            HeatGrid.decode(payload)

    def test_decode_rejects_mismatched_lengths(self) -> None:
        payload = zlib.compress(
            f'{{"v":{GRID_FORMAT_VERSION},"level":{BASE_LEVEL},"k":[1,2],"c":[1]}}'.encode()
        )
        with pytest.raises(ValueError):
            HeatGrid.decode(payload)


class TestBbox:
    def test_empty_grid_has_no_bbox(self) -> None:
        assert HeatGrid.empty().bbox() is None

    def test_bbox_matches_outer_cell_edges(self) -> None:
        n = float(2**BASE_LEVEL)
        x_lo, x_hi = 100_000, 101_000
        y_lo, y_hi = 200_000, 200_005
        grid = HeatGrid.from_counts({cell_key(x_lo, y_lo): 1, cell_key(x_hi, y_hi): 1})
        south, west, north, east = grid.bbox()

        expected_west = x_lo / n * 360.0 - 180.0
        expected_east = (x_hi + 1) / n * 360.0 - 180.0

        def _y_to_lat(y: float) -> float:
            return math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))

        expected_north = _y_to_lat(y_lo)
        expected_south = _y_to_lat(y_hi + 1)

        assert west == pytest.approx(expected_west)
        assert east == pytest.approx(expected_east)
        assert north == pytest.approx(expected_north)
        assert south == pytest.approx(expected_south)
        assert south < north


class TestScaleMax:
    def test_nearest_rank_p98(self) -> None:
        grid = HeatGrid.from_counts({i: i for i in range(1, 101)})
        assert grid.scale_max(percentile=0.98, floor=1) == 98

    def test_floor_applies(self) -> None:
        grid = HeatGrid.from_counts({1: 1, 2: 1, 3: 1})
        assert grid.scale_max(percentile=0.98, floor=5) == 5

    def test_empty_grid_returns_floor(self) -> None:
        assert HeatGrid.empty().scale_max() == 2
        assert HeatGrid.empty().scale_max(floor=9) == 9


def _east_west_track(
    legs: list[tuple[float, float]], *, speed_mps: float = 15.0, dt: float = 5.0
) -> DriveTrack:
    """A drive along latitude 39.7242 through the given (from_lon, to_lon) legs.

    Consecutive legs are driven back to back at ``speed_mps`` with a fix
    every ``dt`` seconds (e.g. out along a road, then back).
    """
    lat = 39.7242
    deg_per_m = 1.0 / (111_320.0 * math.cos(math.radians(lat)))
    track = DriveTrack()
    t = 1_700_000_000.0
    for lon0, lon1 in legs:
        step = speed_mps * dt * deg_per_m * (1 if lon1 >= lon0 else -1)
        n = max(1, round(abs(lon1 - lon0) / abs(step)))
        for i in range(n + 1):
            if track.points and i == 0:
                continue
            track.append(TrackPoint(t=t, lat=lat, lon=lon0 + (lon1 - lon0) * i / n))
            t += dt
    return track


class TestTrackPasses:
    def test_one_way_drive_counts_one_pass_near_and_through(self) -> None:
        near, through = track_passes(_east_west_track([(-104.9880, -104.9680)]))
        assert set(through.values()) == {1}
        assert set(near.values()) == {1}
        assert set(through) <= set(near)

    def test_out_and_back_counts_twice_except_near_the_turnaround(self) -> None:
        # ~1.6 km out and straight back at 15 m/s.
        near, through = track_passes(
            _east_west_track([(-104.9880, -104.9680), (-104.9680, -104.9880)])
        )
        start_cell = next(iter(track_cells(_east_west_track([(-104.9880, -104.9880)]))))
        assert through[start_cell] == 2
        assert near[start_cell] == 2
        # Right at the turnaround the return comes back within seconds and
        # metres: still one pass there.
        turn_cell = next(iter(track_cells(_east_west_track([(-104.9680, -104.9680)]))))
        assert through[turn_cell] == 1

    def test_long_stop_with_no_fixes_does_not_count_twice(self) -> None:
        # Stopped ten minutes in one spot with no fixes (sparse history),
        # then carries on: same cell, no distance driven -> one pass.
        track = DriveTrack()
        track.append(TrackPoint(t=0.0, lat=39.7242, lon=-104.9880))
        track.append(TrackPoint(t=600.0, lat=39.7242, lon=-104.9880))
        track.append(TrackPoint(t=610.0, lat=39.7242, lon=-104.9875))
        _near, through = track_passes(track)
        assert set(through.values()) == {1}

    def test_quick_loop_back_to_a_cell_is_one_pass(self) -> None:
        # ~400 m out and back in under 30 s (a parking lot): back within the gap.
        _near, through = track_passes(
            _east_west_track(
                [(-104.9880, -104.9855), (-104.9855, -104.9880)], speed_mps=15.0, dt=2.0
            )
        )
        assert REPASS_MIN_GAP_S > 30
        assert max(through.values()) == 1

    def test_adjacent_lanes_share_their_counts_through_the_corridor(self) -> None:
        # Two drives one cell apart (e.g. the two directions of one road).
        a = _east_west_track([(-104.9880, -104.9780)])
        b = DriveTrack()
        cell_deg = 360.0 / (1 << BASE_LEVEL)
        for p in a.points:
            b.append(TrackPoint(t=p.t, lat=p.lat - cell_deg * 0.9, lon=p.lon))
        heat = RoadHeat.empty().add_drives([track_passes(a), track_passes(b)])
        shown = heat.display().to_counts()
        # Every drawn cell of either drive shows both passes...
        assert set(shown.values()) == {2}
        # ...but only cells a drive actually crossed are drawn.
        assert set(shown) == track_cells(a) | track_cells(b)

    def test_constants_are_sane(self) -> None:
        assert CORRIDOR_RADIUS == 1
        assert REPASS_MIN_DISTANCE_M >= 100


class TestRoadHeat:
    def test_add_merge_and_display(self) -> None:
        drive = track_passes(_east_west_track([(-104.9880, -104.9780)]))
        one = RoadHeat.empty().add_drives([drive])
        two = RoadHeat.merge([one, one])
        assert set(two.display().to_counts().values()) == {2}
        assert len(two) == len(one)
        assert (
            two.drawn_cells
            == one.drawn_cells
            == len(track_cells(_east_west_track([(-104.9880, -104.9780)])))
        )

    def test_encode_decode_round_trip(self) -> None:
        drive = track_passes(
            _east_west_track([(-104.9880, -104.9680), (-104.9680, -104.9880)])
        )
        heat = RoadHeat.empty().add_drives([drive])
        decoded = RoadHeat.decode(heat.encode())
        assert decoded.to_counts() == heat.to_counts()
        assert RoadHeat.decode(RoadHeat.empty().encode()).to_counts() == ({}, {})

    def test_decodes_a_v1_grid_as_near_equals_through(self) -> None:
        grid = HeatGrid.from_counts({cell_key(5, 6): 3, cell_key(5, 7): 1})
        heat = RoadHeat.decode(grid.encode())
        assert heat.to_counts() == (grid.to_counts(), grid.to_counts())
        assert heat.display().to_counts() == grid.to_counts()

    def test_decode_rejects_bad_data(self) -> None:
        with pytest.raises(ValueError):
            RoadHeat.decode(b"not zlib")
        with pytest.raises(ValueError):
            RoadHeat.decode(zlib.compress(b'{"v":99}'))
        bad_counts = (
            f'{{"v":{HEAT_FORMAT_VERSION},"level":{BASE_LEVEL},'
            '"k":[1,1],"n":[0,1],"t":[0,0]}'
        )
        with pytest.raises(ValueError):
            RoadHeat.decode(zlib.compress(bad_counts.encode()))


class TestTile:
    def test_margin_adds_a_ring_of_neighbouring_cells(self) -> None:
        # Cells just left of and just inside a z=15 tile, at display level 21.
        z, tx, ty = 15, 5000, 6000
        shift = BASE_LEVEL - z
        x0, y0 = tx << shift, ty << shift
        grid = HeatGrid.from_counts(
            {cell_key(x0 - 1, y0 + 3): 4, cell_key(x0, y0 + 3): 2}
        )
        plain = grid.tile(z, tx, ty)
        padded = grid.tile(z, tx, ty, margin=1)
        assert plain["cells"] == [[0, 3, 2]]
        assert sorted(padded["cells"]) == [[-1, 3, 4], [0, 3, 2]]
        assert padded["size"] == plain["size"]

    def test_invalid_coordinates_raise(self) -> None:
        grid = HeatGrid.empty()
        with pytest.raises(ValueError):
            grid.tile(5, 32, 0)  # x out of range (0..31)
        with pytest.raises(ValueError):
            grid.tile(5, -1, 0)
        with pytest.raises(ValueError):
            grid.tile(5, 0, 32)

    def test_tile_far_from_data_is_empty(self) -> None:
        grid = HeatGrid.from_counts({cell_key(0, 0): 5})
        result = grid.tile(10, 1000, 1000)
        assert result["cells"] == []

    def test_coarsening_takes_max_not_sum(self) -> None:
        key1 = cell_key(1000, 2000)
        key2 = cell_key(1001, 2000)
        grid = HeatGrid.from_counts({key1: 3, key2: 5})
        z = 10
        shift_tile = BASE_LEVEL - z
        tx, ty = 1000 >> shift_tile, 2000 >> shift_tile
        result = grid.tile(z, tx, ty)
        assert len(result["cells"]) == 1
        assert result["cells"][0][2] == 5

    @pytest.mark.parametrize("z", [10, 13, 15, 17])
    def test_tile_union_reproduces_coarsened_grid(self, z: int) -> None:
        track = _make_realistic_track(200)
        cells = track_cells(track)
        grid = HeatGrid.empty().add_drive(cells)

        level = min(BASE_LEVEL, round(z + 8 - math.log2(TILE_CELL_PX)))
        shift = BASE_LEVEL - level
        reference: dict[tuple[int, int], int] = {}
        for key, count in grid.to_counts().items():
            bx, by = split_key(key)
            display = (bx >> shift, by >> shift)
            reference[display] = max(reference.get(display, 0), count)

        shift_tile = BASE_LEVEL - z
        covering_tiles = {
            (bx >> shift_tile, by >> shift_tile)
            for key in grid.to_counts()
            for bx, by in [split_key(key)]
        }

        union: dict[tuple[int, int], int] = {}
        for tx, ty in covering_tiles:
            result = grid.tile(z, tx, ty)
            assert result["level"] == level
            size = result["size"]
            for dx, dy, count in result["cells"]:
                assert 0 <= dx < size
                assert 0 <= dy < size
                union[(tx * size + dx, ty * size + dy)] = count

        assert union == reference


class TestPerformance:
    def test_rasterizing_a_synthetic_two_hour_drive_is_fast(self) -> None:
        track = DriveTrack()
        lat, lon = 39.0, -104.0
        t = 1_700_000_000.0
        n = 1440
        for i in range(n):
            # ~104 m/step average, gently curving, spans roughly 150 km total.
            lat -= 0.00090 + 0.00005 * math.sin(i / 30.0)
            lon += 0.00060 + 0.00004 * math.cos(i / 23.0)
            t += 5.0
            track.append(TrackPoint(t=t, lat=lat, lon=lon))

        started = time.monotonic()
        cells = track_cells(track)
        elapsed = time.monotonic() - started

        assert cells
        assert elapsed < 5.0  # generous bound; measured well under 2 s locally
