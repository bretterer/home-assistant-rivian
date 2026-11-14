"""Road heat map: per-cell drive-count grids over a Web Mercator tile grid.

Pure Python (stdlib only, no Home Assistant imports), matching the style of
``drive_track.py`` so it can be shared by the drive tracker, the SQLite
storage layer, and the frontend's tile requests.

A :class:`RoadHeat` is what gets stored: for each base-level (``BASE_LEVEL``)
Web Mercator cell, how many passes went *near* it (within ``CORRIDOR_RADIUS``
cells) and how many went *through* it (see :func:`track_passes`). GPS drift
and a road's two directions of travel put the passes of one road in
neighbouring cells (~14 m wide at 44°N), so a plain per-cell count shows a
road driven 20 times as a mix of 8s, 12s and 20s. Coloring each crossed cell
by its "near" count shows every pass of that road, while drawing only the
crossed cells keeps roads their true width.

Counts are per drive, so heat for disjoint drives (e.g. one per month) sums
exactly with :meth:`RoadHeat.merge` into a year or all time.
:meth:`RoadHeat.display` gives the :class:`HeatGrid` (one count per drawn
cell) whose :meth:`HeatGrid.tile` serves one XYZ tile, coarsened for display.
"""

from __future__ import annotations

from array import array
import bisect
from collections.abc import Iterable, Iterator
import json
import math
from typing import Any, Final
import zlib

from .drive_track import DriveTrack, haversine_m

BASE_LEVEL: Final[int] = 21  # 2**21 cells across the world (~19 m at the equator)
GRID_FORMAT_VERSION: Final[int] = 1  # HeatGrid.encode: one count per cell
HEAT_FORMAT_VERSION: Final[int] = 2  # RoadHeat.encode: near/through counts per cell
# Cells either side of a drive's path that still count it (1 ≈ 14 m at 44°N).
# Measured on real drives, 2 found only ~1% more drives than 1.
CORRIDOR_RADIUS: Final[int] = 1
# A drive passing a cell again counts again only after being away this long
# and this far (see track_passes).
REPASS_MIN_GAP_S: Final[float] = 120.0
REPASS_MIN_DISTANCE_M: Final[float] = 200.0
GLITCH_MIN_DISTANCE_M: Final[float] = 5000.0
GLITCH_MIN_SPEED_MPS: Final[float] = 70.0
MAX_MERCATOR_LAT: Final[float] = 85.05112878
TILE_CELL_PX: Final[int] = 4  # target on-screen cell size in a 256 px tile

_BASE_CELLS: Final[int] = 1 << BASE_LEVEL
_Y_MASK: Final[int] = _BASE_CELLS - 1


def world_xy(lat: float, lon: float, level: int = BASE_LEVEL) -> tuple[float, float]:
    """Return fractional Web Mercator cell coordinates at `level`.

    x is east (0..2**level), y is south (0..2**level), matching the standard
    XYZ tile scheme. Latitude is clamped to +/-MAX_MERCATOR_LAT before
    projecting.
    """
    lat = max(-MAX_MERCATOR_LAT, min(MAX_MERCATOR_LAT, lat))
    n = 2.0**level
    x = (lon + 180.0) / 360.0 * n
    lat_rad = math.radians(lat)
    y = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n
    return x, y


def cell_key(x: int, y: int) -> int:
    """Pack base-level cell coordinates into a single sortable integer key."""
    return (x << BASE_LEVEL) | y


def split_key(key: int) -> tuple[int, int]:
    """Inverse of cell_key()."""
    return key >> BASE_LEVEL, key & _Y_MASK


def _clamp_cell(value: float, limit: int) -> int:
    """Floor `value` to an integer cell index, clamped to [0, limit)."""
    cell = math.floor(value)
    if cell < 0:
        return 0
    if cell >= limit:
        return limit - 1
    return cell


