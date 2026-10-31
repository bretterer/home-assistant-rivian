"""Pure-stdlib logic for "favorite drives": repeated start->end place pairs.

No Home Assistant imports (mirrors places.py/road_snap.py/drive_track.py).
This module only groups drives into routes and computes their stats; all I/O
(SQLite) lives in analytics_db.py.

A **route** is an ordered ``(start_place_id, end_place_id)`` pair with
``MIN_ROUTE_DRIVES`` or more non-micro drives whose start and end place
differ. Routes belong to no vehicle: drives from every car in a dataset are
grouped together, and each route carries overall stats plus per-vehicle
(``by_vin``) stats, so each car's favorites are the routes it drives most. Drives on the same pair can still take clearly different paths (e.g.
a highway vs. a surface-street way to work), so each pair is split into
**variants** by path similarity: a routed drive's cells are the base-level
``road_heat.track_cells`` cells it passed through, coarsened to
``COARSE_LEVEL`` (~110 m, vs. ~19 m at the base level) so minor GPS drift
between repeats of the same road doesn't separate them into different
variants. Drives are grouped greedily, in time order, against each existing
variant's *first* (representative) drive -- not a running centroid, so a
variant's identity doesn't drift as more drives join it. A drive with no
track (no stored GPS route) joins the pair's largest variant for timing
stats, but never splits or creates one.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
import statistics
from typing import Final

from .road_heat import BASE_LEVEL, split_key

MIN_ROUTE_DRIVES: Final[int] = 3
COARSE_LEVEL: Final[int] = 18
_COARSEN_SHIFT: Final[int] = BASE_LEVEL - COARSE_LEVEL
VARIANT_JACCARD_THRESHOLD: Final[float] = 0.6
# A drive taking > this multiple of the pair's median elapsed time is almost
# certainly not a clean run of the route (e.g. it included a long stop), so
# it's excluded from fastest/average/slowest/rank but still listed, flagged.
OUTLIER_MEDIAN_FACTOR: Final[float] = 3.0


def coarsen_cells(cells: Iterable[int]) -> frozenset[int]:
    """Coarsen base-level (``road_heat.BASE_LEVEL``) cell keys to ``COARSE_LEVEL``.

    Packs the coarsened ``(x, y)`` the same way ``road_heat.cell_key`` packs
    base-level cells, just at a different level -- the two are never mixed,
    so there's no need to share the packing helper.
    """
    out: set[int] = set()
    for key in cells:
        x, y = split_key(key)
        cx, cy = x >> _COARSEN_SHIFT, y >> _COARSEN_SHIFT
        out.add((cx << COARSE_LEVEL) | cy)
    return frozenset(out)


def jaccard(a: frozenset[int], b: frozenset[int]) -> float:
    """Return the Jaccard similarity of two cell sets (0.0 if both are empty)."""
    if not a and not b:
        return 0.0
    union = len(a | b)
    if union == 0:
        return 0.0
    return len(a & b) / union


@dataclass(frozen=True)
class RouteDriveInput:
    """One drive's fields as the route grouper/stats need them."""

    drive_id: str
    start_place_id: int | None
    end_place_id: int | None
    sort_ts: float | None
    duration_seconds: float | None
    moving_seconds: float | None
    distance_miles: float | None
    energy_kwh: float | None
    temp_f: float | None
    # Coarsened (COARSE_LEVEL) cell set this drive's track passed through, or
    # None when the drive has no stored GPS route.
    cells: frozenset[int] | None = None
    # The car that drove it. A drive id is only unique per vehicle, so a
    # route's drives are identified by ``key`` ("vin|drive_id").
    vin: str = ""

    @property
    def key(self) -> str:
        """Return the drive's route-wide identity, ``"<vin>|<drive_id>"``."""
        return drive_key(self.vin, self.drive_id)


