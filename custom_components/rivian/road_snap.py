"""Road-snapped GPS gap filling: reconstruct a drive's route across a dropout.

Rivian's live telemetry stream sometimes goes quiet mid-drive, so the stored
GPS track jumps straight between the two fixes bracketing the outage and
draws a bogus straight line on the map. This module finds those gaps
(:func:`find_gaps`), fetches the local road network from the public Overpass
API for OpenStreetMap (:func:`async_fetch_roads`) and snaps the gap onto it
with a small Dijkstra search (:func:`snap_gap`), producing a handful of
resampled points that replace the straight line.

Pure Python (stdlib only, no Home Assistant imports) except
:func:`async_fetch_roads`, which is the one async I/O entry point and uses
HA's shared aiohttp session. Everything else is safe to call from an
executor thread (CPU only, no I/O), matching the rest of the trip-analytics
subsystem's separation between pure computation and I/O.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import heapq
import logging
import math
import time
from typing import Any, Final

import aiohttp

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import VERSION
from .drive_track import TrackPoint, haversine_m
from .road_heat import _is_glitch

_LOGGER = logging.getLogger(__name__)

OVERPASS_URL: Final[str] = "https://overpass-api.de/api/interpreter"
USER_AGENT: Final[str] = (
    f"home-assistant-rivian/{VERSION} "
    "(+https://github.com/bretterer/home-assistant-rivian)"
)
REQUEST_TIMEOUT_SECONDS: Final[float] = 20.0
# At most one Overpass request per this many seconds, across the whole
# process (a module-level lock/timestamp, not per-hass or per-VIN): the
# public instance is a shared community resource.
MIN_REQUEST_INTERVAL_SECONDS: Final[float] = 5.0

# A gap is a jump between consecutive fixes further and slower-recorded than
# ordinary GPS noise between reports, but not so wild it's a glitch (see
# road_heat._is_glitch, reused here so the two callers agree on what a real
# jump looks like).
GAP_MIN_DISTANCE_M: Final[float] = 300.0
GAP_MIN_SECONDS: Final[float] = 30.0

# Bounding box padding and area cap for the Overpass query.
BBOX_PAD_M: Final[float] = 300.0
MAX_BBOX_AREA_M2: Final[float] = 100_000_000.0  # ~100 km^2: covers a ~10 km gap
BBOX_CACHE_GRID_DEG: Final[float] = 0.01

# Road types Overpass returns that a car can never have driven.
EXCLUDED_HIGHWAY_VALUES: Final[tuple[str, ...]] = (
    "footway",
    "cycleway",
    "path",
    "steps",
    "pedestrian",
    "track",
)

SNAP_MAX_DISTANCE_M: Final[float] = 50.0
MAX_DETOUR_RATIO: Final[float] = 1.6
MAX_IMPLIED_SPEED_MPS: Final[float] = 45.0
RESAMPLE_STEP_M: Final[float] = 50.0

# Mean earth radius (metres); matches drive_track.py's haversine constant.
_EARTH_RADIUS_M: Final[float] = 6371008.8
_DEG_TO_M: Final[float] = 111_320.0

# A node key groups way-vertices that share (near enough) the same
# coordinate, so ways meeting at a junction share one graph node. OSM nodes
# at a shared junction have byte-identical coordinates, so scaling to ~1cm
# resolution is generous, not lossy.
NodeKey = tuple[int, int]


def _node_key(lat: float, lon: float) -> NodeKey:
    """Return a hashable key for a road-graph node at (lat, lon)."""
    return (round(lat * 1e7), round(lon * 1e7))


# -- gap detection ------------------------------------------------------------


@dataclass(frozen=True)
class Gap:
    """A recorded jump between two consecutive track fixes worth trying to fill."""

    start_index: int
    end_index: int
    start: TrackPoint
    end: TrackPoint
    distance_m: float
    duration_s: float


def find_gaps(track: Any) -> list[Gap]:
    """Return the drive's real gaps: big, slow jumps that aren't GPS glitches.

    `track` is a :class:`drive_track.DriveTrack`. A jump qualifies as a gap
    when it's further than ``GAP_MIN_DISTANCE_M`` *and* took more than
    ``GAP_MIN_SECONDS``; a jump matching ``road_heat._is_glitch`` (implausibly
    far in too little time) is excluded, since that's bad GPS data, not a
    dropped connection mid-drive.
    """
    gaps: list[Gap] = []
    pts = track.points
    for i in range(1, len(pts)):
        p0, p1 = pts[i - 1], pts[i]
        distance = haversine_m(p0.lat, p0.lon, p1.lat, p1.lon)
        dt = p1.t - p0.t
        if dt <= 0 or _is_glitch(distance, dt):
            continue
        if distance > GAP_MIN_DISTANCE_M and dt > GAP_MIN_SECONDS:
            gaps.append(Gap(i - 1, i, p0, p1, distance, dt))
    return gaps


# -- bounding box helpers -------------------------------------------------------


def gap_bbox(gap: Gap, pad_m: float = BBOX_PAD_M) -> tuple[float, float, float, float]:
    """Return (south, west, north, east) degrees: the gap's endpoints, padded."""
    south = min(gap.start.lat, gap.end.lat)
    north = max(gap.start.lat, gap.end.lat)
    west = min(gap.start.lon, gap.end.lon)
    east = max(gap.start.lon, gap.end.lon)
    lat_pad = pad_m / _DEG_TO_M
    mean_lat_rad = math.radians((south + north) / 2.0)
    lon_pad = pad_m / (_DEG_TO_M * max(0.01, math.cos(mean_lat_rad)))
    return (south - lat_pad, west - lon_pad, north + lat_pad, east + lon_pad)


