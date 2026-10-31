/**
 * rivian-efficiency-card.js
 *
 * `custom:rivian-efficiency-card` is the Efficiency tab: one panel card with the
 * shared vehicle bar in its header and, top to bottom:
 *   - summary chips (average mi/kWh per vehicle, average score, best / worst
 *     conditions drive links),
 *   1. Efficiency vs [X]: a scatter (X = temperature, headwind, air density,
 *      rain, average speed, trip length or climb per mile), one color per
 *      vehicle, point size by distance, a least-squares trend line per vehicle
 *      once it has 5 points,
 *   2. Efficiency by speed band: grouped bars per vehicle,
 *   3. Trend over time: weekly / monthly mi/kWh per vehicle over faint miles bars,
 *   4. A sortable drive table; a row opens that drive on the Drives tab.
 *
 * Backend calls (all `hass.callWS`):
 *   - `rivian/vehicles/list` and the shared selection (rivian-vehicle-bar.js)
 *   - `rivian/analytics/efficiency {vins, days}` -> {drives, speed_bands, trend}
 *   - `rivian/analytics/subscribe {vins}` for live refreshes
 *
 * Optional config: `vins` pins the card to those vehicles (otherwise it follows
 * the shared selection); `drives_path` overrides the Drives tab URL (default:
 * this URL with its last segment replaced by `drives`). Charts are inline SVG,
 * no chart library.
 *
 * Pure helpers are exported for the Node test (tests/frontend/
 * efficiency_card.test.mjs); every top-level use of HTMLElement/customElements/
 * window/document is guarded so the module imports under plain Node.
 */

// -- constants ---------------------------------------------------------------

/** The range toggle: key -> days (null = everything retained). */
export const RANGES = [
  ["30d", 30],
  ["90d", 90],
  ["1y", 365],
  ["all", null],
];

/** The ``days`` to request for a range key. */
export function rangeDays(key) {
  const entry = RANGES.find((r) => r[0] === key) || RANGES[0];
  return entry[1];
}

/** Score thresholds (score = expected energy / actual energy). */
export const SCORE_GOOD = 1.05;
export const SCORE_BAD = 0.95;
/** A trend line needs at least this many points. */
export const MIN_TREND_POINTS = 5;
/** "Best / worst conditions" ignore trips shorter than this (expected values get noisy). */
export const MIN_CONDITION_MILES = 2;

const DAY_S = 86400;
const STACK_BREAKPOINT_PX = 700;
const FALLBACK_COLOR = "#8a8a8a";
const SELECTION_PREFIX = "rivian-drive-explorer-selection:";
/** A one-shot "open this drive" request the Drives tab consumes, even when it is already loaded. */
export const OPEN_REQUEST_KEY = "rivian-drive-explorer-open";
const PAGE = 50;

/** X-axis choices for the scatter. `get(row)` yields a number or null. */
export const X_OPTIONS = [
  { key: "temp", label: "Temperature", unit: "°F", digits: 0, field: "temp_f" },
  { key: "headwind", label: "Headwind", unit: "mph", digits: 1, field: "headwind_mph", note: "negative = tailwind" },
  { key: "density", label: "Air density", unit: "kg/m³", digits: 3, field: "air_density" },
  { key: "rain", label: "Rain", unit: "mm", digits: 1, field: "precip_mm" },
  { key: "speed", label: "Average speed", unit: "mph", digits: 0, field: "avg_speed_mph" },
  { key: "length", label: "Trip length", unit: "mi", digits: 1, field: "trip_length_mi" },
  { key: "climb", label: "Climb", unit: "ft/mi", digits: 0, field: "climb_ft_per_mi" },
];

// -- pure helpers ------------------------------------------------------------

function _finite(v) {
  return typeof v === "number" && Number.isFinite(v);
}

/** The X option for a key (default: temperature). */
export function xOption(key) {
  return X_OPTIONS.find((o) => o.key === key) || X_OPTIONS[0];
}

/** One drive's X value for an axis choice, or null when missing. */
export function extractX(row, key) {
  if (!row) return null;
  const v = row[xOption(key).field];
  return _finite(v) ? v : null;
}

/** Drives with a usable efficiency, oldest first. */
export function usableDrives(drives) {
  return (Array.isArray(drives) ? drives : [])
    .filter((d) => d && _finite(d.efficiency_mi_kwh) && d.efficiency_mi_kwh > 0)
    .slice()
    .sort((a, b) => (a.date_ts || 0) - (b.date_ts || 0));
}

/** `{points: [{x, y, drive}], missing}`: drives lacking the X value are counted, not plotted. */
export function scatterPoints(drives, key) {
  const points = [];
  let missing = 0;
  for (const d of usableDrives(drives)) {
    const x = extractX(d, key);
    if (x === null) missing += 1;
    else points.push({ x, y: d.efficiency_mi_kwh, drive: d });
  }
  return { points, missing };
}

/** Group scatter points by vehicle, preserving the `vins` order. */
export function pointsByVin(points, vins) {
  const out = {};
  for (const vin of vins) out[vin] = [];
  for (const p of points) {
    if (out[p.drive.vin]) out[p.drive.vin].push(p);
  }
  return out;
}

/**
 * Ordinary least squares of y on x. Returns `{slope, intercept, r2, n, xMin, xMax}`
 * or null with fewer than `minPoints` points or no x spread.
 */
export function linearRegression(points, minPoints = MIN_TREND_POINTS) {
  const pts = (Array.isArray(points) ? points : []).filter((p) => _finite(p.x) && _finite(p.y));
  const n = pts.length;
  if (n < minPoints) return null;
  let sx = 0;
  let sy = 0;
  for (const p of pts) {
    sx += p.x;
    sy += p.y;
  }
  const mx = sx / n;
  const my = sy / n;
  let sxx = 0;
  let sxy = 0;
  let syy = 0;
  let xMin = Infinity;
  let xMax = -Infinity;
  for (const p of pts) {
    sxx += (p.x - mx) ** 2;
    sxy += (p.x - mx) * (p.y - my);
    syy += (p.y - my) ** 2;
    xMin = Math.min(xMin, p.x);
    xMax = Math.max(xMax, p.x);
  }
  if (sxx <= 0) return null;
  const slope = sxy / sxx;
  const intercept = my - slope * mx;
  const r2 = syy > 0 ? (sxy * sxy) / (sxx * syy) : 0;
  return { slope, intercept, r2, n, xMin, xMax };
}

/** The two end points `[[x, y], [x, y]]` of a regression line across its x range. */
export function trendLineEnds(reg) {
  if (!reg) return null;
  return [
    [reg.xMin, reg.intercept + reg.slope * reg.xMin],
    [reg.xMax, reg.intercept + reg.slope * reg.xMax],
  ];
}

/** Point radius in px for a trip distance, area ~ distance (3..9 px). */
export function pointRadius(distanceMi, maxDistanceMi) {
  const max = _finite(maxDistanceMi) && maxDistanceMi > 0 ? maxDistanceMi : 1;
  const d = _finite(distanceMi) && distanceMi > 0 ? distanceMi : 0;
  return 3 + 6 * Math.sqrt(Math.min(1, d / max));
}

/** Nearest point to (px, py) within `maxDist` px, using `xs`/`ys` scale functions; else null. */
export function nearestPoint(points, xs, ys, px, py, maxDist = 16) {
  let best = null;
  for (const p of points) {
    const d = Math.hypot(xs(p.x) - px, ys(p.y) - py);
    if (d <= maxDist && (best === null || d < best.d)) best = { d, p };
  }
  return best ? best.p : null;
}

function _binStart(band) {
  const m = /^\s*(\d+)/.exec(String(band));
  return m ? Number(m[1]) : Number.MAX_SAFE_INTEGER;
}

/** "0-9" -> "0–9", "80+" stays. */
export function bandLabel(band) {
  return String(band).replace(/(\d)-(\d)/, "$1–$2");
}

/**
 * Speed-band grouping: `{bands: [labels in speed order], series: {vin: {band: {efficiency, miles, kwh}}}}`.
 * Bands any vehicle has data for are listed, ordered by their lower bound.
 */
export function groupBands(speedBands, vins) {
  const series = {};
  const seen = new Set();
  for (const vin of vins) {
    series[vin] = {};
    for (const row of (speedBands && speedBands[vin]) || []) {
      if (!row || !_finite(row.efficiency)) continue;
      series[vin][row.band] = { efficiency: row.efficiency, miles: row.miles, kwh: row.kwh };
      seen.add(row.band);
    }
  }
  const bands = [...seen].sort((a, b) => _binStart(a) - _binStart(b));
  return { bands, series };
}

/**
 * Trend shaping for a period (`"weekly"` | `"monthly"`):
 * `{span, series: {vin: [{ts, eff, mpge, miles}]}, tMin, tMax}`. `span` is the
 * nominal period length in seconds (the bar width), `tMax` includes the last period.
 */
