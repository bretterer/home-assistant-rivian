"""Pure-stdlib battery analytics behind the Charging & Battery page.

No Home Assistant imports. The WebSocket handlers (``websocket_api.py``) fetch
the raw rows (recorder statistics, stored sessions/drives) and this module
turns them into: session counts outside the 20-80 % band, a battery-% timeline
(synthesized from drives and charging sessions when a vehicle has no recorder
statistics), time spent per SoC band, and a battery-capacity health series.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta, tzinfo
from itertools import pairwise
import statistics
from typing import Any, Final

# SoC band edges, in %, and the band names ``time_in_band`` reports.
BAND_EDGES: Final[tuple[float, ...]] = (10.0, 20.0, 80.0, 90.0)
BAND_NAMES: Final[tuple[str, ...]] = (
    "below_10",
    "b10_20",
    "b20_80",
    "b80_90",
    "above_90",
)
# A recorder-statistics gap longer than this is not "covered" time (the
# sensor was unavailable), so it doesn't count toward the band fractions.
STATISTICS_MAX_GAP_S: Final[float] = 6 * 3600.0
TIMELINE_MAX_POINTS: Final[int] = 2000
# end_soc below this makes ``end_range / end_soc`` too noisy for a projected
# full-charge range.
PROJECTED_RANGE_MIN_SOC_PCT: Final[float] = 10.0

# Charges detected from the battery-% statistics where no session was recorded
# (before AC sessions were recorded, past the recorder's state history, or a
# session the live tracker missed). A rise must gain at least this much; a dip
# of up to the tolerance inside it (statistics noise) doesn't end it.
DETECT_MIN_GAIN_PCT: Final[float] = 2.0
DETECT_DIP_TOLERANCE_PCT: Final[float] = 0.3
# A statistics step whose midpoint lies within this of a recorded session is
# that session's (hourly means smear a session across neighbouring buckets).
DETECT_COVER_SLOP_S: Final[float] = 1800.0
# Average power (from the rise, the pack capacity and the rise's duration) at or
# above this is a fast charge. Hourly means stretch a DC session over a longer
# span, so this sits below ``DCFC_MIN_POWER_KW``; AC tops out near 11.5 kW.
DETECT_DC_MIN_KW: Final[float] = 15.0
# A flat stretch ends a detected charge: once the level has risen slower than
# ``DETECT_PLATEAU_RATE_PCT_PER_H`` since its peak for at least the plateau
# time, the charge stopped (a car resting at its limit, or a pause before a
# second charge). Level 1 adds about 1 %/h, so it never reads as flat. The
# plateau time is ``DETECT_PLATEAU_MIN_S`` (15 min) or one sample step if the
# series is coarser (an hour for hourly statistics): a single 5-minute reading
# of a slow charge can look flat, since readings move in 0.1 % steps. The peak
# only moves on a gain of at least ``DETECT_FLICKER_PCT``, so a 0.1 % reading
# flicker hours later can't stretch a charge out to it.
# A fast charge always follows driving to the charger: a detected rise counts
# as DC only if a drive ended within this long before (or during) it. A
# DC-speed rise with no drive is a level the car reached while Home Assistant
# got no updates from it (its live feed stalled; the car kept charging).
DETECT_DC_DRIVE_GAP_S: Final[float] = 3600.0
# Two detected charges at most this far apart, with no drive, no recorded
# session and a level that held between them, are one charge Home Assistant
# partly didn't see (the feed went silent and the reading froze; see
# detect_charge_spans).
DETECT_MERGE_GAP_S: Final[float] = 8 * 3600.0
DETECT_PLATEAU_MIN_S: Final[float] = 900.0
DETECT_PLATEAU_RATE_PCT_PER_H: Final[float] = 0.5
DETECT_FLICKER_PCT: Final[float] = 0.15

Point = tuple[float, float]


def session_counts(sessions: Iterable[dict[str, Any]]) -> dict[str, int]:
    """Count sessions by kind and by where they started/ended.

    ``ended_above_*`` / ``started_below_*`` cover every session (DC and AC);
    the ``dc_`` variants cover fast charges only (a home session that tops up
    to 80 % is ordinary, a fast charge above 80 % is the one to watch).
    """
    counts = {
        "total": 0,
        "dc": 0,
        "ac": 0,
        "ended_above_80": 0,
        "ended_above_90": 0,
        "started_below_20": 0,
        "started_below_10": 0,
        "dc_ended_above_80": 0,
        "dc_ended_above_90": 0,
        "dc_started_below_20": 0,
        "dc_started_below_10": 0,
    }
    for session in sessions:
        is_dc = session.get("kind") == "dc"
        counts["total"] += 1
        counts["dc" if is_dc else "ac"] += 1
        end_soc = session.get("end_soc")
        start_soc = session.get("start_soc")
        flags = {
            "ended_above_80": end_soc is not None and end_soc > 80.0,
            "ended_above_90": end_soc is not None and end_soc > 90.0,
            "started_below_20": start_soc is not None and start_soc < 20.0,
            "started_below_10": start_soc is not None and start_soc < 10.0,
        }
        for key, hit in flags.items():
            if hit:
                counts[key] += 1
                if is_dc:
                    counts[f"dc_{key}"] += 1
    return counts


def downsample(points: Sequence[Point], max_points: int) -> list[Point]:
    """Reduce ``points`` to about ``max_points``, keeping each bucket's min and max.

    The first and last point are always kept, so a short spike (a charge) is
    never smoothed away.
    """
    if len(points) <= max_points or max_points < 4:
        return list(points)
    buckets = (max_points - 2) // 2
    size = (len(points) - 2) / buckets
    out: list[Point] = [points[0]]
    for b in range(buckets):
        chunk = points[1 + int(b * size) : 1 + int((b + 1) * size)]
        if not chunk:
            continue
        lo = min(chunk, key=lambda p: p[1])
        hi = max(chunk, key=lambda p: p[1])
        out.extend(sorted({lo, hi}, key=lambda p: p[0]))
    out.append(points[-1])
    return out


def _interpolate(points: Sequence[Point], ts: float) -> float | None:
    """Return the linearly interpolated value at ``ts`` (held flat outside)."""
    if not points:
        return None
    if ts <= points[0][0]:
        return points[0][1]
    if ts >= points[-1][0]:
        return points[-1][1]
    for i in range(1, len(points)):
        t1, v1 = points[i]
        if ts <= t1:
            t0, v0 = points[i - 1]
            span = t1 - t0
            return v0 if span <= 0 else v0 + (v1 - v0) * (ts - t0) / span
    return points[-1][1]


def _monotonic(points: Iterable[Point]) -> list[Point]:
    """Sort by time and drop points that don't advance it."""
    out: list[Point] = []
    for ts, soc in sorted(points, key=lambda p: p[0]):
        if out and ts <= out[-1][0]:
            continue
        out.append((ts, soc))
    return out