def bbox_area_m2(bbox: tuple[float, float, float, float]) -> float:
    """Return the approximate area (m^2) of a (south, west, north, east) bbox."""
    south, west, north, east = bbox
    mean_lat_rad = math.radians((south + north) / 2.0)
    height_m = (north - south) * _DEG_TO_M
    width_m = (east - west) * _DEG_TO_M * math.cos(mean_lat_rad)
    return abs(height_m * width_m)


def bbox_key(
    bbox: tuple[float, float, float, float], grid_deg: float = BBOX_CACHE_GRID_DEG
) -> str:
    """Round a bbox outward to a coarse grid so neighbouring gaps share a cache entry."""
    south, west, north, east = bbox
    s = math.floor(south / grid_deg) * grid_deg
    w = math.floor(west / grid_deg) * grid_deg
    n = math.ceil(north / grid_deg) * grid_deg
    e = math.ceil(east / grid_deg) * grid_deg
    return f"{s:.2f}_{w:.2f}_{n:.2f}_{e:.2f}"


# -- Overpass query + parsing ---------------------------------------------------


def build_overpass_query(bbox: tuple[float, float, float, float]) -> str:
    """Return an Overpass QL query for drivable ways within `bbox`."""
    south, west, north, east = bbox
    excluded = "|".join(EXCLUDED_HIGHWAY_VALUES)
    return (
        "[out:json][timeout:20];"
        f'way["highway"]["highway"!~"^({excluded})$"]'
        '["service"!="parking_aisle"]'
        f"({south:.6f},{west:.6f},{north:.6f},{east:.6f});"
        "out body geom;"
    )


@dataclass(frozen=True)
class Way:
    """One OSM way relevant to routing: its vertex chain and one-way-ness."""

    nodes: list[tuple[float, float]]  # (lat, lon), in way order
    oneway: bool


def parse_overpass(data: dict[str, Any]) -> list[Way]:
    """Parse an Overpass ``out body geom`` JSON response into a list of :class:`Way`."""
    ways: list[Way] = []
    for element in data.get("elements") or []:
        if not isinstance(element, dict) or element.get("type") != "way":
            continue
        geometry = element.get("geometry")
        if not isinstance(geometry, list):
            continue
        nodes = [
            (point["lat"], point["lon"])
            for point in geometry
            if isinstance(point, dict) and "lat" in point and "lon" in point
        ]
        if len(nodes) < 2:
            continue
        tags = element.get("tags") or {}
        oneway = str(tags.get("oneway", "")).strip().lower() == "yes"
        ways.append(Way(nodes=nodes, oneway=oneway))
    return ways


def ways_to_json(ways: list[Way]) -> list[dict[str, Any]]:
    """Serialize parsed ways to a small JSON-able structure, for the road cache."""
    return [
        {
            "nodes": [[round(lat, 6), round(lon, 6)] for lat, lon in way.nodes],
            "oneway": way.oneway,
        }
        for way in ways
    ]


