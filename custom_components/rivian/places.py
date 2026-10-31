"""Pure-stdlib logic for "favorite places": frequent drive start/stop spots.

No Home Assistant imports (mirrors road_snap.py/drive_track.py). This module
only computes *where* a place should be and *which* place a point belongs to;
all I/O (SQLite, Nominatim) lives in analytics_db.py/geocode.py.

A place is one of three ``source`` kinds:

- ``zone``: seeded from an HA zone (synced by ``AnalyticsDatabase.sync_zones``).
- ``user``: created directly, or an ``auto`` place renamed by a person (once
  renamed it is never re-clustered away).
- ``auto``: a cluster of >= ``MIN_VISITS`` drive endpoints within
  ``CLUSTER_RADIUS_M`` of each other, found by :func:`cluster_endpoints`.

:func:`drive_endpoints` turns a VIN's drives into an ordered list of
endpoints using the same "parked position" rule ``analytics_db.day()`` uses
for a day's first segment: a drive's recorded start can be a kilometre or two
from where the car actually was parked, because the first live fix can arrive
a minute or two after the car pulls away. ``DAY_GAP_MAX_M`` here mirrors
``analytics_db.DAY_GAP_MAX_M`` (3000 m) -- duplicated rather than imported to
keep this module free of any dependency on the SQLite layer.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
import statistics
from typing import Any, Final

from .drive_track import haversine_m

CLUSTER_RADIUS_M: Final[float] = 200.0
MIN_VISITS: Final[int] = 3
DEFAULT_RADIUS_M: Final[float] = 150.0
# An auto place covers the same radius its points were clustered within, so
# every visit that formed it is also labeled by it.
AUTO_RADIUS_M: Final[float] = CLUSTER_RADIUS_M
ZONE_RADIUS_BOUNDS: Final[tuple[float, float]] = (50.0, 500.0)

# Mirrors analytics_db.DAY_GAP_MAX_M; see module docstring.
DAY_GAP_MAX_M: Final[float] = 3000.0

# The one source of truth for place categories: (key, label, mdi icon). The
# WebSocket validation uses ``PLACE_CATEGORIES`` and ``places/list`` serves
# ``category_options()``, so the cards never carry their own copies.
PLACE_CATEGORY_DEFS: Final[tuple[tuple[str, str, str], ...]] = (
    ("home", "Home", "mdi:home"),
    ("work", "Work", "mdi:briefcase"),
    ("school", "School", "mdi:school"),
    ("shop", "Shopping", "mdi:cart"),
    ("dining", "Dining", "mdi:silverware-fork-knife"),
    ("charging", "Charging", "mdi:ev-station"),
    ("friends", "Friends", "mdi:account-group"),
    ("family", "Family", "mdi:home-heart"),
    ("gym", "Gym", "mdi:dumbbell"),
    ("swim", "Swimming", "mdi:swim"),
    ("mountain_biking", "Mountain biking", "mdi:bike"),
    ("park", "Park", "mdi:pine-tree"),
    ("medical", "Medical", "mdi:hospital-box"),
    ("other", "Other", "mdi:map-marker"),
)
PLACE_CATEGORIES: Final[tuple[str, ...]] = tuple(d[0] for d in PLACE_CATEGORY_DEFS)

# Places (and routes) belong to no vehicle. The only partition is this
# dataset, so the synthetic demo cars' made-up places never label (or mix
# with) the household's real ones.
DATASET_REAL: Final[str] = "real"
DATASET_DEMO: Final[str] = "demo"
DATASETS: Final[tuple[str, ...]] = (DATASET_REAL, DATASET_DEMO)


def category_options() -> list[dict[str, str]]:
    """Return the category list as ``[{key, label, icon}]`` for the frontend."""
    return [
        {"key": k, "label": label, "icon": icon}
        for k, label, icon in PLACE_CATEGORY_DEFS
    ]


@dataclass(frozen=True)
class DriveEndpointInput:
    """One drive's raw coordinates/timestamps, as stored in the ``drives`` table."""

    drive_id: str
    start_lat: float | None
    start_lon: float | None
    start_ts: float | None
    end_lat: float | None
    end_lon: float | None
    end_ts: float | None
    vin: str = ""