def _voxel_traverse(
    x0: float, y0: float, x1: float, y1: float
) -> list[tuple[int, int, float]]:
    """Return the cells the segment (x0,y0)-(x1,y1) crosses, in order.

    Each item is ``(cx, cy, f)`` where ``f`` (0..1) is how far along the
    segment the line enters that cell. Exact grid traversal (Amanatides &
    Woo) on fractional world_xy coordinates; handles horizontal, vertical,
    zero-length and exact-boundary cases without looping forever.
    """
    cx = _clamp_cell(x0, _BASE_CELLS)
    cy = _clamp_cell(y0, _BASE_CELLS)
    end_cx = _clamp_cell(x1, _BASE_CELLS)
    end_cy = _clamp_cell(y1, _BASE_CELLS)

    cells: list[tuple[int, int, float]] = [(cx, cy, 0.0)]
    if cx == end_cx and cy == end_cy:
        return cells

    dx = x1 - x0
    dy = y1 - y0

    if dx > 0:
        step_x = 1
        t_max_x = ((cx + 1) - x0) / dx
        t_delta_x = 1.0 / dx
    elif dx < 0:
        step_x = -1
        t_max_x = (cx - x0) / dx
        t_delta_x = -1.0 / dx
    else:
        step_x = 0
        t_max_x = math.inf
        t_delta_x = math.inf

    if dy > 0:
        step_y = 1
        t_max_y = ((cy + 1) - y0) / dy
        t_delta_y = 1.0 / dy
    elif dy < 0:
        step_y = -1
        t_max_y = (cy - y0) / dy
        t_delta_y = -1.0 / dy
    else:
        step_y = 0
        t_max_y = math.inf
        t_delta_y = math.inf

    # Every step moves the current cell by exactly one neighbour (a tie moves
    # both x and y at once), so the number of steps needed is bounded by the
    # Manhattan distance between the start and end cells. Add slack for
    # float-precision edge cases and bail out rather than loop forever.
    max_steps = abs(end_cx - cx) + abs(end_cy - cy) + 4

    steps = 0
    while (cx, cy) != (end_cx, end_cy):
        steps += 1
        if steps > max_steps:
            break
        if t_max_x < t_max_y:
            entry = t_max_x
            t_max_x += t_delta_x
            cx += step_x
        elif t_max_y < t_max_x:
            entry = t_max_y
            t_max_y += t_delta_y
            cy += step_y
        else:
            entry = t_max_x
            t_max_x += t_delta_x
            t_max_y += t_delta_y
            cx += step_x
            cy += step_y
        cx = max(0, min(_BASE_CELLS - 1, cx))
        cy = max(0, min(_BASE_CELLS - 1, cy))
        cells.append((cx, cy, min(1.0, max(0.0, entry))))
    return cells


def _is_glitch(distance_m: float, dt_s: float) -> bool:
    """Whether a jump between consecutive fixes is a GPS glitch, not driving."""
    if dt_s > 0:
        return (
            distance_m > GLITCH_MIN_DISTANCE_M
            and distance_m / dt_s > GLITCH_MIN_SPEED_MPS
        )
    return distance_m > GLITCH_MIN_DISTANCE_M


def _track_visits(track: DriveTrack) -> Iterator[tuple[int, float, float]]:
    """Yield ``(cell, t, s)`` each time the drive enters a new cell, in order.

    ``t`` is the (interpolated) time the line between fixes enters the cell
    and ``s`` the distance driven so far (m). A GPS glitch (a jump over
    GLITCH_MIN_DISTANCE_M faster than GLITCH_MIN_SPEED_MPS) isn't drawn: the
    drive reappears at the far fix without crossing the cells between.
    """
    pts = track.points
    if not pts:
        return
    x_prev, y_prev = world_xy(pts[0].lat, pts[0].lon)
    prev_key = cell_key(
        _clamp_cell(x_prev, _BASE_CELLS), _clamp_cell(y_prev, _BASE_CELLS)
    )
    yield prev_key, pts[0].t, 0.0
    driven = 0.0
    for i in range(1, len(pts)):
        p0, p1 = pts[i - 1], pts[i]
        x1, y1 = world_xy(p1.lat, p1.lon)
        distance = haversine_m(p0.lat, p0.lon, p1.lat, p1.lon)
        dt = p1.t - p0.t
        if _is_glitch(distance, dt):
            key = cell_key(_clamp_cell(x1, _BASE_CELLS), _clamp_cell(y1, _BASE_CELLS))
            if key != prev_key:
                yield key, p1.t, driven
                prev_key = key
        else:
            for cx, cy, fraction in _voxel_traverse(x_prev, y_prev, x1, y1):
                key = cell_key(cx, cy)
                if key != prev_key:
                    yield key, p0.t + fraction * dt, driven + fraction * distance
                    prev_key = key
            driven += distance
        x_prev, y_prev = x1, y1