def ways_from_json(data: list[Any]) -> list[Way]:
    """Inverse of :func:`ways_to_json`."""
    ways: list[Way] = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        nodes = [
            (float(n[0]), float(n[1]))
            for n in item.get("nodes", [])
            if isinstance(n, (list, tuple)) and len(n) >= 2
        ]
        if len(nodes) < 2:
            continue
        ways.append(Way(nodes=nodes, oneway=bool(item.get("oneway"))))
    return ways


async def async_overpass_json(hass: Any, query: str) -> Any | None:
    """POST an Overpass QL ``query`` and return the decoded JSON, or None on any failure.

    Shares the process-wide rate limit (one request per
    ``MIN_REQUEST_INTERVAL_SECONDS``) with every other Overpass caller
    (``charger_lookup`` too), so they can never burst together.
    """
    session = async_get_clientsession(hass)
    async with _rate_lock:
        wait = MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - _last_request_ts[0])
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request_ts[0] = time.monotonic()
        try:
            timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
            async with session.post(
                OVERPASS_URL,
                data={"data": query},
                headers={"User-Agent": USER_AGENT},
                timeout=timeout,
            ) as response:
                if response.status != 200:
                    _LOGGER.debug("Overpass API returned status %s", response.status)
                    return None
                return await response.json(content_type=None)
        except (TimeoutError, aiohttp.ClientError, ValueError) as err:
            _LOGGER.debug("Overpass API request failed: %s", err)
            return None


async def async_fetch_roads(
    hass: Any, bbox: tuple[float, float, float, float]
) -> list[Way] | None:
    """Fetch and parse the road network within `bbox` from the public Overpass API.

    Rate-limited (see :func:`async_overpass_json`). Returns None on any
    network, HTTP or parse error -- callers should treat that as "try again
    later", not "no road exists here", and leave the gap unfilled without
    recording a definitive 'none' result.
    """
    data = await async_overpass_json(hass, build_overpass_query(bbox))
    if data is None:
        return None
    try:
        return parse_overpass(data)
    except (TypeError, KeyError, ValueError) as err:
        _LOGGER.debug("Overpass API response could not be parsed: %s", err)
        return None


# Module-level rate limit state: a single-element list so it can be mutated
# from inside the lock without a `global` statement.
_rate_lock: Final[asyncio.Lock] = asyncio.Lock()
_last_request_ts: Final[list[float]] = [0.0]


# -- road graph + snapping -------------------------------------------------------


@dataclass(frozen=True)
class _Segment:
    """One physical edge between two adjacent way vertices."""

    a: NodeKey
    b: NodeKey
    a_coord: tuple[float, float]
    b_coord: tuple[float, float]
    length_m: float
    oneway: bool  # if True, only a -> b is drivable


def _build_segments(ways: list[Way]) -> list[_Segment]:
    """Break each way into its consecutive-vertex segments."""
    segments: list[_Segment] = []
    for way in ways:
        nodes = way.nodes
        for i in range(len(nodes) - 1):
            a_coord, b_coord = nodes[i], nodes[i + 1]
            if a_coord == b_coord:
                continue
            length = haversine_m(a_coord[0], a_coord[1], b_coord[0], b_coord[1])
            if length <= 0:
                continue
            segments.append(
                _Segment(
                    a=_node_key(*a_coord),
                    b=_node_key(*b_coord),
                    a_coord=a_coord,
                    b_coord=b_coord,
                    length_m=length,
                    oneway=way.oneway,
                )
            )
    return segments


def _build_adjacency(
    segments: list[_Segment],
) -> tuple[dict[Any, list[tuple[Any, float]]], dict[Any, tuple[float, float]]]:
    """Return (adjacency, node_coords) for the segments' directed graph."""
    adjacency: dict[Any, list[tuple[Any, float]]] = {}
    node_coords: dict[Any, tuple[float, float]] = {}
    for seg in segments:
        node_coords[seg.a] = seg.a_coord
        node_coords[seg.b] = seg.b_coord
        adjacency.setdefault(seg.a, []).append((seg.b, seg.length_m))
        if not seg.oneway:
            adjacency.setdefault(seg.b, []).append((seg.a, seg.length_m))
    return adjacency, node_coords


