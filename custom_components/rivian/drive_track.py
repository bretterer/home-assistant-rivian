"""GPS drive track model: validation, simplification, and compact encoding.

Pure Python (stdlib only, no Home Assistant imports) so it can be shared by
the live drive tracker, the SQLite storage layer, and history backfill
without pulling in any HA machinery.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import json
import math
from typing import Any, Final

TRACK_FORMAT_VERSION: Final[int] = 1

# Mean earth radius (metres), matches the value used by most GIS haversine
# implementations; gives ~111,195 m per degree of latitude.
_EARTH_RADIUS_M: Final[float] = 6371008.8

# Encoding scale factors: encoded_int = round(value * scale).
_SCALE_T: Final[float] = 10.0  # deciseconds
_SCALE_LATLON: Final[float] = 1e5
_SCALE_SPEED: Final[float] = 10.0  # 0.1 m/s
_SCALE_ALT: Final[float] = 10.0  # 0.1 m
_SCALE_SOC: Final[float] = 100.0  # 0.01 %
_SCALE_ODO: Final[float] = 1.0  # 1 m

_PREVIEW_START_TOLERANCE_M: Final[float] = 2.0
_PREVIEW_MAX_ITERATIONS: Final[int] = 20


@dataclass(slots=True)
class TrackPoint:
    """A single GPS fix, with optional companion telemetry."""

    t: float  # POSIX epoch seconds of the GPS fix (gnssLocation.timeStamp)
    lat: float
    lon: float
    speed_mps: float | None = None
    alt_m: float | None = None
    soc: float | None = None  # battery percent, e.g. 63.4
    odo_m: float | None = None  # vehicle odometer, metres


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the great-circle distance in metres between two lat/lon points."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1 - a)))
    return _EARTH_RADIUS_M * c


def _is_finite(value: float | None) -> bool:
    """Return True if value is a real (non-None, non-NaN, non-inf) number."""
    if value is None:
        return False
    try:
        return not (math.isnan(value) or math.isinf(value))
    except TypeError:
        return False


def _is_valid_coordinate(lat: float, lon: float) -> bool:
    """Validate a lat/lon pair per the drive-track append rules."""
    if not _is_finite(lat) or not _is_finite(lon):
        return False
    if abs(lat) > 90 or abs(lon) > 180:
        return False
    return not (lat == 0.0 and lon == 0.0)


def _round_half_even(value: float) -> int:
    """Round to the nearest integer, ties to even."""
    return round(value)


def _delta_encode(scaled: list[int | None]) -> list[int | None]:
    """Encode a list of scaled ints: first value absolute, rest delta from the
    previous non-null value. Nulls pass through unchanged and do not reset the
    delta base.
    """
    encoded: list[int | None] = []
    prev: int | None = None
    for value in scaled:
        if value is None:
            encoded.append(None)
            continue
        encoded.append(value if prev is None else value - prev)
        prev = value
    return encoded


def _encode_required_column(values: list[float], scale: float) -> list[int]:
    """Delta-encode a column that always has a value (t, lat, lon)."""
    scaled = [_round_half_even(v * scale) for v in values]
    return _delta_encode(scaled)  # type: ignore[return-value]


def _encode_optional_column(
    values: list[float | None], scale: float
) -> list[int | None] | None:
    """Delta-encode an optional column; returns None if entirely null."""
    scaled: list[int | None] = [
        None if v is None else _round_half_even(v * scale) for v in values
    ]
    if all(v is None for v in scaled):
        return None
    return _delta_encode(scaled)


def _decode_column(
    col: list[int | None] | None, n: int, scale: float, offset: float = 0.0
) -> list[float | None]:
    """Undo delta encoding and rescale a column back to real units."""
    if col is None:
        return [None] * n
    result: list[int | None] = []
    prev: int | None = None
    for value in col:
        if value is None:
            result.append(None)
            continue
        prev = value if prev is None else prev + value
        result.append(prev)
    return [None if v is None else v / scale + offset for v in result]


def _validate_column(
    obj: dict[str, Any], name: str, n: int, *, required: bool
) -> list[Any] | None:
    """Fetch and validate a column from a decoded envelope dict."""
    col = obj.get(name)
    if col is None:
        if required:
            raise ValueError(f"drive_track: missing required column {name!r}")
        return None
    if not isinstance(col, list):
        raise ValueError(f"drive_track: column {name!r} must be a list")  # noqa: TRY004
    if len(col) != n:
        raise ValueError(
            f"drive_track: column {name!r} length {len(col)} does not match n={n}"
        )
    return col


def _uniform_decimate(points: list[TrackPoint], max_points: int) -> list[TrackPoint]:
    """Uniformly sample points down to at most max_points, keeping endpoints."""
    n = len(points)
    if n <= max_points:
        return list(points)
    if max_points <= 1:
        return [points[0]]
    step = (n - 1) / (max_points - 1)
    indices = sorted({min(n - 1, round(i * step)) for i in range(max_points)})
    indices[0] = 0
    indices[-1] = n - 1
    return [points[i] for i in indices]


class DriveTrack:
    """An ordered sequence of GPS track points for a single drive."""

    def __init__(self, points: Iterable[TrackPoint] | None = None) -> None:
        """Initialize the track from an optional iterable of points."""
        self.points: list[TrackPoint] = list(points) if points is not None else []

    def __len__(self) -> int:
        """Return the number of points in the track."""
        return len(self.points)

    def append(self, point: TrackPoint) -> bool:
        """Append a point if it is valid and newer than the last point.

        Returns False (and does not append) if lat/lon is invalid (None, NaN,
        out of range, or exactly (0, 0)), or point.t is not finite, or
        point.t <= the last point's t. Returns True otherwise.
        """
        if not _is_valid_coordinate(point.lat, point.lon):
            return False
        if not _is_finite(point.t):
            return False
        if self.points and point.t <= self.points[-1].t:
            return False
        self.points.append(point)
        return True

    def extend(self, points: Iterable[TrackPoint]) -> int:
        """Append each point in order; return the count actually appended."""
        count = 0
        for point in points:
            if self.append(point):
                count += 1
        return count

    def bbox(self) -> tuple[float, float, float, float] | None:
        """Return (min_lat, min_lon, max_lat, max_lon), or None if empty."""
        if not self.points:
            return None
        lats = [p.lat for p in self.points]
        lons = [p.lon for p in self.points]
        return (min(lats), min(lons), max(lats), max(lons))

    def distance_m(self) -> float:
        """Return the sum of haversine distances between consecutive points."""
        pts = self.points
        total = 0.0
        for i in range(1, len(pts)):
            total += haversine_m(pts[i - 1].lat, pts[i - 1].lon, pts[i].lat, pts[i].lon)
        return total

    def simplify(self, tolerance_m: float) -> DriveTrack:
        """Douglas-Peucker simplification in a local equirectangular projection.

        Always keeps the first and last point. Kept points keep all their
        attributes unchanged. Implemented iteratively (explicit stack) so it
        has no recursion-depth issues on large tracks.
        """
        pts = self.points
        n = len(pts)
        if n <= 2:
            return DriveTrack(list(pts))

        mean_lat_rad = math.radians(sum(p.lat for p in pts) / n)
        cos_mean_lat = math.cos(mean_lat_rad)
        xs = [math.radians(p.lon) * cos_mean_lat * _EARTH_RADIUS_M for p in pts]
        ys = [math.radians(p.lat) * _EARTH_RADIUS_M for p in pts]

        keep = [False] * n
        keep[0] = True
        keep[-1] = True
        stack: list[tuple[int, int]] = [(0, n - 1)]

        while stack:
            start, end = stack.pop()
            if end <= start + 1:
                continue
            x1, y1 = xs[start], ys[start]
            x2, y2 = xs[end], ys[end]
            dx, dy = x2 - x1, y2 - y1
            seg_len2 = dx * dx + dy * dy

            max_dist = -1.0
            max_idx = -1
            for i in range(start + 1, end):
                xi, yi = xs[i], ys[i]
                if seg_len2 == 0.0:
                    dist = math.hypot(xi - x1, yi - y1)
                else:
                    t = ((xi - x1) * dx + (yi - y1) * dy) / seg_len2
                    t = max(0.0, min(1.0, t))
                    proj_x = x1 + t * dx
                    proj_y = y1 + t * dy
                    dist = math.hypot(xi - proj_x, yi - proj_y)
                if dist > max_dist:
                    max_dist = dist
                    max_idx = i

            if max_dist > tolerance_m:
                keep[max_idx] = True
                stack.append((start, max_idx))
                stack.append((max_idx, end))

        return DriveTrack([p for p, k in zip(pts, keep) if k])

    def preview(self, max_points: int = 150) -> DriveTrack:
        """Return a simplified copy with at most max_points points.

        Tries Douglas-Peucker with a doubling tolerance (starting ~2 m) for
        up to ~20 iterations; if that still exceeds max_points, falls back to
        uniform decimation keeping the endpoints.
        """
        if len(self.points) <= max_points:
            return DriveTrack(list(self.points))

        simplified: DriveTrack = self
        tolerance = _PREVIEW_START_TOLERANCE_M
        for _ in range(_PREVIEW_MAX_ITERATIONS):
            simplified = self.simplify(tolerance)
            if len(simplified) <= max_points:
                return simplified
            tolerance *= 2.0

        return DriveTrack(_uniform_decimate(simplified.points, max_points))

    def encode(self) -> str:
        """Serialize to a compact, columnar, delta-encoded JSON string."""
        pts = self.points
        n = len(pts)
        if n == 0:
            envelope: dict[str, Any] = {
                "v": TRACK_FORMAT_VERSION,
                "t0": 0,
                "n": 0,
                "t": [],
                "lat": [],
                "lon": [],
                "spd": None,
                "alt": None,
                "soc": None,
                "odo": None,
            }
            return json.dumps(envelope, separators=(",", ":"))

        t0 = round(pts[0].t)
        t_rel = [(p.t - t0) for p in pts]

        envelope = {
            "v": TRACK_FORMAT_VERSION,
            "t0": t0,
            "n": n,
            "t": _encode_required_column(t_rel, _SCALE_T),
            "lat": _encode_required_column([p.lat for p in pts], _SCALE_LATLON),
            "lon": _encode_required_column([p.lon for p in pts], _SCALE_LATLON),
            "spd": _encode_optional_column([p.speed_mps for p in pts], _SCALE_SPEED),
            "alt": _encode_optional_column([p.alt_m for p in pts], _SCALE_ALT),
            "soc": _encode_optional_column([p.soc for p in pts], _SCALE_SOC),
            "odo": _encode_optional_column([p.odo_m for p in pts], _SCALE_ODO),
        }
        return json.dumps(envelope, separators=(",", ":"))

    @classmethod
    def decode(cls, data: str | bytes | dict) -> DriveTrack:
        """Deserialize from encode()'s JSON string/bytes, or an already-parsed dict.

        Raises ValueError on an unknown "v" or malformed data.
        """
        if isinstance(data, (str, bytes)):
            try:
                obj = json.loads(data)
            except (json.JSONDecodeError, UnicodeDecodeError) as err:
                raise ValueError(f"drive_track: invalid JSON: {err}") from err
        elif isinstance(data, dict):
            obj = data
        else:
            raise ValueError(  # noqa: TRY004
                f"drive_track: unsupported data type {type(data)!r}"
            )

        if not isinstance(obj, dict):
            raise ValueError(  # noqa: TRY004
                "drive_track: decoded payload must be an object"
            )

        version = obj.get("v")
        if version != TRACK_FORMAT_VERSION:
            raise ValueError(f"drive_track: unsupported format version {version!r}")

        n = obj.get("n", 0)
        if not isinstance(n, int) or n < 0:
            raise ValueError(f"drive_track: invalid point count {n!r}")
        t0 = obj.get("t0", 0)

        t_col = _validate_column(obj, "t", n, required=True)
        lat_col = _validate_column(obj, "lat", n, required=True)
        lon_col = _validate_column(obj, "lon", n, required=True)
        spd_col = _validate_column(obj, "spd", n, required=False)
        alt_col = _validate_column(obj, "alt", n, required=False)
        soc_col = _validate_column(obj, "soc", n, required=False)
        odo_col = _validate_column(obj, "odo", n, required=False)

        t_vals = _decode_column(t_col, n, _SCALE_T, offset=t0)
        lat_vals = _decode_column(lat_col, n, _SCALE_LATLON)
        lon_vals = _decode_column(lon_col, n, _SCALE_LATLON)
        spd_vals = _decode_column(spd_col, n, _SCALE_SPEED)
        alt_vals = _decode_column(alt_col, n, _SCALE_ALT)
        soc_vals = _decode_column(soc_col, n, _SCALE_SOC)
        odo_vals = _decode_column(odo_col, n, _SCALE_ODO)

        points = [
            TrackPoint(
                t=t_vals[i],  # type: ignore[arg-type]
                lat=lat_vals[i],  # type: ignore[arg-type]
                lon=lon_vals[i],  # type: ignore[arg-type]
                speed_mps=spd_vals[i],
                alt_m=alt_vals[i],
                soc=soc_vals[i],
                odo_m=odo_vals[i],
            )
            for i in range(n)
        ]
        return cls(points)

    def to_payload(self) -> dict[str, list]:
        """Return the frontend shape: columnar, real units, None allowed."""

        def _round_or_none(value: float | None, digits: int) -> float | None:
            return None if value is None else round(value, digits)

        return {
            "t": [round(p.t, 1) for p in self.points],
            "lat": [round(p.lat, 5) for p in self.points],
            "lon": [round(p.lon, 5) for p in self.points],
            "speed_mps": [_round_or_none(p.speed_mps, 2) for p in self.points],
            "alt_m": [_round_or_none(p.alt_m, 1) for p in self.points],
            "soc": [_round_or_none(p.soc, 2) for p in self.points],
            "odo_m": [_round_or_none(p.odo_m, 1) for p in self.points],
        }

    def to_points_json(self) -> str:
        """Return an uncompressed JSON list of [t, lat, lon, spd, alt, soc, odo] rows."""
        rows = [
            [p.t, p.lat, p.lon, p.speed_mps, p.alt_m, p.soc, p.odo_m]
            for p in self.points
        ]
        return json.dumps(rows, separators=(",", ":"))

    @classmethod
    def from_points_json(cls, data: str) -> DriveTrack:
        """Inverse of to_points_json()."""
        rows = json.loads(data)
        points = [
            TrackPoint(
                t=row[0],
                lat=row[1],
                lon=row[2],
                speed_mps=row[3],
                alt_m=row[4],
                soc=row[5],
                odo_m=row[6],
            )
            for row in rows
        ]
        return cls(points)