export function trendShape(trend, vins, period = "weekly") {
  const span = period === "monthly" ? 30.44 * DAY_S : 7 * DAY_S;
  const series = {};
  let tMin = Infinity;
  let tMax = -Infinity;
  for (const vin of vins) {
    const rows = (trend && trend[vin] && trend[vin][period]) || [];
    series[vin] = rows
      .filter((r) => Array.isArray(r) && _finite(r[0]) && _finite(r[1]) && r[1] > 0)
      .map((r) => ({ ts: r[0], eff: r[1], mpge: r[2], miles: _finite(r[3]) ? r[3] : 0 }));
    for (const r of series[vin]) {
      tMin = Math.min(tMin, r.ts);
      tMax = Math.max(tMax, r.ts + span);
    }
  }
  if (!Number.isFinite(tMin)) return { span, series, tMin: null, tMax: null };
  return { span, series, tMin, tMax };
}

/** "good" (>= 105 %), "bad" (<= 95 %) or "ok"; null when there is no score. */
export function scoreClass(score) {
  if (!_finite(score)) return null;
  if (score >= SCORE_GOOD) return "good";
  if (score <= SCORE_BAD) return "bad";
  return "ok";
}

/** "104 %" or "–". */
export function formatScore(score) {
  return _finite(score) ? `${Math.round(score * 100)} %` : "–";
}

/** A glyph so the score class never relies on color alone. */
export function scoreGlyph(score) {
  const c = scoreClass(score);
  return c === "good" ? "▲" : c === "bad" ? "▼" : "";
}

function _num(v, digits = 0) {
  return _finite(v) ? v.toFixed(digits) : "–";
}

/** "27 min", "1 h 12 min"; "–" when missing. */
export function formatDuration(seconds) {
  if (!_finite(seconds) || seconds < 0) return "–";
  if (seconds < 60) return `${Math.round(seconds)} s`;
  const mins = Math.round(seconds / 60);
  if (mins < 60) return `${mins} min`;
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  return m ? `${h} h ${m} min` : `${h} h`;
}

function _fmt(ts, tz, opts) {
  if (!_finite(ts)) return "–";
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

/** "Sep 20, 2026" */
export function formatDayYear(ts, tz) {
  return _fmt(ts, tz, { month: "short", day: "numeric", year: "numeric" });
}

/** "Sep '26" for a monthly bucket. */
export function formatMonth(ts, tz) {
  return _fmt(ts, tz, { month: "short", year: "2-digit" });
}

/** A formatted X value with its unit: "42 °F", "−3.2 mph". */
export function formatX(key, value) {
  const o = xOption(key);
  if (!_finite(value)) return "–";
  const text = value.toFixed(o.digits).replace("-", "−");
  return `${text} ${o.unit}`;
}

/** Table columns: key, header, whether numeric. */
export const TABLE_COLUMNS = [
  ["date", "Date"],
  ["vehicle", "Vehicle"],
  ["distance", "Distance"],
  ["duration", "Duration"],
  ["speed", "Avg speed"],
  ["temp", "Temp"],
  ["headwind", "Headwind"],
  ["rain", "Rain"],
  ["climb", "Climb/mi"],
  ["eff", "mi/kWh"],
  ["expected", "Expected"],
  ["score", "Score"],
];

/** Column explanations for the drive table headers (hover/tap/focus). */
export const COLUMN_TITLES = {
  date: "When the drive started",
  vehicle: "Which vehicle made the drive",
  distance: "Distance driven, in miles",
  duration: "Time from start to end of the drive",
  speed: "Average speed over the drive (distance / duration)",
  temp: "Outside temperature during the drive",
  headwind: "Wind component along the direction of travel, in mph. Positive = headwind, negative = tailwind",
  rain: "Precipitation during the drive, in millimeters",
  climb: "Net climb per mile driven, in feet per mile (negative = net descent)",
  eff: "Actual efficiency in miles per kilowatt-hour (higher is better)",
  expected: "Efficiency the model predicts for this drive's weather, terrain and speed (mi/kWh)",
  score: "Score = expected energy / actual energy. 100% = as expected, above 105% is better than expected (up arrow), below 95% is worse (down arrow)",
};

/** Hover/tap explanation for the score everywhere it appears. */
export const SCORE_TITLE = COLUMN_TITLES.score;

/** The title for a range toggle button ("30d" -> "Last 30 days"). */
export function rangeTitle(key) {
  const days = rangeDays(key);
  if (days === null || days === undefined) return "Every drive on record";
  return days >= 365 && days % 365 === 0
    ? `Last ${days / 365 === 1 ? "year" : `${days / 365} years`}`
    : `Last ${days} days`;
}

/** The `aria-sort` value for a table column header. */
export function ariaSortFor(sort, key) {
  if (!sort || sort.key !== key) return "none";
  return sort.dir === "asc" ? "ascending" : "descending";
}

/** The next index when stepping through `n` chart targets with the arrow keys (clamped; first press starts at an end). */
export function stepIndex(current, delta, n) {
  if (!(n > 0)) return -1;
  if (current === null || current === undefined || current < 0) return delta < 0 ? n - 1 : 0;
  return Math.max(0, Math.min(n - 1, current + delta));
}

/** The default direction when first sorting by a column. */
export function defaultDir(key) {
  return key === "vehicle" ? "asc" : "desc";
}

/** Sort the drive table. Missing values always sort last. `nameOf(vin)` feeds the vehicle column. */
export function sortDrives(drives, key, dir = "desc", nameOf = (v) => v) {
  const get = {
    date: (d) => d.date_ts,
    vehicle: (d) => String(nameOf(d.vin) || "").toLowerCase(),
    distance: (d) => d.distance_mi,
    duration: (d) => d.duration_s,
    speed: (d) => d.avg_speed_mph,
    temp: (d) => d.temp_f,
    headwind: (d) => d.headwind_mph,
    rain: (d) => d.precip_mm,
    climb: (d) => d.climb_ft_per_mi,
    eff: (d) => d.efficiency_mi_kwh,
    expected: (d) => d.expected_eff_mi_kwh,
    score: (d) => d.score,
  }[key] || ((d) => d.date_ts);
  const sign = dir === "asc" ? 1 : -1;
  const missing = (v) => v === null || v === undefined || (typeof v === "number" && Number.isNaN(v));
  return (Array.isArray(drives) ? drives : []).slice().sort((a, b) => {
    const x = get(a);
    const y = get(b);
    if (missing(x) && missing(y)) return (b.date_ts || 0) - (a.date_ts || 0);
    if (missing(x)) return 1;
    if (missing(y)) return -1;
    if (x < y) return -sign;
    if (x > y) return sign;
    return (b.date_ts || 0) - (a.date_ts || 0);
  });
}

/** Stable key for a drive across vehicles. */
export function driveKey(drive) {
  return `${drive.vin}|${drive.drive_id}`;
}

/**
 * Summary numbers: `{perVin: {vin: {eff, drives, miles}}, avgScore, scored, best, worst}`.
 * `eff` is energy-weighted (total miles / total kWh). Best / worst conditions are the
 * drives with the highest / lowest *expected* mi/kWh (trips under MIN_CONDITION_MILES
 * ignored unless nothing longer has an expectation).
 */
export function summaryStats(drives, vins) {
  const perVin = {};
  for (const vin of vins) perVin[vin] = { eff: null, drives: 0, miles: 0, kwh: 0 };
  let scoreSum = 0;
  let scored = 0;
  const all = usableDrives(drives);
  for (const d of all) {
    const s = perVin[d.vin];
    if (!s) continue;
    s.drives += 1;
    s.miles += d.distance_mi || 0;
    s.kwh += (d.distance_mi || 0) / d.efficiency_mi_kwh;
    if (_finite(d.score)) {
      scoreSum += d.score;
      scored += 1;
    }
  }
  for (const s of Object.values(perVin)) s.eff = s.kwh > 0 ? s.miles / s.kwh : null;
  const withExp = all.filter((d) => _finite(d.expected_eff_mi_kwh) && d.expected_eff_mi_kwh > 0);
  const long = withExp.filter((d) => (d.distance_mi || 0) >= MIN_CONDITION_MILES);
  const pool = long.length ? long : withExp;
  let best = null;
  let worst = null;
  for (const d of pool) {
    if (best === null || d.expected_eff_mi_kwh > best.expected_eff_mi_kwh) best = d;
    if (worst === null || d.expected_eff_mi_kwh < worst.expected_eff_mi_kwh) worst = d;
  }
  if (best === worst) worst = null;
  return { perVin, avgScore: scored ? scoreSum / scored : null, scored, best, worst };
}

/** The local "YYYY-MM-DD" day an epoch timestamp falls on in `tz` (the Drives tab's day key). */
export function localDayKey(ts, tz) {
  if (!_finite(ts)) return null;
  try {
    const parts = new Intl.DateTimeFormat("en-CA", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      timeZone: tz || undefined,
    }).formatToParts(new Date(ts * 1000));
    const get = (type) => parts.find((p) => p.type === type).value;
    return `${get("year")}-${get("month")}-${get("day")}`;
  } catch (_err) {
    return null;
  }
}

/**
 * What to store so the Drives tab opens on this drive: the explorer persists its
 * selection in localStorage under `rivian-drive-explorer-selection:<sorted vins>`
 * as `{level: "segment", key: <day>, driveId, vin}`. With one vehicle the explorer's
 * segments carry no `vin`, so neither does the selection (it would never match).
 */