def track_cells(track: DriveTrack) -> set[int]:
    """Return the base-level cell keys this drive passed through, once each."""
    return {key for key, _t, _s in _track_visits(track)}


def track_passes(track: DriveTrack) -> tuple[dict[int, int], dict[int, int]]:
    """Count this drive's passes per cell: ``(near, through)``.

    *Through* counts passes through the cell itself; *near* counts passes
    within ``CORRIDOR_RADIUS`` cells of it. A drive that comes back to a cell
    (an out-and-back) counts again, but only after it has been away from it
    for over ``REPASS_MIN_GAP_S`` **and** driven over ``REPASS_MIN_DISTANCE_M``
    since: fixes bunched close in time, a loop around a parking lot, or a long
    stop where sparse fixes resume in the same cell never count twice.
    """
    offsets = range(-CORRIDOR_RADIUS, CORRIDOR_RADIUS + 1)
    near: dict[int, int] = {}
    through: dict[int, int] = {}
    near_seen: dict[int, tuple[float, float]] = {}
    through_seen: dict[int, tuple[float, float]] = {}

    def visit(
        cell: int,
        t: float,
        s: float,
        seen: dict[int, tuple[float, float]],
        passes: dict[int, int],
    ) -> None:
        last = seen.get(cell)
        if (
            last is None
            or t - last[0] > REPASS_MIN_GAP_S
            and s - last[1] > REPASS_MIN_DISTANCE_M
        ):
            passes[cell] = passes.get(cell, 0) + 1
        seen[cell] = (t, s)

    for key, t, s in _track_visits(track):
        visit(key, t, s, through_seen, through)
        x, y = key >> BASE_LEVEL, key & _Y_MASK
        for dx in offsets:
            cx = x + dx
            if not 0 <= cx < _BASE_CELLS:
                continue
            base = cx << BASE_LEVEL
            for dy in offsets:
                cy = y + dy
                if 0 <= cy < _BASE_CELLS:
                    visit(base | cy, t, s, near_seen, near)
    return near, through


def _y_to_lat(y: float, n: float) -> float:
    """Inverse Web Mercator: cell-y (0..n) to latitude in degrees."""
    return math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))