def drive_key(vin: str, drive_id: str) -> str:
    """Return the ``"<vin>|<drive_id>"`` key a route's per-drive stats use."""
    return f"{vin}|{drive_id}"


@dataclass(frozen=True)
class DriveRouteStat:
    """One drive's standing within its route/variant."""

    drive_id: str  # the drive's key, "<vin>|<drive_id>"
    rank: int | None  # 1 = fastest overall; None for an outlier or no duration
    vs_avg_pct: float | None  # (elapsed - overall avg) / avg * 100
    outlier: bool
    vin: str = ""
    # Standing among the same car's own drives: an R2 is never ranked against
    # an R1T, and "vs avg" compares a drive with its own car's average.
    vin_rank: int | None = None
    vin_vs_avg_pct: float | None = None


@dataclass(frozen=True)
class VinRouteStats:
    """One vehicle's own stats over a route (outliers excluded)."""

    count: int
    fastest_seconds: float | None = None
    slowest_seconds: float | None = None
    avg_seconds: float | None = None
    avg_efficiency_mi_kwh: float | None = None


@dataclass(frozen=True)
class RouteStats:
    """Aggregate timing/efficiency stats for one route (pair + variant).

    The flat fields are *overall* (across every vehicle that drove the route);
    ``by_vin`` holds each car's own count/best/average/slowest. The
    ``*_drive_id`` fields hold drive keys (``"<vin>|<drive_id>"``).
    """

    count: int
    fastest_seconds: float | None = None
    fastest_drive_id: str | None = None
    slowest_seconds: float | None = None
    slowest_drive_id: str | None = None
    avg_seconds: float | None = None
    fastest_moving_seconds: float | None = None
    fastest_moving_drive_id: str | None = None
    slowest_moving_seconds: float | None = None
    slowest_moving_drive_id: str | None = None
    avg_moving_seconds: float | None = None
    avg_efficiency_mi_kwh: float | None = None
    avg_temp_f: float | None = None
    last_ts: float | None = None
    drive_stats: dict[str, DriveRouteStat] = field(default_factory=dict)
    by_vin: dict[str, VinRouteStats] = field(default_factory=dict)


@dataclass(frozen=True)
class RouteGroup:
    """One route: a (start, end) pair's one variant, with its drives/stats."""

    start_place_id: int
    end_place_id: int
    variant: int  # 1-indexed, in chronological (first-drive) order
    variant_count: int  # how many variants this pair split into
    drive_ids: list[str]  # drive keys ("<vin>|<drive_id>"), time order
    stats: RouteStats


def _sort_key(drive: RouteDriveInput) -> float:
    return drive.sort_ts if drive.sort_ts is not None else 0.0


def split_variants(
    drives: Sequence[RouteDriveInput],
) -> list[list[RouteDriveInput]]:
    """Split one pair's drives (time order) into path-similarity variants.

    Returns each variant's drives, time-ordered, with tiny variants (fewer
    than ``MIN_ROUTE_DRIVES``) folded into the pair's overall largest variant,
    and the result itself ordered by each variant's earliest drive.
    """
    tracked = [d for d in drives if d.cells]
    untracked = [d for d in drives if not d.cells]

    variants: list[dict[str, object]] = []
    for drive in tracked:
        best: dict[str, object] | None = None
        best_score = 0.0
        for variant in variants:
            score = jaccard(drive.cells, variant["cells"])  # type: ignore[arg-type]
            if score >= VARIANT_JACCARD_THRESHOLD and score > best_score:
                best, best_score = variant, score
        if best is None:
            variants.append({"cells": drive.cells, "drives": [drive]})
        else:
            best["drives"].append(drive)  # type: ignore[union-attr]

    if not variants:
        # No drive in the pair has a stored route: everything is one variant.
        variants.append({"cells": None, "drives": []})

    target = max(variants, key=lambda v: len(v["drives"]))  # type: ignore[arg-type]
    for drive in untracked:
        target["drives"].append(drive)  # type: ignore[union-attr]

    by_size = sorted(variants, key=lambda v: -len(v["drives"]))  # type: ignore[arg-type]
    kept: list[dict[str, object]] = [by_size[0]]
    for variant in by_size[1:]:
        if len(variant["drives"]) < MIN_ROUTE_DRIVES:  # type: ignore[arg-type]
            kept[0]["drives"].extend(variant["drives"])  # type: ignore[union-attr]
        else:
            kept.append(variant)

    for variant in kept:
        variant["drives"].sort(key=_sort_key)  # type: ignore[union-attr]
    kept.sort(key=lambda v: _sort_key(v["drives"][0]) if v["drives"] else 0.0)  # type: ignore[index]

    return [variant["drives"] for variant in kept]  # type: ignore[misc]