def _local_xy(lat: float, lon: float, cos_ref_lat: float) -> tuple[float, float]:
    """Project (lat, lon) to a local equirectangular plane in metres."""
    x = math.radians(lon) * cos_ref_lat * _EARTH_RADIUS_M
    y = math.radians(lat) * _EARTH_RADIUS_M
    return x, y


def _project_point_on_segment(
    px: float, py: float, ax: float, ay: float, bx: float, by: float
) -> tuple[float, float]:
    """Return (fraction 0..1 along a->b, distance metres) of the closest point."""
    dx, dy = bx - ax, by - ay
    seg_len2 = dx * dx + dy * dy
    if seg_len2 == 0.0:
        fraction = 0.0
    else:
        fraction = ((px - ax) * dx + (py - ay) * dy) / seg_len2
        fraction = max(0.0, min(1.0, fraction))
    proj_x, proj_y = ax + fraction * dx, ay + fraction * dy
    return fraction, math.hypot(px - proj_x, py - proj_y)


@dataclass(frozen=True)
class _Snap:
    """Where a gap endpoint lands on the road network."""

    segment: _Segment
    fraction: float
    distance_m: float


def _snap_point(
    lat: float, lon: float, segments: list[_Segment], cos_ref_lat: float
) -> _Snap | None:
    """Return the nearest snap within SNAP_MAX_DISTANCE_M, or None if too far."""
    px, py = _local_xy(lat, lon, cos_ref_lat)
    best: _Snap | None = None
    for seg in segments:
        ax, ay = _local_xy(seg.a_coord[0], seg.a_coord[1], cos_ref_lat)
        bx, by = _local_xy(seg.b_coord[0], seg.b_coord[1], cos_ref_lat)
        fraction, distance = _project_point_on_segment(px, py, ax, ay, bx, by)
        if distance <= SNAP_MAX_DISTANCE_M and (
            best is None or distance < best.distance_m
        ):
            best = _Snap(seg, fraction, distance)
    return best


def _insert_virtual_node(
    adjacency: dict[Any, list[tuple[Any, float]]],
    node_coords: dict[Any, tuple[float, float]],
    key: Any,
    snap: _Snap,
) -> None:
    """Splice a virtual node into the graph at `snap`'s point along its segment."""
    seg, fraction = snap.segment, snap.fraction
    lat = seg.a_coord[0] + fraction * (seg.b_coord[0] - seg.a_coord[0])
    lon = seg.a_coord[1] + fraction * (seg.b_coord[1] - seg.a_coord[1])
    node_coords[key] = (lat, lon)
    dist_from_a = seg.length_m * fraction
    dist_to_b = seg.length_m * (1.0 - fraction)
    adjacency.setdefault(seg.a, []).append((key, dist_from_a))
    adjacency.setdefault(key, []).append((seg.b, dist_to_b))
    if not seg.oneway:
        adjacency.setdefault(seg.b, []).append((key, dist_to_b))
        adjacency.setdefault(key, []).append((seg.a, dist_from_a))


def _maybe_direct_edge(
    adjacency: dict[Any, list[tuple[Any, float]]],
    start_key: Any,
    end_key: Any,
    start_snap: _Snap,
    end_snap: _Snap,
) -> None:
    """Add a direct edge when both endpoints snap onto the same segment."""
    if start_snap.segment is not end_snap.segment:
        return
    length = abs(end_snap.fraction - start_snap.fraction) * start_snap.segment.length_m
    if end_snap.fraction >= start_snap.fraction or not start_snap.segment.oneway:
        adjacency.setdefault(start_key, []).append((end_key, length))


def _dijkstra_path(
    adjacency: dict[Any, list[tuple[Any, float]]], start: Any, end: Any
) -> tuple[float, list[Any]] | None:
    """Return (total length, node-key path) of the shortest start->end path, or None."""
    dist: dict[Any, float] = {start: 0.0}
    prev: dict[Any, Any] = {}
    visited: set[Any] = set()
    heap: list[tuple[float, int, Any]] = [(0.0, 0, start)]
    counter = 1
    while heap:
        d, _, node = heapq.heappop(heap)
        if node in visited:
            continue
        visited.add(node)
        if node == end:
            path = [end]
            while path[-1] != start:
                path.append(prev[path[-1]])
            path.reverse()
            return d, path
        for neighbor, length in adjacency.get(node, ()):
            if neighbor in visited:
                continue
            nd = d + length
            if nd < dist.get(neighbor, math.inf):
                dist[neighbor] = nd
                prev[neighbor] = node
                heapq.heappush(heap, (nd, counter, neighbor))
                counter += 1
    return None


