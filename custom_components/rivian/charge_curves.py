"""Reference DC fast-charge curves and expected-time math (pure stdlib).

A curve is a dict with ``x`` (state of charge, %) and ``y`` (power, kW) lists
(ascending ``x``). ``DCFC_REFERENCE_CURVES`` holds one per pack (R1 Gen 1/Gen 2
Standard, Large and Max, and the R2); the Gen 1 curves are the ones the Plotly
dashboard has always drawn, the rest are approximate (see the notes below). :func:`pack_for` maps a vehicle model/capacity to a pack
key, and :func:`expected_minutes` / :func:`expected_avg_kw` integrate a curve
over an SoC range, so a session can be compared with what its pack should do.
"""

from __future__ import annotations

import re
from typing import Any, Final

# Power for a different pack size is the same curve in kW (the pack's charge
# rate is limited by its chemistry, not its size), so time scales with
# capacity: minutes = sum(d_soc / 100 * capacity_kwh / power_kw) * 60.
_STEP_PCT: Final[float] = 0.1
_X: Final[list[int]] = [
    10,
    15,
    20,
    25,
    30,
    35,
    40,
    45,
    50,
    55,
    60,
    65,
    70,
    75,
    80,
    85,
    90,
]


def _curve(
    key: str,
    label: str,
    models: tuple[str, ...],
    generation: str,
    nominal_kwh: float,
    approximate: bool,
    y: list[int],
    min_capacity: float = 0.0,
    max_capacity: float = 0.0,
) -> dict[str, Any]:
    """Build one reference entry. ``pack``/``name``/``capacity_kwh``/``min_capacity``/
    ``max_capacity`` are the older field names the callers already use."""
    return {
        "pack": key,
        "key": key,
        "name": label,
        "label": label,
        "models": models,
        "generation": generation,
        "nominal_kwh": nominal_kwh,
        "capacity_kwh": nominal_kwh,
        "min_capacity": min_capacity,
        "max_capacity": max_capacity,
        "approximate": approximate,
        "x": _X,
        "y": y,
    }


# Sources (all checked 2026-10): the R1 Gen1 curves are the ones the Plotly
# dashboard has always drawn. Gen2 curves are SHAPED to published 10-80 %
# times after the 2025.18 charging update (Standard ~27 min, Large ~35.5 min,
# Max ~38 min: rivianforums.com "Gen 2 Max Pack Fast Charging Speed with
# 2025.18.01 Update", recharged.com R1S charging test) and the ~215 kW peak
# Rivian quotes; pack sizes are the EPA/owner-reported net capacities (92.5 /
# 109 / 141.5 kWh). They are APPROXIMATE: Rivian publishes no curve.
# The R2 curve is shaped to Rivian's "10-80 % in 29 minutes" (rivian.com
# R2 fast charging story; an independent test measured ~27 min, 226 kW peak,
# evchargingstations.com) on the 87.9 kWh net pack. The R2 Standard pack is
# not yet announced with a capacity (reported 65-75 kWh): its curve is an
# unverified estimate (the R2 shape scaled to a ~70 kWh pack, 27 min 10-80 %).
DCFC_REFERENCE_CURVES: dict[str, dict[str, Any]] = {
    "standard": _curve(
        "standard",
        "R1 Gen 1 Standard Pack (106 kWh Ref)",
        ("R1T", "R1S"),
        "gen1",
        106.0,
        False,
        [205, 205, 200, 195, 185, 170, 155, 140, 125, 110, 95, 82, 70, 58, 45, 32, 20],
        0.0,
        115.0,
    ),
    "large": _curve(
        "large",
        "R1 Gen 1 Large Pack (135 kWh Ref)",
        ("R1T", "R1S"),
        "gen1",
        135.0,
        False,
        [
            215,
            215,
            212,
            208,
            200,
            185,
            170,
            155,
            145,
            130,
            118,
            105,
            92,
            78,
            62,
            45,
            28,
        ],
        115.0,
        139.0,
    ),
    "max": _curve(
        "max",
        "R1 Gen 1 Max Pack (149 kWh Ref)",
        ("R1T", "R1S"),
        "gen1",
        149.0,
        False,
        [
            220,
            220,
            218,
            215,
            210,
            198,
            185,
            172,
            160,
            146,
            132,
            118,
            104,
            88,
            70,
            50,
            32,
        ],
        139.0,
        200.0,
    ),
    "gen2_standard": _curve(
        "gen2_standard",
        "R1 Gen 2 Standard Pack (92.5 kWh LFP, approx.)",
        ("R1T", "R1S"),
        "gen2",
        92.5,
        True,
        [
            175,
            194,
            201,
            201,
            199,
            194,
            187,
            180,
            170,
            157,
            140,
            121,
            100,
            80,
            59,
            40,
            25,
        ],
    ),
    "gen2_large": _curve(
        "gen2_large",
        "R1 Gen 2 Large Pack (109 kWh, approx.)",
        ("R1T", "R1S"),
        "gen2",
        109.0,
        True,
        [
            205,
            205,
            202,
            198,
            190,
            176,
            162,
            147,
            138,
            124,
            112,
            100,
            88,
            74,
            59,
            43,
            27,
        ],
    ),
    "gen2_max": _curve(
        "gen2_max",
        "R1 Gen 2 Max Pack (141.5 kWh, approx.)",
        ("R1T", "R1S"),
        "gen2",
        141.5,
        True,
        [
            215,
            215,
            215,
            215,
            213,
            208,
            195,
            181,
            168,
            154,
            139,
            124,
            109,
            93,
            74,
            53,
            34,
        ],
    ),
    # APPROXIMATE (see the notes above).
    "r2": _curve(
        "r2",
        "R2 Pack (87.9 kWh, estimated)",
        ("R2",),
        "r2",
        87.9,
        True,
        [185, 200, 210, 210, 204, 193, 180, 165, 148, 131, 114, 97, 81, 66, 50, 36, 24],
    ),
    "r2_standard": _curve(
        "r2_standard",
        "R2 Standard Pack (~70 kWh, unverified estimate)",
        ("R2",),
        "r2",
        70.0,
        True,
        [157, 170, 179, 179, 174, 164, 153, 140, 126, 111, 97, 83, 69, 56, 43, 31, 20],
    ),
}