def compute_route_stats(drives: Sequence[RouteDriveInput]) -> RouteStats:
    """Compute one route (or variant)'s aggregate stats and per-drive standing."""
    durations = [d.duration_seconds for d in drives if d.duration_seconds is not None]
    median = statistics.median(durations) if durations else None

    def is_outlier(drive: RouteDriveInput) -> bool:
        return (
            median is not None
            and drive.duration_seconds is not None
            and drive.duration_seconds > median * OUTLIER_MEDIAN_FACTOR
        )

    eligible = [
        d for d in drives if d.duration_seconds is not None and not is_outlier(d)
    ]
    avg_seconds = (
        statistics.fmean(d.duration_seconds for d in eligible) if eligible else None
    )
    fastest = min(eligible, key=lambda d: d.duration_seconds, default=None)
    slowest = max(eligible, key=lambda d: d.duration_seconds, default=None)

    moving_eligible = [d for d in eligible if d.moving_seconds is not None]
    avg_moving = (
        statistics.fmean(d.moving_seconds for d in moving_eligible)
        if moving_eligible
        else None
    )
    fastest_moving = min(moving_eligible, key=lambda d: d.moving_seconds, default=None)
    slowest_moving = max(moving_eligible, key=lambda d: d.moving_seconds, default=None)

    energy_eligible = [
        d for d in eligible if d.distance_miles and d.energy_kwh and d.energy_kwh > 0
    ]
    total_miles = sum(d.distance_miles for d in energy_eligible)
    total_kwh = sum(d.energy_kwh for d in energy_eligible)
    avg_efficiency = round(total_miles / total_kwh, 2) if total_kwh > 0 else None

    temps = [d.temp_f for d in eligible if d.temp_f is not None]
    avg_temp = round(statistics.fmean(temps), 1) if temps else None

    sort_values = [d.sort_ts for d in drives if d.sort_ts is not None]
    last_ts = max(sort_values) if sort_values else None

    ranked = sorted(eligible, key=lambda d: d.duration_seconds)
    rank_map = {d.key: i + 1 for i, d in enumerate(ranked)}

    # Each vehicle's own standing over the same (non-outlier) drives.
    by_vin: dict[str, VinRouteStats] = {}
    vin_rank_map: dict[str, int] = {}
    vin_avg: dict[str, float] = {}
    for vin in dict.fromkeys(d.vin for d in drives):
        own = [d for d in eligible if d.vin == vin]
        own_count = sum(1 for d in drives if d.vin == vin)
        if not own:
            by_vin[vin] = VinRouteStats(count=own_count)
            continue
        own_avg = statistics.fmean(d.duration_seconds for d in own)
        vin_avg[vin] = own_avg
        for i, d in enumerate(sorted(own, key=lambda d: d.duration_seconds)):
            vin_rank_map[d.key] = i + 1
        own_energy = [
            d for d in own if d.distance_miles and d.energy_kwh and d.energy_kwh > 0
        ]
        own_kwh = sum(d.energy_kwh for d in own_energy)
        by_vin[vin] = VinRouteStats(
            count=own_count,
            fastest_seconds=min(d.duration_seconds for d in own),
            slowest_seconds=max(d.duration_seconds for d in own),
            avg_seconds=own_avg,
            avg_efficiency_mi_kwh=(
                round(sum(d.distance_miles for d in own_energy) / own_kwh, 2)
                if own_kwh > 0
                else None
            ),
        )

    drive_stats: dict[str, DriveRouteStat] = {}
    for drive in drives:
        vs_avg_pct = None
        if avg_seconds and drive.duration_seconds is not None:
            vs_avg_pct = round(
                (drive.duration_seconds - avg_seconds) / avg_seconds * 100, 1
            )
        vin_vs_avg_pct = None
        own_avg = vin_avg.get(drive.vin)
        if own_avg and drive.duration_seconds is not None:
            vin_vs_avg_pct = round(
                (drive.duration_seconds - own_avg) / own_avg * 100, 1
            )
        drive_stats[drive.key] = DriveRouteStat(
            drive_id=drive.key,
            rank=rank_map.get(drive.key),
            vs_avg_pct=vs_avg_pct,
            outlier=is_outlier(drive),
            vin=drive.vin,
            vin_rank=vin_rank_map.get(drive.key),
            vin_vs_avg_pct=vin_vs_avg_pct,
        )

    return RouteStats(
        count=len(drives),
        fastest_seconds=fastest.duration_seconds if fastest else None,
        fastest_drive_id=fastest.key if fastest else None,
        slowest_seconds=slowest.duration_seconds if slowest else None,
        slowest_drive_id=slowest.key if slowest else None,
        avg_seconds=avg_seconds,
        fastest_moving_seconds=(
            fastest_moving.moving_seconds if fastest_moving else None
        ),
        fastest_moving_drive_id=fastest_moving.key if fastest_moving else None,
        slowest_moving_seconds=(
            slowest_moving.moving_seconds if slowest_moving else None
        ),
        slowest_moving_drive_id=slowest_moving.key if slowest_moving else None,
        avg_moving_seconds=avg_moving,
        avg_efficiency_mi_kwh=avg_efficiency,
        avg_temp_f=avg_temp,
        last_ts=last_ts,
        drive_stats=drive_stats,
        by_vin=by_vin,
    )