@dataclass(frozen=True)
class Endpoint:
    """One drive-start or drive-end point, after the parked-position rule."""

    lat: float
    lon: float
    t: float | None
    kind: str  # "start" | "end"
    drive_id: str
    vin: str = ""


@dataclass(frozen=True)
class ExistingPlace:
    """A place's geometry/visibility, as clustering/assignment need it."""

    place_id: int
    lat: float
    lon: float
    radius_m: float
    source: str  # "zone" | "user" | "auto"
    hidden: bool = False


@dataclass(frozen=True)
class AutoPlaceState:
    """An existing ``auto`` place's full state, carried over on a rebuild."""

    place_id: int
    lat: float
    lon: float
    radius_m: float
    hidden: bool
    name: str | None
    category: str | None
    geocode_name: str | None
    geocoded_ts: float | None


@dataclass(frozen=True)
class Cluster:
    """One cluster of >= MIN_VISITS endpoints: a place worth keeping.

    ``place_id`` is set (and ``hidden``/``name``/``category``/``geocode_name``/
    ``geocoded_ts`` carried over) when this cluster matches an existing
    ``auto`` place by proximity, so renames/hides/geocoded names survive a
    rebuild; otherwise it is a brand-new auto place.
    """

    lat: float
    lon: float
    visit_count: int  # max(arrivals, departures): one stop is one visit
    place_id: int | None = None
    hidden: bool = False
    name: str | None = None
    category: str | None = None
    geocode_name: str | None = None
    geocoded_ts: float | None = None


def drive_endpoints(rows: Sequence[DriveEndpointInput]) -> list[Endpoint]:
    """Return each drive's start/end as endpoints, in the given (time) order.

    A drive's start is the previous drive's end when that lies within
    ``DAY_GAP_MAX_M`` of this drive's own recorded start, else the drive's own
    start. "Previous drive" skips drives missing end coordinates, matching
    ``analytics_db.day()``'s lookup. Rows must already be ordered by time
    (ascending); a row missing a given coordinate pair contributes no
    endpoint for that side.
    """
    endpoints: list[Endpoint] = []
    prev_end: tuple[float, float] | None = None
    for row in rows:
        if row.start_lat is not None and row.start_lon is not None:
            start_lat, start_lon = row.start_lat, row.start_lon
            if prev_end is not None:
                distance = haversine_m(prev_end[0], prev_end[1], start_lat, start_lon)
                if distance <= DAY_GAP_MAX_M:
                    start_lat, start_lon = prev_end
            endpoints.append(
                Endpoint(
                    lat=start_lat,
                    lon=start_lon,
                    t=row.start_ts,
                    kind="start",
                    drive_id=row.drive_id,
                    vin=row.vin,
                )
            )
        if row.end_lat is not None and row.end_lon is not None:
            endpoints.append(
                Endpoint(
                    lat=row.end_lat,
                    lon=row.end_lon,
                    t=row.end_ts,
                    kind="end",
                    drive_id=row.drive_id,
                    vin=row.vin,
                )
            )
            prev_end = (row.end_lat, row.end_lon)
    return endpoints


def pooled_endpoints(
    rows_by_vin: dict[str, Sequence[DriveEndpointInput]],
) -> list[Endpoint]:
    """Return every vehicle's endpoints pooled into one time-ordered list.

    The parked-position chaining (a drive starts where the *same car's*
    previous drive ended) runs per vehicle, so two cars never link to each
    other; only then are the endpoints pooled for clustering. With a single
    vehicle the order is exactly :func:`drive_endpoints`'s; with several they
    are merged by timestamp (stable, undated endpoints last).
    """
    per_vin = [drive_endpoints(rows) for rows in rows_by_vin.values()]
    if len(per_vin) == 1:
        return per_vin[0]
    pooled = [e for endpoints in per_vin for e in endpoints]
    pooled.sort(key=lambda e: (e.t is None, e.t or 0.0))
    return pooled


def _nearest_containing(
    point: tuple[float, float], places: Iterable[Any]
) -> Any | None:
    """Return the nearest of ``places`` whose radius contains ``point``, or None.

    Each place must expose ``lat``, ``lon`` and ``radius_m``.
    """
    lat, lon = point
    best = None
    best_distance: float | None = None
    for place in places:
        distance = haversine_m(place.lat, place.lon, lat, lon)
        if distance <= place.radius_m and (
            best_distance is None or distance < best_distance
        ):
            best, best_distance = place, distance
    return best