DEFAULT_PACK: Final[str] = "large"
# "R2" as a word of a model label ("R2", "2027 R2"), not inside another name.
_R2_PATTERN: Final[re.Pattern[str]] = re.compile("(?:^|[^A-Z0-9])R2")
GEN2_FIRST_MODEL_YEAR: Final[int] = 2025


def pack_for(
    model: str | None,
    capacity_kwh: float | None,
    model_year: int | None = None,
) -> str:
    """Return the reference pack key for a vehicle.

    The model picks the family (R2 vs R1), a model year of 2025 or later
    picks Gen 2 for an R1, and within that the pack whose nominal capacity is
    nearest the reported one. The reported capacity falls with age, so a
    degraded Gen 1 Large (123 kWh) still maps to Large. With no capacity an
    R1 is assumed to be a Large pack and an R2 the 87.9 kWh pack.
    """
    if model and _R2_PATTERN.search(str(model).upper()):
        if capacity_kwh and abs(capacity_kwh - 70.0) < abs(capacity_kwh - 87.9):
            return "r2_standard"
        return "r2"
    if model_year is not None:
        gen2 = model_year >= GEN2_FIRST_MODEL_YEAR
    else:
        gen2 = bool(model and re.search(r"GEN\s*2", str(model).upper()))
        if not gen2 and capacity_kwh:
            # Net capacities only Gen 2 packs have (Gen 1: 105/106, ~123-135, 149).
            gen2 = any(abs(capacity_kwh - c) <= 2.0 for c in (92.5, 109.0, 141.5))
    prefix = "gen2_" if gen2 else ""
    if not capacity_kwh:
        return f"{prefix}large" if gen2 else DEFAULT_PACK
    candidates = [
        (key, ref)
        for key, ref in DCFC_REFERENCE_CURVES.items()
        if ref["generation"] == ("gen2" if gen2 else "gen1")
    ]
    return min(candidates, key=lambda kr: abs(kr[1]["nominal_kwh"] - capacity_kwh))[0]


def reference(pack: str) -> dict[str, Any]:
    """Return the reference curve entry for a pack key (falls back to Large)."""
    return DCFC_REFERENCE_CURVES.get(pack) or DCFC_REFERENCE_CURVES[DEFAULT_PACK]


def _axes(curve: dict[str, Any]) -> tuple[list[float], list[float]]:
    xs = curve.get("x") if "x" in curve else curve.get("soc")
    ys = curve.get("y") if "y" in curve else curve.get("kw")
    return [float(v) for v in xs or []], [float(v) for v in ys or []]


def power_at(curve: dict[str, Any], soc: float) -> float:
    """Return the curve's power (kW) at ``soc``, linear between points and
    held flat beyond the first/last point."""
    xs, ys = _axes(curve)
    if not xs:
        return 0.0
    if soc <= xs[0]:
        return ys[0]
    if soc >= xs[-1]:
        return ys[-1]
    for i in range(1, len(xs)):
        if soc <= xs[i]:
            span = xs[i] - xs[i - 1]
            frac = 0.0 if span <= 0 else (soc - xs[i - 1]) / span
            return ys[i - 1] + (ys[i] - ys[i - 1]) * frac
    return ys[-1]


def expected_minutes(
    curve: dict[str, Any],
    soc_from: float,
    soc_to: float,
    capacity_kwh: float | None = None,
) -> float | None:
    """Minutes the curve needs to charge ``soc_from`` -> ``soc_to`` (%).

    Integrates energy / power over the range. ``capacity_kwh`` defaults to the
    curve's reference pack. Returns None for an empty/backwards range or a
    curve with no usable power.
    """
    if soc_to <= soc_from:
        return None
    capacity = capacity_kwh or float(curve.get("capacity_kwh") or 0.0)
    if capacity <= 0:
        return None
    steps = max(1, round((soc_to - soc_from) / _STEP_PCT))
    step = (soc_to - soc_from) / steps
    hours = 0.0
    for i in range(steps):
        power = power_at(curve, soc_from + (i + 0.5) * step)
        if power <= 0:
            return None
        hours += (step / 100.0 * capacity) / power
    return hours * 60.0


def expected_avg_kw(
    curve: dict[str, Any],
    soc_from: float,
    soc_to: float,
    capacity_kwh: float | None = None,
) -> float | None:
    """Average power (kW) the curve would deliver over ``soc_from`` -> ``soc_to``."""
    minutes = expected_minutes(curve, soc_from, soc_to, capacity_kwh)
    if not minutes:
        return None
    capacity = capacity_kwh or float(curve.get("capacity_kwh") or 0.0)
    return (soc_to - soc_from) / 100.0 * capacity / (minutes / 60.0)