export function navigationSelection(drive, vins, tz) {
  const key = localDayKey(drive && drive.date_ts, tz);
  if (!drive || !key) return null;
  const set = vins && vins.length ? vins : [drive.vin];
  const selection = { level: "segment", key, driveId: drive.drive_id };
  if (set.length > 1) selection.vin = drive.vin;
  return {
    storageKey: `${SELECTION_PREFIX}${[...set].sort().join(",")}`,
    selection,
  };
}

/** The Drives tab URL: `configured` if given, else `pathname` with its last segment swapped for "drives". */
export function drivesPath(pathname, configured) {
  if (configured) return configured;
  const parts = String(pathname || "").split("/").filter(Boolean);
  if (!parts.length) return "/drives";
  parts[parts.length - 1] = "drives";
  return `/${parts.join("/")}`;
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
  for (let v = Math.ceil(min / step - 1e-9) * step; v <= max + step * 1e-9; v += step) {
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
  return [Math.floor(min / step + 1e-9) * step, Math.ceil(max / step - 1e-9) * step];
}

/** Plot-area geometry for an SVG chart of `width` x `height` with margins. */
export function chartLayout(width, height, margin = {}) {
  const m = { left: 40, right: 10, top: 8, bottom: 34, ...margin };
  const x0 = m.left;
  const x1 = Math.max(x0 + 10, width - m.right);
  const y0 = m.top;
  const y1 = Math.max(y0 + 10, height - m.bottom);
  return { width, height, x0, x1, y0, y1, w: x1 - x0, h: y1 - y0 };
}

/** The scatter's domains: x and y padded and rounded to nice ends. */
export function scatterDomains(points, trends = []) {
  let xMin = Infinity;
  let xMax = -Infinity;
  let yMin = Infinity;
  let yMax = -Infinity;
  for (const p of points) {
    xMin = Math.min(xMin, p.x);
    xMax = Math.max(xMax, p.x);
    yMin = Math.min(yMin, p.y);
    yMax = Math.max(yMax, p.y);
  }
  if (!Number.isFinite(xMin)) return null;
  for (const t of trends) {
    if (!t) continue;
    for (const [, y] of t) {
      yMin = Math.min(yMin, y);
      yMax = Math.max(yMax, y);
    }
  }
  const xPad = (xMax - xMin) * 0.05 || 1;
  const yPad = (yMax - yMin) * 0.08 || 0.5;
  const x = niceDomain(xMin - xPad, xMax + xPad, 6);
  const y = niceDomain(Math.max(0, yMin - yPad), yMax + yPad, 5);
  return { x, y };
}

/** SVG path data for `[[x, y], ...]` through scale functions. */
export function linePath(points, xScale, yScale) {
  let d = "";
  points.forEach((p, i) => {
    d += `${i === 0 ? "M" : "L"}${xScale(p[0]).toFixed(1)},${yScale(p[1]).toFixed(1)}`;
  });
  return d;
}

/** A bar path with a rounded top (data end) and a square baseline. */
export function roundedTopBar(x, y, w, h, r = 4) {
  if (h <= 0 || w <= 0) return "";
  const rr = Math.min(r, w / 2, h);
  return (
    `M${x.toFixed(1)},${(y + h).toFixed(1)}V${(y + rr).toFixed(1)}Q${x.toFixed(1)},${y.toFixed(1)} ${(x + rr).toFixed(1)},${y.toFixed(1)}` +
    `H${(x + w - rr).toFixed(1)}Q${(x + w).toFixed(1)},${y.toFixed(1)} ${(x + w).toFixed(1)},${(y + rr).toFixed(1)}V${(y + h).toFixed(1)}Z`
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

/** Tooltip lines for a scatter point / table drive. */
export function driveTooltipLines(drive, key, vehicleName, tz) {
  const lines = [`${vehicleName || "Vehicle"} · ${formatWhen(drive.date_ts, tz)}`];
  lines.push(`${_num(drive.distance_mi, 1)} mi · ${formatX(key, extractX(drive, key))}`);
  lines.push(`${_num(drive.efficiency_mi_kwh, 2)} mi/kWh`);
  if (_finite(drive.score)) lines.push(`Score ${formatScore(drive.score)}${_finite(drive.expected_eff_mi_kwh) ? ` (expected ${_num(drive.expected_eff_mi_kwh, 2)})` : ""}`);
  return lines;
}

// -- the card ---------------------------------------------------------------

function _loadBarModule() {
  return import(new URL("./rivian-vehicle-bar.js" + new URL(import.meta.url).search, import.meta.url));
}

const _STYLE = `
  :host { display: block; }
  ha-card {
    --rec-text: var(--primary-text-color, #212121);
    --rec-muted: var(--secondary-text-color, #727272);
    --rec-line: var(--divider-color, #e0e0e0);
    --rec-surface: var(--ha-card-background, var(--card-background-color, #fff));
    --rec-good: #2a7a3b;
    --rec-bad: #b3302e;
    display: block;
    padding: 0;
    background: var(--rec-surface);
    color: var(--rec-text);
  }
  ha-card.rec-dark { --rec-good: #6fcf84; --rec-bad: #f08a85; color-scheme: dark; }
  .rec-topbar { border-bottom: 1px solid var(--rec-line); }
  .rec-topbar rivian-vehicle-bar { padding: 8px 12px; }
  .rec-chart:focus-visible { outline: 2px solid var(--primary-color, #03a9f4); outline-offset: 2px; }
  .rec-section { padding: 14px 16px 16px; border-bottom: 1px solid var(--rec-line); }
  .rec-section:last-child { border-bottom: 0; }
  .rec-pair { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); border-bottom: 1px solid var(--rec-line); }
  .rec-pair > .rec-section { min-width: 0; border-bottom: 0; }
  .rec-pair .rec-head { min-height: 32px; }
  .rec-pair > .rec-section + .rec-section { border-left: 1px solid var(--rec-line); }
  .rec-head { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; margin-bottom: 8px; }
  .rec-title { font-size: 16px; font-weight: 600; margin: 0; flex: 1 1 auto; }
  /* The page's main title: larger and bolder than section headings. */
  .rec-title.rec-page-title { font-size: 20px; font-weight: 700; letter-spacing: 0.01em; color: var(--primary-text-color); }
  .rec-sub { font-size: 12px; color: var(--rec-muted); margin: 0 0 8px; }
  .rec-seg { display: inline-flex; border: 1px solid var(--rec-line); border-radius: 8px; overflow: hidden; }
  .rec-seg button {
    font: inherit; font-size: 12px; padding: 5px 11px; border: 0; background: transparent;
    color: var(--rec-text); cursor: pointer; min-height: 30px;
  }
  .rec-seg button + button { border-left: 1px solid var(--rec-line); }
  .rec-seg button[aria-pressed="true"] { background: var(--primary-color, #03a9f4); color: #fff; }
  .rec-select {
    font: inherit; font-size: 13px; color: var(--rec-text); background-color: var(--rec-surface);
    border: 1px solid var(--rec-line); border-radius: 8px; padding: 5px 8px; min-height: 30px;
  }
  .rec-select option { background-color: inherit; color: inherit; }
  /* Customizable <select> (Chromium 135+, incl. HA's Android app): the open
     list is drawn by the page, so it follows the theme -- the native list
     ignored it (white frame, wrong size and scrollbar in dark mode). Other
     browsers keep the native control. */
  @supports (appearance: base-select) {
    .rec-select, .rec-select::picker(select) { appearance: base-select; }
    .rec-select { display: inline-flex; align-items: center; gap: 6px; cursor: pointer; }
    .rec-select::picker-icon { color: var(--secondary-text-color, #727272); font-size: 0.8em; }
    .rec-select::picker(select) {
      background: var(--rec-surface);
      color: var(--rec-text);
      border: 1px solid var(--rec-line);
      border-radius: 8px;
      box-shadow: 0 6px 18px rgba(0, 0, 0, 0.35);
      padding: 4px 0;
      margin-block: 2px;
      max-height: min(320px, 60vh);
      overflow-y: auto;
      scrollbar-width: thin;
      scrollbar-color: var(--rec-line) transparent;
      font-family: inherit;
      font-size: 14px;
    }
    .rec-select option { padding: 6px 12px; background: transparent; color: inherit; min-height: 0; }
    .rec-select option:hover, .rec-select option:focus-visible { background: var(--secondary-background-color, rgba(127, 127, 127, 0.18)); outline: none; }
    .rec-select option:checked { font-weight: 600; }
    .rec-select option::checkmark { color: var(--primary-color, #03a9f4); }
  }
  .rec-chart { position: relative; width: 100%; touch-action: pan-y; user-select: none; }
  .rec-chart svg { display: block; width: 100%; height: auto; overflow: visible; }
  .rec-chart text { fill: var(--rec-muted); font-size: 10.5px; font-family: inherit; }
  .rec-chart .rec-vlabel { font-size: 9.5px; fill: var(--rec-text); }
  .rec-chart .rec-axis { stroke: var(--rec-line); stroke-width: 1; }
  .rec-chart .rec-grid { stroke: var(--rec-line); stroke-width: 1; opacity: 0.6; }
  .rec-chart .rec-cursor { visibility: hidden; stroke: var(--rec-muted); stroke-width: 1; stroke-dasharray: 3 3; }
  .rec-tip {
    position: absolute; z-index: 5; pointer-events: none; display: none; max-width: 260px;
    padding: 7px 9px; border-radius: 8px; font-size: 12px; line-height: 1.4;
    background: var(--rec-surface); color: var(--rec-text);
    border: 1px solid var(--rec-line); box-shadow: 0 2px 8px rgba(0, 0, 0, 0.25);
  }
  .rec-tip b { font-weight: 600; }
  .rec-legend { display: flex; flex-wrap: wrap; gap: 4px 16px; font-size: 12px; margin-top: 6px; color: var(--rec-text); }
  .rec-legend span { display: inline-flex; align-items: center; gap: 6px; }
  .rec-legend .rec-note { color: var(--rec-muted); }
  .rec-swatch { display: inline-block; width: 18px; height: 0; border-top: 2.5px solid var(--sw, #888); }
  .rec-swatch.rec-dot { width: 10px; height: 10px; border-top: 0; border-radius: 50%; background: var(--sw); }
  .rec-swatch.rec-box { width: 12px; height: 10px; border-top: 0; background: var(--sw); border-radius: 2px; }
  .rec-empty { color: var(--rec-muted); font-size: 13px; padding: 18px 0; text-align: center; }
  .rec-note-line { font-size: 12px; color: var(--rec-muted); margin-top: 4px; }
  .rec-vdot {
    display: inline-flex; align-items: center; justify-content: center; flex: none;
    width: 18px; height: 18px; border-radius: 50%; font-size: 10px; font-weight: 700;
    background: var(--vc, #888); color: var(--vink, #fff);
  }
  /* summary chips */
  .rec-chips { display: flex; flex-wrap: wrap; gap: 8px; }
  .rec-chip {
    display: flex; align-items: center; gap: 8px; min-width: 0; padding: 8px 12px;
    border: 1px solid var(--rec-line); border-radius: 10px; font-size: 12px; color: var(--rec-muted);
  }
  .rec-chip.rec-vchip { border-left: 4px solid var(--vc, #888); }
  .rec-chip b { display: block; font-size: 18px; font-weight: 600; color: var(--rec-text); line-height: 1.2; }
  .rec-chip small { display: block; }
  button.rec-chip { font: inherit; font-size: 12px; background: transparent; cursor: pointer; text-align: left; color: var(--rec-muted); }
  button.rec-chip:hover { background: rgba(127, 127, 127, 0.1); }
  .rec-good { color: var(--rec-good); }
  .rec-bad { color: var(--rec-bad); }
  /* table */
  .rec-table { display: grid; font-size: 13px; }
  .rec-tr {
    display: grid; grid-template-columns: 1.2fr 1.2fr 0.8fr 0.9fr 0.8fr 0.7fr 0.8fr 0.6fr 0.8fr 0.8fr 0.8fr 0.8fr;
    gap: 0 8px; align-items: center; padding: 7px 4px; border-bottom: 1px solid var(--rec-line);
  }
  .rec-tr.rec-th { font-size: 11px; color: var(--rec-muted); padding: 4px; }
  .rec-th button { font: inherit; color: inherit; background: transparent; border: 0; padding: 0; cursor: pointer; text-align: left; display: inline-flex; gap: 3px; }
  .rec-th button[aria-pressed="true"] { color: var(--rec-text); font-weight: 600; }
  .rec-tr > span { min-width: 0; overflow-wrap: anywhere; }
  .rec-tr[data-action="open"] { cursor: pointer; }
  .rec-tr[data-action="open"]:hover, .rec-tr[data-action="open"]:focus-visible { background: rgba(127, 127, 127, 0.1); outline: 0; }
  .rec-veh { display: inline-flex; align-items: center; gap: 6px; }
  .rec-more { font: inherit; font-size: 12px; margin-top: 8px; padding: 5px 10px; border-radius: 8px; border: 1px solid var(--rec-line); background: transparent; color: var(--rec-text); cursor: pointer; }
  .rec-stacked .rec-section { padding: 12px; }
  .rec-stacked .rec-pair { display: block; border-bottom: 0; }
  .rec-stacked .rec-pair > .rec-section { border-bottom: 1px solid var(--rec-line); }
  .rec-stacked .rec-pair > .rec-section + .rec-section { border-left: 0; }
  .rec-stacked .rec-tr:not(.rec-th) { display: flex; flex-wrap: wrap; gap: 2px 12px; padding: 9px 4px; }
  .rec-stacked .rec-tr:not(.rec-th) > span { white-space: nowrap; }
  .rec-stacked .rec-tr:not(.rec-th) > span::before { content: attr(data-l) " "; font-size: 10.5px; color: var(--rec-muted); }
  .rec-stacked .rec-tr:not(.rec-th) > .rec-c-date { flex: 1 0 100%; font-weight: 600; }
  .rec-stacked .rec-tr:not(.rec-th) > .rec-c-date::before, .rec-stacked .rec-tr:not(.rec-th) > .rec-c-vehicle::before { content: none; }
  .rec-stacked .rec-tr.rec-th { display: flex; flex-wrap: wrap; gap: 4px 12px; }
  .rec-error { padding: 20px; color: var(--error-color, #db4437); }
`;

const BaseElement = typeof HTMLElement === "undefined" ? class {} : HTMLElement;

/**
 * "dark" | "light" for a computed CSS background color ("rgb(r, g, b)" /
 * "rgba(...)"), or null when it is transparent or unparseable. Native
 * dropdowns follow `color-scheme`, so a card sets it from its own background
 * (a dark custom theme may not set Home Assistant's dark-mode flag).
 */
export function schemeForColor(color) {
  const m = String(color || "").match(/rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:[,\s/]+([\d.]+))?/);
  if (!m || (m[4] !== undefined && Number(m[4]) === 0)) return null;
  const lum = (0.2126 * Number(m[1]) + 0.7152 * Number(m[2]) + 0.0722 * Number(m[3])) / 255;
  return lum < 0.5 ? "dark" : "light";
}

/**
 * Give a native <select> the color scheme of its own background, so the
 * browser draws its open list (frame, padding, scrollbar) to match. Run as the
 * list opens: the computed background is only reliable once it's on screen.
 */
function _themeSelect(select) {
  try {
    const scheme = schemeForColor(getComputedStyle(select).backgroundColor);
    if (scheme) select.style.colorScheme = scheme;
  } catch (_err) {
    // Not rendered: the browser default applies.
  }
}

class RivianEfficiencyCard extends BaseElement {
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

  /** Match native dropdowns to the card's real background (see `schemeForColor`). */
  _syncColorScheme(hass) {
    const key = `${hass && hass.themes ? hass.themes.theme : ""}|${!!(hass && hass.themes && hass.themes.darkMode)}`;
    if (key === this._schemeKey) return;
    this._schemeKey = key;
    requestAnimationFrame(() => {
      let scheme = null;
      try {
        scheme = schemeForColor(getComputedStyle(this._card).backgroundColor);
      } catch (_err) {
        // No layout yet: fall back to Home Assistant's flag.
      }
      this.style.colorScheme = scheme || (hass && hass.themes && hass.themes.darkMode ? "dark" : "light");
    });
  }

  set hass(hass) {
    const dark = !!(hass && hass.themes && hass.themes.darkMode);
    const themeChanged = this._hass && !!(this._hass.themes && this._hass.themes.darkMode) !== dark;
    this._hass = hass;
    if (!this._built) return;
    if (this._barEl) this._barEl.hass = hass;
    this._card.classList.toggle("rec-dark", dark);
    this._syncColorScheme(hass);
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
    this._range = "90d";
    this._xKey = "temp";
    this._period = "weekly";
    this._data = { drives: [], speed_bands: {}, trend: {} };
    this._sort = { key: "date", dir: "desc" };
    this._limit = PAGE;
    this._token = 0;

    this.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = _STYLE;
    this.shadowRoot.appendChild(style);
    this._card = document.createElement("ha-card");
    // Theme each native dropdown as it opens (see `_themeSelect`).
    const themeSelect = (ev) => {
      const t = ev.composedPath ? ev.composedPath()[0] : ev.target;
      if (t && t.tagName === "SELECT") _themeSelect(t);
    };
    this.shadowRoot.addEventListener("pointerdown", themeSelect, true);
    this.shadowRoot.addEventListener("focusin", themeSelect, true);
    this.shadowRoot.addEventListener("keydown", themeSelect, true);
    this.shadowRoot.appendChild(this._card);
    this._topbar = document.createElement("div");
    this._topbar.className = "rec-topbar";
    this._topbar.style.display = "none";
    this._card.appendChild(this._topbar);
    this._sections = {};
    // Speed bands and the trend share one row on a wide card (`.rec-pair`); stacked, they're one column.
    let pair = null;
    for (const id of ["summary", "scatter", "bands", "trend", "table"]) {
      const el = document.createElement("div");
      el.className = "rec-section";
      el.dataset.section = id;
      if (id === "bands" || id === "trend") {
        if (!pair) {
          pair = document.createElement("div");
          pair.className = "rec-pair";
          this._card.appendChild(pair);
        }
        pair.appendChild(el);
      } else {
        this._card.appendChild(el);
      }
      this._sections[id] = el;
    }
    this._card.addEventListener("click", (ev) => this._onClick(ev));
    this._card.addEventListener("change", (ev) => this._onChange(ev));
    this._card.addEventListener("keydown", (ev) => this._onKey(ev));
    this._card.addEventListener("pointermove", (ev) => this._onPointer(ev));
    this._card.addEventListener("pointerdown", (ev) => this._onPointer(ev));
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
      this._card.classList.toggle("rec-stacked", stacked);
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

  get _dark() {
    return !!(this._hass && this._hass.themes && this._hass.themes.darkMode);
  }

  get _tz() {
    return this._hass && this._hass.config ? this._hass.config.time_zone : undefined;
  }

  get _multi() {
    return this._vins.length > 1;
  }

  _showError(err) {
    console.error("rivian-efficiency-card:", err);
    const msg = err && err.message ? err.message : err && err.code ? err.code : String(err);
    this._sections.summary.innerHTML = `<div class="rec-error">Could not load efficiency data: ${esc(msg)}</div>`;
  }

  // -- scope ------------------------------------------------------------

  async _start() {
    try {
      this._bar = await _loadBarModule();
      this._vehicleList = await this._bar.getVehicles(this._hass);
    } catch (err) {
      console.warn("rivian-efficiency-card: vehicle list unavailable", err);
    }
    if (this._pinned) {
      this._vins = [...this._config.vins];
    } else {
      let selection = [];
      try {
        selection = this._bar ? await this._bar.getSelection(this._hass) : [];
      } catch (err) {
        console.warn("rivian-efficiency-card: vehicle selection unavailable", err);
      }
      this._vins = [...selection];
      if (this._bar && !this._unsubSelection) {
        this._unsubSelection = this._bar.onSelectionChange((vins) => this._onSelectionChanged(vins));
      }
      this._mountBar();
    }
    if (!this._vins.length) {
      this._sections.summary.innerHTML = '<div class="rec-empty">No vehicles to show.</div>';
      return;
    }
    this._subscribe();
    await this._refresh();
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
    this._limit = PAGE;
    this._subscribe();
    this._refresh().catch((err) => this._showError(err));
  }

  _subscribe() {
    if (this._unsubPromise || !this._hass || !this._vins.length) return;
    this._unsubPromise = this._hass.connection
      .subscribeMessage(
        () => {
          clearTimeout(this._refreshTimer);
          this._refreshTimer = setTimeout(() => this._refresh().catch((e) => this._showError(e)), 400);
        },
        { type: "rivian/analytics/subscribe", vins: [...this._vins] }
      )
      .catch((err) => {
        console.warn("rivian-efficiency-card: live updates unavailable", err);
        return null;
      });
  }

  _unsubscribe() {
    if (!this._unsubPromise) return;
    this._unsubPromise.then((unsub) => unsub && unsub()).catch(() => {});
    this._unsubPromise = null;
  }

  // -- data -------------------------------------------------------------

  async _refresh() {
    const token = ++this._token;
    const data = await this._hass.callWS({
      type: "rivian/analytics/efficiency",
      vins: [...this._vins],
      days: rangeDays(this._range),
    });
    if (token !== this._token) return;
    this._data = {
      drives: (data && data.drives) || [],
      speed_bands: (data && data.speed_bands) || {},
      trend: (data && data.trend) || {},
    };
    this._ready = true;
    this._renderAll();
  }

  _info(vin) {
    const v = this._vehicleList.find((x) => x.vin === vin) || null;
    const color = v ? (this._dark ? v.color_dark || v.color : v.color) || FALLBACK_COLOR : FALLBACK_COLOR;
    const ink = this._bar ? this._bar.inkOn(color) : "#ffffff";
    return { vin, name: (v && (v.name || v.model)) || "Rivian", letter: (v && v.letter) || "", color, ink };
  }

  _dotHtml(vin) {
    const i = this._info(vin);
    return `<span class="rec-vdot" style="--vc:${esc(i.color)};--vink:${esc(i.ink)}" title="${esc(i.name)}">${esc(i.letter)}</span>`;
  }

  // -- rendering ----------------------------------------------------------

  _renderAll() {
    if (!this._ready) return;
    this._renderSummary();
    this._renderScatter();
    this._renderBands();
    this._renderTrend();
    this._renderTable();
  }

  _chartWidth(el) {
    const w = el.clientWidth - (this._stacked ? 24 : 32);
    return Math.max(260, Math.round(w || 600));
  }

  _rangeToggle() {
    const buttons = RANGES.map(
      ([key]) => `<button type="button" data-action="range" data-range="${key}" aria-pressed="${key === this._range}" title="${esc(rangeTitle(key))}" aria-label="${esc(rangeTitle(key))}">${key === "all" ? "All" : key}</button>`
    ).join("");
    return `<div class="rec-seg" role="group" aria-label="Time range">${buttons}</div>`;
  }

  _renderSummary() {
    const el = this._sections.summary;
    const stats = summaryStats(this._data.drives, this._vins);
    let html = `<div class="rec-head"><h3 class="rec-title rec-page-title">Efficiency</h3>${this._rangeToggle()}</div>`;
    if (!usableDrives(this._data.drives).length) {
      el.innerHTML = html + '<div class="rec-empty">No drives with energy data in this range.</div>';
      return;
    }
    html += '<div class="rec-chips">';
    for (const vin of this._vins) {
      const s = stats.perVin[vin];
      const info = this._info(vin);
      html += `<div class="rec-chip rec-vchip" style="--vc:${esc(info.color)}" title="Average efficiency in miles per kilowatt-hour over ${s ? s.drives : 0} drives (higher is better)"><div><small>${this._multi ? `${esc(info.name)} · ` : ""}average</small><b>${s && s.eff !== null ? s.eff.toFixed(2) : "–"} <small style="display:inline">mi/kWh</small></b><small>${s ? s.drives : 0} drives · ${s ? Math.round(s.miles) : 0} mi</small></div></div>`;
    }
    if (stats.avgScore !== null) {
      const cls = scoreClass(stats.avgScore);
      html += `<div class="rec-chip" title="${esc(SCORE_TITLE)}"><div><small>Average score</small><b class="${cls === "good" ? "rec-good" : cls === "bad" ? "rec-bad" : ""}">${esc(scoreGlyph(stats.avgScore))} ${esc(formatScore(stats.avgScore))}</b><small>actual vs expected, ${stats.scored} drives</small></div></div>`;
    }
    const tz = this._tz;
    for (const [label, d] of [
      ["Best conditions", stats.best],
      ["Worst conditions", stats.worst],
    ]) {
      if (!d) continue;
      html += `<button type="button" class="rec-chip" data-action="open" data-key="${esc(driveKey(d))}" title="Open this drive on the Drives tab. Expected = the efficiency the model predicts for this drive's weather, terrain and speed; scored = actual vs expected energy."><div><small>${label}</small><b>${esc(_num(d.expected_eff_mi_kwh, 2))} <small style="display:inline">mi/kWh expected</small></b><small>${esc(formatDayYear(d.date_ts, tz))} · ${esc(_num(d.distance_mi, 1))} mi · scored ${esc(formatScore(d.score))}${this._multi ? ` · ${esc(this._info(d.vin).name)}` : ""}</small></div></button>`;
    }
    el.innerHTML = html + "</div>";
  }

  _renderScatter() {
    const el = this._sections.scatter;
    const opt = xOption(this._xKey);
    const select = `<select class="rec-select" data-action="xkey" aria-label="X axis" title="What to plot efficiency against">${X_OPTIONS.map((o) => `<option value="${o.key}"${o.key === this._xKey ? " selected" : ""}>${esc(o.label)}</option>`).join("")}</select>`;
    let html = `<div class="rec-head"><h3 class="rec-title">Efficiency vs ${esc(opt.label.toLowerCase())}</h3>${select}</div>`;
    const { points, missing } = scatterPoints(this._data.drives, this._xKey);
    if (!points.length) {
      el.innerHTML = html + `<div class="rec-empty">No drives with ${esc(opt.label.toLowerCase())} data in this range.</div>${missing ? `<div class="rec-note-line">${missing} drives without ${esc(opt.label.toLowerCase())} data</div>` : ""}`;
      this._sc = null;
      return;
    }
    const byVin = pointsByVin(points, this._vins);
    const regs = {};
    const trends = [];
    for (const vin of this._vins) {
      regs[vin] = linearRegression(byVin[vin]);
      trends.push(trendLineEnds(regs[vin]));
    }
    const dom = scatterDomains(points, trends);
    const W = this._chartWidth(el);
    const H = this._stacked ? 260 : 340;
    const g = chartLayout(W, H, { left: 40, right: 10, top: 10, bottom: 38 });
    const xs = scaleLinear(dom.x[0], dom.x[1], g.x0, g.x1);
    const ys = scaleLinear(dom.y[0], dom.y[1], g.y1, g.y0);
    const maxDist = Math.max(...points.map((p) => p.drive.distance_mi || 0), 1);
    const parts = [];
    for (const v of niceTicks(dom.y[0], dom.y[1], 5)) {
      parts.push(`<line class="rec-grid" x1="${g.x0}" x2="${g.x1}" y1="${ys(v).toFixed(1)}" y2="${ys(v).toFixed(1)}"/>`);
      parts.push(`<text x="${g.x0 - 5}" y="${(ys(v) + 3.5).toFixed(1)}" text-anchor="end">${+v.toFixed(2)}</text>`);
    }
    for (const v of niceTicks(dom.x[0], dom.x[1], this._stacked ? 5 : 8)) {
      parts.push(`<text x="${xs(v).toFixed(1)}" y="${g.y1 + 14}" text-anchor="middle">${esc(+v.toFixed(3))}</text>`);
    }
    if (dom.x[0] < 0 && dom.x[1] > 0 && opt.key === "headwind") {
      parts.push(`<line class="rec-axis" x1="${xs(0).toFixed(1)}" x2="${xs(0).toFixed(1)}" y1="${g.y0}" y2="${g.y1}" stroke-dasharray="3 3"/>`);
    }
    parts.push(`<line class="rec-axis" x1="${g.x0}" x2="${g.x1}" y1="${g.y1}" y2="${g.y1}"/>`);
    parts.push(`<text x="${(g.x0 + g.x1) / 2}" y="${g.y1 + 30}" text-anchor="middle">${esc(opt.label)} (${esc(opt.unit)})${opt.note ? ` · ${esc(opt.note)}` : ""}</text>`);
    parts.push(`<text x="10" y="${(g.y0 + g.y1) / 2}" text-anchor="middle" transform="rotate(-90 10 ${(g.y0 + g.y1) / 2})">mi/kWh</text>`);
    // Points first (larger first so small ones stay visible), then trend lines on top.
    for (const vin of this._vins) {
      const color = this._info(vin).color;
      const pts = byVin[vin].slice().sort((a, b) => (b.drive.distance_mi || 0) - (a.drive.distance_mi || 0));
      for (const p of pts) {
        parts.push(
          `<circle cx="${xs(p.x).toFixed(1)}" cy="${ys(p.y).toFixed(1)}" r="${pointRadius(p.drive.distance_mi, maxDist).toFixed(1)}" fill="${esc(color)}" fill-opacity="0.72" stroke="var(--rec-surface)" stroke-width="1.5"/>`
        );
      }
    }
    for (const vin of this._vins) {
      const ends = trendLineEnds(regs[vin]);
      if (!ends) continue;
      const color = this._info(vin).color;
      parts.push(`<path d="${linePath(ends, xs, ys)}" fill="none" stroke="var(--rec-surface)" stroke-width="4.5" stroke-linecap="round"/>`);
      const reg = regs[vin];
      const trendTitle = `${this._multi ? `${this._info(vin).name} trend: ` : "Trend: "}${reg.slope < 0 ? "\u2212" : "+"}${Math.abs(reg.slope).toFixed(Math.abs(reg.slope) < 0.01 ? 4 : 3)} mi/kWh per ${opt.unit} (R\u00b2 ${reg.r2.toFixed(2)}; R\u00b2 is how much of the variation the line explains)`;
      parts.push(`<path d="${linePath(ends, xs, ys)}" fill="none" stroke="${esc(color)}" stroke-width="2.2" stroke-linecap="round"><title>${esc(trendTitle)}</title></path>`);
    }
    parts.push('<circle data-hover r="0" fill="none" stroke="var(--rec-text)" stroke-width="2"/>');
    const legend = [];
    if (this._multi) {
      for (const vin of this._vins) {
        legend.push(`<span><i class="rec-swatch rec-dot" style="--sw:${esc(this._info(vin).color)}"></i>${esc(this._info(vin).name)}</span>`);
      }
    }
    legend.push('<span class="rec-note">Point size = trip distance. Line = trend (5+ drives). Hover, tap, or focus the chart and use the arrow keys for each drive.</span>');
    const trendTexts = this._vins
      .filter((vin) => regs[vin])
      .map((vin) => {
        const r = regs[vin];
        const sign = r.slope < 0 ? "−" : "+";
        return `${this._multi ? `${esc(this._info(vin).name)}: ` : ""}${sign}${Math.abs(r.slope).toFixed(Math.abs(r.slope) < 0.01 ? 4 : 3)} mi/kWh per ${esc(opt.unit)} (R² ${r.r2.toFixed(2)})`;
      });
    html +=
      `<div class="rec-chart" data-chart="scatter" tabindex="0" role="group" aria-label="Efficiency versus ${esc(opt.label)}: hover, tap, or use the arrow keys to read each drive"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Efficiency versus ${esc(opt.label)}">${parts.join("")}</svg><div class="rec-tip"></div></div>` +
      `<div class="rec-legend">${legend.join("")}</div>` +
      (trendTexts.length ? `<div class="rec-note-line">${trendTexts.join(" · ")}</div>` : "") +
      (missing ? `<div class="rec-note-line">${missing} drive${missing === 1 ? "" : "s"} without ${esc(opt.label.toLowerCase())} data</div>` : "");
    el.innerHTML = html;
    this._sc = { g, xs, ys, points };
  }

  _renderBands() {
    const el = this._sections.bands;
    const { bands, series } = groupBands(this._data.speed_bands, this._vins);
    let html = '<div class="rec-head"><h3 class="rec-title">Efficiency by speed</h3></div>';
    if (!bands.length) {
      el.innerHTML = html + '<div class="rec-empty">No speed-band data in this range.</div>';
      this._bd = null;
      return;
    }
    let maxEff = 0;
    for (const vin of this._vins) for (const b of bands) if (series[vin][b]) maxEff = Math.max(maxEff, series[vin][b].efficiency);
    const [, yHi] = niceDomain(0, maxEff * 1.08, 5);
    const W = this._chartWidth(el);
    const H = this._stacked ? 230 : 290;
    const g = chartLayout(W, H, { left: 40, right: 10, top: 14, bottom: 38 });
    const ys = scaleLinear(0, yHi, g.y1, g.y0);
    const slot = g.w / bands.length;
    const n = Math.max(1, this._vins.length);
    const inner = Math.min(slot * 0.78, 40 * n);
    const barW = Math.max(6, (inner - (n - 1) * 2) / n);
    const parts = [];
    for (const v of niceTicks(0, yHi, 5)) {
      parts.push(`<line class="rec-grid" x1="${g.x0}" x2="${g.x1}" y1="${ys(v).toFixed(1)}" y2="${ys(v).toFixed(1)}"/>`);
      parts.push(`<text x="${g.x0 - 5}" y="${(ys(v) + 3.5).toFixed(1)}" text-anchor="end">${+v.toFixed(2)}</text>`);
    }
    parts.push(`<line class="rec-axis" x1="${g.x0}" x2="${g.x1}" y1="${g.y1}" y2="${g.y1}"/>`);
    const hits = [];
    bands.forEach((band, i) => {
      const cx = g.x0 + slot * (i + 0.5);
      const left = cx - (n * barW + (n - 1) * 2) / 2;
      parts.push(`<text x="${cx.toFixed(1)}" y="${g.y1 + 14}" text-anchor="middle">${esc(bandLabel(band))}</text>`);
      this._vins.forEach((vin, j) => {
        const row = series[vin][band];
        if (!row) return;
        const x = left + j * (barW + 2);
        const y = ys(row.efficiency);
        parts.push(`<path d="${roundedTopBar(x, y, barW, g.y1 - y, 4)}" fill="${esc(this._info(vin).color)}"/>`);
        if (barW >= 20 || n === 1) {
          parts.push(`<text class="rec-vlabel" x="${(x + barW / 2).toFixed(1)}" y="${(y - 4).toFixed(1)}" text-anchor="middle">${row.efficiency.toFixed(2)}</text>`);
        }
        hits.push({ x, w: barW, vin, band, row });
      });
    });
    parts.push(`<text x="${(g.x0 + g.x1) / 2}" y="${g.y1 + 30}" text-anchor="middle">Speed (mph)</text>`);
    parts.push(`<text x="10" y="${(g.y0 + g.y1) / 2}" text-anchor="middle" transform="rotate(-90 10 ${(g.y0 + g.y1) / 2})">mi/kWh</text>`);
    const legend = this._multi
      ? this._vins.map((vin) => `<span><i class="rec-swatch rec-box" style="--sw:${esc(this._info(vin).color)}"></i>${esc(this._info(vin).name)}</span>`).join("")
      : "";
    el.innerHTML =
      html +
      `<div class="rec-chart" data-chart="bands" tabindex="0" role="group" aria-label="Efficiency by speed band: hover, tap, or use the arrow keys to read each bar"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Efficiency by speed band">${parts.join("")}</svg><div class="rec-tip"></div></div>` +
      (legend ? `<div class="rec-legend">${legend}</div>` : "") +
      `<div class="rec-note-line">From each drive's 3-minute chunks, weighted by distance. Hover, tap, or focus the chart and use the arrow keys to see miles driven.</div>`;
    this._bd = { g, hits, slot };
  }

  _renderTrend() {
    const el = this._sections.trend;
    const tz = this._tz;
    const toggle = `<div class="rec-seg" role="group" aria-label="Period">${[
      ["weekly", "Weekly"],
      ["monthly", "Monthly"],
    ]
      .map(([k, l]) => `<button type="button" data-action="period" data-period="${k}" aria-pressed="${k === this._period}" title="${k === "weekly" ? "One point per week" : "One point per month"}">${l}</button>`)
      .join("")}</div>`;
    let html = `<div class="rec-head"><h3 class="rec-title">Trend over time</h3>${toggle}</div>`;
    const shape = trendShape(this._data.trend, this._vins, this._period);
    if (shape.tMin === null) {
      el.innerHTML = html + '<div class="rec-empty">No trend data in this range.</div>';
      this._tr = null;
      return;
    }
    let effMin = Infinity;
    let effMax = -Infinity;
    let milesMax = 0;
    for (const rows of Object.values(shape.series)) {
      for (const r of rows) {
        effMin = Math.min(effMin, r.eff);
        effMax = Math.max(effMax, r.eff);
        milesMax = Math.max(milesMax, r.miles);
      }
    }
    const [yLo, yHi] = niceDomain(Math.max(0, effMin - (effMax - effMin) * 0.15 - 0.2), effMax + (effMax - effMin) * 0.15 + 0.2, 5);
    const W = this._chartWidth(el);
    const H = this._stacked ? 250 : 290;
    const g = chartLayout(W, H, { left: 40, right: 10, top: 10, bottom: 26 });
    const xs = scaleLinear(shape.tMin, shape.tMax, g.x0, g.x1);
    const ys = scaleLinear(yLo, yHi, g.y1, g.y0);
    const barZone = g.h * 0.28;
    const bs = scaleLinear(0, Math.max(milesMax, 1), 0, barZone);
    const n = Math.max(1, this._vins.length);
    const periodPx = xs(shape.tMin + shape.span) - xs(shape.tMin);
    const parts = [];
    for (const v of niceTicks(yLo, yHi, 5)) {
      parts.push(`<line class="rec-grid" x1="${g.x0}" x2="${g.x1}" y1="${ys(v).toFixed(1)}" y2="${ys(v).toFixed(1)}"/>`);
      parts.push(`<text x="${g.x0 - 5}" y="${(ys(v) + 3.5).toFixed(1)}" text-anchor="end">${+v.toFixed(2)}</text>`);
    }
    parts.push(`<line class="rec-axis" x1="${g.x0}" x2="${g.x1}" y1="${g.y1}" y2="${g.y1}"/>`);
    // Miles bars (faint, one per vehicle per period) sit under the lines.
    const slotW = Math.max(2, periodPx * 0.8);
    const barW = Math.max(2, (slotW - (n - 1)) / n);
    this._vins.forEach((vin, j) => {
      const color = this._info(vin).color;
      for (const r of shape.series[vin]) {
        const h = bs(r.miles);
        if (h <= 0) continue;
        const x = xs(r.ts) + (periodPx - slotW) / 2 + j * (barW + 1);
        parts.push(`<rect x="${x.toFixed(1)}" y="${(g.y1 - h).toFixed(1)}" width="${barW.toFixed(1)}" height="${h.toFixed(1)}" fill="${esc(color)}" fill-opacity="0.22"/>`);
      }
    });
    // X labels: one per few periods.
    const allTs = [...new Set(Object.values(shape.series).flatMap((rows) => rows.map((r) => r.ts)))].sort((a, b) => a - b);
    const every = Math.max(1, Math.ceil(allTs.length / (this._stacked ? 5 : 6)));
    allTs.forEach((ts, i) => {
      if (i % every !== 0) return;
      const x = xs(ts) + periodPx / 2;
      const label = this._period === "monthly" ? formatMonth(ts, tz) : formatDay(ts, tz);
      parts.push(`<text x="${x.toFixed(1)}" y="${g.y1 + 15}" text-anchor="middle">${esc(label)}</text>`);
    });
    for (const vin of this._vins) {
      const rows = shape.series[vin];
      if (!rows.length) continue;
      const color = this._info(vin).color;
      const pts = rows.map((r) => [r.ts + shape.span / 2, r.eff]);
      if (pts.length > 1) parts.push(`<path d="${linePath(pts, xs, ys)}" fill="none" stroke="${esc(color)}" stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round"/>`);
      for (const p of pts) {
        parts.push(`<circle cx="${xs(p[0]).toFixed(1)}" cy="${ys(p[1]).toFixed(1)}" r="3.2" fill="${esc(color)}" stroke="var(--rec-surface)" stroke-width="1.5"/>`);
      }
    }
    parts.push(`<line class="rec-cursor" data-cursor y1="${g.y0}" y2="${g.y1}" x1="-10" x2="-10"/>`);
    const legend = [];
    if (this._multi) {
      for (const vin of this._vins) legend.push(`<span><i class="rec-swatch" style="--sw:${esc(this._info(vin).color)}"></i>${esc(this._info(vin).name)}</span>`);
    }
    legend.push('<span class="rec-note">Line: mi/kWh · faint bars: miles per period. Hover, tap, or use the arrow keys for each period.</span>');
    html +=
      `<div class="rec-chart" data-chart="trend" tabindex="0" role="group" aria-label="Efficiency trend over time: hover, tap, or use the arrow keys to read each period"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Efficiency trend over time">${parts.join("")}</svg><div class="rec-tip"></div></div>` +
      `<div class="rec-legend">${legend.join("")}</div>`;
    el.innerHTML = html;
    this._tr = { g, xs, shape, periodPx, allTs };
  }

  _renderTable() {
    const el = this._sections.table;
    const tz = this._tz;
    const nameOf = (vin) => this._info(vin).name;
    const all = usableDrives(this._data.drives);
    let head = `<div class="rec-head"><h3 class="rec-title">Drives</h3><span class="rec-note-line" style="margin:0">${all.length} in range · tap a row to open it on the Drives tab</span></div>`;
    if (!all.length) {
      el.innerHTML = head + '<div class="rec-empty">No drives to show.</div>';
      return;
    }
    const rows = sortDrives(all, this._sort.key, this._sort.dir, nameOf);
    let html = '<div class="rec-table" role="table"><div class="rec-tr rec-th" role="row">';
    for (const [key, label] of TABLE_COLUMNS) {
      const on = this._sort.key === key;
      const arrow = on ? (this._sort.dir === "asc" ? "▲" : "▼") : "";
      html += `<span role="columnheader" aria-sort="${ariaSortFor(this._sort, key)}"><button type="button" data-action="sort" data-key="${key}" aria-pressed="${on}" title="${esc(COLUMN_TITLES[key] || label)}. Click to sort.">${label} ${arrow}</button></span>`;
    }
    html += "</div>";
    for (const d of rows.slice(0, this._limit)) {
      const cls = scoreClass(d.score);
      const scoreHtml = `<span class="rec-c-score ${cls === "good" ? "rec-good" : cls === "bad" ? "rec-bad" : ""}" data-l="Score" title="${esc(SCORE_TITLE)}">${esc(scoreGlyph(d.score))} ${esc(formatScore(d.score))}</span>`;
      html +=
        `<div class="rec-tr" role="row" tabindex="0" data-action="open" data-key="${esc(driveKey(d))}" title="Open this drive on the Drives tab">` +
        `<span class="rec-c-date">${esc(formatWhen(d.date_ts, tz))}</span>` +
        `<span class="rec-c-vehicle"><span class="rec-veh">${this._dotHtml(d.vin)}${esc(nameOf(d.vin))}</span></span>` +
        `<span data-l="Dist">${esc(_num(d.distance_mi, 1))} mi</span>` +
        `<span data-l="Time">${esc(formatDuration(d.duration_s))}</span>` +
        `<span data-l="Avg">${esc(_num(d.avg_speed_mph, 0))} mph</span>` +
        `<span data-l="Temp">${_finite(d.temp_f) ? `${esc(_num(d.temp_f, 0))} °F` : "–"}</span>` +
        `<span data-l="Wind" title="${esc(COLUMN_TITLES.headwind)}">${_finite(d.headwind_mph) ? esc(formatX("headwind", d.headwind_mph)) : "–"}</span>` +
        `<span data-l="Rain">${_finite(d.precip_mm) ? `${esc(_num(d.precip_mm, 1))} mm` : "–"}</span>` +
        `<span data-l="Climb" title="${esc(COLUMN_TITLES.climb)}">${_finite(d.climb_ft_per_mi) ? `${esc(_num(d.climb_ft_per_mi, 0))} ft/mi` : "–"}</span>` +
        `<span data-l="mi/kWh"><b>${esc(_num(d.efficiency_mi_kwh, 2))}</b></span>` +
        `<span data-l="Expected" title="${esc(COLUMN_TITLES.expected)}">${esc(_num(d.expected_eff_mi_kwh, 2))}</span>` +
        `${scoreHtml}</div>`;
    }
    html += "</div>";
    if (rows.length > this._limit) {
      html += `<button type="button" class="rec-more" data-action="more">Show ${Math.min(PAGE, rows.length - this._limit)} more (${rows.length - this._limit} left)</button>`;
    }
    el.innerHTML = head + html;
  }

  // -- events -------------------------------------------------------------

  _driveByKey(key) {
    return this._data.drives.find((d) => driveKey(d) === key) || null;
  }

  _onClick(ev) {
    const target = ev.target && ev.target.closest ? ev.target.closest("[data-action]") : null;
    if (!target) return;
    const action = target.dataset.action;
    if (action === "range") {
      if (target.dataset.range === this._range) return;
      this._range = target.dataset.range;
      this._limit = PAGE;
      this._renderSummary();
      this._refresh().catch((e) => this._showError(e));
    } else if (action === "period") {
      this._period = target.dataset.period;
      this._renderTrend();
    } else if (action === "sort") {
      const key = target.dataset.key;
      this._sort = this._sort.key === key ? { key, dir: this._sort.dir === "asc" ? "desc" : "asc" } : { key, dir: defaultDir(key) };
      this._limit = PAGE;
      this._renderTable();
    } else if (action === "more") {
      this._limit += PAGE;
      this._renderTable();
    } else if (action === "open") {
      const drive = this._driveByKey(target.dataset.key);
      if (drive) this._openDrive(drive);
    }
  }

  _onKey(ev) {
    const chart = ev.target && ev.target.classList && ev.target.classList.contains("rec-chart") ? ev.target : null;
    if (chart && (ev.key === "ArrowLeft" || ev.key === "ArrowRight" || ev.key === "Escape")) {
      ev.preventDefault();
      if (ev.key === "Escape") {
        chart.dataset.kidx = "-1";
        this._hideTip(chart);
        return;
      }
      this._stepChart(chart, ev.key === "ArrowRight" ? 1 : -1);
      return;
    }
    if (ev.key !== "Enter" && ev.key !== " ") return;
    const row = ev.target && ev.target.closest ? ev.target.closest('.rec-tr[data-action="open"]') : null;
    if (!row) return;
    ev.preventDefault();
    const drive = this._driveByKey(row.dataset.key);
    if (drive) this._openDrive(drive);
  }

  _onChange(ev) {
    const t = ev.target;
    if (!t || !t.dataset || t.dataset.action !== "xkey") return;
    this._xKey = t.value;
    this._renderScatter();
  }

  /** Persist the explorer's selection for this drive, then navigate to the Drives tab. */
  _openDrive(drive) {
    const nav = navigationSelection(drive, this._vins, this._tz);
    try {
      if (nav) {
        window.localStorage.setItem(nav.storageKey, JSON.stringify(nav.selection));
        // The Drives tab may already be loaded (HA keeps a visited view), and then it
        // never rereads its stored selection: it consumes this request when shown.
        window.localStorage.setItem(OPEN_REQUEST_KEY, JSON.stringify({ selection: nav.selection, ts: Date.now() }));
      }
    } catch (_err) {
      // The Drives tab just opens on its default selection.
    }
    const path = drivesPath(window.location.pathname, this._config && this._config.drives_path);
    history.pushState(null, "", path);
    window.dispatchEvent(new CustomEvent("location-changed", { detail: { replace: false } }));
  }

  _hideTip(wrap) {
    const tip = wrap.querySelector(".rec-tip");
    if (tip) tip.style.display = "none";
  }

  _showTip(wrap, html, px, py) {
    const tip = wrap.querySelector(".rec-tip");
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

  _onLeave(ev) {
    if (ev.pointerType === "touch") return;
    const wrap = ev.target && ev.target.closest ? ev.target.closest(".rec-chart") : null;
    if (!wrap) return;
    this._hideTip(wrap);
    const cur = wrap.querySelector("[data-cursor]");
    if (cur) {
      cur.style.visibility = "hidden";
      cur.setAttribute("x1", -10);
      cur.setAttribute("x2", -10);
    }
    const ring = wrap.querySelector("[data-hover]");
    if (ring) ring.setAttribute("r", 0);
  }

  /** The chart's keyboard stops in SVG coordinates, left to right. */
  _chartStops(kind) {
    if (kind === "scatter" && this._sc) {
      const { xs, ys, points } = this._sc;
      return points.map((p) => ({ px: xs(p.x), py: ys(p.y) })).sort((a, b) => a.px - b.px);
    }
    if (kind === "bands" && this._bd) {
      const { g, hits } = this._bd;
      return hits.map((h) => ({ px: h.x + h.w / 2, py: (g.y0 + g.y1) / 2 })).sort((a, b) => a.px - b.px);
    }
    if (kind === "trend" && this._tr) {
      const { g, xs, periodPx, allTs } = this._tr;
      return allTs.map((ts) => ({ px: xs(ts) + periodPx / 2, py: g.y0 + 10 }));
    }
    return [];
  }

  /** Arrow-key stepping: shows the same readout a hover or tap would. */
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

  _onPointer(ev) {
    const wrap = ev.target && ev.target.closest ? ev.target.closest(".rec-chart") : null;
    if (!wrap) return;
    const svg = wrap.querySelector("svg");
    const rect = svg.getBoundingClientRect();
    const vb = svg.viewBox.baseVal;
    const scale = vb.width / (rect.width || 1);
    const px = (ev.clientX - rect.left) * scale;
    const py = (ev.clientY - rect.top) * scale;
    const local = [ev.clientX - rect.left, ev.clientY - rect.top];
    this._probeChart(wrap, px, py, local);
  }

  /** Show the readout for the chart position (px, py in SVG units; `local` in CSS pixels). */
  _probeChart(wrap, px, py, local) {
    const kind = wrap.dataset.chart;
    const tz = this._tz;
    const tipHtml = (lines) => lines.map((l, i) => (i === 0 ? `<b>${esc(l)}</b>` : esc(l))).join("<br>");
    if (kind === "scatter" && this._sc) {
      const { g, xs, ys, points } = this._sc;
      const ring = wrap.querySelector("[data-hover]");
      const hit = nearestPoint(points, xs, ys, px, py, this._stacked ? 20 : 14);
      if (!hit || px < g.x0 - 8 || px > g.x1 + 8) {
        ring.setAttribute("r", 0);
        return this._hideTip(wrap);
      }
      ring.setAttribute("cx", xs(hit.x));
      ring.setAttribute("cy", ys(hit.y));
      ring.setAttribute("r", pointRadius(hit.drive.distance_mi, Math.max(...points.map((p) => p.drive.distance_mi || 0), 1)) + 2);
      this._showTip(wrap, tipHtml(driveTooltipLines(hit.drive, this._xKey, this._info(hit.drive.vin).name, tz)), local[0], local[1]);
    } else if (kind === "bands" && this._bd) {
      const { g, hits } = this._bd;
      const hit = hits.find((h) => px >= h.x - 1 && px <= h.x + h.w + 1);
      if (!hit || py < g.y0 || py > g.y1) return this._hideTip(wrap);
      const lines = [
        `${this._multi ? `${this._info(hit.vin).name} · ` : ""}${bandLabel(hit.band)} mph`,
        `${hit.row.efficiency.toFixed(2)} mi/kWh · ${(hit.row.efficiency * 33.705).toFixed(0)} MPGe`,
        `${_num(hit.row.miles, 0)} mi · ${_num(hit.row.kwh, 1)} kWh`,
      ];
      this._showTip(wrap, tipHtml(lines), local[0], local[1]);
    } else if (kind === "trend" && this._tr) {
      const { g, xs, shape, periodPx, allTs } = this._tr;
      if (px < g.x0 || px > g.x1) return this._hideTip(wrap);
      let best = null;
      for (const ts of allTs) {
        const d = Math.abs(xs(ts) + periodPx / 2 - px);
        if (best === null || d < best.d) best = { d, ts };
      }
      if (!best) return;
      const cx = xs(best.ts) + periodPx / 2;
      const cur = wrap.querySelector("[data-cursor]");
      cur.style.visibility = "visible";
      cur.setAttribute("x1", cx);
      cur.setAttribute("x2", cx);
      const title = this._period === "monthly" ? formatMonth(best.ts, tz) : `Week of ${formatDay(best.ts, tz)}`;
      const lines = [`<b>${esc(title)}</b>`];
      for (const vin of this._vins) {
        const r = shape.series[vin].find((x) => x.ts === best.ts);
        if (!r) continue;
        lines.push(`${this._multi ? `${esc(this._info(vin).name)}: ` : ""}<b>${r.eff.toFixed(2)}</b> mi/kWh · ${_num(r.mpge, 0)} MPGe · ${_num(r.miles, 0)} mi`);
      }
      this._showTip(wrap, lines.join("<br>"), local[0], local[1]);
    }
  }
}

if (typeof customElements !== "undefined" && !customElements.get("rivian-efficiency-card")) {
  customElements.define("rivian-efficiency-card", RivianEfficiencyCard);
  if (typeof window !== "undefined") {
    window.customCards = window.customCards || [];
    window.customCards.push({
      type: "rivian-efficiency-card",
      name: "Rivian Efficiency",
      description: "Efficiency versus conditions, by speed band, over time, and a drive-by-drive table with expected-vs-actual scores.",
    });
  }
}