def assign(point: tuple[float, float], places: Iterable[ExistingPlace]) -> int | None:
    """Return the id of the nearest *non-hidden* place containing ``point``.

    A hidden place never labels a point (so its drives stay unlabeled), but
    it still exists for :func:`cluster_endpoints` to treat as "owned" ground.
    """
    visible = [p for p in places if not p.hidden]
    nearest = _nearest_containing(point, visible)
    return nearest.place_id if nearest is not None else None


def _median(values: list[float]) -> float:
    return statistics.median(values)


def _nearest_auto_match(
    lat: float,
    lon: float,
    existing_auto: Sequence[AutoPlaceState],
    used_ids: set[int],
) -> AutoPlaceState | None:
    """Return the nearest not-yet-reused existing auto place within its own radius."""
    best: AutoPlaceState | None = None
    best_distance: float | None = None
    for candidate in existing_auto:
        if candidate.place_id in used_ids:
            continue
        distance = haversine_m(candidate.lat, candidate.lon, lat, lon)
        if distance <= candidate.radius_m and (
            best_distance is None or distance < best_distance
        ):
            best, best_distance = candidate, distance
    return best


def cluster_endpoints(
    endpoints: Sequence[Endpoint],
    fixed_places: Sequence[ExistingPlace],
    existing_auto: Sequence[AutoPlaceState],
) -> list[Cluster]:
    """Greedily cluster endpoints not already inside a zone/user place.

    ``fixed_places`` (zone- or user-sourced; ``auto`` places are excluded)
    absorb any endpoint within their radius -- including a *hidden* one,
    which still "owns" its points so they are never clustered elsewhere.
    Everything left over is clustered in the given (time) order: each point
    joins the nearest cluster within ``CLUSTER_RADIUS_M`` (centroid updated as
    the running median of its points), or starts a new one. Clusters with
    ``MIN_VISITS`` visits -- max(arrivals, departures), since one stop is
    both -- become the returned places, matched back to an
    existing ``auto`` place (reusing its id/hidden/name/category/geocode
    state) when the new centroid falls within that place's own radius.
    """
    raw_clusters: list[dict[str, Any]] = []
    for point in endpoints:
        if _nearest_containing((point.lat, point.lon), fixed_places) is not None:
            continue
        target = None
        best_distance: float | None = None
        for candidate in raw_clusters:
            distance = haversine_m(
                candidate["lat"], candidate["lon"], point.lat, point.lon
            )
            if distance <= CLUSTER_RADIUS_M and (
                best_distance is None or distance < best_distance
            ):
                target, best_distance = candidate, distance
        if target is None:
            target = {
                "lats": [],
                "lons": [],
                "lat": point.lat,
                "lon": point.lon,
                "starts": 0,
                "ends": 0,
            }
            raw_clusters.append(target)
        target["starts" if point.kind == "start" else "ends"] += 1
        target["lats"].append(point.lat)
        target["lons"].append(point.lon)
        target["lat"] = _median(target["lats"])
        target["lon"] = _median(target["lons"])

    results: list[Cluster] = []
    used_ids: set[int] = set()
    for candidate in raw_clusters:
        # A stop is an arrival *and* the next drive's departure from the same
        # spot, so count whichever is larger, not both.
        visit_count = max(candidate["starts"], candidate["ends"])
        if visit_count < MIN_VISITS:
            continue
        match = _nearest_auto_match(
            candidate["lat"], candidate["lon"], existing_auto, used_ids
        )
        if match is not None:
            used_ids.add(match.place_id)
            results.append(
                Cluster(
                    lat=candidate["lat"],
                    lon=candidate["lon"],
                    visit_count=visit_count,
                    place_id=match.place_id,
                    hidden=match.hidden,
                    name=match.name,
                    category=match.category,
                    geocode_name=match.geocode_name,
                    geocoded_ts=match.geocoded_ts,
                )
            )
        else:
            results.append(
                Cluster(
                    lat=candidate["lat"], lon=candidate["lon"], visit_count=visit_count
                )
            )
    return results


def place_label(name: str | None, geocode_name: str | None, place_id: int) -> str:
    """Return the display label for a place: name, else geocode name, else "Place #N"."""
    return name or geocode_name or f"Place #{place_id}"
