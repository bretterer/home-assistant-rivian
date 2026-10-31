/**
 * rivian-charging-card.js
 *
 * `custom:rivian-charging-card` is the Charging tab: one panel card with the
 * shared vehicle bar in its header and, top to bottom:
 *   1. Battery % over time (one line per selected vehicle, SoC bands with a
 *      labeled 50 % line, charging sessions as shaded spans, a shared cursor
 *      readout, and brush-to-zoom that refetches the timeline for the span and
 *      filters the fast-charge list and history table to it).
 *   2. A scorecard per vehicle (fast / AC charge counts, ended above 80 %,
 *      started below 20 %, time in 20-80 %). Tapping a count highlights those
 *      sessions on the timeline and filters the history table.
 *   3. Fast-charge sessions: a checkbox list (timeframe / network / charger
 *      filters, color by brand or vehicle) and a power-vs-SoC chart with a
 *      colored legend entry per session over each pack's white ideal curve
 *      (a +-10 % band).
 *   4. Battery health: capacity over time, dots colored by temperature, kWh
 *      (left) and % of original (right) as two scales of one line, projected
 *      range behind a toggle.
 *   5. The charging history table (AC and DC), sortable and filterable.
 *
 * Backend calls (all `hass.callWS`):
 *   - `rivian/vehicles/list` and the shared selection (rivian-vehicle-bar.js)
 *   - `rivian/charging/sessions {vins}`, `rivian/charging/reference {vins}`,
 *     `rivian/battery/soc_timeline {vins, start, end}`,
 *     `rivian/battery/capacity {vins}`
 *   - `rivian/charging/delete_session {vin, session_id}` (admin only)
 *   - `rivian/analytics/subscribe {vins}` for live refreshes
 *
 * Optional config: `vins` pins the card to those vehicles (otherwise it
 * follows the shared selection). Charts are inline SVG, no chart library.
 *
 * Pure helpers are exported for the Node test (tests/frontend/
 * charging_card.test.mjs); every top-level use of HTMLElement/customElements/
 * window/document is guarded so the module imports under plain Node.
 */

// -- constants ---------------------------------------------------------------

/** SoC bands, bottom to top. Tones map to the red/orange/green status colors. */
export const BANDS = [
  { key: "red_low", from: 0, to: 10, tone: "red", label: "stress" },
  { key: "orange_low", from: 10, to: 20, tone: "orange", label: "caution" },
  { key: "green", from: 20, to: 80, tone: "green", label: "ideal 20–80 %" },
  { key: "orange_high", from: 80, to: 90, tone: "orange", label: "caution" },
  { key: "red_high", from: 90, to: 100, tone: "red", label: "stress" },
];

/** The timeline's range toggle: key -> days (null = everything). */
export const RANGES = [
  ["7d", 7],
  ["30d", 30],
  ["1y", 365],
  ["all", null],
];

const DAY_S = 86400;
const MAX_ALL_DAYS = 5 * 365;
const DASHES = ["", "7 3", "2 3", "9 3 2 3"];
const STACK_BREAKPOINT_PX = 700;
const FALLBACK_COLOR = "#8a8a8a";

// -- pure helpers ------------------------------------------------------------

/** A band's tooltip: "Ideal 20-80 %: the gentlest range for the battery". */
export function bandTitle(band) {
  const range = `${band.from}\u2013${band.to} %`;
  if (band.tone === "green") return `Ideal ${range}: the gentlest range for the battery`;
  if (band.tone === "orange") return `Caution ${range}: spending long here wears the battery a little faster`;
  return `Stress ${range}: avoid sitting here for long`;
}

/** Which SoC band a percentage falls in (see BANDS). */
export function socBand(soc) {
  if (typeof soc !== "number" || Number.isNaN(soc)) return null;
  if (soc < 10) return "red_low";
  if (soc < 20) return "orange_low";
  if (soc <= 80) return "green";
  if (soc <= 90) return "orange_high";
  return "red_high";
}

/** The `{start, end}` epoch-second window for a range key. `earliest` bounds "all". */
export function rangeWindow(key, nowSec, earliestSec = null) {
  const entry = RANGES.find((r) => r[0] === key) || RANGES[1];
  const end = nowSec;
  if (entry[1] !== null) return { start: end - entry[1] * DAY_S, end };
  const floor = end - MAX_ALL_DAYS * DAY_S;
  const first = typeof earliestSec === "number" && earliestSec > 0 ? earliestSec : end - 365 * DAY_S;
  return { start: Math.max(floor, Math.min(first, end - 7 * DAY_S)), end };
}

/** A linear scale function mapping [d0, d1] onto [r0, r1]. */
export function scaleLinear(d0, d1, r0, r1) {
  const span = d1 - d0;
  return (v) => (span === 0 ? (r0 + r1) / 2 : r0 + ((v - d0) / span) * (r1 - r0));
}

/** The inverse of `scaleLinear`. */
export function invertLinear(d0, d1, r0, r1) {
  const span = r1 - r0;
  return (px) => (span === 0 ? d0 : d0 + ((px - r0) / span) * (d1 - d0));
}

/** "Nice" tick values covering [min, max], about `count` of them. */
export function niceTicks(min, max, count = 5) {
  if (!(max > min)) return [min];
  const rough = (max - min) / Math.max(1, count);
  const pow = Math.pow(10, Math.floor(Math.log10(rough)));
  const frac = rough / pow;
  const step = (frac <= 1 ? 1 : frac <= 2 ? 2 : frac <= 5 ? 5 : 10) * pow;
  const out = [];
  for (let v = Math.ceil(min / step) * step; v <= max + step * 1e-9; v += step) {
    out.push(Math.round(v / step) * step);
  }
  return out;
}

/** Round a [min, max] domain outward to nice ends. */
export function niceDomain(min, max, count = 5) {
  if (!(max > min)) return [min - 1, max + 1];
  const ticks = niceTicks(min, max, count);
  if (ticks.length < 2) return [min, max];
  const step = ticks[1] - ticks[0];
  return [Math.floor(min / step) * step, Math.ceil(max / step) * step];
}

/** Plot-area geometry for an SVG chart of `width` x `height` with margins. */
export function chartLayout(width, height, margin = {}) {
  const m = { left: 36, right: 10, top: 8, bottom: 22, ...margin };
  const x0 = m.left;
  const x1 = Math.max(x0 + 10, width - m.right);
  const y0 = m.top;
  const y1 = Math.max(y0 + 10, height - m.bottom);
  return { width, height, x0, x1, y0, y1, w: x1 - x0, h: y1 - y0 };
}

function _tzParts(ts, tz) {
  const fmt = new Intl.DateTimeFormat("en-US", {
    timeZone: tz || undefined,
    hourCycle: "h23",
    year: "numeric",
    month: "numeric",
    day: "numeric",
    hour: "numeric",
    minute: "numeric",
    second: "numeric",
  });
  const out = {};
  for (const p of fmt.formatToParts(new Date(ts * 1000))) out[p.type] = Number(p.value);
  return out;
}

/** Epoch seconds of local midnight for the day containing `ts` in `tz`. */
export function localMidnight(ts, tz) {
  try {
    const p = _tzParts(ts, tz);
    return Math.floor(ts) - (p.hour * 3600 + p.minute * 60 + p.second);
  } catch (_err) {
    return Math.floor(ts / DAY_S) * DAY_S;
  }
}

const _TICK_STEPS_DAYS = [1, 2, 7, 14, 30, 61, 91, 182, 365, 730];

/** Day-aligned time-axis ticks `[{ts, label}]` between start and end. */
export function timeTicks(start, end, tz, maxTicks = 7) {
  if (!(end > start)) return [];
  const spanDays = (end - start) / DAY_S;
  if (spanDays <= 3) return hourTicks(start, end, tz, maxTicks);
  // Long spans tick on month starts labeled "Jan 2026": a day label like
  // "Jan 23" on a year-long axis reads as a year.
  if (spanDays > 120) return monthTicks(start, end, tz, maxTicks);
  let step = _TICK_STEPS_DAYS.find((d) => spanDays / d <= maxTicks);
  if (step === undefined) step = _TICK_STEPS_DAYS[_TICK_STEPS_DAYS.length - 1];
  const withYear = spanDays > 400;
  let opts = { month: "short", day: "numeric" };
  if (withYear) opts = { month: "short", year: "2-digit" };
  let fmt;
  try {
    fmt = new Intl.DateTimeFormat(undefined, { ...opts, timeZone: tz || undefined });
  } catch (_err) {
    fmt = new Intl.DateTimeFormat(undefined, opts);
  }
  const ticks = [];
  let t = localMidnight(start, tz);
  if (t < start) t = localMidnight(t + DAY_S * 1.5, tz);
  let guard = 0;
  while (t <= end && guard++ < 200) {
    ticks.push({ ts: t, label: fmt.format(new Date(t * 1000)) });
    t = localMidnight(t + step * DAY_S + DAY_S / 2, tz);
  }
  return ticks;
}

/** Ticks on local month starts between start and end, labeled "Jan 2026". */
export function monthTicks(start, end, tz, maxTicks = 7) {
  if (!(end > start)) return [];
  let fmt;
  const opts = { month: "short", year: "numeric" };
  try {
    fmt = new Intl.DateTimeFormat(undefined, { ...opts, timeZone: tz || undefined });
  } catch (_err) {
    fmt = new Intl.DateTimeFormat(undefined, opts);
  }
  const dayOf = (ts) => {
    try {
      return _tzParts(ts, tz).day;
    } catch (_err) {
      return new Date(ts * 1000).getUTCDate();
    }
  };
  // Local midnight on the 1st of the month containing `ts`.
  const monthStart = (ts) => {
    const m = localMidnight(ts, tz);
    return localMidnight(m - (dayOf(m) - 1) * DAY_S + DAY_S / 2, tz);
  };
  const starts = [];
  let t = monthStart(start);
  if (t < start) t = monthStart(t + 32 * DAY_S);
  let guard = 0;
  while (t <= end && guard++ < 400) {
    starts.push(t);
    t = monthStart(t + 32 * DAY_S);
  }
  const step = [1, 2, 3, 6, 12, 24, 60].find((k) => Math.ceil(starts.length / k) <= maxTicks) || 60;
  return starts.filter((_ts, i) => i % step === 0).map((ts) => ({ ts, label: fmt.format(new Date(ts * 1000)) }));
}

/** Linear interpolation of `[[x, y], ...]` (sorted by x) at `x`; null outside the series. */
export function valueAt(points, x) {
  if (!Array.isArray(points) || !points.length) return null;
  if (x < points[0][0] || x > points[points.length - 1][0]) return null;
  let lo = 0;
  let hi = points.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (points[mid][0] <= x) lo = mid;
    else hi = mid;
  }
  const a = points[lo];
  const b = points[hi];
  if (b[0] === a[0] || x <= a[0]) return a[1];
  return a[1] + ((b[1] - a[1]) * (x - a[0])) / (b[0] - a[0]);
}

/** "27 min", "1 h 12 min", "45 s"; "–" when missing. */
export function formatDuration(seconds) {
  if (typeof seconds !== "number" || Number.isNaN(seconds) || seconds < 0) return "–";
  if (seconds < 60) return `${Math.round(seconds)} s`;
  const mins = Math.round(seconds / 60);
  if (mins < 60) return `${mins} min`;
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  return m ? `${h} h ${m} min` : `${h} h`;
}

function _fmt(ts, tz, opts) {
  if (typeof ts !== "number" || Number.isNaN(ts)) return "–";
  const d = new Date(ts * 1000);
  try {
    return new Intl.DateTimeFormat(undefined, { ...opts, timeZone: tz || undefined }).format(d);
  } catch (_err) {
    return d.toLocaleString();
  }
}