def clip_series(points: Sequence[Point], start_ts: float, end_ts: float) -> list[Point]:
    """Restrict a series to ``[start_ts, end_ts]``.

    The value at each edge is interpolated, and held flat past the last
    point, so a vehicle that has not moved since its last event still has a
    line through the window's end.
    """
    if not points or end_ts <= start_ts:
        return []
    first = _interpolate(points, start_ts)
    last = _interpolate(points, end_ts)
    out: list[Point] = []
    if first is not None:
        out.append((start_ts, first))
    out.extend(p for p in points if start_ts < p[0] < end_ts)
    if last is not None:
        out.append((end_ts, last))
    return _monotonic(out)


def synthesize_timeline(
    events: dict[str, Any], start_ts: float, end_ts: float
) -> list[Point]:
    """Build a battery-% series from stored drives and charging sessions.

    Each drive contributes its route's SoC points (or its start -> end SoC);
    each session its SoC samples (or start -> end). Between events the line
    runs straight from one event's end level to the next one's start level:
    a level that changed while nothing was recorded (unrecorded driving or
    charging) is a gradual estimate, not a flat line ending in a vertical
    jump. Returns ``[]`` when there is nothing to draw.
    """
    items: list[tuple[float, float, float, float, list[Point]]] = []
    for drive in events.get("drives", []):
        items.append(
            (
                drive["start_ts"],
                drive["end_ts"],
                drive["start_soc"],
                drive["end_soc"],
                list(drive.get("points") or []),
            )
        )
    for session in events.get("sessions", []):
        items.append(
            (
                session["start_ts"],
                session["end_ts"],
                session["start_soc"],
                session["end_soc"],
                list(session.get("points") or []),
            )
        )
    items.sort(key=lambda it: it[0])

    raw: list[Point] = []
    for t0, t1, s0, s1, inner in items:
        raw.append((t0, s0))
        raw.extend((t, v) for t, v in inner if t0 < t < t1)
        raw.append((t1, s1))
    series = _monotonic(raw)
    if not series:
        return []
    return clip_series(series, start_ts, end_ts)