def build_routes(drives: Iterable[RouteDriveInput]) -> list[RouteGroup]:
    """Group drives into routes (pairs with >= MIN_ROUTE_DRIVES) and their variants.

    Drives missing a start or end place, or whose start and end place are the
    same, never form a route. Input order doesn't matter; each pair's drives
    are sorted by ``sort_ts`` before variant splitting.
    """
    pairs: dict[tuple[int, int], list[RouteDriveInput]] = {}
    for drive in drives:
        if drive.start_place_id is None or drive.end_place_id is None:
            continue
        if drive.start_place_id == drive.end_place_id:
            continue
        pairs.setdefault((drive.start_place_id, drive.end_place_id), []).append(drive)

    results: list[RouteGroup] = []
    for (start_id, end_id), group in pairs.items():
        if len(group) < MIN_ROUTE_DRIVES:
            continue
        ordered = sorted(group, key=_sort_key)
        variants = split_variants(ordered)
        variant_count = len(variants)
        for index, variant_drives in enumerate(variants, start=1):
            results.append(
                RouteGroup(
                    start_place_id=start_id,
                    end_place_id=end_id,
                    variant=index,
                    variant_count=variant_count,
                    drive_ids=[d.key for d in variant_drives],
                    stats=compute_route_stats(variant_drives),
                )
            )
    return results


def route_label(start_label: str, end_label: str, variant: int) -> str:
    """Return the display label, e.g. "Home -> Work" (or "... (via 2)" past variant 1)."""
    base = f"{start_label} → {end_label}"
    if variant > 1:
        return f"{base} (via {variant})"
    return base