class HeatGrid:
    """Immutable heat grid: base-level cell keys (sorted) with drive counts."""

    __slots__ = ("_counts", "_keys")

    def __init__(self, keys: array, counts: array) -> None:
        """Initialize directly from sorted parallel key/count arrays."""
        self._keys = keys
        self._counts = counts

    @classmethod
    def empty(cls) -> HeatGrid:
        """Return a grid with no cells."""
        return cls(array("Q"), array("I"))

    @classmethod
    def from_counts(cls, counts: dict[int, int]) -> HeatGrid:
        """Build a grid from a {cell_key: count} mapping, dropping counts <= 0."""
        items = sorted((key, count) for key, count in counts.items() if count > 0)
        keys = array("Q", (key for key, _ in items))
        values = array("I", (count for _, count in items))
        return cls(keys, values)

    def to_counts(self) -> dict[int, int]:
        """Return the grid as a {cell_key: count} mapping."""
        return dict(zip(self._keys, self._counts, strict=True))

    def __len__(self) -> int:
        """Return the number of populated cells."""
        return len(self._keys)

    def add_drive(self, cells: Iterable[int]) -> HeatGrid:
        """Return a new grid with +1 on each (deduplicated) cell in `cells`."""
        counts = self.to_counts()
        for key in set(cells):
            counts[key] = counts.get(key, 0) + 1
        return HeatGrid.from_counts(counts)

    @staticmethod
    def merge(grids: Iterable[HeatGrid]) -> HeatGrid:
        """Sum several grids of disjoint drives cell-by-cell."""
        acc: dict[int, int] = {}
        for grid in grids:
            for key, count in zip(grid._keys, grid._counts, strict=True):
                acc[key] = acc.get(key, 0) + count
        return HeatGrid.from_counts(acc)

    def encode(self) -> bytes:
        """Serialize to a compact, delta-encoded, zlib-compressed JSON blob."""
        keys = list(self._keys)
        deltas: list[int] = []
        prev: int | None = None
        for key in keys:
            deltas.append(key if prev is None else key - prev)
            prev = key
        envelope: dict[str, Any] = {
            "v": GRID_FORMAT_VERSION,
            "level": BASE_LEVEL,
            "k": deltas,
            "c": list(self._counts),
        }
        payload = json.dumps(envelope, separators=(",", ":")).encode()
        return zlib.compress(payload)

    @classmethod
    def decode(cls, data: bytes) -> HeatGrid:
        """Deserialize encode()'s output.

        Raises ValueError on corrupt bytes, a mismatched version/level, or
        keys that are not strictly increasing.
        """
        try:
            payload = zlib.decompress(data)
        except zlib.error as err:
            raise ValueError(f"road_heat: invalid compressed data: {err}") from err
        try:
            obj = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as err:
            raise ValueError(f"road_heat: invalid JSON: {err}") from err
        if not isinstance(obj, dict):
            raise ValueError("road_heat: decoded payload must be an object")  # noqa: TRY004

        if obj.get("v") != GRID_FORMAT_VERSION:
            raise ValueError(f"road_heat: unsupported format version {obj.get('v')!r}")
        if obj.get("level") != BASE_LEVEL:
            raise ValueError(f"road_heat: unsupported level {obj.get('level')!r}")

        k_col = obj.get("k")
        c_col = obj.get("c")
        if not isinstance(k_col, list) or not isinstance(c_col, list):
            raise ValueError("road_heat: 'k' and 'c' must be lists")  # noqa: TRY004
        if len(k_col) != len(c_col):
            raise ValueError("road_heat: 'k' and 'c' length mismatch")

        keys: list[int] = []
        prev: int | None = None
        for delta in k_col:
            if not isinstance(delta, int):
                raise ValueError("road_heat: key deltas must be integers")  # noqa: TRY004
            key = delta if prev is None else prev + delta
            if prev is not None and key <= prev:
                raise ValueError("road_heat: keys are not strictly increasing")
            keys.append(key)
            prev = key

        for count in c_col:
            if not isinstance(count, int) or count <= 0:
                raise ValueError("road_heat: counts must be positive integers")

        return cls(array("Q", keys), array("I", c_col))

    def bbox(self) -> tuple[float, float, float, float] | None:
        """Return (south, west, north, east) degrees of the outer cell edges."""
        if not self._keys:
            return None
        xs = []
        ys = []
        for key in self._keys:
            x, y = split_key(key)
            xs.append(x)
            ys.append(y)
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        n = float(_BASE_CELLS)
        west = min_x / n * 360.0 - 180.0
        east = (max_x + 1) / n * 360.0 - 180.0
        north = _y_to_lat(min_y, n)
        south = _y_to_lat(max_y + 1, n)
        return south, west, north, east

    def scale_max(self, percentile: float = 0.98, floor: int = 2) -> int:
        """Return the nearest-rank percentile of the cell counts, at least `floor`."""
        if not self._counts:
            return floor
        counts = sorted(self._counts)
        n = len(counts)
        rank = max(1, min(n, math.ceil(percentile * n)))
        return max(counts[rank - 1], floor)

    def tile(
        self, z: int, x: int, y: int, cell_px: int = TILE_CELL_PX, margin: int = 0
    ) -> dict[str, Any]:
        """Return the display-coarsened cells inside XYZ tile (z, x, y).

        Display level L = min(BASE_LEVEL, z + 8 - log2(cell_px)). A base
        cell's count is folded into its display cell by MAX (never sum), so
        one drive crossing several fine cells still counts once. ``margin``
        adds that many display cells around the tile (offsets -margin ..
        size-1+margin), so a renderer joining neighbouring cells can continue
        its lines across tile edges.
        """
        n_tiles = 1 << z if z >= 0 else 0
        if not (0 <= x < n_tiles) or not (0 <= y < n_tiles):
            raise ValueError(f"road_heat: tile ({z},{x},{y}) out of range")

        level = min(BASE_LEVEL, round(z + 8 - math.log2(cell_px)))
        level = max(level, 0)
        shift_display = BASE_LEVEL - level

        if z <= BASE_LEVEL:
            shift = BASE_LEVEL - z
            x_lo, x_hi = x << shift, (x + 1) << shift
            y_lo, y_hi = y << shift, (y + 1) << shift
        else:
            shift = z - BASE_LEVEL
            x_lo = x >> shift
            x_hi = x_lo + 1
            y_lo = y >> shift
            y_hi = y_lo + 1

        size = (1 << (level - z)) if level >= z else 1
        tile_origin_x = x_lo >> shift_display
        tile_origin_y = y_lo >> shift_display
        if margin:
            pad = margin << shift_display
            x_lo, x_hi = max(0, x_lo - pad), min(_BASE_CELLS, x_hi + pad)
            y_lo, y_hi = max(0, y_lo - pad), min(_BASE_CELLS, y_hi + pad)

        result: dict[tuple[int, int], int] = {}
        if self._keys:
            key_lo = x_lo << BASE_LEVEL
            key_hi = x_hi << BASE_LEVEL
            lo_idx = bisect.bisect_left(self._keys, key_lo)
            hi_idx = bisect.bisect_left(self._keys, key_hi)
            for i in range(lo_idx, hi_idx):
                key = self._keys[i]
                bx, by = split_key(key)
                if by < y_lo or by >= y_hi:
                    continue
                count = self._counts[i]
                dx = (bx >> shift_display) - tile_origin_x
                dy = (by >> shift_display) - tile_origin_y
                existing = result.get((dx, dy))
                if existing is None or count > existing:
                    result[(dx, dy)] = count

        cells = [[dx, dy, count] for (dx, dy), count in result.items()]
        cells.sort(key=lambda c: (c[1], c[0]))
        return {"level": level, "size": size, "cells": cells}