def time_in_band(
    points: Sequence[Point], max_gap_s: float | None = None
) -> dict[str, float] | None:
    """Return the fraction of covered time spent in each SoC band.

    The level is linearly interpolated between consecutive points, and each
    piece is split where it crosses a band edge. With ``max_gap_s`` a pair of
    points further apart than that is a gap, not covered time. None when no
    time is covered.
    """
    totals = [0.0] * len(BAND_NAMES)
    for (t0, v0), (t1, v1) in pairwise(points):
        dt = t1 - t0
        if dt <= 0 or (max_gap_s is not None and dt > max_gap_s):
            continue
        cuts = [0.0, 1.0]
        if v1 != v0:
            for edge in BAND_EDGES:
                frac = (edge - v0) / (v1 - v0)
                if 0.0 < frac < 1.0:
                    cuts.append(frac)
        cuts.sort()
        for a, b in pairwise(cuts):
            mid = v0 + (v1 - v0) * (a + b) / 2.0
            band = sum(1 for edge in BAND_EDGES if mid > edge)
            # A level exactly on 20 belongs to the 20-80 band.
            if mid == BAND_EDGES[1]:
                band = 2
            totals[band] += (b - a) * dt
    covered = sum(totals)
    if covered <= 0:
        return None
    return {
        name: round(t / covered, 4) for name, t in zip(BAND_NAMES, totals, strict=True)
    }


def _local_midnight(ts: float, tz: tzinfo) -> float:
    local = datetime.fromtimestamp(ts, tz=tz)
    midnight = datetime.combine(local.date(), datetime.min.time(), tzinfo=tz)
    return midnight.timestamp()


def capacity_series(
    rows: Sequence[dict[str, Any]],
    sensor_days: Sequence[Point],
    tz: tzinfo,
    nominal_kwh: float,
) -> dict[str, Any]:
    """Build the battery-health series.

    ``rows`` are drives (``ts``, ``capacity_kwh``, ``end_soc``,
    ``end_range_mi``); ``sensor_days`` are ``(ts, kwh)`` daily maxima of the
    capacity sensor's long-term statistics. Per local day the capacity is the
    maximum seen; the original is the first day's value (``nominal_kwh`` when
    nothing was observed). The projected full-charge range is each day's
    median ``end_range_mi / end_soc * 100`` over drives that have both.
    """
    by_day: dict[float, float] = {}
    for ts, kwh in list(sensor_days) + [
        (r["ts"], r["capacity_kwh"]) for r in rows if r.get("capacity_kwh")
    ]:
        if not kwh or kwh <= 0:
            continue
        day = _local_midnight(ts, tz)
        by_day[day] = max(by_day.get(day, 0.0), float(kwh))
    days = sorted(by_day)
    original = by_day[days[0]] if days else nominal_kwh

    ranges: dict[float, list[float]] = {}
    for r in rows:
        soc, miles = r.get("end_soc"), r.get("end_range_mi")
        if soc is None or miles is None or soc < PROJECTED_RANGE_MIN_SOC_PCT:
            continue
        ranges.setdefault(_local_midnight(r["ts"], tz), []).append(miles / soc * 100.0)

    return {
        "points": [[int(d), round(by_day[d], 2)] for d in days],
        "original_kwh": round(original, 2),
        "pct_points": [
            [int(d), round(by_day[d] / original * 100.0, 2) if original else None]
            for d in days
        ],
        "projected_range": [
            [int(d), round(statistics.median(v), 1)] for d, v in sorted(ranges.items())
        ],
    }