_START_KEY: Final[str] = "__gap_start__"
_END_KEY: Final[str] = "__gap_end__"


def snap_gap(gap: Gap, ways: list[Way]) -> list[TrackPoint] | None:
    """Try to fill `gap` with a road-snapped path; None if it can't be trusted.

    Pure CPU (no I/O): both endpoints must snap onto a road within
    ``SNAP_MAX_DISTANCE_M``, the shortest path respecting one-way
    restrictions must be at most ``MAX_DETOUR_RATIO`` times the straight-line
    distance, and its implied average speed must be at most
    ``MAX_IMPLIED_SPEED_MPS``. The accepted path is resampled roughly every
    ``RESAMPLE_STEP_M`` with interpolated time/altitude; speed/soc/odo are
    filled the same way as the rest of the fill's points describe (see
    :func:`_resample_path`). Returns None (never []) when no point could be
    generated on an otherwise-accepted path.
    """
    if not ways or gap.distance_m <= 0:
        return None
    segments = _build_segments(ways)
    if not segments:
        return None

    mean_lat_rad = math.radians((gap.start.lat + gap.end.lat) / 2.0)
    cos_ref_lat = math.cos(mean_lat_rad)

    start_snap = _snap_point(gap.start.lat, gap.start.lon, segments, cos_ref_lat)
    end_snap = _snap_point(gap.end.lat, gap.end.lon, segments, cos_ref_lat)
    if start_snap is None or end_snap is None:
        return None

    adjacency, node_coords = _build_adjacency(segments)
    _insert_virtual_node(adjacency, node_coords, _START_KEY, start_snap)
    _insert_virtual_node(adjacency, node_coords, _END_KEY, end_snap)
    _maybe_direct_edge(adjacency, _START_KEY, _END_KEY, start_snap, end_snap)

    result = _dijkstra_path(adjacency, _START_KEY, _END_KEY)
    if result is None:
        return None
    path_length_m, path = result

    if path_length_m > MAX_DETOUR_RATIO * gap.distance_m:
        return None
    implied_speed = path_length_m / gap.duration_s if gap.duration_s > 0 else math.inf
    if implied_speed > MAX_IMPLIED_SPEED_MPS:
        return None

    coords = [node_coords[key] for key in path]
    points = _resample_path(coords, path_length_m, gap)
    return points or None


def _resample_path(
    coords: list[tuple[float, float]], path_length_m: float, gap: Gap
) -> list[TrackPoint]:
    """Resample a lat/lon polyline every ~RESAMPLE_STEP_M into interior TrackPoints."""
    if path_length_m <= 0 or len(coords) < 2:
        return []
    speed_mps = path_length_m / gap.duration_s if gap.duration_s > 0 else None
    alt0, alt1 = gap.start.alt_m, gap.end.alt_m

    points: list[TrackPoint] = []
    walked = 0.0
    next_target = RESAMPLE_STEP_M
    for i in range(1, len(coords)):
        (lat0, lon0), (lat1_, lon1_) = coords[i - 1], coords[i]
        seg_len = haversine_m(lat0, lon0, lat1_, lon1_)
        if seg_len <= 0:
            continue
        while next_target < path_length_m and next_target <= walked + seg_len:
            fraction_in_seg = (next_target - walked) / seg_len
            lat = lat0 + fraction_in_seg * (lat1_ - lat0)
            lon = lon0 + fraction_in_seg * (lon1_ - lon0)
            fraction_total = next_target / path_length_m
            t = gap.start.t + fraction_total * gap.duration_s
            alt = (
                alt0 + fraction_total * (alt1 - alt0)
                if alt0 is not None and alt1 is not None
                else None
            )
            points.append(
                TrackPoint(
                    t=t,
                    lat=lat,
                    lon=lon,
                    speed_mps=speed_mps,
                    alt_m=alt,
                    soc=None,
                    odo_m=None,
                )
            )
            next_target += RESAMPLE_STEP_M
        walked += seg_len
    return points