/** "Sep 20, 3:35 PM" */
export function formatWhen(ts, tz) {
  return _fmt(ts, tz, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

/** "Sep 20" */
export function formatDay(ts, tz) {
  return _fmt(ts, tz, { month: "short", day: "numeric" });
}

/** "Fast charge" / "AC charge". */
export function kindLabel(kind) {
  return kind === "dc" ? "Fast charge" : "AC charge";
}

/** An AC session averaging below this (kW) is Level 1 (mirrors the backend's AC_L1_MAX_KW). */
export const AC_L1_MAX_KW = 2.0;

/** "dc" | "ac_l2" | "ac_l1" | "ac" (inferred, rate unknown): the server's `charge_type`, else derived from kind and average power. */
export function chargeTypeKey(session) {
  if (session && session.charge_type) return session.charge_type;
  if (session && session.kind === "dc") return "dc";
  const avg = session && session.avg_power_kw;
  return typeof avg === "number" && avg > 0 && avg < AC_L1_MAX_KW ? "ac_l1" : "ac_l2";
}

const CHARGE_TYPE_LABELS = { dc: "DC Fast", ac_l2: "AC L2", ac_l1: "AC L1", ac: "AC" };
const CHARGE_TYPE_LONG = {
  dc: "DC fast charge",
  ac_l2: "AC Level 2 charge",
  ac_l1: "AC Level 1 charge",
  ac: "AC charge, rate unknown (inferred from the battery level)",
};

/** "DC Fast" / "AC L2" / "AC L1". */
export function chargeTypeLabel(session) {
  return CHARGE_TYPE_LABELS[chargeTypeKey(session)];
}

/** "DC fast charge" / "AC Level 2 charge" / "AC Level 1 charge". */
export function chargeTypeLong(session) {
  return CHARGE_TYPE_LONG[chargeTypeKey(session)];
}

/** "7.2 kW" (one decimal under 20 kW), or "–". */
export function formatRate(kw) {
  if (typeof kw !== "number" || Number.isNaN(kw) || kw <= 0) return "–";
  return `${kw < 20 ? kw.toFixed(1) : Math.round(kw)} kW`;
}

/** "72 °F", or "–". */
export function formatTempF(value) {
  return typeof value === "number" && !Number.isNaN(value) ? `${Math.round(value)} °F` : "–";
}

function _num(v, digits = 0) {
  return typeof v === "number" && !Number.isNaN(v) ? v.toFixed(digits) : "–";
}

/** "37 → 77 %" */
export function socRange(session) {
  return `${_num(session && session.start_soc, 0)} → ${_num(session && session.end_soc, 0)} %`;
}

/** The session's place label, or "–". */
export function placeLabel(session) {
  return (session && session.place && session.place.label) || "–";
}

/** Stable key for a session across vehicles. */
export function sessionKey(session) {
  return `${session.vin}|${session.session_id}`;
}

/** Tooltip lines for a session: vehicle, kind, place, SoC range, kWh, power, duration. */
export function sessionTooltipLines(session, vehicleName, tz) {
  const lines = [`${vehicleName || "Vehicle"} · ${chargeTypeLong(session)}${session.inferred ? " · inferred" : ""}`];
  const where = placeLabel(session);
  lines.push(`${where !== "–" ? `${where} · ` : ""}${formatWhen(session.start_ts, tz)}`);
  const station = stationLine(session);
  if (station) lines.push(station);
  lines.push(`${socRange(session)} · ${_num(session.energy_added_kwh, 1)} kWh`);
  if (session.kind === "dc" || typeof session.max_power_kw === "number") {
    lines.push(`Peak ${formatRate(session.max_power_kw)} · avg ${formatRate(session.avg_power_kw)}`);
  }
  const temps = [];
  if (typeof session.battery_temp_f === "number") temps.push(`battery ${formatTempF(session.battery_temp_f)}`);
  if (typeof session.outside_temp_f === "number") temps.push(`outside ${formatTempF(session.outside_temp_f)}`);
  if (temps.length) lines.push(temps.join(" · ").replace(/^./, (c) => c.toUpperCase()));
  lines.push(formatDuration(session.duration_s));
  if (session.inferred) lines.push(INFERRED_TITLE);
  return lines;
}

/**
 * The timeline payload's `detected` charges (`{vin: [{start_ts, end_ts,
 * start_soc, end_soc, kind, avg_power_kw, energy_added_kwh}]}`: charges the
 * battery level shows but no recorded session covers) as span objects for the
 * selected `vins`, each flagged `detected: true`.
 */
export function detectedSessions(detected, vins) {
  const out = [];
  for (const vin of vins || []) {
    for (const d of (detected && detected[vin]) || []) {
      if (typeof d.start_ts !== "number" || typeof d.end_ts !== "number") continue;
      out.push({ ...d, vin, detected: true, duration_s: d.end_ts - d.start_ts });
    }
  }
  return out.sort((a, b) => a.start_ts - b.start_ts);
}

/** Tooltip lines for a detected (unrecorded) charge. */
export function detectedTooltipLines(span, vehicleName, tz) {
  const kind = span.kind === "dc" ? "Fast charge" : "Slow charge (AC)";
  const lines = [`${vehicleName || "Vehicle"} · ${kind} · detected`, formatWhen(span.start_ts, tz)];
  const energy = typeof span.energy_added_kwh === "number" ? ` · ~${_num(span.energy_added_kwh, 1)} kWh` : "";
  lines.push(`${socRange(span)}${energy}`);
  if (typeof span.avg_power_kw === "number") lines.push(`avg ~${_num(span.avg_power_kw, 1)} kW`);
  if (span.unreported) lines.push("Home Assistant got no updates from the car for part of it; the end time is estimated");
  lines.push(formatDuration(span.duration_s));
  lines.push("Not in the charging history: seen in the battery level");
  return lines;
}

/** "27 min vs 24 min expected · 91 % of expected" for a DC session, else "". */
export function expectedText(session) {
  const ex = session && session.expected;
  if (!ex || typeof ex.minutes !== "number") return "";
  const actual = formatDuration(session.duration_s);
  const approx = ex.approximate ? "~" : "";
  const pct = typeof ex.pct_of_expected === "number" ? ` · ${Math.round(ex.pct_of_expected)} % of expected` : "";
  return `${actual} vs ${approx}${Math.round(ex.minutes)} min expected${pct}`;
}

/** Does a session satisfy the scorecard metric? (strict above / below, as the backend counts.) */
export function matchesMetric(session, metric) {
  const end = session.end_soc;
  const start = session.start_soc;
  switch (metric) {
    case "dc":
      return session.kind === "dc";
    case "ac":
      return session.kind === "ac";
    case "end80":
      return typeof end === "number" && end > 80;
    case "end90":
      return typeof end === "number" && end > 90;
    case "start20":
      return typeof start === "number" && start < 20;
    case "start10":
      return typeof start === "number" && start < 10;
    default:
      return true;
  }
}

/** Filter = `{vin, metric, fastOnly}` (null = no filter). */
export function matchesFilter(session, filter) {
  if (!filter) return true;
  if (filter.vin && session.vin !== filter.vin) return false;
  if (filter.fastOnly && session.kind !== "dc") return false;
  return matchesMetric(session, filter.metric);
}

/** The sessions satisfying a filter. */
export function filterSessions(sessions, filter) {
  return (Array.isArray(sessions) ? sessions : []).filter((s) => matchesFilter(s, filter));
}

const _METRIC_LABELS = {
  dc: "Fast charges",
  ac: "AC charges",
  end80: "Ended above 80 %",
  end90: "Ended above 90 %",
  start20: "Started below 20 %",
  start10: "Started below 10 %",
};

/** "Rivi · Ended above 70 % (fast only)". */
export function filterLabel(filter, vehicleName) {
  if (!filter) return "";
  const base = _METRIC_LABELS[filter.metric] || filter.metric;
  const fast = filter.fastOnly && filter.metric !== "dc" ? " (fast only)" : "";
  return `${vehicleName ? `${vehicleName} · ` : ""}${base}${fast}`;
}

/** Same filter? (tapping the active count again clears it.) */
export function sameFilter(a, b) {
  if (!a || !b) return false;
  return a.vin === b.vin && a.metric === b.metric && !!a.fastOnly === !!b.fastOnly;
}

/**
 * The scorecard tiles for one vehicle from `counts_by_vin[vin]` and
 * `time_in_band[vin]`. `all` counts every session, `fast` only DC ones
 * (null when the tile is DC-only or AC-only).
 */
export function scorecardTiles(counts, band) {
  const c = counts || {};
  const n = (v) => (typeof v === "number" ? v : 0);
  const tiles = [
    { id: "dc", label: "Fast charges", metric: "dc", all: n(c.dc), fast: null },
    { id: "ac", label: "Home / AC charges", metric: "ac", all: n(c.ac), fast: null },
    {
      id: "end80",
      label: "Ended above 80 %",
      metric: "end80",
      all: n(c.ended_above_80),
      fast: n(c.dc_ended_above_80),
      sub: { label: "above 90 %", metric: "end90", all: n(c.ended_above_90), fast: n(c.dc_ended_above_90) },
    },
    {
      id: "start20",
      label: "Started below 20 %",
      metric: "start20",
      all: n(c.started_below_20),
      fast: n(c.dc_started_below_20),
      sub: { label: "below 10 %", metric: "start10", all: n(c.started_below_10), fast: n(c.dc_started_below_10) },
    },
  ];
  const pct = band && typeof band.b20_80 === "number" ? Math.round(band.b20_80 * 100) : null;
  return { tiles, timeInIdeal: pct === null ? "—" : `${pct} %` };
}

/** "12 · 3 fast" (or just "12" when there is no fast split). */
export function countText(all, fast) {
  return fast === null || fast === undefined ? `${all}` : `${all} · ${fast} fast`;
}

/** Newest first. */
export function sortNewestFirst(sessions) {
  return (Array.isArray(sessions) ? sessions : []).slice().sort((a, b) => (b.start_ts || 0) - (a.start_ts || 0));
}

/** The keys of the newest `n` DC sessions (the default checked set). */
export function defaultChecked(sessions, n = 3) {
  return new Set(
    sortNewestFirst((Array.isArray(sessions) ? sessions : []).filter((s) => s.kind === "dc"))
      .slice(0, n)
      .map(sessionKey)
  );
}

/** Sort the history table. `nameOf(vin)` supplies the vehicle name for the vehicle column. */
export function sortHistory(sessions, key, dir = "desc", nameOf = (v) => v) {
  const get = {
    when: (s) => s.start_ts || 0,
    vehicle: (s) => String(nameOf(s.vin) || "").toLowerCase(),
    place: (s) => placeLabel(s).toLowerCase(),
    type: (s) => ({ dc: 0, ac_l2: 1, ac_l1: 2, ac: 3 })[chargeTypeKey(s)],
    kind: (s) => s.kind || "",
    peak: (s) => (typeof s.max_power_kw === "number" ? s.max_power_kw : -1),
    avg: (s) => (typeof s.avg_power_kw === "number" ? s.avg_power_kw : -1),
    battery_temp: (s) => (typeof s.battery_temp_f === "number" ? s.battery_temp_f : -999),
    outside_temp: (s) => (typeof s.outside_temp_f === "number" ? s.outside_temp_f : -999),
    soc: (s) => (typeof s.end_soc === "number" ? s.end_soc : -1),
    kwh: (s) => (typeof s.energy_added_kwh === "number" ? s.energy_added_kwh : -1),
    duration: (s) => (typeof s.duration_s === "number" ? s.duration_s : -1),
  }[key] || ((s) => s.start_ts || 0);
  const sign = dir === "asc" ? 1 : -1;
  return (Array.isArray(sessions) ? sessions : []).slice().sort((a, b) => {
    const x = get(a);
    const y = get(b);
    if (x < y) return -sign;
    if (x > y) return sign;
    return (b.start_ts || 0) - (a.start_ts || 0);
  });
}

/** Linear interpolation of an expected curve `{soc: [], kw: []}` at `soc`; null outside it. */
export function curveAt(curve, soc) {
  if (!curve || !Array.isArray(curve.soc) || !curve.soc.length) return null;
  return valueAt(
    curve.soc.map((s, i) => [s, curve.kw[i]]),
    soc
  );
}

/** The expected-curve band: `{soc, lo, hi}` at +-`fraction` (default 10 %). */
export function referenceBand(curve, fraction = 0.1) {
  if (!curve || !Array.isArray(curve.soc) || !Array.isArray(curve.kw)) return { soc: [], lo: [], hi: [] };
  return {
    soc: curve.soc.slice(),
    lo: curve.kw.map((k) => k * (1 - fraction)),
    hi: curve.kw.map((k) => k * (1 + fraction)),
  };
}

/** SVG polygon path for a band (`xs`, `lo`, `hi`) through x/y scale functions. */
export function bandPath(band, xScale, yScale) {
  const n = band.soc.length;
  if (n < 2) return "";
  const top = band.soc.map((s, i) => `${xScale(s).toFixed(1)},${yScale(band.hi[i]).toFixed(1)}`);
  const bottom = band.soc.map((s, i) => `${xScale(s).toFixed(1)},${yScale(band.lo[i]).toFixed(1)}`).reverse();
  return `M${top.join("L")}L${bottom.join("L")}Z`;
}

/** The x (SoC) and y (kW) domains for the power chart from the checked sessions and references. */
export function powerDomains(sessions, references) {
  let minSoc = Infinity;
  let maxSoc = -Infinity;
  let maxKw = 0;
  for (const s of sessions) {
    for (const p of s.samples || []) {
      if (typeof p.soc !== "number" || typeof p.power_kw !== "number") continue;
      minSoc = Math.min(minSoc, p.soc);
      maxSoc = Math.max(maxSoc, p.soc);
      maxKw = Math.max(maxKw, p.power_kw);
    }
  }
  for (const ref of references) {
    const curve = ref && ref.curve;
    if (!curve || !curve.soc || !curve.soc.length) continue;
    minSoc = Math.min(minSoc, curve.soc[0]);
    maxSoc = Math.max(maxSoc, curve.soc[curve.soc.length - 1]);
    maxKw = Math.max(maxKw, ...curve.kw.map((k) => k * 1.1));
  }
  if (!Number.isFinite(minSoc)) {
    minSoc = 10;
    maxSoc = 90;
  }
  const xLo = Math.max(0, Math.floor((minSoc - 2) / 10) * 10);
  const xHi = Math.min(100, Math.ceil((maxSoc + 2) / 10) * 10);
  const [, yHi] = niceDomain(0, Math.max(maxKw, 10), 5);
  return { x: [xLo, Math.max(xHi, xLo + 10)], y: [0, yHi] };
}

/** "−0.4 % since Sep 3 · 135.0 → 134.4 kWh" for a `battery/capacity` entry. */
export function capacitySummary(entry, tz) {
  const pct = (entry && entry.pct_points) || [];
  const kwh = (entry && entry.points) || [];
  if (!kwh.length) return "No capacity readings yet";
  const lastKwh = kwh[kwh.length - 1][1];
  if (pct.length < 2) return `${_num(lastKwh, 1)} kWh · only one reading so far`;
  const first = pct[0];
  const last = pct[pct.length - 1];
  const delta = Math.round((last[1] - first[1]) * 10) / 10;
  let head;
  if (delta === 0) head = `No change since ${formatDay(first[0], tz)}`;
  else head = `${delta < 0 ? "−" : "+"}${Math.abs(delta).toFixed(1)} % since ${formatDay(first[0], tz)}`;
  return `${head} · ${_num(kwh[0][1], 1)} → ${_num(lastKwh, 1)} kWh`;
}

/** "~376 mi projected full range", or "" when none. */
export function projectedRangeText(entry) {
  const pts = (entry && entry.projected_range) || [];
  if (!pts.length) return "";
  return `~${Math.round(pts[pts.length - 1][1])} mi projected full range`;
}

/** SVG path data for `[[x, y], ...]` through scale functions; breaks where `gap(prev, next)` is true. */
export function linePath(points, xScale, yScale, gap = null) {
  let d = "";
  let prev = null;
  for (const p of points) {
    const cmd = prev === null || (gap && gap(prev, p)) ? "M" : "L";
    d += `${cmd}${xScale(p[0]).toFixed(1)},${yScale(p[1]).toFixed(1)}`;
    prev = p;
  }
  return d;
}

/** The pixel x-extent `[x0, x1]` of a session span, at least `minPx` wide. */
export function spanExtent(session, xScale, minPx = 4) {
  const a = xScale(session.start_ts);
  const b = xScale(session.end_ts);
  const w = Math.max(minPx, b - a);
  return [a - (w - (b - a)) / 2, a - (w - (b - a)) / 2 + w];
}

/** The sessions whose span covers pixel `px` (with `slop` px of slack), nearest first. */
export function sessionsAtPixel(sessions, xScale, px, slop = 3) {
  return sessions
    .map((s) => ({ s, ext: spanExtent(s, xScale, 5) }))
    .filter(({ ext }) => px >= ext[0] - slop && px <= ext[1] + slop)
    .sort((a, b) => Math.abs(px - (a.ext[0] + a.ext[1]) / 2) - Math.abs(px - (b.ext[0] + b.ext[1]) / 2))
    .map(({ s }) => s);
}

/** Confirm text for deleting a session. */
export function deleteMessage(session, vehicleName, tz) {
  const where = placeLabel(session);
  return (
    `Delete this ${kindLabel(session.kind).toLowerCase()}` +
    `${vehicleName ? ` for ${vehicleName}` : ""}?\n\n` +
    `${formatWhen(session.start_ts, tz)}${where !== "–" ? ` · ${where}` : ""}\n` +
    `${socRange(session)} · ${_num(session.energy_added_kwh, 1)} kWh\n\n` +
    "This cannot be undone."
  );
}

/** HTML-escape text for string-built markup. */
export function esc(text) {
  return String(text === null || text === undefined ? "" : text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/**
 * The instructions shown under the Battery level chart, in order. The current
 * state's most useful step comes first (getting back out when zoomed in).
 */
export function batteryChartDirections(zoomed) {
  const out = "Double-click the chart (or press Reset zoom) to return to the full time range.";
  const steps = [
    "Drag across the chart to zoom in on a span.",
    "On touch: press and hold, drag, then tap Zoom to selection.",
    out,
    "Hover or tap for values; arrow keys step through charging sessions.",
    "The 7d / 30d / 1y / All buttons set the full time range.",
  ];
  return zoomed ? [out, ...steps.filter((s) => s !== out)] : steps;
}

// -- zoom (brush) -------------------------------------------------------------

/** Smallest zoomable span: half an hour (a single charge is rarely shorter). */
export const MIN_ZOOM_S = 1800;

const HISTORY_PREFS_KEY = "rivian-charging-history-filters";

/** Fewest pixels a drag must cover to count as a selection rather than a click. */
export const BRUSH_MIN_PX = 8;

/**
 * A brush drag from `px0` to `px1` (SVG pixels) over a plot `g` whose x-axis
 * spans [x0, x1] seconds -> `{start, end}` epoch seconds, or null when the drag
 * was too short (a click). Order-independent, clamped to the plot, widened to
 * `minSpan` around its centre and kept inside [x0, x1].
 */
export function brushSpan(px0, px1, g, x0, x1, minPx = BRUSH_MIN_PX, minSpan = MIN_ZOOM_S) {
  const a = Math.min(Math.max(Math.min(px0, px1), g.x0), g.x1);
  const b = Math.min(Math.max(Math.max(px0, px1), g.x0), g.x1);
  if (b - a < minPx) return null;
  const inv = invertLinear(x0, x1, g.x0, g.x1);
  let start = inv(a);
  let end = inv(b);
  if (end - start < minSpan) {
    const mid = (start + end) / 2;
    start = mid - minSpan / 2;
    end = mid + minSpan / 2;
  }
  if (start < x0) {
    end += x0 - start;
    start = x0;
  }
  if (end > x1) {
    start -= end - x1;
    end = x1;
  }
  return { start: Math.round(Math.max(start, x0)), end: Math.round(Math.min(end, x1)) };
}

/** True once a press has been held still for `holdMs` without drifting past `tolPx`. */
export function isLongPress(downMs, nowMs, movedPx, holdMs = 450, tolPx = 10) {
  return nowMs - downMs >= holdMs && movedPx <= tolPx;
}

/** "Sep 20, 3:35 PM – Sep 24, 9:00 AM", or "Sep 20, 3:35 PM – 5:10 PM" within one day. */
export function spanLabel(span, tz) {
  if (!span) return "";
  if (formatDay(span.start, tz) === formatDay(span.end, tz)) {
    const t = (ts) => _fmt(ts, tz, { hour: "numeric", minute: "2-digit" });
    return `${formatDay(span.start, tz)}, ${t(span.start)} – ${t(span.end)}`;
  }
  return `${formatWhen(span.start, tz)} – ${formatWhen(span.end, tz)}`;
}

const _HOUR_STEPS = [1, 2, 3, 4, 6, 12, 24];

/** Hour-aligned ticks `[{ts, label}]` for a short (up to ~3 day) window. */
export function hourTicks(start, end, tz, maxTicks = 7) {
  if (!(end > start)) return [];
  const spanH = (end - start) / 3600;
  const step = _HOUR_STEPS.find((h) => spanH / h <= maxTicks) || 24;
  let hourFmt;
  let dayFmt;
  try {
    hourFmt = new Intl.DateTimeFormat(undefined, { hour: "numeric", timeZone: tz || undefined });
    dayFmt = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", timeZone: tz || undefined });
  } catch (_err) {
    hourFmt = new Intl.DateTimeFormat(undefined, { hour: "numeric" });
    dayFmt = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" });
  }
  const mid = localMidnight(start, tz);
  const ticks = [];
  let t = mid + Math.ceil((start - mid) / (step * 3600)) * step * 3600;
  let guard = 0;
  while (t <= end && guard++ < 100) {
    const isMidnight = t === localMidnight(t, tz);
    const d = new Date(t * 1000);
    ticks.push({ ts: t, label: isMidnight ? dayFmt.format(d) : hourFmt.format(d) });
    t += step * 3600;
  }
  return ticks;
}

// -- fast-charge filters, brand colors and legend labels ---------------------------

/** Brand display order (also the chip order). */
export const BRAND_ORDER = ["tesla", "rivian", "electrify_america", "chargepoint", "evgo", "blink", "home", "other"];

/**
 * Fixed brand colors `[light, dark]`, taken in the dataviz categorical order
 * (aqua, yellow, magenta, green, violet, red -- the validated adjacent run, so no
 * slot is a blue or orange that the first vehicles use); Home and Other are neutral.
 */
export const BRAND_COLORS = {
  tesla: ["#e34948", "#e66767"],
  rivian: ["#eda100", "#c98500"],
  electrify_america: ["#4a3aa7", "#9085e9"],
  chargepoint: ["#008300", "#008300"],
  evgo: ["#e87ba4", "#d55181"],
  blink: ["#1baf7a", "#199e70"],
  home: ["#52514e", "#c3c2b7"],
  other: ["#8a8a8a", "#8a8a8a"],
};

/** A brand's color for the theme; unknown brands fall back to Other. */
export function brandColor(brand, dark = false) {
  const pair = BRAND_COLORS[brand] || BRAND_COLORS.other;
  return pair[dark ? 1 : 0];
}

/** A session's line color: its brand's, or its vehicle's. */
export function sessionColor(session, mode, vehicleColor, dark = false) {
  return mode === "vehicle" ? vehicleColor : brandColor(session && session.brand, dark);
}

/** The brands present in `sessions` as `[{brand, label, count}]`, in `BRAND_ORDER`. */
export function brandsPresent(sessions) {
  const seen = new Map();
  for (const s of Array.isArray(sessions) ? sessions : []) {
    const brand = (s && s.brand) || "other";
    const row = seen.get(brand) || { brand, label: (s && s.brand_label) || brand, count: 0 };
    row.count += 1;
    seen.set(brand, row);
  }
  const rank = (b) => (BRAND_ORDER.includes(b) ? BRAND_ORDER.indexOf(b) : BRAND_ORDER.length);
  return [...seen.values()].sort((a, b) => rank(a.brand) - rank(b.brand) || a.label.localeCompare(b.label));
}

/** A session's charger tags: its version ("V3") and/or max output ("350 kW"). */
export function chargerTags(session) {
  const tags = [];
  const v = session && typeof session.station_version === "string" ? session.station_version.trim() : "";
  if (v) tags.push(v);
  const kw = session && session.charger_max_kw;
  if (typeof kw === "number" && kw > 0) tags.push(`${Math.round(kw)} kW`);
  return tags;
}

/** The distinct charger tags across `sessions`: versions first, then kW ascending. */
export function chargerTagsPresent(sessions) {
  const set = new Set();
  for (const s of Array.isArray(sessions) ? sessions : []) for (const t of chargerTags(s)) set.add(t);
  const isKw = (t) => t.endsWith(" kW");
  return [...set].sort((a, b) => {
    if (isKw(a) !== isKw(b)) return isKw(a) ? 1 : -1;
    return isKw(a) ? parseFloat(a) - parseFloat(b) : a.localeCompare(b, undefined, { numeric: true });
  });
}

/** The title for a range toggle key ("30d" -> "Last 30 days", "1y" -> "Last year", "all" -> "Everything"). */
export function rangeTitle(key) {
  const m = /^(\d+)([dy])$/.exec(String(key || ""));
  if (!m) return "Everything on record";
  const n = Number(m[1]);
  if (m[2] === "y") return n === 1 ? "Last year" : `Last ${n} years`;
  return `Last ${n} days`;
}

/** History table column explanations (hover/tap/focus). */
export const HISTORY_COLUMN_TITLES = {
  when: "When the charge started",
  vehicle: "Which vehicle was charged",
  place: "Where it was charged (station or place, when known)",
  type: "DC Fast = DC fast charging; AC L2 = Level 2 (240 V, home wall charger or public AC); AC L1 = Level 1 (120 V outlet, under 2 kW)",
  kind: "DC fast charging or AC charging",
  peak: "Fastest charging rate during the session (for AC, estimated from how fast the battery level rose)",
  avg: "Average charging rate: energy added divided by charging time",
  battery_temp: "Average battery temperature while charging, when the vehicle reports it",
  outside_temp: "Outside air temperature where and while it charged (from Open-Meteo weather history)",
  soc: "Battery percentage at the start and end of the charge",
  kwh: "Energy added to the battery, in kilowatt-hours",
  duration: "Time spent charging",
};

/** The `aria-sort` value for a table column header. */
export function ariaSortFor(sort, key) {
  if (!sort || sort.key !== key) return "none";
  return sort.dir === "asc" ? "ascending" : "descending";
}

/** The next index when stepping through `n` chart stops with the arrow keys (clamped; the first press starts at an end). */
export function stepIndex(current, delta, n) {
  if (!(n > 0)) return -1;
  if (current === null || current === undefined || current < 0) return delta < 0 ? n - 1 : 0;
  return Math.max(0, Math.min(n - 1, current + delta));
}

/** Scorecard explanations: tile label -> what it counts. */
export const SCORE_TILE_TITLES = {
  "Fast charges": "Number of DC fast-charge sessions in view",
  "Home / AC charges": "Number of home or Level 2 (AC) charging sessions in view",
  "Ended above 80 %": "Charges that ended above 80% battery (frequent charging this high is harder on the battery)",
  "Started below 20 %": "Charges that started below 20% battery (deep discharge is harder on the battery)",
  "above 90 %": "Charges that ended above 90% battery",
  "below 10 %": "Charges that started below 10% battery",
  "Time in 20–80 %": "Share of the time in range that the battery sat between 20% and 80%, the gentlest zone",
};

/** The tooltip for a scorecard count button: what it counts and what a tap does. */
export function scoreCountTitle(label, fastOnly) {
  const base = SCORE_TILE_TITLES[label] || label;
  return `${base}${fastOnly ? " (fast charges only)" : ""}. Tap to highlight these sessions on the timeline and filter the history; tap again to clear.`;
}

/** History type filter: [key, label, title] (All / Home / L1-L2 / Fast). */
export const HISTORY_TYPES = [
  ["all", "All", "Every charging session"],
  ["home", "Home", "Charging at home (a home charger, your Home zone or a place categorized as home)"],
  ["ac", "L1/L2", "Slow AC charging, Level 1 or Level 2, at home or elsewhere"],
  ["fast", "Fast", "DC fast charging"],
];

/** History timeframe chips: [key, label, days] (null = no limit). */
export const HISTORY_FRAMES = [
  ["week", "Week", 7],
  ["month", "Month", 30],
  ["year", "Year", 365],
  ["all", "All", null],
];

/** Does a session belong to the history type filter (`all` | `home` | `ac` | `fast`)? */
export function historyTypeMatches(session, type) {
  if (!type || type === "all") return true;
  if (!session) return false;
  if (type === "home") return !!(session.is_home_charge || session.is_home === true);
  if (type === "fast") return session.kind === "dc";
  if (type === "ac") return session.kind === "ac";
  return true;
}

/** Epoch second the history timeframe starts at (`null` = no limit). */
export function historyFrameStart(frame, nowSec) {
  const entry = HISTORY_FRAMES.find((f) => f[0] === frame);
  if (!entry || entry[2] === null) return null;
  return nowSec - entry[2] * DAY_S;
}

/** The sessions passing the history type and timeframe filters (a session counts by its end). */
export function filterHistory(sessions, type, frame, nowSec) {
  const start = historyFrameStart(frame, nowSec);
  return (Array.isArray(sessions) ? sessions : []).filter((s) => {
    if (!historyTypeMatches(s, type)) return false;
    if (start === null) return true;
    const end = typeof s.end_ts === "number" ? s.end_ts : s.start_ts;
    return typeof end === "number" && end >= start;
  });
}

/** "1 session" / "37 sessions". */
export function sessionCountText(n) {
  return `${n} session${n === 1 ? "" : "s"}`;
}

export const INFERRED_TITLE = "Not recorded by Home Assistant or Rivian: inferred from the battery level history";

/** Fast-charge timeframe chips: key -> days (null = everything). */
export const FAST_FRAMES = [
  ["30d", 30],
  ["90d", 90],
  ["1y", 365],
  ["all", null],
];

/** The active `{start, end}` for the fast list: a zoom wins over the timeframe chip; null = no limit. */
export function fastSpan(frame, zoom, nowSec) {
  if (zoom) return { start: zoom.start, end: zoom.end };
  const entry = FAST_FRAMES.find((f) => f[0] === frame);
  if (!entry || entry[1] === null) return null;
  return { start: nowSec - entry[1] * DAY_S, end: nowSec };
}

/** Does the session overlap the span (null = always)? */
export function inSpan(session, span) {
  if (!span) return true;
  const end = typeof session.end_ts === "number" ? session.end_ts : session.start_ts;
  return end >= span.start && session.start_ts <= span.end;
}

/** DC sessions inside `span` whose brand is in `brands` and charger tag in `tags` (empty set = no limit). */
export function filterFast(sessions, { span = null, brands = null, tags = null } = {}) {
  return (Array.isArray(sessions) ? sessions : []).filter((s) => {
    if (s.kind !== "dc") return false;
    if (!inSpan(s, span)) return false;
    if (brands && brands.size && !brands.has(s.brand || "other")) return false;
    if (tags && tags.size && !chargerTags(s).some((t) => tags.has(t))) return false;
    return true;
  });
}

/** The brand name for a session ("Electrify America"), or "" when unknown. */
export function brandName(session) {
  const label = session && session.brand_label;
  if (!label || (session.brand === "other" && label === "Other")) return "";
  return label;
}

/** Where a session was: its place label, else its station name, else "". */
export function whereText(session) {
  const p = placeLabel(session);
  if (p !== "–") return p;
  return (session && session.station_name) || "";
}

/**
 * "Sep 26 · Electrify America · Denver" -- the legend / row heading for a session.
 * The brand is dropped when the location already names it; `vehicleName` prefixes.
 */
export function sessionLegendLabel(session, tz, vehicleName = "") {
  const where = whereText(session);
  let brand = brandName(session);
  if (brand && where.toLowerCase().includes(brand.toLowerCase())) brand = "";
  return [vehicleName, formatDay(session.start_ts, tz), brand, where].filter(Boolean).join(" · ");
}

/** "Electrify America · 350 kW" -- the station detail line for a row ("" for none). */
export function stationLine(session) {
  const parts = [];
  const brand = brandName(session);
  const name = session && session.station_name;
  if (name && brand && name.toLowerCase().includes(brand.toLowerCase())) parts.push(name);
  else {
    if (brand) parts.push(brand);
    if (name) parts.push(name);
  }
  parts.push(...chargerTags(session));
  return parts.join(" · ");
}

/** Counts mirroring the backend's `session_counts` keys, for a zoomed span. */
export function countSessions(sessions) {
  const out = {
    total: 0,
    dc: 0,
    ac: 0,
    ended_above_80: 0,
    ended_above_90: 0,
    started_below_20: 0,
    started_below_10: 0,
    dc_ended_above_80: 0,
    dc_ended_above_90: 0,
    dc_started_below_20: 0,
    dc_started_below_10: 0,
  };
  for (const s of Array.isArray(sessions) ? sessions : []) {
    out.total += 1;
    const dc = s.kind === "dc";
    out[dc ? "dc" : "ac"] += 1;
    const bump = (key, hit) => {
      if (!hit) return;
      out[key] += 1;
      if (dc) out[`dc_${key}`] += 1;
    };
    bump("ended_above_80", typeof s.end_soc === "number" && s.end_soc > 80);
    bump("ended_above_90", typeof s.end_soc === "number" && s.end_soc > 90);
    bump("started_below_20", typeof s.start_soc === "number" && s.start_soc < 20);
    bump("started_below_10", typeof s.start_soc === "number" && s.start_soc < 10);
  }
  return out;
}

// -- battery health: temperature colors and dual axes --------------------------------

/** Temperature color stops `[degF, [r, g, b]]`: cold blue, mild green, hot red. */
const _TEMP_STOPS = {
  light: [
    [20, [42, 120, 214]],
    [60, [27, 175, 122]],
    [100, [227, 73, 72]],
  ],
  dark: [
    [20, [57, 135, 229]],
    [60, [25, 158, 112]],
    [100, [230, 103, 103]],
  ],
};

/** The temperature scale's end points (degF). */
export const TEMP_DOMAIN = [20, 100];

/** Ticks drawn under the legend gradient. */
export const TEMP_TICKS = [20, 40, 60, 80, 100];

/** `#rrggbb` for a temperature (degF) on the cold-blue / mild-green / hot-red scale; gray when unknown. */
export function tempColor(tempF, dark = false) {
  if (typeof tempF !== "number" || Number.isNaN(tempF)) return "#8a8a8a";
  const stops = _TEMP_STOPS[dark ? "dark" : "light"];
  const t = Math.min(Math.max(tempF, stops[0][0]), stops[stops.length - 1][0]);
  let i = 0;
  while (i < stops.length - 2 && t > stops[i + 1][0]) i += 1;
  const [t0, c0] = stops[i];
  const [t1, c1] = stops[i + 1];
  const f = (t - t0) / (t1 - t0);
  const ch = c0.map((v, k) => Math.round(v + (c1[k] - v) * f));
  return `#${ch.map((v) => v.toString(16).padStart(2, "0")).join("")}`;
}

/** CSS `linear-gradient` for the temperature legend bar. */
export function tempGradientCss(dark = false) {
  const span = TEMP_DOMAIN[1] - TEMP_DOMAIN[0];
  const stops = [];
  for (let t = TEMP_DOMAIN[0]; t <= TEMP_DOMAIN[1]; t += 10) stops.push(t);
  const parts = stops.map((t) => `${tempColor(t, dark)} ${(((t - TEMP_DOMAIN[0]) / span) * 100).toFixed(1)}%`);
  return `linear-gradient(to right, ${parts.join(", ")})`;
}

/** kWh for a percentage of the original capacity. */
export function pctToKwh(pct, original) {
  return (pct * original) / 100;
}

/** Percentage of the original capacity for a kWh value. */
export function kwhToPct(kwh, original) {
  return (kwh / original) * 100;
}

/**
 * Tick sets for one line drawn on two axes: `pct` ticks (right axis) and `kwh`
 * ticks (left axis), each `{v, pct}` where `pct` is the plotted position on
 * the shared percentage scale.
 */
export function dualAxisTicks(pLo, pHi, original, count = 4) {
  return {
    pct: niceTicks(pLo, pHi, count).map((v) => ({ v, pct: v })),
    kwh: niceTicks(pctToKwh(pLo, original), pctToKwh(pHi, original), count).map((v) => ({
      v,
      pct: kwhToPct(v, original),
    })),
  };
}

/** The common original capacity (kWh) when every entry's is within 2 %; else null (no kWh axis). */
export function sharedOriginal(entries) {
  const vals = (entries || []).map((e) => e && e.original_kwh).filter((v) => typeof v === "number" && v > 0);
  if (!vals.length) return null;
  const lo = Math.min(...vals);
  const hi = Math.max(...vals);
  return hi / lo <= 1.02 ? vals[0] : null;
}

/** Merge a capacity entry's `points` (kWh + temp) and `pct_points` by day -> `[{t, kwh, pct, temp, source}]`. */
export function healthPoints(entry) {
  const pct = new Map(((entry && entry.pct_points) || []).map((p) => [p[0], p[1]]));
  return ((entry && entry.points) || [])
    .filter((p) => pct.has(p[0]))
    .map((p) => ({
      t: p[0],
      kwh: p[1],
      pct: pct.get(p[0]),
      temp: typeof p[2] === "number" ? p[2] : null,
      source: p[3] || null,
    }));
}

/** The health chart's window keys: key -> days (null = all capacity history). */
export const HEALTH_RANGES = [
  ["90d", 90],
  ["1y", 365],
  ["all", null],
];

/** The merged health points limited to the last N days before `latest` (all = everything). */
export function limitHealth(points, key, latest) {
  const entry = HEALTH_RANGES.find((r) => r[0] === key);
  if (!entry || entry[1] === null) return points;
  return points.filter((p) => p.t >= latest - entry[1] * DAY_S);
}

/** Which temperature the dots show, per vehicle: battery, outside, or battery-else-outside. */
export function tempSourceNote(entries, nameOf, multi) {
  const parts = [];
  for (const e of entries || []) {
    const srcs = new Set(
      healthPoints(e)
        .filter((p) => p.temp !== null)
        .map((p) => p.source || "outside")
    );
    if (!srcs.size) continue;
    const text =
      srcs.size > 1 ? "battery temperature, else outside" : srcs.has("battery") ? "battery temperature" : "outside temperature";
    parts.push(multi ? `${nameOf(e.vin)}: ${text}` : text);
  }
  return parts.length ? `Dot color: ${parts.join(" · ")}` : "No temperature recorded";
}

/** "64 °F battery" / "52 °F outside" / "" for a health point. */
export function tempText(point) {
  if (!point || typeof point.temp !== "number") return "";
  return `${Math.round(point.temp)} °F ${point.source === "battery" ? "battery" : "outside"}`;
}

// -- the card ---------------------------------------------------------------

function _loadBarModule() {
  return import(new URL("./rivian-vehicle-bar.js" + new URL(import.meta.url).search, import.meta.url));
}

const _STYLE = `
  :host { display: block; }
  ha-card {
    --crc-text: var(--primary-text-color, #212121);
    --crc-muted: var(--secondary-text-color, #727272);
    --crc-line: var(--divider-color, #e0e0e0);
    --crc-surface: var(--ha-card-background, var(--card-background-color, #fff));
    --crc-band-green: rgba(46, 160, 67, 0.11);
    --crc-band-orange: rgba(237, 137, 0, 0.13);
    --crc-band-red: rgba(214, 50, 48, 0.12);
    --crc-ink-green: #2a7a3b;
    --crc-ink-orange: #a8610a;
    --crc-ink-red: #b3302e;
    display: block;
    padding: 0;
    background: var(--crc-surface);
    color: var(--crc-text);
  }
  ha-card.crc-dark {
    --crc-band-green: rgba(76, 190, 100, 0.16);
    --crc-band-orange: rgba(245, 160, 40, 0.18);
    --crc-band-red: rgba(240, 90, 85, 0.18);
    --crc-ink-green: #6fcf84;
    --crc-ink-orange: #f0b25a;
    --crc-ink-red: #f08a85;
  }
  .crc-topbar { border-bottom: 1px solid var(--crc-line); }
  .crc-topbar rivian-vehicle-bar { padding: 8px 12px; }
  .crc-chart:focus-visible { outline: 2px solid var(--primary-color, #03a9f4); outline-offset: 2px; }
  .crc-section { padding: 14px 16px 16px; border-bottom: 1px solid var(--crc-line); }
  .crc-section:last-child { border-bottom: 0; }
  .crc-head { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; margin-bottom: 8px; }
  .crc-title { font-size: 16px; font-weight: 600; margin: 0; flex: 1 1 auto; }
  /* The page's main title: larger and bolder than section headings. */
  .crc-page-title { font-size: 20px; font-weight: 700; letter-spacing: 0.01em; color: var(--primary-text-color); padding: 14px 16px 0; }
  .crc-sub { font-size: 12px; color: var(--crc-muted); margin: 0 0 8px; }
  .crc-seg { display: inline-flex; border: 1px solid var(--crc-line); border-radius: 8px; overflow: hidden; }
  .crc-seg button {
    font: inherit; font-size: 12px; padding: 5px 11px; border: 0; background: transparent;
    color: var(--crc-text); cursor: pointer; min-height: 30px;
  }
  .crc-seg button + button { border-left: 1px solid var(--crc-line); }
  .crc-seg button[aria-pressed="true"] { background: var(--primary-color, #03a9f4); color: #fff; }
  .crc-seg button:disabled { opacity: 0.45; cursor: default; }
  .crc-seg-label { align-items: center; }
  .crc-seg-label > span { font-size: 12px; color: var(--crc-muted); padding: 0 8px; }
  .crc-btn {
    font: inherit; font-size: 12px; padding: 5px 11px; min-height: 30px; border-radius: 8px;
    border: 1px solid var(--crc-line); background: transparent; color: var(--crc-text); cursor: pointer;
  }
  .crc-btn.crc-primary { background: var(--primary-color, #03a9f4); border-color: transparent; color: #fff; }
  .crc-zoomtag { font-size: 12px; color: var(--crc-text); padding: 3px 9px; border-radius: 999px; background: rgba(127, 127, 127, 0.16); }
  .crc-brushable { cursor: crosshair; -webkit-touch-callout: none; }
  .crc-chart .crc-brush { fill: var(--primary-color, #03a9f4); fill-opacity: 0.18; stroke: var(--primary-color, #03a9f4); stroke-width: 1; pointer-events: none; }
  .crc-chart .crc-mid { stroke: var(--crc-text); stroke-width: 1.2; stroke-dasharray: 5 4; opacity: 0.7; }
  .crc-chart text.crc-midlabel { fill: var(--crc-text); font-weight: 600; }
  .crc-chart text.crc-axtitle { font-size: 10px; letter-spacing: 0.02em; }
  .crc-filters { display: grid; gap: 6px; margin: 0 0 10px; }
  .crc-frow { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 6px; }
  .crc-flabel { font-size: 11px; color: var(--crc-muted); min-width: 66px; }
  .crc-chip {
    display: inline-flex; align-items: center; gap: 6px; font: inherit; font-size: 12px; padding: 4px 10px; min-height: 28px;
    border-radius: 999px; border: 1px solid var(--crc-line); background: transparent; color: var(--crc-text); cursor: pointer;
  }
  .crc-chip small { color: var(--crc-muted); }
  .crc-chip[aria-pressed="true"] { background: color-mix(in srgb, var(--primary-color, #03a9f4) 18%, transparent); border-color: var(--primary-color, #03a9f4); }
  .crc-chipdot { width: 10px; height: 10px; border-radius: 50%; background: var(--sw, #888); flex: none; }
  .crc-rsw { width: 5px; align-self: stretch; min-height: 26px; border-radius: 3px; background: var(--sw, #888); flex: none; }
  .crc-rstation { color: var(--crc-text); opacity: 0.85; }
  .crc-legend-col { flex-direction: column; gap: 3px; }
  .crc-lg { align-items: center; }
  .crc-swatch.crc-ideal-sw { border-top: 0; height: 3px; background: #fff; box-shadow: 0 0 0 1px rgba(20, 20, 20, 0.7); }
  ha-card.crc-dark .crc-swatch.crc-ideal-sw { box-shadow: none; }
  .crc-templegend { display: grid; gap: 4px; margin-top: 8px; font-size: 12px; }
  .crc-gradwrap { display: flex; align-items: flex-start; gap: 8px; color: var(--crc-muted); max-width: 420px; }
  .crc-gradwrap > div { flex: 1 1 auto; }
  .crc-gradend { padding-top: 0; font-size: 11px; }
  .crc-grad { height: 10px; border-radius: 5px; }
  .crc-gradticks { display: flex; justify-content: space-between; font-size: 10px; margin-top: 2px; }
  .crc-stn { display: block; color: var(--crc-muted); font-size: 11px; }
  .crc-chart { position: relative; width: 100%; touch-action: pan-y; user-select: none; }
  .crc-chart svg { display: block; width: 100%; height: auto; overflow: visible; }
  .crc-chart text { fill: var(--crc-muted); font-size: 10.5px; font-family: inherit; }
  .crc-chart .crc-axis { stroke: var(--crc-line); stroke-width: 1; }
  .crc-chart .crc-grid { stroke: var(--crc-line); stroke-width: 1; opacity: 0.6; }
  .crc-chart .crc-bandlabel { font-size: 9.5px; opacity: 0.9; }
  .crc-chart .crc-cursor { visibility: hidden; stroke: var(--crc-muted); stroke-width: 1; stroke-dasharray: 3 3; }
  .crc-tip {
    position: absolute; z-index: 5; pointer-events: none; display: none; max-width: 260px;
    padding: 7px 9px; border-radius: 8px; font-size: 12px; line-height: 1.4;
    background: var(--crc-surface); color: var(--crc-text);
    border: 1px solid var(--crc-line); box-shadow: 0 2px 8px rgba(0, 0, 0, 0.25);
  }
  .crc-tip b { font-weight: 600; }
  .crc-readout { min-height: 20px; font-size: 12px; color: var(--crc-muted); margin: 6px 0 2px; display: flex; flex-wrap: wrap; gap: 2px 14px; }
  .crc-readout .crc-rt { color: var(--crc-text); }
  .crc-legend { display: flex; flex-wrap: wrap; gap: 4px 16px; font-size: 12px; margin-top: 6px; color: var(--crc-text); }
  .crc-legend span { display: inline-flex; align-items: center; gap: 6px; }
  .crc-legend .crc-note { color: var(--crc-muted); }
  .crc-directions { display: flex; flex-wrap: wrap; gap: 2px 14px; font-size: 12px; margin-top: 6px; padding-top: 6px; border-top: 1px solid var(--crc-line, rgba(127, 127, 127, 0.25)); color: var(--crc-muted); }
  .crc-directions b { color: var(--crc-text); font-weight: 600; }
  .crc-swatch { display: inline-block; width: 18px; height: 0; border-top: 2.5px solid var(--sw, #888); }
  .crc-swatch.crc-dashed { border-top-style: dashed; }
  .crc-swatch.crc-box { width: 12px; height: 10px; border-top: 0; background: var(--sw); opacity: 0.4; border-radius: 2px; }
  .crc-swatch.crc-sw-dc { width: 16px; height: 12px; opacity: 1; border-radius: 1px; background: linear-gradient(to top, var(--sw) 0 3px, transparent 3px), repeating-linear-gradient(-45deg, color-mix(in srgb, var(--sw) 75%, transparent) 0 1.5px, color-mix(in srgb, var(--sw) 16%, transparent) 1.5px 3px); }
  .crc-swatch.crc-sw-ac { width: 16px; height: 12px; opacity: 1; border-radius: 1px; background: linear-gradient(to top, color-mix(in srgb, var(--sw) 60%, transparent) 0 2px, transparent 2px), repeating-linear-gradient(-45deg, color-mix(in srgb, var(--sw) 65%, transparent) 0 1.3px, transparent 1.3px 6.5px); }
  .crc-swatch.crc-sw-detected { width: 12px; height: 10px; opacity: 1; background: transparent; border: 1.3px dashed var(--sw); border-radius: 1px; }
  .crc-dot {
    display: inline-flex; align-items: center; justify-content: center; flex: none;
    width: 18px; height: 18px; border-radius: 50%; font-size: 10px; font-weight: 700;
    background: var(--vc, #888); color: var(--vink, #fff);
  }
  .crc-empty { color: var(--crc-muted); font-size: 13px; padding: 18px 0; text-align: center; }
  /* scorecard */
  .crc-score { display: grid; gap: 10px; }
  .crc-vcard { border: 1px solid var(--crc-line); border-radius: 10px; padding: 10px 12px; border-left: 4px solid var(--vc, #888); }
  .crc-vname { display: flex; align-items: center; gap: 8px; font-weight: 600; margin-bottom: 8px; }
  .crc-tiles { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 8px; }
  .crc-tile { min-width: 0; }
  .crc-tlabel { font-size: 11px; color: var(--crc-muted); margin-bottom: 2px; }
  .crc-tmain { font-size: 20px; font-weight: 600; line-height: 1.2; }
  .crc-tmain small { font-size: 12px; font-weight: 500; color: var(--crc-muted); }
  .crc-tsub { font-size: 11px; color: var(--crc-muted); margin-top: 1px; }
  .crc-count {
    font: inherit; color: inherit; background: transparent; border: 0; padding: 1px 3px; margin: -1px -3px;
    border-radius: 6px; cursor: pointer; text-align: left;
  }
  .crc-count:hover { background: rgba(127, 127, 127, 0.15); }
  .crc-count[aria-pressed="true"] { background: var(--primary-color, #03a9f4); color: #fff; }
  .crc-count small { color: inherit; }
  /* fast charge list */
  .crc-fast { display: grid; grid-template-columns: minmax(280px, 40%) minmax(0, 1fr); gap: 16px; align-items: start; }
  .crc-stacked .crc-fast { grid-template-columns: minmax(0, 1fr); }
  .crc-list { display: flex; flex-direction: column; gap: 2px; }
  .crc-row {
    display: flex; flex-wrap: wrap; align-items: center; gap: 2px 8px;
    padding: 6px 4px; border-radius: 8px; font-size: 13px;
  }
  .crc-row:hover { background: rgba(127, 127, 127, 0.08); }
  .crc-row input[type="checkbox"] { width: 18px; height: 18px; margin: 0; flex: none; accent-color: var(--primary-color, #03a9f4); }
  .crc-row .crc-rmain { flex: 1 1 140px; min-width: 0; }
  .crc-row .crc-rline { color: var(--crc-muted); font-size: 12px; }
  .crc-rwhen { font-weight: 600; }
  .crc-del {
    font: inherit; font-size: 12px; color: var(--crc-ink-red); background: transparent;
    border: 1px solid var(--crc-line); border-radius: 6px; padding: 3px 8px; cursor: pointer; min-height: 28px;
  }
  .crc-del:hover { border-color: var(--crc-ink-red); }
  .crc-more { font: inherit; font-size: 12px; margin-top: 6px; padding: 5px 10px; border-radius: 8px; border: 1px solid var(--crc-line); background: transparent; color: var(--crc-text); cursor: pointer; }
  /* health */
  .crc-hsum { display: grid; gap: 4px; margin-top: 8px; font-size: 13px; }
  .crc-hsum div { display: flex; flex-wrap: wrap; align-items: center; gap: 2px 8px; }
  .crc-hsum .crc-muted { color: var(--crc-muted); }
  /* history table */
  .crc-filter { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; padding: 3px 6px 3px 10px; border-radius: 999px; background: var(--primary-color, #03a9f4); color: #fff; }
  .crc-filter button { font: inherit; border: 0; background: rgba(255, 255, 255, 0.25); color: inherit; border-radius: 50%; width: 20px; height: 20px; cursor: pointer; line-height: 1; padding: 0; }
  .crc-table { display: grid; font-size: 13px; }
  .crc-tr {
    display: grid; grid-template-columns: 1.3fr 1fr 1.4fr 0.8fr 1fr 0.8fr 0.7fr 0.7fr 0.7fr 0.7fr 0.8fr;
    gap: 0 8px; align-items: center; padding: 7px 4px; border-bottom: 1px solid var(--crc-line);
  }
  .crc-tr.crc-th { font-size: 11px; color: var(--crc-muted); padding: 4px; }
  .crc-th button { font: inherit; color: inherit; background: transparent; border: 0; padding: 0; cursor: pointer; text-align: left; display: inline-flex; gap: 3px; }
  .crc-th button[aria-pressed="true"] { color: var(--crc-text); font-weight: 600; }
  .crc-tr > span { min-width: 0; overflow-wrap: anywhere; }
  .crc-veh { display: inline-flex; align-items: center; gap: 6px; }
  .crc-badge { font-size: 11px; padding: 1px 7px; border-radius: 999px; border: 1px solid var(--crc-line); white-space: nowrap; }
  .crc-badge.crc-inferred { border-style: dashed; color: var(--crc-muted); margin-left: 4px; }
  .crc-hfilters { margin: 4px 0 8px; }
  .crc-hfilters .crc-flabel { min-width: 0; margin-left: 6px; }
  .crc-hfilters .crc-flabel:first-child { margin-left: 0; }
  .crc-tr.crc-row-inferred { color: var(--crc-muted); }
  .crc-badge.crc-dc { background: var(--crc-text); color: var(--crc-surface); border-color: var(--crc-text); }
  .crc-stacked .crc-tr:not(.crc-th) {
    grid-template-columns: minmax(0, 1fr) auto auto;
    grid-template-areas: "when veh kind" "place soc soc" "kwh peak avg" "btemp otemp dur";
    row-gap: 2px;
  }
  .crc-stacked .crc-tr.crc-th { display: flex; flex-wrap: wrap; gap: 4px 12px; }
  .crc-stacked .crc-c-when { grid-area: when; font-weight: 600; }
  .crc-stacked .crc-c-veh { grid-area: veh; }
  .crc-stacked .crc-c-kind { grid-area: kind; }
  .crc-stacked .crc-c-place { grid-area: place; color: var(--crc-muted); }
  .crc-stacked .crc-c-soc { grid-area: soc; text-align: right; }
  .crc-stacked .crc-c-kwh { grid-area: kwh; color: var(--crc-muted); }
  .crc-stacked .crc-c-dur { grid-area: dur; text-align: right; color: var(--crc-muted); }
  .crc-stacked .crc-c-peak { grid-area: peak; color: var(--crc-muted); }
  .crc-stacked .crc-c-avg { grid-area: avg; text-align: right; color: var(--crc-muted); }
  .crc-stacked .crc-c-btemp { grid-area: btemp; color: var(--crc-muted); }
  .crc-stacked .crc-c-otemp { grid-area: otemp; color: var(--crc-muted); }
  .crc-stacked .crc-c-peak::before, .crc-stacked .crc-c-avg::before,
  .crc-stacked .crc-c-btemp::before, .crc-stacked .crc-c-otemp::before { content: attr(data-l) " "; font-size: 10.5px; }
  .crc-stacked .crc-tiles { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .crc-stacked .crc-section { padding: 12px; }
  .crc-error { padding: 20px; color: var(--error-color, #db4437); }
`;

const BaseElement = typeof HTMLElement === "undefined" ? class {} : HTMLElement;

class RivianChargingCard extends BaseElement {
  static getStubConfig() {
    return {};
  }

  setConfig(config) {
    this._config = config || {};
    if (!this._built) this._build();
  }

  getCardSize() {
    return 14;
  }

  get hass() {
    return this._hass;
  }

  set hass(hass) {
    const dark = !!(hass && hass.themes && hass.themes.darkMode);
    const themeChanged = this._hass && !!(this._hass.themes && this._hass.themes.darkMode) !== dark;
    this._hass = hass;
    if (!this._built) return;
    if (this._barEl) this._barEl.hass = hass;
    this._card.classList.toggle("crc-dark", dark);
    if (!this._started) {
      this._started = true;
      this._start().catch((err) => this._showError(err));
    } else if (themeChanged) {
      this._renderAll();
    }
  }

  _build() {
    this._built = true;
    this._started = false;
    this._vins = [];
    this._vehicleList = [];
    this._bar = null;
    this._barEl = null;
    this._unsubSelection = null;
    this._unsubPromise = null;
    this._range = "30d";
    this._zoom = null; // brushed {start, end}; null = the range chip's window
    this._sel = null; // a touch selection waiting on "Zoom to selection"
    this._brush = null;
    this._fast = { frame: "all", brands: new Set(), tags: new Set(), colorBy: "brand" };
    this._health = { range: "all", showRange: false };
    this._data = { sessions: [], counts: {}, reference: {}, timeline: { series: {}, time_in_band: {} }, capacity: {} };
    this._checked = null;
    this._filter = null;
    this._sort = { key: "when", dir: "desc" };
    this._hist = this._loadHistoryPrefs();
    this._dcLimit = 8;
    this._historyLimit = 20;
    this._token = 0;
    this._cursorTs = null;
    this._geom = {};

    this.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = _STYLE;
    this.shadowRoot.appendChild(style);
    this._card = document.createElement("ha-card");
    this.shadowRoot.appendChild(this._card);
    this._topbar = document.createElement("div");
    this._topbar.className = "crc-topbar";
    this._topbar.style.display = "none";
    this._card.appendChild(this._topbar);
    const pageTitle = document.createElement("div");
    pageTitle.className = "crc-page-title";
    pageTitle.textContent = "Charging";
    this._card.appendChild(pageTitle);
    this._sections = {};
    for (const id of ["timeline", "score", "fast", "health", "history"]) {
      const el = document.createElement("div");
      el.className = "crc-section";
      el.dataset.section = id;
      this._card.appendChild(el);
      this._sections[id] = el;
    }
    this._card.addEventListener("click", (ev) => this._onClick(ev));
    this._card.addEventListener("change", (ev) => this._onChange(ev));
    this._card.addEventListener("keydown", (ev) => this._onKey(ev));
    this._card.addEventListener("pointermove", (ev) => this._onPointer(ev));
    this._card.addEventListener("pointerdown", (ev) => {
      this._onDown(ev);
      this._onPointer(ev);
    });
    this._card.addEventListener("pointerup", (ev) => this._onUp(ev));
    this._card.addEventListener("pointercancel", () => this._clearBrush());
    this._card.addEventListener("dblclick", (ev) => this._onDblClick(ev));
    this._card.addEventListener("contextmenu", (ev) => {
      // A long press on touch opens the context menu; the brush owns that gesture.
      if (ev.target && ev.target.closest && ev.target.closest(".crc-brushable")) ev.preventDefault();
    });
    this._card.addEventListener(
      "touchmove",
      (ev) => {
        if (this._brush && this._brush.active && ev.cancelable) ev.preventDefault();
      },
      { passive: false }
    );
    this._card.addEventListener("pointerleave", (ev) => this._onLeave(ev), true);
    this._observeResize();
  }

  _observeResize() {
    if (this._resizeObserver || typeof ResizeObserver === "undefined") return;
    this._resizeObserver = new ResizeObserver(() => {
      const width = this.getBoundingClientRect().width;
      const stacked = width > 0 && width < STACK_BREAKPOINT_PX;
      const changed = stacked !== this._stacked || Math.abs(width - (this._lastWidth || 0)) > 2;
      this._stacked = stacked;
      this._lastWidth = width;
      this._card.classList.toggle("crc-stacked", stacked);
      if (changed && this._ready) {
        cancelAnimationFrame(this._raf);
        this._raf = requestAnimationFrame(() => this._renderAll());
      }
    });
    this._resizeObserver.observe(this);
  }

  connectedCallback() {
    if (!this._built) return;
    this._observeResize();
    if (this._hass && !this._started) {
      this._started = true;
      this._start().catch((err) => this._showError(err));
    }
    if (this._hass && this._vins.length) this._subscribe();
    if (this._bar && !this._unsubSelection && !this._pinned) {
      this._unsubSelection = this._bar.onSelectionChange((vins) => this._onSelectionChanged(vins));
    }
  }

  disconnectedCallback() {
    this._unsubscribe();
    if (this._unsubSelection) this._unsubSelection();
    this._unsubSelection = null;
    if (this._resizeObserver) {
      this._resizeObserver.disconnect();
      this._resizeObserver = null;
    }
    clearTimeout(this._refreshTimer);
  }

  get _pinned() {
    return !!(this._config && Array.isArray(this._config.vins) && this._config.vins.length);
  }

  get _isAdmin() {
    return !!(this._hass && this._hass.user && this._hass.user.is_admin);
  }

  get _dark() {
    return !!(this._hass && this._hass.themes && this._hass.themes.darkMode);
  }

  get _tz() {
    return this._hass && this._hass.config ? this._hass.config.time_zone : undefined;
  }

  _showError(err) {
    console.error("rivian-charging-card:", err);
    const msg = err && err.message ? err.message : err && err.code ? err.code : String(err);
    this._sections.timeline.innerHTML = `<div class="crc-error">Could not load charging data: ${esc(msg)}</div>`;
  }

  // -- scope ------------------------------------------------------------

  async _start() {
    try {
      this._bar = await _loadBarModule();
      this._vehicleList = await this._bar.getVehicles(this._hass);
    } catch (err) {
      console.warn("rivian-charging-card: vehicle list unavailable", err);
    }
    if (this._pinned) {
      this._vins = [...this._config.vins];
    } else {
      let selection = [];
      try {
        selection = this._bar ? await this._bar.getSelection(this._hass) : [];
      } catch (err) {
        console.warn("rivian-charging-card: vehicle selection unavailable", err);
      }
      this._vins = [...selection];
      if (this._bar && !this._unsubSelection) {
        this._unsubSelection = this._bar.onSelectionChange((vins) => this._onSelectionChanged(vins));
      }
      this._mountBar();
    }
    if (!this._vins.length) {
      this._sections.timeline.innerHTML = '<div class="crc-empty">No vehicles to show.</div>';
      return;
    }
    this._subscribe();
    await this._refreshAll();
  }

  _mountBar() {
    if (this._barEl || !this._bar) return;
    const el = document.createElement("rivian-vehicle-bar");
    el.hass = this._hass;
    this._topbar.appendChild(el);
    this._topbar.style.display = "";
    this._barEl = el;
  }

  _onSelectionChanged(vins) {
    if (!this._bar || this._pinned) return;
    const next = this._bar.normalizeSelection(vins, this._vehicleList);
    if (this._bar.sameSelection(next, this._vins)) return;
    this._unsubscribe();
    this._vins = [...next];
    this._checked = null;
    this._filter = null;
    this._subscribe();
    this._refreshAll().catch((err) => this._showError(err));
  }

  _subscribe() {
    if (this._unsubPromise || !this._hass || !this._vins.length) return;
    this._unsubPromise = this._hass.connection
      .subscribeMessage(
        () => {
          clearTimeout(this._refreshTimer);
          this._refreshTimer = setTimeout(() => this._refreshAll().catch((e) => this._showError(e)), 400);
        },
        { type: "rivian/analytics/subscribe", vins: [...this._vins] }
      )
      .catch((err) => {
        console.warn("rivian-charging-card: live updates unavailable", err);
        return null;
      });
  }

  _unsubscribe() {
    if (!this._unsubPromise) return;
    this._unsubPromise.then((unsub) => unsub && unsub()).catch(() => {});
    this._unsubPromise = null;
  }

  // -- data -------------------------------------------------------------

  _now() {
    return Date.now() / 1000;
  }

  /** The battery chart's `{start, end}`: the brushed zoom span, else the range chip's window. */
  _window() {
    if (this._zoom) return { start: this._zoom.start, end: this._zoom.end };
    let earliest = null;
    for (const s of this._data.sessions) earliest = earliest === null ? s.start_ts : Math.min(earliest, s.start_ts);
    for (const e of Object.values(this._data.capacity)) {
      const first = e && e.points && e.points[0];
      if (first) earliest = earliest === null ? first[0] : Math.min(earliest, first[0]);
    }
    return rangeWindow(this._range, this._now(), earliest);
  }

  async _fetchTimeline() {
    const win = this._window();
    return this._hass.callWS({
      type: "rivian/battery/soc_timeline",
      vins: [...this._vins],
      start: win.start,
      end: win.end,
    });
  }

  async _refreshAll() {
    const token = ++this._token;
    const vins = [...this._vins];
    const [sessions, reference, capacity] = await Promise.all([
      this._hass.callWS({ type: "rivian/charging/sessions", vins }),
      this._hass.callWS({ type: "rivian/charging/reference", vins }),
      this._hass.callWS({ type: "rivian/battery/capacity", vins }),
    ]);
    this._data.sessions = (sessions && sessions.sessions) || [];
    this._data.counts = (sessions && sessions.counts_by_vin) || {};
    this._data.reference = reference || {};
    this._data.capacity = capacity || {};
    const timeline = await this._fetchTimeline();
    if (token !== this._token) return;
    this._data.timeline = timeline || { series: {}, time_in_band: {} };
    const keys = new Set(this._data.sessions.map(sessionKey));
    if (this._checked === null) this._checked = defaultChecked(this._data.sessions, 3);
    else this._checked = new Set([...this._checked].filter((k) => keys.has(k)));
    if (this._filter && !this._vins.includes(this._filter.vin)) this._filter = null;
    this._ready = true;
    this._renderAll();
  }

  async _setRange(key) {
    if (key === this._range && !this._zoom) return;
    this._range = key;
    this._zoom = null;
    this._sel = null;
    this._historyLimit = 20;
    const token = ++this._token;
    const timeline = await this._fetchTimeline();
    if (token !== this._token) return;
    this._data.timeline = timeline || { series: {}, time_in_band: {} };
    this._renderAll();
  }

  /** Zoom the battery chart to `span` (null resets): refetches the timeline for it, then re-filters the lists. */
  async _setZoom(span) {
    this._zoom = span ? { start: span.start, end: span.end } : null;
    this._sel = null;
    this._historyLimit = 20;
    const token = ++this._token;
    const timeline = await this._fetchTimeline();
    if (token !== this._token) return;
    this._data.timeline = timeline || { series: {}, time_in_band: {} };
    this._ensureChecked();
    this._renderAll();
  }

  /** The remembered history type / timeframe (localStorage may be unavailable). */
  _loadHistoryPrefs() {
    const prefs = { type: "all", frame: "all" };
    try {
      const raw = JSON.parse(window.localStorage.getItem(HISTORY_PREFS_KEY) || "{}");
      if (HISTORY_TYPES.some((t) => t[0] === raw.type)) prefs.type = raw.type;
      if (HISTORY_FRAMES.some((f) => f[0] === raw.frame)) prefs.frame = raw.frame;
    } catch (_err) {
      /* storage unavailable: defaults */
    }
    return prefs;
  }

  _saveHistoryPrefs() {
    try {
      window.localStorage.setItem(HISTORY_PREFS_KEY, JSON.stringify(this._hist));
    } catch (_err) {
      /* storage unavailable: not remembered */
    }
  }

  /** The sessions inside the zoomed span (all of them when not zoomed). */
  _scoped() {
    return this._zoom ? this._data.sessions.filter((s) => inSpan(s, this._zoom)) : this._data.sessions;
  }

  /** The fast-charge list: DC sessions in the active span and brand / charger filters. */
  _fastVisible() {
    return sortNewestFirst(
      filterFast(this._data.sessions, {
        span: fastSpan(this._fast.frame, this._zoom, this._now()),
        brands: this._fast.brands,
        tags: this._fast.tags,
      })
    );
  }

  /** After a filter changes: when nothing visible is checked, check the newest three visible. */
  _ensureChecked() {
    const visible = this._fastVisible();
    if (!visible.some((s) => this._checked.has(sessionKey(s)))) {
      for (const s of visible.slice(0, 3)) this._checked.add(sessionKey(s));
    }
  }

  // -- vehicles ---------------------------------------------------------

  _info(vin) {
    const v = this._vehicleList.find((x) => x.vin === vin) || null;
    const color = v ? (this._dark ? v.color_dark || v.color : v.color) || FALLBACK_COLOR : FALLBACK_COLOR;
    const ink = this._bar ? this._bar.inkOn(color) : "#ffffff";
    return { vin, name: (v && (v.name || v.model)) || "Rivian", letter: (v && v.letter) || "", color, ink };
  }

  _dotHtml(vin) {
    const i = this._info(vin);
    return `<span class="crc-dot" style="--vc:${esc(i.color)};--vink:${esc(i.ink)}">${esc(i.letter)}</span>`;
  }

  get _multi() {
    return this._vins.length > 1;
  }

  // -- rendering ----------------------------------------------------------

  _renderAll() {
    if (!this._ready) return;
    this._renderTimeline();
    this._renderScore();
    this._renderFast();
    this._renderHealth();
    this._renderHistory();
  }

  _chartWidth(sectionEl) {
    const w = sectionEl.clientWidth - (this._stacked ? 24 : 32);
    return Math.max(260, Math.round(w || 600));
  }

  _tipHtml() {
    return '<div class="crc-tip"></div>';
  }

  _renderTimeline() {
    const el = this._sections.timeline;
    const tz = this._tz;
    const series = this._data.timeline.series || {};
    const win = this._window();
    const zoomed = !!this._zoom;
    const entries = this._vins.map((vin) => ({ vin, ...(series[vin] || { points: [], source: "statistics" }) }));
    const hasData = entries.some((e) => e.points && e.points.length > 1);
    const toggle = RANGES.map(
      ([key]) =>
        `<button type="button" data-action="range" data-range="${key}" aria-pressed="${key === this._range && !zoomed}" title="${esc(rangeTitle(key))}" aria-label="${esc(rangeTitle(key))}">${key === "all" ? "All" : key}</button>`
    ).join("");
    let head = `<div class="crc-head"><h3 class="crc-title">Battery level</h3>`;
    if (zoomed) {
      head +=
        `<span class="crc-zoomtag">Zoomed: ${esc(spanLabel(this._zoom, tz))}</span>` +
        '<button type="button" class="crc-btn" data-action="zoom-reset" title="Go back to the full time range">Reset zoom</button>';
    } else if (this._sel) {
      head +=
        `<span class="crc-zoomtag">Selected: ${esc(spanLabel(this._sel, tz))}</span>` +
        '<button type="button" class="crc-btn crc-primary" data-action="zoom-apply" title="Zoom the battery chart to the selected span">Zoom to selection</button>' +
        '<button type="button" class="crc-btn" data-action="zoom-clear" title="Clear the selection" aria-label="Clear the selection">Clear</button>';
    }
    head += `<div class="crc-seg" role="group" aria-label="Time range">${toggle}</div></div>`;
    if (!hasData) {
      el.innerHTML =
        head +
        `<div class="crc-empty">No battery history for this range yet.</div>` +
        (zoomed ? "" : "");
      this._tl = null;
      return;
    }
    let x0 = win.start;
    if (this._range === "all" && !zoomed) {
      x0 = Math.min(...entries.filter((e) => e.points.length).map((e) => e.points[0][0]));
      x0 = Math.max(x0, win.start);
    }
    const x1 = win.end;
    const W = this._chartWidth(el);
    const H = this._stacked ? 230 : 280;
    const g = chartLayout(W, H, { left: 30, right: 6, top: 8, bottom: 22 });
    const xs = scaleLinear(x0, x1, g.x0, g.x1);
    const ys = scaleLinear(0, 100, g.y1, g.y0);
    const parts = [];
    for (const b of BANDS) {
      const top = ys(b.to);
      const bot = ys(b.from);
      parts.push(
        `<rect x="${g.x0}" y="${top.toFixed(1)}" width="${g.w}" height="${(bot - top).toFixed(1)}" fill="var(--crc-band-${b.tone})"><title>${esc(bandTitle(b))}</title></rect>`
      );
    }
    // Band labels sit in the plot's left edge in the band's ink color.
    for (const b of BANDS) {
      // The wide green band's label sits above the 50 % line, clear of its marker.
      const mid = (b.key === "green" ? ys(66) : (ys(b.from) + ys(b.to)) / 2) + 3.5;
      parts.push(
        `<text class="crc-bandlabel" x="${g.x0 + 5}" y="${mid.toFixed(1)}" style="fill:var(--crc-ink-${b.tone})">${esc(b.label)}</text>`
      );
    }
    for (const v of [0, 10, 20, 80, 90, 100]) {
      parts.push(`<line class="crc-grid" x1="${g.x0}" x2="${g.x1}" y1="${ys(v).toFixed(1)}" y2="${ys(v).toFixed(1)}"/>`);
      parts.push(`<text x="${g.x0 - 5}" y="${(ys(v) + 3.5).toFixed(1)}" text-anchor="end">${v}</text>`);
    }
    // The labeled 50 % midpoint.
    parts.push(`<line class="crc-mid" x1="${g.x0}" x2="${g.x1}" y1="${ys(50).toFixed(1)}" y2="${ys(50).toFixed(1)}"/>`);
    parts.push(`<text class="crc-midlabel" x="${g.x0 - 5}" y="${(ys(50) + 3.5).toFixed(1)}" text-anchor="end">50</text>`);
    parts.push(`<text class="crc-midlabel" x="${g.x1 - 4}" y="${(ys(50) - 4).toFixed(1)}" text-anchor="end">50 % midpoint</text>`);
    for (const t of timeTicks(x0, x1, tz, this._stacked ? 4 : 7)) {
      const x = xs(t.ts);
      parts.push(`<line class="crc-axis" x1="${x.toFixed(1)}" x2="${x.toFixed(1)}" y1="${g.y1}" y2="${g.y1 + 4}"/>`);
      parts.push(`<text x="${x.toFixed(1)}" y="${g.y1 + 15}" text-anchor="middle">${esc(t.label)}</text>`);
    }
    // Charging: fast (DC) spans are solid, slow (AC) spans hatched; a charge seen
    // only in the battery level (no recorded session) also gets a dashed outline.
    const recorded = this._data.sessions.filter((s) => s.end_ts >= x0 && s.start_ts <= x1);
    const detected = detectedSessions(this._data.timeline.detected, this._vins).filter((s) => s.end_ts >= x0 && s.start_ts <= x1);
    const visible = [...recorded, ...detected];
    this._tlSessions = visible;
    const filtering = !!this._filter;
    // Fast: dense /// over a tint; slow: sparse ///. One pair per vehicle color.
    const defs = this._vins
      .map((vin, i) => {
        const c = esc(this._info(vin).color);
        return (
          `<pattern id="crc-dense-${i}" width="3.5" height="3.5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="3.5" height="3.5" fill="${c}" fill-opacity="0.16"/><line x1="0" y1="0" x2="0" y2="3.5" stroke="${c}" stroke-width="1.8" stroke-opacity="0.75"/></pattern>` +
          `<pattern id="crc-sparse-${i}" width="9" height="9" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="9" stroke="${c}" stroke-width="1.4" stroke-opacity="0.6"/></pattern>`
        );
      })
      .join("");
    parts.push(`<defs>${defs}</defs>`);
    for (const s of visible) {
      const info = this._info(s.vin);
      const [a, b] = spanExtent(s, xs, 4);
      const dc = s.kind === "dc";
      const unrecorded = !!(s.detected || s.inferred);
      const hit = !filtering || (!s.detected && matchesFilter(s, this._filter));
      const fade = (hit ? 1 : 0.2) * (unrecorded ? 0.75 : 1);
      const fill = `fill="url(#crc-${dc ? "dense" : "sparse"}-${Math.max(0, this._vins.indexOf(s.vin))})"`;
      const stroke = unrecorded
        ? ` stroke="${esc(info.color)}" stroke-width="1.2" stroke-dasharray="4 3"`
        : filtering && hit
          ? ` stroke="${esc(info.color)}" stroke-width="1.5"`
          : "";
      const w = (b - a).toFixed(1);
      parts.push(
        `<g opacity="${fade}"><rect x="${a.toFixed(1)}" y="${g.y0}" width="${w}" height="${g.h}" ${fill}${stroke}/>` +
          (unrecorded ? "" : `<rect x="${a.toFixed(1)}" y="${g.y1 - (dc ? 5 : 3)}" width="${w}" height="${dc ? 5 : 3}" fill="${esc(info.color)}" fill-opacity="${dc ? 0.95 : 0.55}"/>`) +
          "</g>"
      );
    }
    // Lines last so they sit above the spans.
    for (const e of entries) {
      const pts = (e.points || []).filter((p) => p[0] >= x0 - 1 && p[0] <= x1 + 1);
      if (!pts.length) continue;
      const info = this._info(e.vin);
      const dash = e.source === "synthesized" ? ' stroke-dasharray="6 3"' : "";
      const d = linePath(pts, xs, ys, e.source === "synthesized" ? null : (a, b) => b[0] - a[0] > (zoomed ? 3 * 3600 : 2 * DAY_S));
      if (pts.length === 1) parts.push(`<circle cx="${xs(pts[0][0]).toFixed(1)}" cy="${ys(pts[0][1]).toFixed(1)}" r="3" fill="${esc(info.color)}"/>`);
      else parts.push(`<path d="${d}" fill="none" stroke="${esc(info.color)}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"${dash}/>`);
    }
    parts.push(`<line class="crc-cursor" data-cursor y1="${g.y0}" y2="${g.y1}" x1="-10" x2="-10"/>`);
    // The brush: live while dragging, or the touch selection waiting on "Zoom to selection".
    let bx = 0;
    let bw = 0;
    if (this._sel) {
      bx = Math.max(g.x0, xs(this._sel.start));
      bw = Math.max(2, Math.min(g.x1, xs(this._sel.end)) - bx);
    }
    parts.push(
      `<rect class="crc-brush" data-brush x="${bx.toFixed(1)}" y="${g.y0}" width="${bw.toFixed(1)}" height="${g.h}" style="visibility:${bw ? "visible" : "hidden"}"/>`
    );
    const synthesized = entries.some((e) => e.source === "synthesized" && e.points.length);
    const legend = [];
    if (this._multi) {
      for (const e of entries) {
        const i = this._info(e.vin);
        const dashed = e.source === "synthesized" ? " crc-dashed" : "";
        legend.push(`<span><i class="crc-swatch${dashed}" style="--sw:${esc(i.color)}"></i>${esc(i.name)}</span>`);
      }
    }
    const sw = this._multi ? "var(--crc-text)" : esc(this._info(this._vins[0]).color);
    legend.push(`<span title="DC fast charging: dense hatching"><i class="crc-swatch crc-box crc-sw-dc" style="--sw:${sw}"></i>Fast charge (DC)</span>`);
    legend.push(`<span title="Level 2 or Level 1 charging, at home or elsewhere: sparse hatching"><i class="crc-swatch crc-box crc-sw-ac" style="--sw:${sw}"></i>Slow charge (AC: L2 / L1)</span>`);
    if (detected.length || recorded.some((s) => s.inferred)) {
      legend.push(
        `<span title="The battery level rose here but no charging session was recorded, for example before home charging was recorded. Fast or slow is estimated from how quickly it rose. Those already added to the charging history are marked Inferred there."><i class="crc-swatch crc-box crc-sw-detected" style="--sw:${sw}"></i>Dashed outline: inferred from the battery level, not recorded</span>`
      );
    }
    if (synthesized) legend.push('<span class="crc-note">Dashed line: estimated from drives &amp; charges</span>');
    const directions = batteryChartDirections(zoomed);
    el.innerHTML =
      head +
      `<div class="crc-chart crc-brushable" data-chart="timeline" tabindex="0" role="group" aria-label="Battery percentage over time: hover, tap, or use the arrow keys to step through charging sessions"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Battery percentage over time with charging sessions">${parts.join("")}</svg>${this._tipHtml()}</div>` +
      `<div class="crc-readout" data-readout="timeline">Hover, tap, or focus the chart and use the arrow keys for values.</div>` +
      `<div class="crc-legend">${legend.join("")}</div>` +
      `<div class="crc-directions"><b>How to use:</b> ${directions.map((d) => `<span>${esc(d)}</span>`).join("")}</div>`;
    this._tl = { g, xs, ys, x0, x1, entries };
  }

  _renderScore() {
    const el = this._sections.score;
    const bands = this._data.timeline.time_in_band || {};
    const zoomed = !!this._zoom;
    const scoped = this._scoped();
    let html = `<div class="crc-head"><h3 class="crc-title">Scorecard</h3></div><p class="crc-sub">Tap a count to highlight those sessions above and filter the history below. Time in 20–80 % ${zoomed ? "and the counts cover the zoomed span" : "covers the range selected above"}.</p><div class="crc-score">`;
    for (const vin of this._vins) {
      const info = this._info(vin);
      const counts = zoomed ? countSessions(scoped.filter((s) => s.vin === vin)) : this._data.counts[vin];
      const { tiles, timeInIdeal } = scorecardTiles(counts, bands[vin]);
      const btn = (metric, text, fastOnly, label = "") => {
        const f = { vin, metric, fastOnly };
        const on = sameFilter(f, this._filter);
        return `<button type="button" class="crc-count" data-action="filter" data-vin="${esc(vin)}" data-metric="${metric}" data-fast="${fastOnly ? 1 : 0}" aria-pressed="${on}" title="${esc(scoreCountTitle(label, fastOnly))}">${text}</button>`;
      };
      const pair = (metric, all, fast, label) =>
        fast === null
          ? btn(metric, `${all}`, false, label)
          : `${btn(metric, `${all}`, false, label)} <small>·</small> ${btn(metric, `${fast} <small>fast</small>`, true, label)}`;
      html += `<div class="crc-vcard" style="--vc:${esc(info.color)};--vink:${esc(info.ink)}"><div class="crc-vname">${this._dotHtml(vin)}${esc(info.name)}</div><div class="crc-tiles">`;
      for (const t of tiles) {
        html += `<div class="crc-tile" title="${esc(SCORE_TILE_TITLES[t.label] || t.label)}"><div class="crc-tlabel">${esc(t.label)}</div><div class="crc-tmain">${pair(t.metric, t.all, t.fast, t.label)}</div>`;
        if (t.sub) html += `<div class="crc-tsub" title="${esc(SCORE_TILE_TITLES[t.sub.label] || t.sub.label)}">${esc(t.sub.label)}: ${pair(t.sub.metric, t.sub.all, t.sub.fast, t.sub.label)}</div>`;
        html += "</div>";
      }
      html += `<div class="crc-tile" title="${esc(SCORE_TILE_TITLES["Time in 20–80 %"])}"><div class="crc-tlabel">Time in 20–80 %</div><div class="crc-tmain">${esc(timeInIdeal)}</div></div>`;
      html += "</div></div>";
    }
    el.innerHTML = html + "</div>";
  }

  /** A session's line / swatch color under the current "Color by" mode. */
  _colorOf(s) {
    return sessionColor(s, this._fast.colorBy, this._info(s.vin).color, this._dark);
  }

  _renderFast() {
    const el = this._sections.fast;
    const dcAll = this._data.sessions.filter((s) => s.kind === "dc");
    const tz = this._tz;
    const f = this._fast;
    const zoomed = !!this._zoom;
    const frames = FAST_FRAMES.map(
      ([key]) =>
        `<button type="button" data-action="fast-frame" data-frame="${key}" aria-pressed="${!zoomed && key === f.frame}" title="${esc(zoomed ? "Following the battery-chart zoom" : rangeTitle(key))}" aria-label="${esc(rangeTitle(key))}"${zoomed ? " disabled" : ""}>${key === "all" ? "All" : key}</button>`
    ).join("");
    const colorBy = ["brand", "vehicle"]
      .map((k) => `<button type="button" data-action="color-by" data-mode="${k}" aria-pressed="${f.colorBy === k}" title="${k === "brand" ? "Color each curve by charging network" : "Color each curve by vehicle"}">${k === "brand" ? "Brand" : "Vehicle"}</button>`)
      .join("");
    let html =
      '<div class="crc-head"><h3 class="crc-title">Fast charging</h3>' +
      `<div class="crc-seg crc-seg-label" role="group" aria-label="Color by"><span>Color by</span>${colorBy}</div></div>`;
    if (!dcAll.length) {
      el.innerHTML = html + '<div class="crc-empty">No fast-charge sessions recorded yet.</div>';
      this._pw = null;
      return;
    }
    // Filters: timeframe, brand chips, charger chips.
    const brands = brandsPresent(dcAll);
    const tags = chargerTagsPresent(dcAll);
    let filters = '<div class="crc-filters">';
    filters += `<div class="crc-frow"><span class="crc-flabel">Timeframe</span><div class="crc-seg" role="group" aria-label="Timeframe">${frames}</div>${zoomed ? `<span class="crc-note">Following the battery-chart zoom</span>` : ""}</div>`;
    if (brands.length > 1) {
      filters += '<div class="crc-frow"><span class="crc-flabel">Network</span>';
      for (const b of brands) {
        const on = f.brands.has(b.brand);
        filters += `<button type="button" class="crc-chip" data-action="brand-chip" data-brand="${esc(b.brand)}" aria-pressed="${on}" title="${esc(`${b.label}: ${b.count} fast charge${b.count === 1 ? "" : "s"}. Tap to ${on ? "stop filtering to" : "filter to"} this network.`)}"><i class="crc-chipdot" style="--sw:${esc(brandColor(b.brand, this._dark))}"></i>${esc(b.label)} <small>${b.count}</small></button>`;
      }
      filters += "</div>";
    }
    if (tags.length) {
      filters += '<div class="crc-frow"><span class="crc-flabel">Charger</span>';
      for (const t of tags) {
        filters += `<button type="button" class="crc-chip" data-action="tag-chip" data-tag="${esc(t)}" aria-pressed="${f.tags.has(t)}" title="${esc(`Charger ${t}: version or maximum output. Tap to filter.`)}">${esc(t)}</button>`;
      }
      filters += "</div>";
    }
    filters += "</div>";
    html += filters;

    const dc = this._fastVisible();
    if (!dc.length) {
      el.innerHTML = html + '<div class="crc-empty">No fast charges match these filters.</div>';
      this._pw = null;
      return;
    }
    const shown = dc.slice(0, this._dcLimit);
    // Keep checked sessions visible even past the limit.
    for (const s of dc) if (this._checked.has(sessionKey(s)) && !shown.includes(s)) shown.push(s);
    let list = '<div class="crc-list">';
    for (const s of shown) {
      const key = sessionKey(s);
      const info = this._info(s.vin);
      const exp = expectedText(s);
      const station = stationLine(s);
      const where = whereText(s);
      list +=
        `<div class="crc-row"><input type="checkbox" data-action="check" data-key="${esc(key)}" ${this._checked.has(key) ? "checked" : ""} aria-label="Show ${esc(formatWhen(s.start_ts, tz))}" title="Plot this session's power curve">` +
        `<i class="crc-rsw" style="--sw:${esc(this._colorOf(s))}" title="This session's curve color"></i>` +
        `${this._multi ? this._dotHtml(s.vin) : ""}` +
        `<div class="crc-rmain"><div><span class="crc-rwhen">${esc(formatWhen(s.start_ts, tz))}</span>${where ? ` · ${esc(where)}` : ""}</div>` +
        `${station ? `<div class="crc-rline crc-rstation">${esc(station)}</div>` : ""}` +
        `<div class="crc-rline">${esc(socRange(s))} · ${esc(_num(s.energy_added_kwh, 1))} kWh · peak ${esc(_num(s.max_power_kw, 0))} kW${this._multi ? ` · ${esc(info.name)}` : ""}</div>` +
        `${exp ? `<div class="crc-rline" title="Charging time compared with the ideal curve for this pack and the same battery range (~ means approximate)">${esc(exp)}</div>` : ""}</div>` +
        `${this._isAdmin ? `<button type="button" class="crc-del" data-action="delete" data-key="${esc(key)}" title="Delete the session on ${esc(formatWhen(s.start_ts, tz))} (asks for confirmation)" aria-label="Delete the session on ${esc(formatWhen(s.start_ts, tz))}">Delete</button>` : ""}</div>`;
    }
    list += "</div>";
    if (dc.length > shown.length) list += `<button type="button" class="crc-more" data-action="more-dc" title="Show more fast-charge sessions in the list">Show ${dc.length - shown.length} more</button>`;
    else if (this._dcLimit > 8 && dc.length > 8) list += '<button type="button" class="crc-more" data-action="less-dc">Show fewer</button>';

    const checked = dc.filter((s) => this._checked.has(sessionKey(s)));
    const W = this._stacked ? this._chartWidth(el) : Math.max(260, Math.round(el.clientWidth * 0.6 - 40));
    const H = this._stacked ? 250 : 300;
    // One ideal curve per distinct pack among the plotted vehicles.
    const refs = [];
    for (const vin of new Set(checked.map((s) => s.vin))) {
      const ref = this._data.reference[vin];
      if (!ref || !ref.curve) continue;
      const hit = refs.find((r) => r.ref.pack === ref.pack);
      if (hit) hit.vins.push(vin);
      else refs.push({ ref, vins: [vin] });
    }
    let chart;
    if (!checked.length) {
      chart = '<div class="crc-empty">Check a session to plot its power curve.</div>';
      this._pw = null;
    } else {
      const dom = powerDomains(checked, refs.map((r) => r.ref));
      const g = chartLayout(W, H, { left: 36, right: 8, top: 8, bottom: 34 });
      const xs = scaleLinear(dom.x[0], dom.x[1], g.x0, g.x1);
      const ys = scaleLinear(dom.y[0], dom.y[1], g.y1, g.y0);
      const parts = [];
      for (const v of niceTicks(dom.y[0], dom.y[1], 5)) {
        parts.push(`<line class="crc-grid" x1="${g.x0}" x2="${g.x1}" y1="${ys(v).toFixed(1)}" y2="${ys(v).toFixed(1)}"/>`);
        parts.push(`<text x="${g.x0 - 5}" y="${(ys(v) + 3.5).toFixed(1)}" text-anchor="end">${v}</text>`);
      }
      for (const v of niceTicks(dom.x[0], dom.x[1], 9)) {
        parts.push(`<text x="${xs(v).toFixed(1)}" y="${g.y1 + 14}" text-anchor="middle">${v}</text>`);
      }
      parts.push(`<line class="crc-axis" x1="${g.x0}" x2="${g.x1}" y1="${g.y1}" y2="${g.y1}"/>`);
      parts.push(`<text x="${(g.x0 + g.x1) / 2}" y="${g.y1 + 28}" text-anchor="middle">Battery %</text>`);
      parts.push(`<text x="10" y="${(g.y0 + g.y1) / 2}" text-anchor="middle" transform="rotate(-90 10 ${(g.y0 + g.y1) / 2})">kW</text>`);
      // The ±10 % band (neutral) and the ideal curve: white, with a dark halo in light mode.
      for (const { ref } of refs) {
        const path = bandPath(referenceBand(ref.curve, 0.1), xs, ys);
        if (path) parts.push(`<path d="${path}" fill="var(--crc-text)" fill-opacity="0.12" stroke="none"/>`);
      }
      for (const { ref } of refs) {
        const pts = ref.curve.soc.map((s, i) => [s, ref.curve.kw[i]]);
        const d = linePath(pts, xs, ys);
        if (!this._dark) parts.push(`<path d="${d}" fill="none" stroke="rgba(20,20,20,0.7)" stroke-width="5" stroke-linejoin="round" stroke-linecap="round"/>`);
        parts.push(`<path class="crc-ideal" d="${d}" fill="none" stroke="#ffffff" stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round"/>`);
      }
      const seen = {};
      const lines = [];
      checked.forEach((s) => {
        const color = this._colorOf(s);
        const idx = (seen[color] = (seen[color] || 0) + 1) - 1;
        const dash = DASHES[idx % DASHES.length];
        const pts = (s.samples || [])
          .filter((p) => typeof p.soc === "number" && typeof p.power_kw === "number")
          .map((p) => [p.soc, p.power_kw])
          .sort((a, b) => a[0] - b[0]);
        lines.push({ s, dash, color, pts });
        if (pts.length > 1) {
          parts.push(
            `<path d="${linePath(pts, xs, ys)}" fill="none" stroke="${esc(color)}" stroke-width="2" stroke-linejoin="round"${dash ? ` stroke-dasharray="${dash}"` : ""}/>`
          );
        }
      });
      parts.push(`<line class="crc-cursor" data-cursor y1="${g.y0}" y2="${g.y1}" x1="-10" x2="-10"/>`);
      const legend = lines
        .map(
          ({ s, dash, color }) =>
            `<span class="crc-lg"><i class="crc-swatch${dash ? " crc-dashed" : ""}" style="--sw:${esc(color)}"></i>${esc(sessionLegendLabel(s, tz, this._multi ? this._info(s.vin).name : ""))}</span>`
        )
        .join("");
      const refLegend = refs
        .map(({ ref, vins }) => {
          const names = this._multi ? ` (${esc(vins.map((v) => this._info(v).name).join(", "))})` : "";
          return `<span class="crc-lg" title="The charge curve Rivian specifies for this pack; the shaded band is plus or minus 10 %${ref.approximate ? ". Approximate: derived from a similar pack" : ""}"><i class="crc-swatch crc-ideal-sw"></i>Ideal: ${esc(ref.label || "reference")}${names} ±10 %${ref.approximate ? " · approximate" : ""}</span>`;
        })
        .join("");
      chart =
        `<div class="crc-chart" data-chart="power" tabindex="0" role="group" aria-label="Charging power versus battery percentage: hover, tap, or use the arrow keys to read values"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Charging power versus battery percentage">${parts.join("")}</svg>${this._tipHtml()}</div>` +
        `<div class="crc-readout" data-readout="power">Hover, tap, or focus the chart and use the arrow keys for values.</div>` +
        `<div class="crc-legend crc-legend-col">${legend}${refLegend}</div>`;
      this._pw = { g, xs, ys, dom, lines, refs: refs.map((r) => ({ vin: r.vins[0], ref: r.ref, vins: r.vins })) };
    }
    el.innerHTML = `${html}<div class="crc-fast"><div>${list}</div><div>${chart}</div></div>`;
  }

  _renderHealth() {
    const el = this._sections.health;
    const tz = this._tz;
    const cap = this._data.capacity || {};
    const h = this._health;
    const allEntries = this._vins.map((vin) => ({ vin, ...(cap[vin] || {}) })).filter((e) => (e.pct_points || []).length);
    const hasProjection = allEntries.some((e) => (e.projected_range || []).length);
    const rangeSeg = HEALTH_RANGES.map(
      ([key]) => `<button type="button" data-action="health-range" data-range="${key}" aria-pressed="${h.range === key}" title="${esc(key === "all" ? "All capacity history" : rangeTitle(key))}" aria-label="${esc(key === "all" ? "All capacity history" : rangeTitle(key))}">${key === "all" ? "All" : key}</button>`
    ).join("");
    let head = '<div class="crc-head"><h3 class="crc-title">Battery health</h3>';
    if (hasProjection) {
      head += `<button type="button" class="crc-chip" data-action="health-range-toggle" aria-pressed="${h.showRange}" title="Overlay the full-charge range (miles) implied by each capacity reading">Show projected range</button>`;
    }
    head += `<div class="crc-seg" role="group" aria-label="Health range">${rangeSeg}</div></div>`;
    if (!allEntries.length) {
      el.innerHTML = head + '<div class="crc-empty">No capacity readings yet.</div>';
      this._hl = null;
      return;
    }
    // Windowed merged points per vehicle.
    let latest = 0;
    for (const e of allEntries) for (const p of e.pct_points) latest = Math.max(latest, p[0]);
    const entries = allEntries.map((e) => ({ ...e, pts: limitHealth(healthPoints(e), h.range, latest) })).filter((e) => e.pts.length);
    let tMin = Infinity;
    let tMax = -Infinity;
    let pMin = Infinity;
    let pMax = -Infinity;
    let rMin = Infinity;
    let rMax = -Infinity;
    for (const e of entries) {
      for (const p of e.pts) {
        tMin = Math.min(tMin, p.t);
        tMax = Math.max(tMax, p.t);
        pMin = Math.min(pMin, p.pct);
        pMax = Math.max(pMax, p.pct);
      }
      if (h.showRange) {
        for (const [t, v] of e.projected_range || []) {
          if (t < tMin - DAY_S) continue;
          rMin = Math.min(rMin, v);
          rMax = Math.max(rMax, v);
        }
      }
    }
    if (tMax === tMin) tMax = tMin + DAY_S;
    const pad = (tMax - tMin) * 0.03;
    const [pLo, pHi] = niceDomain(Math.min(pMin, 100) - 0.3, Math.max(pMax, 100) + 0.3, 4);
    const hasRange = h.showRange && Number.isFinite(rMin);
    const [rLo, rHi] = hasRange ? niceDomain(rMin - 2, rMax + 2, 4) : [0, 1];
    const original = sharedOriginal(entries);
    const W = this._chartWidth(el);
    const H = this._stacked ? 230 : 260;
    const g = chartLayout(W, H, { left: 44, right: 44 + (hasRange ? 42 : 0), top: 20, bottom: 22 });
    const xs = scaleLinear(tMin - pad, tMax + pad, g.x0, g.x1);
    const ys = scaleLinear(pLo, pHi, g.y1, g.y0);
    const yr = scaleLinear(rLo, rHi, g.y1, g.y0);
    const parts = [];
    const ticks = dualAxisTicks(pLo, pHi, original || 100, 4);
    for (const t of ticks.pct) {
      parts.push(`<line class="crc-grid" x1="${g.x0}" x2="${g.x1}" y1="${ys(t.pct).toFixed(1)}" y2="${ys(t.pct).toFixed(1)}"/>`);
      // Right axis: % of original (the shared scale).
      parts.push(`<text x="${g.x1 + 5}" y="${(ys(t.pct) + 3.5).toFixed(1)}">${+t.v.toFixed(2)}%</text>`);
      if (!original) parts.push(`<text x="${g.x0 - 5}" y="${(ys(t.pct) + 3.5).toFixed(1)}" text-anchor="end">${+t.v.toFixed(2)}%</text>`);
    }
    if (original) {
      for (const t of ticks.kwh) {
        parts.push(`<text x="${g.x0 - 5}" y="${(ys(t.pct) + 3.5).toFixed(1)}" text-anchor="end">${+t.v.toFixed(1)}</text>`);
      }
      parts.push(`<text class="crc-axtitle" x="${g.x0 - 5}" y="${g.y0 - 8}" text-anchor="end">kWh</text>`);
    }
    parts.push(
      hasRange
        ? `<text class="crc-axtitle" x="${g.x1 + 5}" y="${g.y0 - 8}">%</text>`
        : `<text class="crc-axtitle" x="${W - 2}" y="${g.y0 - 8}" text-anchor="end">% of original</text>`
    );
    if (hasRange) {
      const rx = g.x1 + 46;
      for (const v of niceTicks(rLo, rHi, 4)) {
        parts.push(`<text x="${rx}" y="${(yr(v) + 3.5).toFixed(1)}">${Math.round(v)}</text>`);
      }
      parts.push(`<text class="crc-axtitle" x="${rx}" y="${g.y0 - 8}">mi</text>`);
    }
    for (const t of timeTicks(tMin - pad, tMax + pad, tz, this._stacked ? 4 : 7)) {
      parts.push(`<text x="${xs(t.ts).toFixed(1)}" y="${g.y1 + 15}" text-anchor="middle">${esc(t.label)}</text>`);
    }
    parts.push(`<line class="crc-axis" x1="${g.x0}" x2="${g.x1}" y1="${g.y1}" y2="${g.y1}"/>`);
    const r = entries.some((e) => e.pts.length > 200) ? 2.4 : 3.2;
    for (const e of entries) {
      const color = this._info(e.vin).color;
      if (hasRange && (e.projected_range || []).length) {
        parts.push(`<path d="${linePath(e.projected_range, xs, yr)}" fill="none" stroke="${esc(color)}" stroke-width="1.8" stroke-dasharray="5 3" opacity="0.9"/>`);
      }
      parts.push(
        `<path d="${linePath(e.pts.map((p) => [p.t, p.pct]), xs, ys)}" fill="none" stroke="${esc(color)}" stroke-width="1.6" stroke-linejoin="round" opacity="0.75"/>`
      );
      const ring = this._multi ? esc(color) : "var(--crc-surface)";
      parts.push(
        e.pts
          .map(
            (p) =>
              `<circle cx="${xs(p.t).toFixed(1)}" cy="${ys(p.pct).toFixed(1)}" r="${r}" fill="${tempColor(p.temp, this._dark)}" stroke="${ring}" stroke-width="${this._multi ? 1.3 : 1}"/>`
          )
          .join("")
      );
    }
    parts.push(`<line class="crc-cursor" data-cursor y1="${g.y0}" y2="${g.y1}" x1="-10" x2="-10"/>`);
    const legend = [];
    if (this._multi) {
      for (const e of entries) legend.push(`<span><i class="crc-swatch" style="--sw:${esc(this._info(e.vin).color)}"></i>${esc(this._info(e.vin).name)}</span>`);
    }
    legend.push(original ? '<span class="crc-note">Same line on two scales: kWh (left), % of original (right)</span>' : '<span class="crc-note">% of original capacity (kWh differs by vehicle: see the readout)</span>');
    if (hasRange) legend.push('<span class="crc-note">Dashed: projected full range (outer right axis, mi)</span>');
    const tempLegend =
      `<div class="crc-templegend" title="Each dot is colored by the outside temperature when the reading was taken; capacity readings are lower when cold"><div class="crc-gradwrap"><span class="crc-gradend">Cold</span><div><div class="crc-grad" style="background:${tempGradientCss(this._dark)}"></div>` +
      `<div class="crc-gradticks">${TEMP_TICKS.map((t) => `<span>${t}°F</span>`).join("")}</div></div><span class="crc-gradend">Hot</span></div>` +
      `<div class="crc-note">${esc(tempSourceNote(entries, (vin) => this._info(vin).name, this._multi))}</div></div>`;
    const approx = entries.some((e) => e.approximate);
    let sum = '<div class="crc-hsum">';
    for (const e of entries) {
      const rng = projectedRangeText(e);
      sum += `<div>${this._dotHtml(e.vin)}<b>${esc(this._info(e.vin).name)}</b><span>${esc(capacitySummary(e, tz))}</span>${rng ? `<span class="crc-muted">${esc(rng)}</span>` : ""}</div>`;
    }
    sum += "</div>";
    el.innerHTML =
      head +
      (approx ? '<p class="crc-sub">Capacity is approximate for some vehicles (pack nominal used as the starting value).</p>' : "") +
      `<div class="crc-chart" data-chart="health" tabindex="0" role="group" aria-label="Battery capacity over time: hover, tap, or use the arrow keys to read each point"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Battery capacity over time, colored by temperature">${parts.join("")}</svg>${this._tipHtml()}</div>` +
      `<div class="crc-readout" data-readout="health">Hover, tap, or focus the chart and use the arrow keys for values.</div>` +
      `<div class="crc-legend">${legend.join("")}</div>${tempLegend}${sum}`;
    this._hl = { g, xs, ys, yr, entries, hasRange, original };
  }

  _renderHistory() {
    const el = this._sections.history;
    const tz = this._tz;
    const nameOf = (vin) => this._info(vin).name;
    const rows = sortHistory(
      filterHistory(filterSessions(this._scoped(), this._filter), this._hist.type, this._hist.frame, this._now()),
      this._sort.key,
      this._sort.dir,
      nameOf
    );
    let head = '<div class="crc-head"><h3 class="crc-title">Charging history</h3>';
    if (this._zoom) head += `<span class="crc-zoomtag">${esc(spanLabel(this._zoom, tz))}</span>`;
    if (this._filter) {
      head += `<span class="crc-filter">${esc(filterLabel(this._filter, this._multi ? nameOf(this._filter.vin) : ""))}<button type="button" data-action="clear-filter" aria-label="Clear filter" title="Clear the filter">×</button></span>`;
    }
    head += "</div>";
    const typeBtns = HISTORY_TYPES.map(
      ([key, label, title]) => `<button type="button" data-action="hist-type" data-type="${key}" aria-pressed="${this._hist.type === key}" title="${esc(title)}" aria-label="${esc(title)}">${esc(label)}</button>`
    ).join("");
    const frameBtns = HISTORY_FRAMES.map(([key, label, days]) => {
      const title = days === null ? "All time" : `The last ${days} days`;
      return `<button type="button" data-action="hist-frame" data-frame="${key}" aria-pressed="${this._hist.frame === key}" title="${title}" aria-label="${title}">${esc(label)}</button>`;
    }).join("");
    head +=
      `<div class="crc-filters crc-hfilters"><div class="crc-frow"><span class="crc-flabel">Type</span><div class="crc-seg" role="group" aria-label="Charging type">${typeBtns}</div>` +
      `<span class="crc-flabel">Timeframe</span><div class="crc-seg" role="group" aria-label="History timeframe">${frameBtns}</div>` +
      `<span class="crc-note" data-role="history-count" aria-live="polite">${esc(sessionCountText(rows.length))}</span></div></div>`;
    if (!rows.length) {
      el.innerHTML = head + '<div class="crc-empty">No charging sessions to show.</div>';
      return;
    }
    const cols = [
      ["when", "Date"],
      ["vehicle", "Vehicle"],
      ["place", "Place"],
      ["type", "Type"],
      ["soc", "Battery"],
      ["kwh", "kWh"],
      ["peak", "Peak"],
      ["avg", "Avg"],
      ["battery_temp", "Battery temp"],
      ["outside_temp", "Outside"],
      ["duration", "Duration"],
    ];
    let html = '<div class="crc-table" role="table"><div class="crc-tr crc-th" role="row">';
    for (const [key, label] of cols) {
      const on = this._sort.key === key;
      const arrow = on ? (this._sort.dir === "asc" ? "▲" : "▼") : "";
      html += `<span role="columnheader" aria-sort="${ariaSortFor(this._sort, key)}"><button type="button" data-action="sort" data-key="${key}" aria-pressed="${on}" title="${esc(HISTORY_COLUMN_TITLES[key] || label)}. Click to sort.">${label} ${arrow}</button></span>`;
    }
    html += "</div>";
    for (const s of rows.slice(0, this._historyLimit)) {
      const info = this._info(s.vin);
      const station = stationLine(s);
      const where = placeLabel(s);
      html +=
        `<div class="crc-tr${s.inferred ? " crc-row-inferred" : ""}" role="row"><span class="crc-c-when">${esc(formatWhen(s.start_ts, tz))}</span>` +
        `<span class="crc-c-veh"><span class="crc-veh">${this._dotHtml(s.vin)}${esc(info.name)}</span></span>` +
        `<span class="crc-c-place">${esc(where)}${station && station !== where ? `<small class="crc-stn">${esc(station)}</small>` : ""}</span>` +
        `<span class="crc-c-kind"><span class="crc-badge${s.kind === "dc" ? " crc-dc" : ""}" title="${esc(chargeTypeLong(s))}">${esc(chargeTypeLabel(s))}</span>${s.inferred ? `<span class="crc-badge crc-inferred" title="${esc(INFERRED_TITLE)}">Inferred</span>` : ""}</span>` +
        `<span class="crc-c-soc">${esc(socRange(s))}</span><span class="crc-c-kwh">${typeof s.energy_added_kwh === "number" ? `${esc(_num(s.energy_added_kwh, 1))} kWh` : "–"}</span>` +
        `<span class="crc-c-peak" data-l="Peak">${esc(formatRate(s.max_power_kw))}</span>` +
        `<span class="crc-c-avg" data-l="Avg">${esc(formatRate(s.avg_power_kw))}</span>` +
        `<span class="crc-c-btemp" data-l="Battery">${esc(formatTempF(s.battery_temp_f))}</span>` +
        `<span class="crc-c-otemp" data-l="Outside">${esc(formatTempF(s.outside_temp_f))}</span>` +
        `<span class="crc-c-dur">${esc(formatDuration(s.duration_s))}</span></div>`;
    }
    html += "</div>";
    if (rows.length > this._historyLimit) {
      html += `<button type="button" class="crc-more" data-action="more-history">Show ${Math.min(30, rows.length - this._historyLimit)} more (${rows.length - this._historyLimit} left)</button>`;
    }
    el.innerHTML = head + html;
  }

  // -- events -------------------------------------------------------------

  _onClick(ev) {
    const target = ev.target && ev.target.closest ? ev.target.closest("[data-action]") : null;
    if (!target) return;
    const action = target.dataset.action;
    if (action === "range") {
      this._setRange(target.dataset.range).catch((e) => this._showError(e));
    } else if (action === "zoom-reset") {
      this._setZoom(null).catch((e) => this._showError(e));
    } else if (action === "zoom-apply") {
      if (this._sel) this._setZoom(this._sel).catch((e) => this._showError(e));
    } else if (action === "zoom-clear") {
      this._sel = null;
      this._renderTimeline();
    } else if (action === "filter") {
      const f = { vin: target.dataset.vin, metric: target.dataset.metric, fastOnly: target.dataset.fast === "1" };
      this._filter = sameFilter(f, this._filter) ? null : f;
      this._historyLimit = 20;
      this._renderTimeline();
      this._renderScore();
      this._renderHistory();
    } else if (action === "hist-type" || action === "hist-frame") {
      if (action === "hist-type") this._hist.type = target.dataset.type;
      else this._hist.frame = target.dataset.frame;
      this._saveHistoryPrefs();
      this._historyLimit = 20;
      this._renderHistory();
    } else if (action === "clear-filter") {
      this._filter = null;
      this._renderTimeline();
      this._renderScore();
      this._renderHistory();
    } else if (action === "sort") {
      const key = target.dataset.key;
      this._sort = this._sort.key === key ? { key, dir: this._sort.dir === "asc" ? "desc" : "asc" } : { key, dir: key === "when" || key === "kwh" || key === "duration" ? "desc" : "asc" };
      this._renderHistory();
    } else if (action === "more-dc") {
      this._dcLimit += 12;
      this._renderFast();
    } else if (action === "less-dc") {
      this._dcLimit = 8;
      this._renderFast();
    } else if (action === "more-history") {
      this._historyLimit += 30;
      this._renderHistory();
    } else if (action === "delete") {
      this._deleteSession(target.dataset.key).catch((e) => this._showError(e));
    } else if (action === "fast-frame") {
      this._fast.frame = target.dataset.frame;
      this._ensureChecked();
      this._renderFast();
    } else if (action === "color-by") {
      this._fast.colorBy = target.dataset.mode === "vehicle" ? "vehicle" : "brand";
      this._renderFast();
    } else if (action === "brand-chip" || action === "tag-chip") {
      const set = action === "brand-chip" ? this._fast.brands : this._fast.tags;
      const value = action === "brand-chip" ? target.dataset.brand : target.dataset.tag;
      if (set.has(value)) set.delete(value);
      else set.add(value);
      this._ensureChecked();
      this._renderFast();
    } else if (action === "health-range") {
      this._health.range = target.dataset.range;
      this._renderHealth();
    } else if (action === "health-range-toggle") {
      this._health.showRange = !this._health.showRange;
      this._renderHealth();
    }
  }

  _onChange(ev) {
    const t = ev.target;
    if (!t || !t.dataset || t.dataset.action !== "check") return;
    if (t.checked) this._checked.add(t.dataset.key);
    else this._checked.delete(t.dataset.key);
    this._renderFast();
  }

  async _deleteSession(key) {
    const session = this._data.sessions.find((s) => sessionKey(s) === key);
    if (!session) return;
    if (typeof window !== "undefined" && !window.confirm(deleteMessage(session, this._info(session.vin).name, this._tz))) return;
    await this._hass.callWS({ type: "rivian/charging/delete_session", vin: session.vin, session_id: session.session_id });
    await this._refreshAll();
  }

  // -- brush to zoom --------------------------------------------------------

  /** SVG-space (viewBox) coordinates of a pointer event over a chart wrapper. */
  _svgPoint(ev, wrap) {
    const svg = wrap.querySelector("svg");
    const rect = svg.getBoundingClientRect();
    const scale = svg.viewBox.baseVal.width / (rect.width || 1);
    return { px: (ev.clientX - rect.left) * scale, py: (ev.clientY - rect.top) * scale };
  }

  _drawBrush() {
    const b = this._brush;
    if (!b || !b.active || !this._tl) return;
    const rect = b.wrap.querySelector("[data-brush]");
    if (!rect) return;
    const { g } = this._tl;
    const a = Math.min(Math.max(Math.min(b.px0, b.px1), g.x0), g.x1);
    const c = Math.min(Math.max(Math.max(b.px0, b.px1), g.x0), g.x1);
    rect.setAttribute("x", a);
    rect.setAttribute("width", Math.max(0, c - a));
    rect.style.visibility = c - a > 0 ? "visible" : "hidden";
  }

  _clearBrush() {
    const b = this._brush;
    if (!b) return;
    clearTimeout(b.timer);
    const rect = b.wrap && b.wrap.querySelector("[data-brush]");
    if (rect && !this._sel) rect.style.visibility = "hidden";
    this._brush = null;
  }

  _onDown(ev) {
    const wrap = ev.target && ev.target.closest ? ev.target.closest(".crc-chart") : null;
    if (!wrap || wrap.dataset.chart !== "timeline" || !this._tl) return;
    const touch = ev.pointerType === "touch";
    if (!touch && ev.button !== undefined && ev.button !== 0) return;
    const { px } = this._svgPoint(ev, wrap);
    const { g } = this._tl;
    if (px < g.x0 || px > g.x1) return;
    this._clearBrush();
    const b = { id: ev.pointerId, touch, wrap, px0: px, px1: px, cx: ev.clientX, cy: ev.clientY, moved: 0, downAt: performance.now(), active: !touch };
    this._brush = b;
    if (touch) {
      b.timer = setTimeout(() => {
        if (this._brush !== b || b.active) return;
        if (!isLongPress(b.downAt, performance.now(), b.moved)) return;
        b.active = true;
        try {
          wrap.setPointerCapture(b.id);
        } catch (_err) {
          /* capture is best-effort */
        }
        if (typeof navigator !== "undefined" && navigator.vibrate) navigator.vibrate(12);
        this._drawBrush();
      }, 450);
    } else {
      try {
        wrap.setPointerCapture(ev.pointerId);
      } catch (_err) {
        /* capture is best-effort */
      }
    }
  }

  _onBrushMove(ev) {
    const b = this._brush;
    if (!b || ev.pointerId !== b.id) return;
    b.moved = Math.max(b.moved, Math.hypot(ev.clientX - b.cx, ev.clientY - b.cy));
    if (b.touch && !b.active) {
      if (b.moved > 10) this._clearBrush();
      return;
    }
    b.px1 = this._svgPoint(ev, b.wrap).px;
    this._drawBrush();
  }

  _onUp(ev) {
    const b = this._brush;
    if (!b || ev.pointerId !== b.id) return;
    this._brush = null;
    clearTimeout(b.timer);
    if (!b.active || !this._tl) return;
    const { g, x0, x1 } = this._tl;
    const span = brushSpan(b.px0, b.px1, g, x0, x1);
    try {
      b.wrap.releasePointerCapture(b.id);
    } catch (_err) {
      /* already released */
    }
    if (!span) {
      const rect = b.wrap.querySelector("[data-brush]");
      if (rect && !this._sel) rect.style.visibility = "hidden";
      return;
    }
    if (b.touch) {
      this._sel = span;
      this._renderTimeline();
    } else {
      this._setZoom(span).catch((e) => this._showError(e));
    }
  }

  _onDblClick(ev) {
    const wrap = ev.target && ev.target.closest ? ev.target.closest(".crc-chart") : null;
    if (!wrap || wrap.dataset.chart !== "timeline") return;
    if (this._zoom) this._setZoom(null).catch((e) => this._showError(e));
    else if (this._sel) {
      this._sel = null;
      this._renderTimeline();
    }
  }

  _onLeave(ev) {
    if (ev.pointerType === "touch") return;
    const wrap = ev.target && ev.target.closest ? ev.target.closest(".crc-chart") : null;
    if (!wrap) return;
    this._hideTip(wrap);
    const cur = wrap.querySelector("[data-cursor]");
    if (cur) {
      cur.style.visibility = "hidden";
      cur.setAttribute("x1", -10);
      cur.setAttribute("x2", -10);
    }
  }

  _hideTip(wrap) {
    const tip = wrap.querySelector(".crc-tip");
    if (tip) tip.style.display = "none";
  }

  _showTip(wrap, html, px, py) {
    const tip = wrap.querySelector(".crc-tip");
    if (!tip) return;
    tip.innerHTML = html;
    tip.style.display = "block";
    const w = wrap.clientWidth;
    const tw = tip.offsetWidth;
    let left = px + 14;
    if (left + tw > w) left = Math.max(0, px - tw - 14);
    tip.style.left = `${left}px`;
    tip.style.top = `${Math.max(0, py - tip.offsetHeight - 10)}px`;
  }

  /** The chart's keyboard stops in SVG coordinates, left to right. */
  _chartStops(kind) {
    if (kind === "timeline" && this._tl) {
      const { g, xs, x0, x1 } = this._tl;
      const sessions = (this._tlSessions || []).slice().sort((a, b) => a.start_ts - b.start_ts);
      if (sessions.length) {
        return sessions.map((s) => {
          const [a, b] = spanExtent(s, xs, 4);
          return { px: (a + b) / 2, py: g.y0 + g.h / 2 };
        });
      }
      return Array.from({ length: 12 }, (_v, i) => ({ px: g.x0 + ((i + 0.5) / 12) * g.w, py: g.y0 + g.h / 2, ts: x0 + ((i + 0.5) / 12) * (x1 - x0) }));
    }
    if (kind === "power" && this._pw) {
      const { g, dom, lines, ys } = this._pw;
      return Array.from({ length: 21 }, (_v, i) => {
        const px = g.x0 + (i / 20) * g.w;
        const soc = invertLinear(dom.x[0], dom.x[1], g.x0, g.x1)(px);
        const first = lines.map((ln) => valueAt(ln.pts, soc)).find((v) => v !== null);
        return { px, py: first === undefined ? g.y0 : ys(first) };
      });
    }
    if (kind === "health" && this._hl) {
      const { g, xs, ys, entries } = this._hl;
      const byT = new Map();
      for (const e of entries) for (const p of e.pts) if (!byT.has(p.t)) byT.set(p.t, { px: xs(p.t), py: ys(p.pct) });
      return [...byT.values()].sort((a, b) => a.px - b.px).map((st) => ({ ...st, py: Math.min(g.y1, Math.max(g.y0, st.py)) }));
    }
    return [];
  }

  /** Arrow-key stepping: shows the same readout (and session tooltip) a hover or tap would. */
  _stepChart(wrap, delta) {
    const stops = this._chartStops(wrap.dataset.chart);
    const cur = wrap.dataset.kidx === undefined ? null : Number(wrap.dataset.kidx);
    const idx = stepIndex(cur, delta, stops.length);
    if (idx < 0) return;
    wrap.dataset.kidx = String(idx);
    const svg = wrap.querySelector("svg");
    const rect = svg.getBoundingClientRect();
    const scale = svg.viewBox.baseVal.width / (rect.width || 1);
    const { px, py } = stops[idx];
    this._probeChart(wrap, px, py, [px / scale, py / scale]);
  }

  _onKey(ev) {
    const chart = ev.target && ev.target.classList && ev.target.classList.contains("crc-chart") ? ev.target : null;
    if (!chart) return;
    if (ev.key === "Escape") {
      ev.preventDefault();
      chart.dataset.kidx = "-1";
      this._hideTip(chart);
    } else if (ev.key === "ArrowLeft" || ev.key === "ArrowRight") {
      ev.preventDefault();
      this._stepChart(chart, ev.key === "ArrowRight" ? 1 : -1);
    }
  }

  _onPointer(ev) {
    this._onBrushMove(ev);
    const wrap = ev.target && ev.target.closest ? ev.target.closest(".crc-chart") : null;
    if (!wrap) return;
    const svg = wrap.querySelector("svg");
    const rect = svg.getBoundingClientRect();
    const vb = svg.viewBox.baseVal;
    const scale = vb.width / (rect.width || 1);
    const px = (ev.clientX - rect.left) * scale;
    const py = (ev.clientY - rect.top) * scale;
    this._probeChart(wrap, px, py, [ev.clientX - rect.left, ev.clientY - rect.top]);
  }

  /** Show the readout for the chart position (px, py in SVG units; `local` in CSS pixels). */
  _probeChart(wrap, px, py, local) {
    const kind = wrap.dataset.chart;
    const cursor = wrap.querySelector("[data-cursor]");
    const readout = this._card.querySelector(`[data-readout="${kind}"]`);
    const tz = this._tz;
    const setCursor = (x) => {
      cursor.style.visibility = "visible";
      cursor.setAttribute("x1", x);
      cursor.setAttribute("x2", x);
    };
    if (kind === "timeline" && this._tl) {
      const { g, xs, x0, x1, entries } = this._tl;
      if (px < g.x0 || px > g.x1) return this._hideTip(wrap);
      const ts = invertLinear(x0, x1, g.x0, g.x1)(px);
      setCursor(px);
      const vals = entries
        .map((e) => ({ e, v: valueAt(e.points, ts) }))
        .filter((r) => r.v !== null)
        .map(({ e, v }) => `<span><i class="crc-swatch" style="--sw:${esc(this._info(e.vin).color)}"></i>${this._multi ? `${esc(this._info(e.vin).name)} ` : ""}<b class="crc-rt">${v.toFixed(0)} %</b></span>`);
      readout.innerHTML = `<span>${esc(formatWhen(ts, tz))}</span>${vals.join("")}`;
      const hit = this._brush && this._brush.active ? [] : sessionsAtPixel(this._tlSessions || [], xs, px, 3).slice(0, 3);
      if (hit.length && (py >= g.y0 && py <= g.y1)) {
        const html = hit
          .map((s) => (s.detected ? detectedTooltipLines(s, this._info(s.vin).name, tz) : sessionTooltipLines(s, this._info(s.vin).name, tz)).map((l, i) => (i === 0 ? `<b>${esc(l)}</b>` : esc(l))).join("<br>"))
          .join("<hr style='border:0;border-top:1px solid var(--crc-line);margin:5px 0'>");
        this._showTip(wrap, html, local[0], local[1]);
      } else this._hideTip(wrap);
    } else if (kind === "power" && this._pw) {
      const { g, dom, lines, refs, ys } = this._pw;
      if (px < g.x0 || px > g.x1) return this._hideTip(wrap);
      const soc = invertLinear(dom.x[0], dom.x[1], g.x0, g.x1)(px);
      setCursor(px);
      const parts = lines
        .map(({ s, color, pts }) => {
          const v = valueAt(pts, soc);
          return v === null ? "" : `<span><i class="crc-swatch" style="--sw:${esc(color)}"></i>${esc(formatDay(s.start_ts, tz))} <b class="crc-rt">${v.toFixed(0)} kW</b></span>`;
        })
        .filter(Boolean);
      const exp = refs
        .map(({ vin, ref }) => {
          const v = curveAt(ref && ref.curve, soc);
          return v === null ? "" : `<span>${this._multi ? `${esc(this._info(vin).name)} ` : ""}ideal <b class="crc-rt">${v.toFixed(0)} kW</b></span>`;
        })
        .filter(Boolean);
      readout.innerHTML = `<span>${soc.toFixed(0)} %</span>${parts.join("")}${exp.join("")}`;
      // Tooltip for the line nearest the pointer: station, brand, place and the value here.
      let best = null;
      for (const ln of lines) {
        const v = valueAt(ln.pts, soc);
        if (v === null) continue;
        const d = Math.abs(ys(v) - py);
        if (best === null || d < best.d) best = { d, ln, v };
      }
      if (best && best.d <= 24) {
        const tl = sessionTooltipLines(best.ln.s, this._info(best.ln.s.vin).name, tz);
        const html = `<b>${esc(tl[0])}</b><br>${tl.slice(1).map(esc).join("<br>")}<br><b>${soc.toFixed(0)} % · ${best.v.toFixed(0)} kW</b>`;
        this._showTip(wrap, html, local[0], local[1]);
      } else this._hideTip(wrap);
    } else if (kind === "health" && this._hl) {
      const { g, xs, entries } = this._hl;
      if (px < g.x0 || px > g.x1) return;
      // Find the nearest day among all vehicles' points.
      let best = null;
      for (const e of entries) for (const p of e.pts) {
        const d = Math.abs(xs(p.t) - px);
        if (best === null || d < best.d) best = { d, t: p.t };
      }
      if (!best) return;
      setCursor(xs(best.t));
      const parts = entries.map((e) => {
        const p = e.pts.find((q) => q.t === best.t);
        if (!p) return "";
        const rg = (e.projected_range || []).find((q) => q[0] === best.t);
        const temp = tempText(p);
        return `<span><i class="crc-swatch" style="--sw:${esc(this._info(e.vin).color)}"></i>${this._multi ? `${esc(this._info(e.vin).name)} ` : ""}<b class="crc-rt">${p.pct.toFixed(1)} %</b> · ${p.kwh.toFixed(1)} kWh${temp ? ` · ${esc(temp)}` : ""}${rg && this._health.showRange ? ` · ${Math.round(rg[1])} mi` : ""}</span>`;
      });
      readout.innerHTML = `<span>${esc(formatDay(best.t, tz))}</span>${parts.join("")}`;
    }
  }
}

if (typeof customElements !== "undefined" && !customElements.get("rivian-charging-card")) {
  customElements.define("rivian-charging-card", RivianChargingCard);
  if (typeof window !== "undefined") {
    window.customCards = window.customCards || [];
    window.customCards.push({
      type: "rivian-charging-card",
      name: "Rivian Charging & Battery",
      description: "Battery level, charging scorecard, fast-charge curves, battery health and charging history.",
    });
  }
}