def window_bounds(
    start: float | None, end: float | None, now: float, default_days: int = 30
) -> tuple[float, float]:
    """Return ``(start_ts, end_ts)`` for a timeline request, defaulting to the last 30 days."""
    end_ts = float(end) if end is not None else now
    start_ts = (
        float(start)
        if start is not None
        else end_ts - timedelta(days=default_days).total_seconds()
    )
    return start_ts, end_ts


def capacity_history_series(
    history: Sequence[dict[str, Any]],
    range_rows: Sequence[dict[str, Any]],
    tz: tzinfo,
    nominal_kwh: float,
    live: tuple[float, float] | None = None,
) -> dict[str, Any]:
    """Build the battery-health series from the stored ``capacity_history``.

    ``history`` rows are ``{day 'YYYY-MM-DD', kwh, temp_f, temp_source}``;
    ``live`` is an optional ``(ts, kwh)`` reading from today (it replaces the
    stored value for its day only if larger, and appears without a
    temperature). ``range_rows`` are drives (``ts``, ``end_soc``,
    ``end_range_mi``) for the projected full-charge range, as in
    :func:`capacity_series`. ``points`` are
    ``[day_ts, kwh, temp_f|None, temp_source|None]`` (``day_ts`` is local
    midnight, epoch seconds); the original is the first day's value
    (``nominal_kwh`` with no history).
    """
    by_day: dict[float, list[Any]] = {}
    for row in history:
        kwh = row.get("kwh")
        if not kwh or kwh <= 0:
            continue
        try:
            midnight = datetime.strptime(row["day"], "%Y-%m-%d").replace(tzinfo=tz)
        except (KeyError, ValueError):
            continue
        by_day[midnight.timestamp()] = [
            float(kwh),
            row.get("temp_f"),
            row.get("temp_source"),
        ]
    if live is not None and live[1] and live[1] > 0:
        day = _local_midnight(live[0], tz)
        if day not in by_day or live[1] > by_day[day][0]:
            prior = by_day.get(day)
            by_day[day] = [
                float(live[1]),
                prior[1] if prior else None,
                prior[2] if prior else None,
            ]
    days = sorted(by_day)
    original = by_day[days[0]][0] if days else nominal_kwh

    ranges: dict[float, list[float]] = {}
    for r in range_rows:
        soc, miles = r.get("end_soc"), r.get("end_range_mi")
        if soc is None or miles is None or soc < PROJECTED_RANGE_MIN_SOC_PCT:
            continue
        ranges.setdefault(_local_midnight(r["ts"], tz), []).append(miles / soc * 100.0)

    return {
        "points": [
            [int(d), round(by_day[d][0], 2), by_day[d][1], by_day[d][2]] for d in days
        ],
        "original_kwh": round(original, 2),
        "pct_points": [
            [int(d), round(by_day[d][0] / original * 100.0, 2) if original else None]
            for d in days
        ],
        "projected_range": [
            [int(d), round(statistics.median(v), 1)] for d, v in sorted(ranges.items())
        ],
    }