class RoadHeat:
    """Stored heat for a set of drives: per cell, passes near it and through it.

    Counts come from ``track_passes``: passes within ``CORRIDOR_RADIUS`` of a
    cell (*near*) and passes through it (*through*). Cells that were only
    near a drive are kept, because a later drive may pass through them.
    """

    __slots__ = ("_keys", "_near", "_through")

    def __init__(self, keys: array, near: array, through: array) -> None:
        """Initialize from sorted parallel key/near/through arrays."""
        self._keys = keys
        self._near = near
        self._through = through

    @classmethod
    def empty(cls) -> RoadHeat:
        """Return heat with no cells."""
        return cls(array("Q"), array("I"), array("I"))

    @classmethod
    def from_counts(cls, near: dict[int, int], through: dict[int, int]) -> RoadHeat:
        """Build from {cell: count} maps; cells with no "near" drives are dropped."""
        keys = sorted(key for key, count in near.items() if count > 0)
        return cls(
            array("Q", keys),
            array("I", (near[key] for key in keys)),
            array("I", (through.get(key, 0) for key in keys)),
        )

    def to_counts(self) -> tuple[dict[int, int], dict[int, int]]:
        """Return ({cell: near}, {cell: through}); through omits zero counts."""
        near = dict(zip(self._keys, self._near, strict=True))
        through = {
            key: count
            for key, count in zip(self._keys, self._through, strict=True)
            if count
        }
        return near, through

    def __len__(self) -> int:
        """Return the number of stored cells (drawn or not)."""
        return len(self._keys)

    @property
    def drawn_cells(self) -> int:
        """Return how many cells some drive passed through."""
        return sum(1 for count in self._through if count)

    def add_drives(
        self, drives: Iterable[tuple[dict[int, int], dict[int, int]]]
    ) -> RoadHeat:
        """Return new heat with each drive's ``track_passes`` result added."""
        near, through = self.to_counts()
        for drive_near, drive_through in drives:
            for key, count in drive_near.items():
                near[key] = near.get(key, 0) + count
            for key, count in drive_through.items():
                through[key] = through.get(key, 0) + count
        return RoadHeat.from_counts(near, through)

    @staticmethod
    def merge(heats: Iterable[RoadHeat]) -> RoadHeat:
        """Sum heat of disjoint drive sets cell by cell."""
        near: dict[int, int] = {}
        through: dict[int, int] = {}
        for heat in heats:
            for key, n, t in zip(heat._keys, heat._near, heat._through, strict=True):
                near[key] = near.get(key, 0) + n
                if t:
                    through[key] = through.get(key, 0) + t
        return RoadHeat.from_counts(near, through)

    def display(self) -> HeatGrid:
        """Return the drawn cells (passed through), each counting the passes near it."""
        keys = array("Q")
        counts = array("I")
        for key, n, t in zip(self._keys, self._near, self._through, strict=True):
            if t:
                keys.append(key)
                counts.append(n)
        return HeatGrid(keys, counts)

    def encode(self) -> bytes:
        """Serialize to a delta-encoded, zlib-compressed JSON blob."""
        deltas: list[int] = []
        prev = 0
        for index, key in enumerate(self._keys):
            deltas.append(key if index == 0 else key - prev)
            prev = key
        envelope = {
            "v": HEAT_FORMAT_VERSION,
            "level": BASE_LEVEL,
            "r": CORRIDOR_RADIUS,
            "k": deltas,
            "n": list(self._near),
            "t": list(self._through),
        }
        return zlib.compress(json.dumps(envelope, separators=(",", ":")).encode())

    @classmethod
    def decode(cls, data: bytes) -> RoadHeat:
        """Deserialize encode()'s output, or a v1 HeatGrid blob (near = through).

        Raises ValueError on corrupt data or an unknown version/level.
        """
        try:
            obj = json.loads(zlib.decompress(data))
        except (zlib.error, json.JSONDecodeError, UnicodeDecodeError) as err:
            raise ValueError(f"road_heat: invalid heat data: {err}") from err
        if isinstance(obj, dict) and obj.get("v") == GRID_FORMAT_VERSION:
            # Counted before corridors existed; recounting from routes upgrades it.
            grid = HeatGrid.decode(data)
            return cls(grid._keys, grid._counts, array("I", grid._counts))
        if not isinstance(obj, dict) or obj.get("v") != HEAT_FORMAT_VERSION:
            raise ValueError("road_heat: unsupported heat format")
        if obj.get("level") != BASE_LEVEL:
            raise ValueError(f"road_heat: unsupported level {obj.get('level')!r}")
        k_col, n_col, t_col = obj.get("k"), obj.get("n"), obj.get("t")
        if not all(isinstance(col, list) for col in (k_col, n_col, t_col)):
            raise ValueError("road_heat: 'k', 'n' and 't' must be lists")
        if not len(k_col) == len(n_col) == len(t_col):
            raise ValueError("road_heat: column length mismatch")
        keys: list[int] = []
        prev = -1
        for index, delta in enumerate(k_col):
            if not isinstance(delta, int):
                raise ValueError("road_heat: key deltas must be integers")  # noqa: TRY004
            key = delta if index == 0 else prev + delta
            if key <= prev:
                raise ValueError("road_heat: keys are not strictly increasing")
            keys.append(key)
            prev = key
        for n, t in zip(n_col, t_col, strict=True):
            if not (isinstance(n, int) and isinstance(t, int) and n > 0 and t >= 0):
                raise ValueError("road_heat: invalid near/through counts")
        return cls(array("Q", keys), array("I", n_col), array("I", t_col))