def detect_charge_spans(
    points: Sequence[Point],
    recorded: Iterable[tuple[float, float]],
    capacity_kwh: float | None,
    *,
    min_gain_pct: float = DETECT_MIN_GAIN_PCT,
    tolerance_pct: float = DETECT_DIP_TOLERANCE_PCT,
    cover_slop_s: float = DETECT_COVER_SLOP_S,
    dc_min_kw: float = DETECT_DC_MIN_KW,
    drive_ends: Iterable[float] | None = None,
) -> list[dict[str, Any]]:
    """Charges visible in a battery-% series that no recorded session covers.

    ``recorded`` is each stored session's ``(start_ts, end_ts)``. The series is
    split into steps; a step whose midpoint lies within ``cover_slop_s`` of a
    recorded session belongs to it. A detected charge starts at an uncovered
    rising step and runs while the steps stay uncovered, the level doesn't
    fall more than ``tolerance_pct`` below its peak, and it hasn't sat flat
    (rising under ``DETECT_PLATEAU_RATE_PCT_PER_H`` for the plateau time) since
    the peak; leading points within ``tolerance_pct`` of the start are trimmed,
    it ends where it first reached its peak, and it is kept when it gained at
    least ``min_gain_pct``. So the uncovered remainder of a partly recorded
    charge is still detected, and two charges with a pause between them are
    two spans.

    Returns ``[{start_ts, end_ts, start_soc, end_soc, kind, avg_power_kw,
    energy_added_kwh, unreported}]`` in time order; ``kind`` is ``'dc'`` when
    the average power reaches ``dc_min_kw`` (``'ac'`` without a capacity) and,
    when ``drive_ends`` (each drive's end time) is given, a drive ended within
    ``DETECT_DC_DRIVE_GAP_S`` before or during it. A DC-speed rise with no
    drive is ``'ac'`` with ``unreported`` True and no ``avg_power_kw``: the
    level jumped when the car woke, so its charging rate is unknown. With
    ``drive_ends``, two detected charges with nothing between them (no drive,
    no recorded session, the level held, at most ``DETECT_MERGE_GAP_S`` apart)
    are joined into one ``unreported`` charge: Home Assistant got no updates
    for part of it (the reading froze while the car kept charging). Its end is
    estimated from the charging rate before the gap (it can't be later than
    the next reading), since the car usually reached its limit well before.
    """
    ends = sorted(drive_ends) if drive_ends is not None else None
    pts = _monotonic(points)
    spans = [
        (a - cover_slop_s, b + cover_slop_s)
        for a, b in recorded
        if a is not None and b is not None
    ]

    def covered(k: int) -> bool:
        mid = (pts[k][0] + pts[k + 1][0]) / 2
        return any(a <= mid <= b for a, b in spans)

    raw: list[list[int]] = []
    n = len(pts)
    steps = sorted(b[0] - a[0] for a, b in pairwise(pts))
    plateau_s = max(DETECT_PLATEAU_MIN_S, steps[len(steps) // 2] if steps else 0.0)
    i = 0
    while i < n - 1:
        if not (pts[i + 1][1] > pts[i][1] and not covered(i)):
            i += 1
            continue
        peak = i + 1
        j = i + 1
        while (
            j + 1 < n
            and not covered(j)
            and pts[j + 1][1] >= pts[peak][1] - tolerance_pct
        ):
            elapsed = pts[j + 1][0] - pts[peak][0]
            if (
                elapsed >= plateau_s
                and pts[j + 1][1] - pts[peak][1]
                < DETECT_PLATEAU_RATE_PCT_PER_H * elapsed / 3600.0
            ):
                break  # flat since the peak: this charge is over
            j += 1
            if pts[j][1] >= pts[peak][1] + DETECT_FLICKER_PCT:
                peak = j
        # Trim a flat lead-in (a charge that began late in a bucket, or a
        # flicker before it): steps rising slower than the plateau rate.
        while i + 1 < peak and pts[i + 1][1] - pts[i][1] < (
            DETECT_PLATEAU_RATE_PCT_PER_H * (pts[i + 1][0] - pts[i][0]) / 3600.0
        ):
            i += 1
        if pts[peak][1] - pts[i][1] >= min_gain_pct:
            raw.append([i, peak])
        i = peak

    def no_drive(t0: float, t1: float) -> bool:
        return ends is not None and not any(t0 < e <= t1 for e in ends)

    # One charge whose middle Home Assistant didn't see: no updates came (the
    # reading froze, flat) and the next one showed a higher level. Join two
    # detected charges when nothing happened between them: no drive, no
    # recorded session, the level held, and at most DETECT_MERGE_GAP_S apart.
    # The third item is the last reported point before the gap (or None).
    merged: list[list[Any]] = []
    for i0, p0 in raw:
        if merged:
            pi, pp, _ = merged[-1]
            if (
                pts[i0][0] - pts[pp][0] <= DETECT_MERGE_GAP_S
                and no_drive(pts[pp][0], pts[i0][0])
                and not any(covered(k) for k in range(pp, i0))
                and min(v for _, v in pts[pp : i0 + 1]) >= pts[pp][1] - tolerance_pct
            ):
                merged[-1] = [pi, p0, pp]
                continue
        merged.append([i0, p0, None])

    out: list[dict[str, Any]] = []
    for i, peak, gap_at in merged:
        joined = gap_at is not None
        start, end = pts[i][0], pts[peak][0]
        if joined and gap_at > i:
            # Extrapolate the end at the charging rate seen before the gap.
            rate = (pts[gap_at][1] - pts[i][1]) / (pts[gap_at][0] - pts[i][0])
            if rate > 0:
                estimated = pts[gap_at][0] + (pts[peak][1] - pts[gap_at][1]) / rate
                end = min(end, max(pts[gap_at][0], estimated))
        gain = pts[peak][1] - pts[i][1]
        hours = (end - start) / 3600.0
        energy = gain / 100.0 * capacity_kwh if capacity_kwh else None
        avg_kw = energy / hours if energy is not None and hours > 0 else None
        fast = avg_kw is not None and avg_kw >= dc_min_kw and not joined
        unreported = joined
        if (
            fast
            and ends is not None
            and not any(start - DETECT_DC_DRIVE_GAP_S <= e <= end for e in ends)
        ):
            fast, unreported, avg_kw = False, True, None
        out.append(
            {
                "start_ts": start,
                "end_ts": end,
                "start_soc": round(pts[i][1], 1),
                "end_soc": round(pts[peak][1], 1),
                "kind": "dc" if fast else "ac",
                "unreported": unreported,
                "avg_power_kw": round(avg_kw, 1) if avg_kw is not None else None,
                "energy_added_kwh": round(energy, 1) if energy is not None else None,
            }
        )
    return out


def _hour_epoch(key: str) -> float | None:
    """A UTC ISO hour key (``"2026-09-26T20:00"``) as epoch seconds."""
    try:
        parsed = datetime.fromisoformat(key)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def mean_hourly_temp(
    hourly: dict[str, float], start_ts: float, end_ts: float | None
) -> float | None:
    """Mean of the hourly temperatures during ``[start_ts, end_ts]``.

    Hours within 30 minutes either side count; a session shorter than that
    takes the hour nearest its midpoint (within 90 minutes). None when no
    hour is close enough.
    """
    end = end_ts if end_ts is not None else start_ts
    points = [
        (ts, float(v))
        for k, v in hourly.items()
        if v is not None and (ts := _hour_epoch(k)) is not None
    ]
    inside = [v for ts, v in points if start_ts - 1800.0 <= ts <= end + 1800.0]
    if inside:
        return round(sum(inside) / len(inside), 1)
    mid = (start_ts + end) / 2.0
    near = [(abs(ts - mid), v) for ts, v in points if abs(ts - mid) <= 5400.0]
    return round(min(near)[1], 1) if near else None


def peak_from_soc_samples(
    samples: Iterable[dict[str, Any]],
    capacity_kwh: float | None,
    min_window_s: float = 900.0,
) -> float | None:
    """Highest charging rate (kW) over any ``min_window_s``+ stretch of SoC points.

    For an AC session, whose stored points are a coarse SoC trace with no
    power: each point is compared with the first later point at least
    ``min_window_s`` after it. None without a capacity or two usable points.
    """
    if not capacity_kwh:
        return None
    pts: list[Point] = []
    for sample in samples:
        ts = sample.get("timestamp")
        soc = sample.get("soc")
        if ts is None or soc is None:
            continue
        try:
            parsed = datetime.fromisoformat(str(ts))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        pts.append((parsed.timestamp(), float(soc)))
    pts = _monotonic(pts)
    best: float | None = None
    for i, (t0, s0) in enumerate(pts):
        for t1, s1 in pts[i + 1 :]:
            if t1 - t0 >= min_window_s:
                rate = (s1 - s0) / 100.0 * capacity_kwh / ((t1 - t0) / 3600.0)
                if rate > 0 and (best is None or rate > best):
                    best = rate
                break
    return round(best, 1) if best is not None else None


def charge_type(kind: str | None, avg_power_kw: float | None, l1_max_kw: float) -> str:
    """``'dc'``, ``'ac_l2'`` or ``'ac_l1'`` (AC averaging below ``l1_max_kw``)."""
    if kind == "dc":
        return "dc"
    if avg_power_kw is not None and 0 < avg_power_kw < l1_max_kw:
        return "ac_l1"
    return "ac_l2"
