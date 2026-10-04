/**
 * rivian-routes-card.js
 *
 * The "Routes" tab card: favorite drives -- repeated start->end place pairs
 * ("routes"), each compared like a Strava segment. A list pane (sorted by
 * drive count) sits beside a Leaflet map showing every drive of the selected
 * route (thin gray, fastest green, slowest red, the selected drive blue on
 * top), a stats overlay panel, a dot chart of elapsed time over date, and a
 * sortable table of the route's drives.
 *
 * Routes belong to the household, not to a vehicle: one route can be driven
 * by several cars. The card shows the routes of the vehicles selected in the
 * shared vehicle bar, ordered by *their* drive counts (each car's favorites
 * are the routes it drives most); with several cars every drive is drawn and
 * colored in its own vehicle's color and the stats panel adds per-vehicle
 * best/average rows. A mixed real + demo selection queries each dataset
 * ('real' / 'demo') separately and concatenates the results.
 *
 * Backend calls (all via `hass.callWS`):
 *   - `rivian/routes/list {dataset, vins}` -- open to all users.
 *   - `rivian/routes/route {dataset, vins, route_id}` -- open to all users;
 *     one route's stats, every drive's summary (tagged with its `vin` and
 *     `key` = "vin|drive_id"), and each drive's preview polyline (<=150 points).
 *   - `rivian/routes/rename {dataset, route_id}` -- admin only (enforced
 *     server-side; this card also hides the control from non-admins via
 *     `hass.user.is_admin`).
 *   - `rivian/analytics/subscribe` -- refreshes the list/detail on live updates.
 *
 * A number of pure helpers are exported purely so a Node smoke test
 * (tests/frontend/routes_card.test.mjs) can import and exercise them without
 * a DOM or customElements environment. The module guards every top-level use
 * of HTMLElement/customElements/window/document so it can be imported under
 * plain Node, mirroring rivian-places-card.js/rivian-drive-explorer-card.js.
 */

const STACK_BREAKPOINT_PX = 700;

/** "14:02" (< 1h) or "1:14:02" (>= 1h); "--:--" for null/undefined/NaN. */
export function formatDuration(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return "--:--";
  const total = Math.max(0, Math.round(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const mm = h > 0 ? String(m).padStart(2, "0") : String(m);
  const ss = String(s).padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

/** Signed duration delta, e.g. "+1:12" / "-0:40" / "+0:00"; "--" for null. */
export function formatDelta(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return "--";
  const sign = seconds < 0 ? "-" : "+";
  return `${sign}${formatDuration(Math.abs(seconds))}`;
}

/** "+8%" / "-5%" / "--" for a vs-average percentage. */
export function formatVsAvgPct(pct) {
  if (pct === null || pct === undefined || Number.isNaN(pct)) return "--";
  const sign = pct > 0 ? "+" : "";
  return `${sign}${Math.round(pct)}%`;
}

/** Color token for a vs-average value: faster (negative) is green, slower is red. */
export function colorForVsAvg(pct) {
  if (pct === null || pct === undefined || Number.isNaN(pct)) return "var(--secondary-text-color)";
  if (pct < -0.5) return "var(--success-color, #2e7d32)";
  if (pct > 0.5) return "var(--error-color, #b00020)";
  return "var(--secondary-text-color)";
}

/** Short date label, e.g. "Jan 5" (or "Jan 5, 2025" with a year outside `nowTs`'s year). */
export function formatShortDate(ts, nowTs = Date.now() / 1000) {
  if (ts === null || ts === undefined || Number.isNaN(ts)) return "--";
  const d = new Date(ts * 1000);
  const now = new Date(nowTs * 1000);
  const opts = { month: "short", day: "numeric" };
  if (d.getFullYear() !== now.getFullYear()) opts.year = "numeric";
  return d.toLocaleDateString(undefined, opts);
}

/** A route drive's identity across vehicles: its `key` ("vin|drive_id"), else the bare drive id. */
export function driveKey(drive) {
  return drive ? drive.key || drive.drive_id : null;
}

/**
 * The raw `rivian/routes/list` result sorted for the list pane: by the
 * selected vehicles' own drive count on each route (`selected_count`, the
 * total when absent), then total drives, then id for stability.
 */
export function sortRoutes(routes) {
  const list = Array.isArray(routes) ? routes : [];
  const mine = (r) => (r.selected_count !== undefined ? r.selected_count : r.drive_count) || 0;
  return [...list].sort(
    (a, b) => mine(b) - mine(a) || (b.drive_count || 0) - (a.drive_count || 0) || (a.id || 0) - (b.id || 0)
  );
}

/**
 * The datasets a vehicle selection spans, as `[{dataset, vins}]` (real
 * first): real and demo vehicles' routes never mix, so each is queried
 * separately.
 */
export function datasetGroups(vehicles, vins) {
  const list = Array.isArray(vehicles) ? vehicles : [];
  const real = [];
  const demo = [];
  for (const vin of Array.isArray(vins) ? vins : []) {
    const vehicle = list.find((v) => v.vin === vin);
    if (!vehicle) continue;
    (vehicle.is_demo ? demo : real).push(vin);
  }
  const groups = [];
  if (real.length) groups.push({ dataset: "real", vins: real });
  if (demo.length) groups.push({ dataset: "demo", vins: demo });
  return groups;
}

function _vehicleParts(vehicle) {
  return {
    letter: (vehicle && vehicle.letter) || "",
    color: (vehicle && vehicle.color) || "#8a8a8a",
  };
}

/**
 * Each selected vehicle's drive count on a route: `[{vin, letter, color,
 * count}]`, most drives first (from the route's `stats.by_vin`).
 */
export function routeVehicleCounts(route, vehicles, vins) {
  const by = (route && route.stats && route.stats.by_vin) || {};
  const list = Array.isArray(vehicles) ? vehicles : [];
  const wanted = Array.isArray(vins) ? new Set(vins) : null;
  const out = [];
  for (const [vin, s] of Object.entries(by)) {
    if (!s || !s.count || (wanted && !wanted.has(vin))) continue;
    out.push({ vin, count: s.count, ..._vehicleParts(list.find((v) => v.vin === vin)) });
  }
  return out.sort((a, b) => b.count - a.count || a.letter.localeCompare(b.letter));
}

/** "9 A · 6 B" for a `routeVehicleCounts` result. */
export function formatVehicleCounts(parts) {
  return (Array.isArray(parts) ? parts : [])
    .map((p) => `${p.count}${p.letter ? ` ${p.letter}` : ""}`)
    .join(" · ");
}

/**
 * The route's stats for just the selected vehicles. With every car that
 * drove the route selected (or no per-vehicle stats) these are the route's
 * overall stats; with a subset they are combined from `by_vin` (count sum,
 * min of bests, max of slowest, count-weighted averages) so the panel matches
 * the drives actually shown.
 */
export function selectedRouteStats(route, vins) {
  const stats = (route && route.stats) || {};
  const by = stats.by_vin || {};
  const keys = Object.keys(by);
  const wanted = Array.isArray(vins) ? vins.filter((v) => by[v]) : keys;
  if (!keys.length || !wanted.length || wanted.length === keys.length) return stats;
  let count = 0;
  let fastest = null;
  let slowest = null;
  let avgSum = 0;
  let avgW = 0;
  let effSum = 0;
  let effW = 0;
  for (const vin of wanted) {
    const s = by[vin];
    const n = s.count || 0;
    count += n;
    if (s.fastest_seconds !== null && s.fastest_seconds !== undefined) {
      fastest = fastest === null ? s.fastest_seconds : Math.min(fastest, s.fastest_seconds);
    }
    if (s.slowest_seconds !== null && s.slowest_seconds !== undefined) {
      slowest = slowest === null ? s.slowest_seconds : Math.max(slowest, s.slowest_seconds);
    }
    if (s.avg_seconds !== null && s.avg_seconds !== undefined) {
      avgSum += s.avg_seconds * n;
      avgW += n;
    }
    if (s.avg_efficiency_mi_kwh !== null && s.avg_efficiency_mi_kwh !== undefined) {
      effSum += s.avg_efficiency_mi_kwh * n;
      effW += n;
    }
  }
  return {
    count,
    fastest_seconds: fastest,
    slowest_seconds: slowest,
    avg_seconds: avgW ? avgSum / avgW : null,
    avg_efficiency_mi_kwh: effW ? effSum / effW : null,
  };
}

/**
 * One row per selected vehicle that drove the route -- its drive count and
 * best / average / slowest time -- for the stats panel's vehicle-vs-vehicle
 * comparison: `[{vin, letter, color, count, fastest, avg, slowest}]` with the
 * times already formatted.
 */
export function vehicleStatRows(route, vehicles, vins) {
  const by = (route && route.stats && route.stats.by_vin) || {};
  const list = Array.isArray(vehicles) ? vehicles : [];
  const wanted = Array.isArray(vins) ? vins : Object.keys(by);
  const rows = [];
  for (const vin of wanted) {
    const s = by[vin];
    if (!s || !s.count) continue;
    rows.push({
      vin,
      count: s.count,
      fastest: formatDuration(s.fastest_seconds),
      avg: formatDuration(s.avg_seconds),
      slowest: formatDuration(s.slowest_seconds),
      ..._vehicleParts(list.find((v) => v.vin === vin)),
    });
  }
  return rows;
}

/**
 * Keys of the fastest and slowest non-outlier drives among `drives` (the ones
 * on screen), so the outlines always mark drives that are actually drawn.
 */
export function fastestSlowestKeys(drives) {
  const eligible = (Array.isArray(drives) ? drives : []).filter(
    (d) => !d.outlier && d.duration_seconds !== null && d.duration_seconds !== undefined
  );
  if (!eligible.length) return { fastest: null, slowest: null };
  const fastest = eligible.reduce((a, b) => (b.duration_seconds < a.duration_seconds ? b : a));
  const slowest = eligible.reduce((a, b) => (b.duration_seconds > a.duration_seconds ? b : a));
  return { fastest: driveKey(fastest), slowest: driveKey(slowest) };
}

/**
 * How one drive's polyline is drawn. With several vehicles each drive takes
 * its vehicle's color; the fastest / slowest get a green / red outline and the
 * selected drive is thicker (drawn last, by `z`). With one vehicle the other
 * drives stay neutral gray as before.
 */
export function routeDriveStyle(drive, { multi, selectedKey, fastestKey, slowestKey, vehicles }) {
  const key = driveKey(drive);
  const vehicle = (Array.isArray(vehicles) ? vehicles : []).find((v) => v.vin === drive.vin);
  const base = multi ? _vehicleParts(vehicle).color : "var(--secondary-text-color, #9e9e9e)";
  const style = { color: base, weight: 2, opacity: 0.55, casing: null, z: 0 };
  if (key === slowestKey) {
    style.z = 1;
    style.weight = 3;
    style.opacity = 0.9;
    style.casing = { color: "#c62828", weight: 7 };
    if (!multi) style.color = "#c62828";
  }
  if (key === fastestKey) {
    style.z = 2;
    style.weight = 3;
    style.opacity = 0.9;
    style.casing = { color: "#2e7d32", weight: 7 };
    if (!multi) style.color = "#2e7d32";
  }
  if (key === selectedKey) {
    style.z = 3;
    style.weight = 5;
    style.opacity = 1;
    if (!multi) style.color = "var(--primary-color, #03a9f4)";
    else if (!style.casing) style.casing = { color: "#ffffff", weight: 9 };
  }
  return style;
}

const SORT_ACCESSORS = {
  date: (d) => (d.sort_ts !== null && d.sort_ts !== undefined ? d.sort_ts : d.start_ts),
  elapsed: (d) => d.duration_seconds,
  moving: (d) => d.moving_seconds,
  efficiency: (d) => d.efficiency_mi_kwh,
  temp: (d) => d.temp_f,
};

/**
 * Sort a route's drives by one of date/elapsed/moving/efficiency/temp.
 * Nulls always sort last, regardless of direction, so missing data never
 * jumps to the top of a descending sort.
 */
export function sortRouteDrives(drives, key = "date", dir = "asc") {
  const list = Array.isArray(drives) ? drives : [];
  const accessor = SORT_ACCESSORS[key] || SORT_ACCESSORS.date;
  const sign = dir === "desc" ? -1 : 1;
  return [...list].sort((a, b) => {
    const av = accessor(a);
    const bv = accessor(b);
    const aNull = av === null || av === undefined || Number.isNaN(av);
    const bNull = bv === null || bv === undefined || Number.isNaN(bv);
    if (aNull && bNull) return 0;
    if (aNull) return 1;
    if (bNull) return -1;
    return (av - bv) * sign;
  });
}

/** The most recent drive in a route (by sort_ts/start_ts), or null if none. */
export function defaultSelectedDrive(route) {
  const drives = (route && route.drives) || [];
  if (!drives.length) return null;
  return drives.reduce((best, d) => {
    const bestTs = SORT_ACCESSORS.date(best);
    const ts = SORT_ACCESSORS.date(d);
    return ts !== null && ts !== undefined && (bestTs === null || bestTs === undefined || ts > bestTs)
      ? d
      : best;
  }, drives[0]);
}

/** "9 of 15 drives have a route on the map", or null when every drive (or none) does. */
export function mapCoverageText(route) {
  const drives = (route && route.drives) || [];
  if (!drives.length) return null;
  const withMap = drives.filter((d) => d.preview && d.preview.lat && d.preview.lat.length).length;
  if (withMap === drives.length) return null;
  return `${withMap} of ${drives.length} drive${drives.length === 1 ? "" : "s"} have a route on the map`;
}

/**
 * Build the stats-overlay panel's lines: Fastest/Average/Slowest/Drives/
 * Avg mi/kWh, plus (when `selected` is given) its own time, rank and the two
 * vs-best/vs-avg deltas.
 */
export function buildOverlayLines(route, selected, vins = null) {
  const stats = selectedRouteStats(route, vins);
  const lines = [];
  const selKey = driveKey(selected);
  const isThis = (id, seconds) => {
    if (!selected) return false;
    if (id) return id === selKey || id === selected.drive_id;
    return seconds !== null && seconds !== undefined && seconds === selected.duration_seconds;
  };
  lines.push({
    label: "Fastest",
    value: formatDuration(stats.fastest_seconds),
    sub: isThis(stats.fastest_drive_id, stats.fastest_seconds) ? "(this drive)" : null,
  });
  lines.push({ label: "Average", value: formatDuration(stats.avg_seconds) });
  lines.push({
    label: "Slowest",
    value: formatDuration(stats.slowest_seconds),
    sub: isThis(stats.slowest_drive_id, stats.slowest_seconds) ? "(this drive)" : null,
  });
  lines.push({ label: "Drives", value: String(stats.count || 0) });
  lines.push({
    label: "Avg mi/kWh",
    value: stats.avg_efficiency_mi_kwh !== null && stats.avg_efficiency_mi_kwh !== undefined
      ? stats.avg_efficiency_mi_kwh.toFixed(2)
      : "--",
  });

  if (selected) {
    // A drive is ranked and compared against its own car's drives on the
    // route (an R2 is never ranked against an R1T), when that is known.
    const own = ((route && route.stats && route.stats.by_vin) || {})[selected.vin] || null;
    const rank = selected.vin_rank !== undefined && selected.vin_rank !== null ? selected.vin_rank : selected.rank;
    const total = own && own.count ? own.count : stats.count;
    const rankText = rank && total ? `#${rank} of ${total}` : "unranked (outlier)";
    const ref = own || stats;
    const vsBest =
      ref.fastest_seconds !== null && ref.fastest_seconds !== undefined && selected.duration_seconds !== null
        ? selected.duration_seconds - ref.fastest_seconds
        : null;
    const vsAvg =
      ref.avg_seconds !== null && ref.avg_seconds !== undefined && selected.duration_seconds !== null
        ? selected.duration_seconds - ref.avg_seconds
        : null;
    lines.push({ label: "This drive", value: formatDuration(selected.duration_seconds) });
    lines.push({ label: "Rank", value: rankText });
    lines.push({
      label: "vs best / avg",
      value: `${formatDelta(vsBest)} vs best · ${formatDelta(vsAvg)} vs avg`,
    });
  }
  return lines;
}

/**
 * Lay out a route's drives as a dot chart: one dot per drive, x by date
 * (linear between the earliest/latest), y by elapsed time (linear, inverted
 * so faster is higher), plus the best/average lines in the same space.
 * Drives missing a date or duration are omitted from `points` but still
 * count toward nothing (pure layout; the caller decides how to mark them).
 */
export function dotChartLayout(
  drives,
  { width = 600, height = 160, padTop = 12, padBottom = 20, padLeft = 48, padRight = 16 } = {}
) {
  const usable = (Array.isArray(drives) ? drives : []).filter(
    (d) => SORT_ACCESSORS.date(d) !== null && SORT_ACCESSORS.date(d) !== undefined && d.duration_seconds !== null && d.duration_seconds !== undefined
  );
  if (!usable.length) {
    return { points: [], bestY: null, avgY: null, width, height };
  }
  const xs = usable.map(SORT_ACCESSORS.date);
  const ys = usable.map((d) => d.duration_seconds);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  // The domain is the drives' own range (plus 8 % each side), not 0..max:
  // commutes differ by minutes, which a zero-based axis squashes flat.
  const rawMin = Math.min(...ys);
  const rawMax = Math.max(...ys);
  const pad = Math.max(30, (rawMax - rawMin) * 0.08);
  const minY = Math.max(0, rawMin - pad);
  const maxY = rawMax + pad;
  const spanX = maxX - minX || 1;
  const spanY = maxY - minY || 1;
  const plotW = width - padLeft - padRight;
  const plotH = height - padTop - padBottom;

  const xFor = (x) => (spanX === 1 && minX === maxX ? padLeft + plotW / 2 : padLeft + ((x - minX) / spanX) * plotW);
  // Faster (smaller elapsed time) plots higher on the chart.
  const yFor = (y) => padTop + ((y - minY) / spanY) * plotH;

  const points = usable.map((d) => ({
    driveId: driveKey(d),
    vin: d.vin,
    x: xFor(SORT_ACCESSORS.date(d)),
    y: yFor(d.duration_seconds),
    outlier: !!d.outlier,
  }));

  return { points, width, height, xFor, yFor, minX, maxX, minY, maxY, padLeft, padRight, padTop, padBottom };
}

// -- DOM-dependent card -------------------------------------------------------

const ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services";
const ESRI_ATTRIBUTION =
  'Tiles &copy; Esri | Map data &copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors';
// Copied from rivian-places-card.js's BASEMAPS (nothing shared between
// modules -- see that file's header comment about stale-cache risk).
const BASEMAPS = {
  map: {
    label: "Map",
    layers: (dark) =>
      dark
        ? [
            [`${ESRI}/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}`, 16],
            [`${ESRI}/Canvas/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}`, 16],
          ]
        : [
            [`${ESRI}/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}`, 16],
            [`${ESRI}/Canvas/World_Light_Gray_Reference/MapServer/tile/{z}/{y}/{x}`, 16],
          ],
  },
  streets: {
    label: "Streets",
    layers: () => [[`${ESRI}/World_Street_Map/MapServer/tile/{z}/{y}/{x}`, 19]],
  },
  satellite: {
    label: "Satellite",
    layers: () => [
      [`${ESRI}/World_Imagery/MapServer/tile/{z}/{y}/{x}`, 19],
      [`${ESRI}/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}`, 19],
    ],
  },
};
const BASEMAP_STORAGE_KEY = "rivian-routes-basemap";
const MAP_MAX_ZOOM = 20;

function _initialBasemap(configDefault) {
  try {
    const saved = window.localStorage.getItem(BASEMAP_STORAGE_KEY);
    if (saved && BASEMAPS[saved]) return saved;
  } catch (_err) {
    // Storage can be unavailable (private mode, blocked site data).
  }
  return BASEMAPS[configDefault] ? configDefault : "map";
}

function _escapeText(el, text) {
  el.textContent = text === null || text === undefined ? "" : String(text);
}

let _leafletModulePromise = null;
/**
 * Leaflet renders string tooltip and popup content as HTML. Place, station and
 * route names can come from OpenStreetMap (which anyone can edit) or from user
 * input, so every string handed to a tooltip/popup becomes plain text here;
 * only a literal "<br>" survives, as a line break. Applied once to the shared
 * Leaflet module (every card imports the same instance), so tooltips added
 * later are covered too. A DOM node passed as content is left as is.
 */
export function plainTooltipNode(text) {
  const el = document.createElement("div");
  String(text)
    .split(/<br\s*\/?>/i)
    .forEach((part, i) => {
      if (i) el.appendChild(document.createElement("br"));
      el.appendChild(document.createTextNode(part));
    });
  return el;
}

function _hardenLeaflet(L) {
  const proto = L && L.DivOverlay && L.DivOverlay.prototype;
  if (!proto || proto.__rivianPlainText) return L;
  const original = proto.setContent;
  proto.setContent = function (content) {
    let safe = content;
    if (typeof content === "string") safe = plainTooltipNode(content);
    else if (typeof content === "function") {
      safe = function (source) {
        const out = content(source);
        return typeof out === "string" ? plainTooltipNode(out) : out;
      };
    }
    return original.call(this, safe);
  };
  proto.__rivianPlainText = true;
  return L;
}

function _loadLeaflet() {
  if (!_leafletModulePromise) {
    _leafletModulePromise = import(new URL("./leaflet/leaflet-src.esm.js", import.meta.url)).then(
      _hardenLeaflet
    );
  }
  return _leafletModulePromise;
}

let _leafletCssPromise = null;
function _loadLeafletCss() {
  if (!_leafletCssPromise) {
    _leafletCssPromise = fetch(new URL("./leaflet/leaflet.css", import.meta.url)).then((r) => r.text());
  }
  return _leafletCssPromise;
}

/** A touch-first device (phone/tablet), where one-finger drags should scroll the page. */
function _isTouchDevice() {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia("(pointer: coarse)").matches;
}

/** Column explanations for the drives table headers (hover/tap/focus). */
export const COLUMN_TITLES = {
  date: "When the drive started",
  elapsed: "Total time from start to end, including stops",
  moving: "Time spent actually moving (stops excluded)",
  efficiency: "Miles per kilowatt-hour for this drive: higher is better",
  temp: "Outside temperature during the drive, in degrees Fahrenheit",
  car: "Which vehicle made the drive",
  vs: "Elapsed time compared with the average for this route (negative is faster, green)",
};

/** The `aria-sort` value for a sortable column header. */
export function ariaSortFor(sortKey, sortDir, key) {
  if (sortKey !== key) return "none";
  return sortDir === "desc" ? "descending" : "ascending";
}

/** Hover/tap explanations for the overlay panel's labels. */
export const OVERLAY_TITLES = {
  Fastest: "The quickest elapsed time on this route",
  Average: "Average elapsed time (outlier drives excluded)",
  Slowest: "The slowest elapsed time on this route (outliers excluded)",
  Drives: "Number of drives counted on this route",
  "Avg mi/kWh": "Energy-weighted average efficiency in miles per kilowatt-hour",
  "This drive": "Elapsed time of the selected drive",
  Rank: "Position among this car's drives on the route, 1 = fastest",
  "vs best / avg": "How much slower (+) or faster (-) than the best and average time",
};

/**
 * One drive's readout line, shared by the chart, map and table tooltips:
 * "Sep 20 \u00b7 16:30 elapsed \u00b7 14:02 moving \u00b7 3.42 mi/kWh \u00b7 52\u00b0F \u00b7 -8% vs avg".
 */
export function driveReadout(drive, who = "") {
  if (!drive) return "";
  const parts = [];
  parts.push(`${who}${formatShortDate(drive.sort_ts !== null && drive.sort_ts !== undefined ? drive.sort_ts : drive.start_ts)}`);
  parts.push(`${formatDuration(drive.duration_seconds)} elapsed`);
  if (drive.moving_seconds !== null && drive.moving_seconds !== undefined) {
    parts.push(`${formatDuration(drive.moving_seconds)} moving`);
  }
  if (typeof drive.efficiency_mi_kwh === "number") parts.push(`${drive.efficiency_mi_kwh.toFixed(2)} mi/kWh`);
  if (typeof drive.temp_f === "number") parts.push(`${Math.round(drive.temp_f)}\u00b0F`);
  const vs = drive.vin_vs_avg_pct !== undefined && drive.vin_vs_avg_pct !== null ? drive.vin_vs_avg_pct : drive.vs_avg_pct;
  if (typeof vs === "number") parts.push(`${formatVsAvgPct(vs)} vs avg`);
  if (drive.outlier) parts.push("outlier, excluded from stats");
  return parts.join(" \u00b7 ");
}

const SORT_COLUMNS = [
  ["date", "Date"],
  ["elapsed", "Elapsed"],
  ["moving", "Moving"],
  ["efficiency", "mi/kWh"],
  ["temp", "Temp"],
];

const _CARD_STYLE = `
  :host { display: block; }
  .rrc-topbar {
    border-bottom: 1px solid var(--divider-color, #e0e0e0);
  }
  .rrc-topbar rivian-vehicle-bar {
    padding: 8px 12px;
  }
  .rrc-vb {
    display: inline-flex;
    align-items: center;
    gap: 3px;
    margin-left: 6px;
    font-weight: 600;
  }
  .rrc-vdot {
    display: inline-block;
    width: 8px;
    height: 8px;
    margin-right: 5px;
    border-radius: 50%;
  }
  .rrc-vb::before {
    content: "";
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--vb-c, #888);
  }
  ha-card {
    display: flex;
    flex-direction: column;
    overflow: hidden;
    padding: 0;
    background: var(--ha-card-background, var(--card-background-color, #fff));
    color: var(--primary-text-color, #212121);
  }
  .rrc-body {
    display: flex;
    flex: 1;
    min-height: 0;
  }
  .rrc-body.rrc-stacked {
    flex-direction: column;
  }
  .rrc-list-pane {
    width: 300px;
    min-width: 240px;
    flex-shrink: 0;
    display: flex;
    flex-direction: column;
    border-right: 1px solid var(--divider-color, #e0e0e0);
    overflow: hidden;
  }
  /* Phone width: the page does all the scrolling. The map, stats, chart and
     table come first and the route list last (like the Drives tab), and
     nothing inside the card has its own scroll. */
  .rrc-stacked .rrc-list-pane {
    order: 2;
    width: auto;
    min-width: 0;
    border-right: none;
    border-top: 1px solid var(--divider-color, #e0e0e0);
    overflow: visible;
  }
  .rrc-stacked .rrc-list {
    overflow: visible;
  }
  .rrc-stacked .rrc-right-pane {
    order: 1;
    overflow: visible;
  }
  /* The stats card would cover most of a phone-sized map, so it sits
     under the map instead of on it. */
  .rrc-stacked .rrc-overlay {
    position: static;
    max-width: none;
    margin: 8px 12px 0;
    box-shadow: none;
    border: 1px solid var(--divider-color, #e0e0e0);
  }
  .rrc-list-header {
    padding: 8px 12px;
    font-size: 20px; font-weight: 700; letter-spacing: 0.01em; color: var(--primary-text-color);
    border-bottom: 1px solid var(--divider-color, #e0e0e0);
    flex-shrink: 0;
  }
  .rrc-list {
    flex: 1;
    overflow: auto;
    min-height: 0;
  }
  .rrc-row {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: 8px 12px;
    cursor: pointer;
    border-bottom: 1px solid var(--divider-color, rgba(0, 0, 0, 0.06));
  }
  .rrc-row:hover, .rrc-row.selected {
    background: var(--secondary-background-color, rgba(0, 0, 0, 0.04));
  }
  .rrc-row-label {
    font-size: 0.95em;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .rrc-row-meta {
    font-size: 0.78em;
    color: var(--secondary-text-color);
  }
  .rrc-empty {
    padding: 16px 12px;
    color: var(--secondary-text-color);
    font-size: 0.9em;
  }
  .rrc-right-pane {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-width: 0;
    min-height: 0;
    overflow: auto;
  }
  .rrc-map-container {
    position: relative;
    flex-shrink: 0;
    height: 46%;
    min-height: 240px;
  }
  .rrc-stacked .rrc-map-container {
    height: 40vh;
    min-height: 240px;
    max-height: 420px;
  }
  .rrc-map {
    position: absolute;
    inset: 0;
  }
  .rrc-basemaps {
    position: absolute;
    top: 10px;
    right: 10px;
    z-index: 1000;
    display: flex;
    border-radius: 6px;
    overflow: hidden;
    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.35);
  }
  .rrc-basemaps button {
    font: inherit;
    font-size: 12px;
    padding: 5px 10px;
    border: none;
    cursor: pointer;
    background: var(--card-background-color, #fff);
    color: var(--primary-text-color, #212121);
  }
  .rrc-basemaps button + button {
    border-left: 1px solid var(--divider-color, rgba(0, 0, 0, 0.12));
  }
  .rrc-basemaps button.active {
    background: var(--primary-color, #03a9f4);
    color: var(--text-primary-color, #fff);
  }
  .rrc-overlay {
    position: absolute;
    top: 10px;
    left: 10px;
    z-index: 1000;
    background: var(--card-background-color, rgba(255, 255, 255, 0.92));
    color: var(--primary-text-color, #212121);
    border-radius: 8px;
    padding: 8px 10px;
    font-size: 0.78em;
    box-shadow: 0 1px 6px rgba(0, 0, 0, 0.35);
    min-width: 200px;
    max-width: 280px;
  }
  .rrc-overlay-title {
    font-weight: 600;
    margin-bottom: 4px;
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .rrc-overlay-title ha-icon {
    cursor: pointer;
    --mdc-icon-size: 16px;
    color: var(--secondary-text-color);
  }
  .rrc-overlay-line {
    display: flex;
    justify-content: space-between;
    gap: 10px;
    white-space: nowrap;
  }
  .rrc-overlay-line + .rrc-overlay-line {
    margin-top: 2px;
  }
  .rrc-overlay-note {
    margin-top: 6px;
    color: var(--secondary-text-color);
    font-style: italic;
    white-space: normal;
  }
  .rrc-chart-wrap {
    flex-shrink: 0;
    padding: 8px 12px;
    border-top: 1px solid var(--divider-color, #e0e0e0);
  }
  .rrc-chart-readout {
    min-height: 1.3em;
    margin-top: 2px;
    font-size: 0.78em;
    color: var(--secondary-text-color);
  }
  .rrc-chart-wrap circle:focus-visible, table.rrc-table th:focus-visible, table.rrc-table tr:focus-visible,
  .rrc-row:focus-visible, .rrc-overlay-title ha-icon:focus-visible, .rrc-basemaps button:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: 1px;
  }
  .rrc-chart-title {
    font-size: 0.78em;
    font-weight: 600;
    color: var(--secondary-text-color);
    margin-bottom: 4px;
  }
  .rrc-table-wrap {
    flex: 1;
    overflow: auto;
    padding: 0 12px 12px;
  }
  table.rrc-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.85em;
  }
  table.rrc-table th {
    text-align: right;
    padding: 4px 6px;
    cursor: pointer;
    color: var(--secondary-text-color);
    white-space: nowrap;
    border-bottom: 1px solid var(--divider-color, #e0e0e0);
  }
  table.rrc-table th:first-child, table.rrc-table td:first-child {
    text-align: left;
  }
  table.rrc-table td {
    text-align: right;
    padding: 4px 6px;
    white-space: nowrap;
    border-bottom: 1px solid var(--divider-color, rgba(0, 0, 0, 0.06));
  }
  table.rrc-table tr {
    cursor: pointer;
  }
  table.rrc-table tr:hover, table.rrc-table tr.selected {
    background: var(--secondary-background-color, rgba(0, 0, 0, 0.04));
  }
  .rrc-outlier-flag {
    color: var(--warning-color, #ff9800);
    margin-left: 4px;
  }
  .rrc-rename-row {
    display: flex;
    gap: 6px;
    padding: 6px 12px;
    border-bottom: 1px solid var(--divider-color, #e0e0e0);
  }
  .rrc-rename-row input {
    flex: 1;
    font: inherit;
    padding: 4px 6px;
    border-radius: 4px;
    border: 1px solid var(--divider-color, #e0e0e0);
    background: var(--card-background-color, #fff);
    color: var(--primary-text-color);
  }
  .rrc-rename-row button {
    font: inherit;
    font-size: 0.85em;
    padding: 4px 8px;
    border-radius: 4px;
    border: 1px solid var(--divider-color, #e0e0e0);
    background: var(--primary-color, #03a9f4);
    color: var(--text-primary-color, #fff);
    cursor: pointer;
  }
  .rrc-error {
    padding: 16px;
    color: var(--error-color, #b00020);
  }
`;

/** Load the shared vehicle bar/selection module with this module's own cache-buster. */
function _loadBarModule() {
  return import(new URL("./rivian-vehicle-bar.js" + new URL(import.meta.url).search, import.meta.url));
}

const BaseElement = typeof HTMLElement === "undefined" ? class {} : HTMLElement;

class RivianRoutesCard extends BaseElement {
  static getStubConfig() {
    return {};
  }

  setConfig(config) {
    const next = config || {};
    // A configured `vin` pins the card; otherwise it follows the shared
    // vehicle selection (and shows its first vehicle for now).
    const vinChanged = !!this._config && (this._config.vin || null) !== (next.vin || null);
    this._config = next;
    if (!this._built) {
      this._build();
    } else if (vinChanged) {
      this._unsubscribe();
      this._resetState();
      this._started = false;
      if (this._hass) this.hass = this._hass;
    }
  }

  getCardSize() {
    return 8;
  }

  get hass() {
    return this._hass;
  }

  set hass(hass) {
    const themeChanged = this._hass && hass && this._hass.themes?.darkMode !== hass.themes?.darkMode;
    this._hass = hass;
    if (!this._built) return;
    if (this._barEl) this._barEl.hass = hass;
    if (!this._started) {
      this._started = true;
      this._start().catch((err) => this._showError(err));
    }
    if (themeChanged) this._applyTileLayer();
  }

  _resetState() {
    this._routes = [];
    this._selectedRouteId = null;
    this._routeDetail = null;
    this._selectedDriveId = null;
    this._sortKey = "date";
    this._sortDir = "desc";
    this._renaming = false;
    this._polylines = new Map();
    this._fittedRouteId = null;
  }

  _build() {
    this._built = true;
    this._started = false;
    this._map = null;
    this._tileLayers = [];
    this._basemap = _initialBasemap(this._config && this._config.basemap);
    this._leaflet = null;
    this._vins = [];
    this._vehicleList = [];
    this._selection = [];
    this._bar = null;
    this._barEl = null;
    this._unsubSelection = null;
    this._resizeObserver = null;
    this._resetState();

    this.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = _CARD_STYLE;
    this.shadowRoot.appendChild(style);

    this._leafletStyleEl = document.createElement("style");
    this.shadowRoot.appendChild(this._leafletStyleEl);

    this._card = document.createElement("ha-card");
    this.shadowRoot.appendChild(this._card);

    this._renderShell();
  }

  _renderShell() {
    this._card.textContent = "";
    const height = (this._config && this._config.height) || null;
    if (height) this._card.style.height = height;
    else this._card.style.height = "calc(100vh - var(--header-height, 56px) - 32px)";
    this._card.style.minHeight = "420px";

    // Vehicle bar slot (filled once the card follows the shared selection).
    this._topbarEl = document.createElement("div");
    this._topbarEl.className = "rrc-topbar";
    this._topbarEl.style.display = "none";
    this._card.appendChild(this._topbarEl);

    this._body = document.createElement("div");
    this._body.className = "rrc-body";
    this._card.appendChild(this._body);

    this._listPane = document.createElement("div");
    this._listPane.className = "rrc-list-pane";
    this._body.appendChild(this._listPane);

    const header = document.createElement("div");
    header.className = "rrc-list-header";
    _escapeText(header, "Fav Routes");
    this._listPane.appendChild(header);

    this._listEl = document.createElement("div");
    this._listEl.className = "rrc-list";
    this._listPane.appendChild(this._listEl);

    this._rightPane = document.createElement("div");
    this._rightPane.className = "rrc-right-pane";
    this._body.appendChild(this._rightPane);

    this._mapContainer = document.createElement("div");
    this._mapContainer.className = "rrc-map-container";
    this._rightPane.appendChild(this._mapContainer);

    this._mapEl = document.createElement("div");
    this._mapEl.className = "rrc-map";
    this._mapContainer.appendChild(this._mapEl);

    if (!(this._config && this._config.tile_url)) {
      this._basemapEl = document.createElement("div");
      this._basemapEl.className = "rrc-basemaps";
      for (const [key, style] of Object.entries(BASEMAPS)) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.dataset.basemap = key;
        _escapeText(btn, style.label);
        btn.title = `Show the ${style.label} basemap`;
        btn.setAttribute("aria-label", `${style.label} basemap`);
        btn.addEventListener("click", () => this._setBasemap(key));
        this._basemapEl.appendChild(btn);
      }
      this._mapContainer.appendChild(this._basemapEl);
      this._markActiveBasemap();
    }

    this._overlayEl = document.createElement("div");
    this._overlayEl.className = "rrc-overlay";
    this._mapContainer.appendChild(this._overlayEl);

    this._renameRow = document.createElement("div");
    this._renameRow.className = "rrc-rename-row";
    this._renameRow.style.display = "none";
    this._rightPane.appendChild(this._renameRow);

    this._chartWrap = document.createElement("div");
    this._chartWrap.className = "rrc-chart-wrap";
    this._rightPane.appendChild(this._chartWrap);

    this._tableWrap = document.createElement("div");
    this._tableWrap.className = "rrc-table-wrap";
    this._rightPane.appendChild(this._tableWrap);

    this._applyLayout();
    this._observeResize();
    this._renderList();
    this._renderOverlay();
  }

  _observeResize() {
    if (this._resizeObserver || typeof ResizeObserver === "undefined") return;
    this._resizeObserver = new ResizeObserver(() => {
      this._applyLayout();
      if (this._map) this._map.invalidateSize();
    });
    this._resizeObserver.observe(this);
  }

  _applyLayout() {
    const width = this.getBoundingClientRect ? this.getBoundingClientRect().width : 0;
    const stacked = width > 0 && width < STACK_BREAKPOINT_PX;
    this._body.classList.toggle("rrc-stacked", stacked);
    if (this._overlayEl) {
      const parent = stacked ? this._rightPane : this._mapContainer;
      if (this._overlayEl.parentNode !== parent) {
        if (stacked) this._rightPane.insertBefore(this._overlayEl, this._mapContainer.nextSibling);
        else this._mapContainer.appendChild(this._overlayEl);
      }
    }
    const height = (this._config && this._config.height) || null;
    if (!height) this._card.style.height = stacked ? "auto" : "calc(100vh - var(--header-height, 56px) - 32px)";
    this._card.style.minHeight = stacked ? "" : "420px";
    this._applyMapTouchMode(stacked);
    this._stacked = stacked;
  }

  /** Stacked on a touch screen, one-finger drags scroll the page instead of panning the map. */
  _applyMapTouchMode(stacked) {
    if (!this._map || !this._map.dragging) return;
    const pageScrolls = !!stacked && _isTouchDevice();
    if (pageScrolls && this._map.dragging.enabled()) this._map.dragging.disable();
    else if (!pageScrolls && !this._map.dragging.enabled()) this._map.dragging.enable();
  }

  connectedCallback() {
    if (!this._built) return;
    this._observeResize();
    this._applyLayout();
    if (this._map) requestAnimationFrame(() => this._map.invalidateSize());
    if (this._hass && !this._started) {
      this._started = true;
      this._start().catch((err) => this._showError(err));
    }
    if (this._hass) this._subscribe();
    if (this._bar && !this._unsubSelection && !(this._config && this._config.vin)) {
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
  }

  _subscribe() {
    if (this._unsubPromise || !this._hass || !this._vins.length) return;
    this._unsubPromise = this._hass.connection
      .subscribeMessage(
        () => {
          this._refresh().catch((err) => this._showError(err));
        },
        { type: "rivian/analytics/subscribe", vins: [...this._vins] }
      )
      .catch((err) => {
        console.warn("rivian-routes-card: live updates unavailable", err);
        return null;
      });
  }

  _unsubscribe() {
    if (!this._unsubPromise) return;
    this._unsubPromise.then((unsub) => unsub && unsub()).catch(() => {});
    this._unsubPromise = null;
  }

  get _isAdmin() {
    return !!(this._hass && this._hass.user && this._hass.user.is_admin);
  }

  async _start() {
    await this._initScope();
    if (!this._vins.length) {
      this._showMessage("No vehicles to show.");
      return;
    }
    this._subscribe();
    await this._ensureMap();
    await this._refresh();
  }

  /** The vehicles to show: the config's `vin`, else the shared selection. */
  async _initScope() {
    try {
      this._bar = await _loadBarModule();
      this._vehicleList = await this._bar.getVehicles(this._hass);
    } catch (err) {
      console.warn("rivian-routes-card: vehicle list unavailable", err);
    }
    if (this._config && this._config.vin) {
      this._vins = [this._config.vin];
      this._selection = [...this._vins];
      return;
    }
    try {
      this._selection = this._bar ? await this._bar.getSelection(this._hass) : [];
    } catch (err) {
      console.warn("rivian-routes-card: vehicle selection unavailable", err);
      this._selection = [];
    }
    this._vins = [...this._selection];
    if (this._bar && !this._unsubSelection) {
      this._unsubSelection = this._bar.onSelectionChange((vins) => this._onSelectionChanged(vins));
    }
    this._mountBar();
  }

  _mountBar() {
    if (this._barEl || !this._bar) return;
    const el = document.createElement("rivian-vehicle-bar");
    el.hass = this._hass;
    this._topbarEl.appendChild(el);
    this._topbarEl.style.display = "";
    this._barEl = el;
  }

  _onSelectionChanged(vins) {
    if (!this._bar || (this._config && this._config.vin)) return;
    this._selection = this._bar.normalizeSelection(vins, this._vehicleList);
    if (this._bar.sameSelection(this._selection, this._vins)) return;
    this._unsubscribe();
    this._vins = [...this._selection];
    if (this._map) for (const layer of this._polylines.values()) this._map.removeLayer(layer);
    this._resetState();
    this._subscribe();
    this._refresh().catch((err) => this._showError(err));
  }

  _showMessage(text) {
    this._card.textContent = "";
    const el = document.createElement("div");
    el.className = "rrc-error";
    _escapeText(el, text);
    this._card.appendChild(el);
  }

  async _refresh() {
    const groups = this._datasetGroups();
    const results = await Promise.all(
      groups.map((g) =>
        this._hass.callWS({
          type: "rivian/routes/list",
          ...(g.dataset ? { dataset: g.dataset } : {}),
          vins: g.vins,
        })
      )
    );
    const routes = [];
    results.forEach((result, i) => {
      for (const route of (result && result.routes) || []) {
        routes.push({ ...route, dataset: (result && result.dataset) || groups[i].dataset || "real" });
      }
    });
    this._routes = routes;
    if (this._selectedRouteId === null && this._routes.length) {
      this._selectedRouteId = sortRoutes(this._routes)[0].id;
    }
    this._renderList();
    await this._refreshDetail();
  }

  /** `[{dataset, vins}]` for the selection (a pinned unknown vehicle is queried without a dataset). */
  _datasetGroups() {
    const groups = datasetGroups(this._vehicleList, this._vins);
    if (groups.length || !this._vins.length) return groups;
    return [{ dataset: undefined, vins: [...this._vins] }];
  }

  _selectedRoute() {
    return this._routes.find((r) => r.id === this._selectedRouteId) || null;
  }

  /** Several vehicles' drives are on screen: color by vehicle. */
  get _multi() {
    return this._vins.length > 1;
  }

  async _refreshDetail() {
    if (this._selectedRouteId === null) {
      this._routeDetail = null;
      this._renderOverlay();
      this._renderMap();
      this._renderChart();
      this._renderTable();
      return;
    }
    const route = this._selectedRoute();
    const group = this._datasetGroups().find((g) => g.dataset === (route && route.dataset));
    const result = await this._hass.callWS({
      type: "rivian/routes/route",
      ...(route && route.dataset ? { dataset: route.dataset } : {}),
      vins: group ? group.vins : [...this._vins],
      route_id: this._selectedRouteId,
    });
    this._routeDetail = (result && result.route) || null;
    if (this._routeDetail) this._routeDetail.dataset = route ? route.dataset : undefined;
    if (this._selectedDriveId === null || !(this._routeDetail && this._routeDetail.drives.some((d) => driveKey(d) === this._selectedDriveId))) {
      const def = defaultSelectedDrive(this._routeDetail);
      this._selectedDriveId = def ? driveKey(def) : null;
    }
    this._renderOverlay();
    this._renderMap();
    this._renderChart();
    this._renderTable();
  }

  _showError(err) {
    console.error("rivian-routes-card", err);
    this._card.textContent = "";
    const el = document.createElement("div");
    el.className = "rrc-error";
    _escapeText(el, `rivian-routes-card: ${err && err.message ? err.message : err}`);
    this._card.appendChild(el);
  }

  // -- list pane --------------------------------------------------------------

  _renderList() {
    this._listEl.textContent = "";
    const routes = sortRoutes(this._routes);
    if (!routes.length) {
      const empty = document.createElement("div");
      empty.className = "rrc-empty";
      _escapeText(empty, "No repeated routes yet. A route appears once you've driven the same start -> end 3 or more times.");
      this._listEl.appendChild(empty);
      return;
    }
    for (const route of routes) this._listEl.appendChild(this._buildRow(route));
  }

  _buildRow(route) {
    const row = document.createElement("div");
    row.className = "rrc-row" + (route.id === this._selectedRouteId ? " selected" : "");
    const label = document.createElement("div");
    label.className = "rrc-row-label";
    _escapeText(label, route.label);
    const meta = document.createElement("div");
    meta.className = "rrc-row-meta";
    const stats = route.stats || {};
    const shown = selectedRouteStats(route, this._vins);
    const count = this._multi || this._vins.length === 1
      ? shown.count || route.drive_count
      : route.drive_count;
    _escapeText(
      meta,
      `${count} drive${count === 1 ? "" : "s"} · best ${formatDuration(shown.fastest_seconds ?? stats.fastest_seconds)} · avg ${formatDuration(shown.avg_seconds ?? stats.avg_seconds)}`
    );
    if (this._multi) {
      for (const part of routeVehicleCounts(route, this._vehicleList, this._vins)) {
        const chip = document.createElement("span");
        chip.className = "rrc-vb";
        chip.style.setProperty("--vb-c", part.color);
        _escapeText(chip, `${part.count}${part.letter ? ` ${part.letter}` : ""}`);
        chip.title = `${part.count} drive${part.count === 1 ? "" : "s"} by vehicle ${part.letter || ""}`.trim();
        meta.appendChild(chip);
      }
    }
    row.appendChild(label);
    row.appendChild(meta);
    row.addEventListener("click", () => this._selectRoute(route.id));
    row.tabIndex = 0;
    row.setAttribute("role", "button");
    row.setAttribute("aria-pressed", String(route.id === this._selectedRouteId));
    row.title = `${route.label}: ${meta.textContent}. Best = fastest elapsed time, avg = average. Tap to show it on the map.`;
    row.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        this._selectRoute(route.id);
      }
    });
    return row;
  }

  _selectRoute(id) {
    if (id === this._selectedRouteId) return;
    this._selectedRouteId = id;
    this._selectedDriveId = null;
    this._renaming = false;
    this._fittedRouteId = null;
    this._renderList();
    this._renderRenameRow();
    this._refreshDetail().catch((err) => this._showError(err));
    // Phone width: the list is at the bottom, so bring the map back into view.
    if (this._stacked && this._card.scrollIntoView) {
      const top = this._card.getBoundingClientRect().top;
      if (top < 0) this._card.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }

  _selectDrive(driveId) {
    this._selectedDriveId = driveId;
    this._renderOverlay();
    this._renderMap(true);
    this._renderChart();
    this._renderTable();
  }

  // -- overlay / rename --------------------------------------------------------

  _selectedDrive() {
    if (!this._routeDetail) return null;
    return this._routeDetail.drives.find((d) => driveKey(d) === this._selectedDriveId) || null;
  }

  _renderOverlay() {
    this._overlayEl.textContent = "";
    if (!this._routeDetail) return;
    const title = document.createElement("div");
    title.className = "rrc-overlay-title";
    const titleText = document.createElement("span");
    _escapeText(titleText, this._routeDetail.label);
    title.appendChild(titleText);
    if (this._isAdmin) {
      const pencil = document.createElement("ha-icon");
      pencil.setAttribute("icon", "mdi:pencil");
      const startRename = () => {
        this._renaming = true;
        this._renderRenameRow();
      };
      pencil.addEventListener("click", startRename);
      pencil.tabIndex = 0;
      pencil.setAttribute("role", "button");
      pencil.title = "Rename this route";
      pencil.setAttribute("aria-label", "Rename this route");
      pencil.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          startRename();
        }
      });
      title.appendChild(pencil);
    }
    this._overlayEl.appendChild(title);

    const selected = this._selectedDrive();
    const lines = buildOverlayLines(this._routeDetail, selected, this._vins);
    const addLine = (labelText, valueText, color) => {
      const row = document.createElement("div");
      row.className = "rrc-overlay-line";
      const labelEl = document.createElement("span");
      _escapeText(labelEl, labelText);
      if (OVERLAY_TITLES[labelText]) row.title = OVERLAY_TITLES[labelText];
      if (color) {
        const dot = document.createElement("span");
        dot.className = "rrc-vdot";
        dot.style.background = color;
        labelEl.insertBefore(dot, labelEl.firstChild);
      }
      const valueEl = document.createElement("span");
      _escapeText(valueEl, valueText);
      row.appendChild(labelEl);
      row.appendChild(valueEl);
      this._overlayEl.appendChild(row);
    };
    const driveLabels = new Set(["This drive", "Rank", "vs best / avg"]);
    const text = (line) => (line.sub ? `${line.value} ${line.sub}` : line.value);
    for (const line of lines.filter((l) => !driveLabels.has(l.label))) {
      addLine(line.label, text(line));
    }
    // Vehicle vs. vehicle on the same route: each car's own best / average.
    if (this._multi) {
      for (const r of vehicleStatRows(this._routeDetail, this._vehicleList, this._vins)) {
        addLine(
          `${r.letter || r.vin.slice(-4)} · ${r.count}`,
          `best ${r.fastest} · avg ${r.avg}`,
          r.color
        );
      }
    }
    for (const line of lines.filter((l) => driveLabels.has(l.label))) {
      addLine(line.label, text(line));
    }

    const coverage = mapCoverageText(this._routeDetail);
    if (coverage) {
      const note = document.createElement("div");
      note.className = "rrc-overlay-note";
      _escapeText(note, coverage);
      this._overlayEl.appendChild(note);
    }
  }

  _renderRenameRow() {
    this._renameRow.textContent = "";
    if (!this._renaming || !this._routeDetail) {
      this._renameRow.style.display = "none";
      return;
    }
    this._renameRow.style.display = "flex";
    const input = document.createElement("input");
    input.type = "text";
    input.value = this._routeDetail.name || "";
    input.placeholder = this._routeDetail.label;
    input.setAttribute("aria-label", "Route name");
    input.title = "Route name (leave empty to use the automatic name)";
    this._renameRow.appendChild(input);
    const saveBtn = document.createElement("button");
    saveBtn.type = "button";
    _escapeText(saveBtn, "Save");
    saveBtn.title = "Save the route name";
    saveBtn.addEventListener("click", () => {
      this._renameRoute(input.value.trim() || null).catch((err) => this._showError(err));
    });
    this._renameRow.appendChild(saveBtn);
  }

  async _renameRoute(name) {
    await this._hass.callWS({
      type: "rivian/routes/rename",
      dataset: (this._routeDetail && this._routeDetail.dataset) || "real",
      route_id: this._selectedRouteId,
      name,
    });
    this._renaming = false;
    await this._refresh();
    this._renderRenameRow();
  }

  // -- chart --------------------------------------------------------------------

  _renderChart() {
    this._chartWrap.textContent = "";
    const title = document.createElement("div");
    title.className = "rrc-chart-title";
    _escapeText(title, "Elapsed time by date");
    this._chartWrap.appendChild(title);

    if (!this._routeDetail || !this._routeDetail.drives.length) return;
    const width = Math.max(240, (this._chartWrap.clientWidth || 600) - 24);
    const height = 170;
    const layout = dotChartLayout(this._routeDetail.drives, { width, height, padTop: 14, padBottom: 22 });
    const stats = selectedRouteStats(this._routeDetail, this._vins);
    const marks = fastestSlowestKeys(this._routeDetail.drives);

    const svgNS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("width", "100%");
    svg.setAttribute("height", String(height));
    svg.setAttribute("role", "group");
    svg.setAttribute("aria-label", "Elapsed time by date, one dot per drive; dashed lines mark best and average");

    const dark = !!(this._hass && this._hass.themes && this._hass.themes.darkMode);
    const gridColor = dark ? "rgba(255,255,255,0.12)" : "rgba(0,0,0,0.08)";
    const avgColor = dark ? "#bdbdbd" : "#616161";
    const bestColor = dark ? "#66bb6a" : "#2e7d32";
    const slowColor = dark ? "#ef5350" : "#c62828";

    if (layout.yFor) {
      // A few round time ticks down the left, with light grid lines.
      const span = layout.maxY - layout.minY;
      const step = [30, 60, 120, 300, 600, 900, 1800, 3600].find((s) => span / s <= 5) || 3600;
      for (let v = Math.ceil(layout.minY / step) * step; v <= layout.maxY; v += step) {
        const y = layout.yFor(v);
        svg.appendChild(this._hLine(svgNS, layout, y, gridColor, null));
        svg.appendChild(this._svgText(svgNS, layout.padLeft - 6, y + 3, formatDuration(v), "end", "var(--secondary-text-color)"));
      }
      // Best and average reference lines, labelled at the right end.
      const refs = [
        [stats.fastest_seconds, bestColor, "best"],
        [stats.avg_seconds, avgColor, "avg"],
      ];
      for (const [seconds, color, name] of refs) {
        if (seconds === null || seconds === undefined) continue;
        const y = layout.yFor(seconds);
        svg.appendChild(this._hLine(svgNS, layout, y, color, "5 3"));
        svg.appendChild(
          this._svgText(svgNS, width - layout.padRight, y - 3, `${name} ${formatDuration(seconds)}`, "end", color)
        );
      }
      // First and last dates along the bottom.
      const dateY = height - 6;
      const first = this._routeDetail.drives.reduce((a, b) => ((a.start_ts || 0) <= (b.start_ts || 0) ? a : b));
      const last = this._routeDetail.drives.reduce((a, b) => ((a.start_ts || 0) >= (b.start_ts || 0) ? a : b));
      svg.appendChild(this._svgText(svgNS, layout.padLeft, dateY, formatShortDate(first.start_ts), "start", "var(--secondary-text-color)"));
      if (last !== first) {
        svg.appendChild(
          this._svgText(svgNS, width - layout.padRight, dateY, formatShortDate(last.start_ts), "end", "var(--secondary-text-color)")
        );
      }
    }

    const fastestId = marks.fastest;
    const slowestId = marks.slowest;
    for (const point of layout.points) {
      const circle = document.createElementNS(svgNS, "circle");
      circle.setAttribute("cx", String(point.x));
      circle.setAttribute("cy", String(point.y));
      const isSelected = point.driveId === this._selectedDriveId;
      circle.setAttribute("r", isSelected ? "6" : "4");
      let fill = "var(--secondary-text-color, #9e9e9e)";
      if (this._multi) fill = this._vehicleColor(point.vin, dark);
      if (point.outlier) fill = "var(--warning-color, #ff9800)";
      if (point.driveId === fastestId || point.driveId === slowestId) {
        // Fastest / slowest are outlined (a vehicle-colored dot keeps its color).
        circle.setAttribute("stroke", point.driveId === fastestId ? bestColor : slowColor);
        circle.setAttribute("stroke-width", "2.5");
        if (!this._multi) fill = point.driveId === fastestId ? bestColor : slowColor;
      }
      if (isSelected && !this._multi) fill = "var(--primary-color, #03a9f4)";
      circle.setAttribute("fill", fill);
      if (isSelected) {
        circle.setAttribute("stroke", "var(--card-background-color, #fff)");
        circle.setAttribute("stroke-width", "2");
      }
      circle.style.cursor = "pointer";
      circle.addEventListener("click", () => this._selectDrive(point.driveId));
      circle.setAttribute("tabindex", "0");
      circle.setAttribute("role", "button");
      circle.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          this._selectDrive(point.driveId);
        }
      });
      const driveRow = this._routeDetail.drives.find((d) => driveKey(d) === point.driveId);
      if (driveRow) {
        const who = this._multi ? `${this._vehicleLetter(driveRow.vin)} \u00b7 ` : "";
        const text = driveReadout(driveRow, who);
        const tip = document.createElementNS(svgNS, "title");
        tip.textContent = text;
        circle.appendChild(tip);
        circle.setAttribute("aria-label", text);
        const show = () => {
          if (this._chartReadoutEl) _escapeText(this._chartReadoutEl, text);
        };
        const hide = () => {
          if (this._chartReadoutEl) _escapeText(this._chartReadoutEl, this._chartReadoutIdle);
        };
        circle.addEventListener("pointerenter", show);
        circle.addEventListener("focus", show);
        circle.addEventListener("pointerleave", hide);
        circle.addEventListener("blur", hide);
      }
      svg.appendChild(circle);
    }

    this._chartWrap.appendChild(svg);

    // Readout under the chart: hover or focus a dot to preview its drive;
    // tapping selects the drive, which keeps its readout shown.
    const readout = document.createElement("div");
    readout.className = "rrc-chart-readout";
    readout.setAttribute("aria-live", "polite");
    const selectedRow = this._selectedDrive();
    const idleText = selectedRow
      ? driveReadout(selectedRow, this._multi ? `${this._vehicleLetter(selectedRow.vin)} \u00b7 ` : "")
      : "Hover, focus or tap a dot to see that drive";
    _escapeText(readout, idleText);
    this._chartWrap.appendChild(readout);
    this._chartReadoutEl = readout;
    this._chartReadoutIdle = idleText;
  }

  _vehicleLetter(vin) {
    const vehicle = this._vehicleList.find((v) => v.vin === vin);
    return (vehicle && vehicle.letter) || "";
  }

  /** A vehicle's color for the current theme (neutral gray when unknown). */
  _vehicleColor(vin, dark) {
    const vehicle = this._vehicleList.find((v) => v.vin === vin);
    if (!vehicle) return "#8a8a8a";
    return (dark ? vehicle.color_dark || vehicle.color : vehicle.color) || "#8a8a8a";
  }

  _hLine(svgNS, layout, y, color, dash) {
    const line = document.createElementNS(svgNS, "line");
    line.setAttribute("x1", String(layout.padLeft));
    line.setAttribute("x2", String(layout.width - layout.padRight));
    line.setAttribute("y1", String(y));
    line.setAttribute("y2", String(y));
    line.setAttribute("stroke", color);
    line.setAttribute("stroke-width", "1");
    if (dash) line.setAttribute("stroke-dasharray", dash);
    return line;
  }

  _svgText(svgNS, x, y, text, anchor, fill) {
    const el = document.createElementNS(svgNS, "text");
    el.setAttribute("x", String(x));
    el.setAttribute("y", String(y));
    el.setAttribute("text-anchor", anchor);
    el.setAttribute("font-size", "10");
    el.setAttribute("fill", fill);
    el.textContent = text;
    return el;
  }

  // -- table --------------------------------------------------------------------

  _setSort(key) {
    if (this._sortKey === key) {
      this._sortDir = this._sortDir === "asc" ? "desc" : "asc";
    } else {
      this._sortKey = key;
      this._sortDir = key === "date" ? "desc" : "asc";
    }
    this._renderTable();
  }

  _renderTable() {
    this._tableWrap.textContent = "";
    if (!this._routeDetail || !this._routeDetail.drives.length) return;

    const table = document.createElement("table");
    table.className = "rrc-table";
    const thead = document.createElement("thead");
    const headRow = document.createElement("tr");
    if (this._multi) {
      const carTh = document.createElement("th");
      _escapeText(carTh, "Car");
      carTh.title = COLUMN_TITLES.car;
      headRow.appendChild(carTh);
    }
    for (const [key, label] of SORT_COLUMNS) {
      const th = document.createElement("th");
      const arrow = this._sortKey === key ? (this._sortDir === "asc" ? " ▲" : " ▼") : "";
      _escapeText(th, label + arrow);
      th.addEventListener("click", () => this._setSort(key));
      th.title = `${COLUMN_TITLES[key] || label}. Click to sort.`;
      th.tabIndex = 0;
      th.setAttribute("aria-sort", ariaSortFor(this._sortKey, this._sortDir, key));
      th.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          this._setSort(key);
        }
      });
      headRow.appendChild(th);
    }
    const vsTh = document.createElement("th");
    _escapeText(vsTh, "vs avg");
    vsTh.title = COLUMN_TITLES.vs;
    headRow.appendChild(vsTh);
    thead.appendChild(headRow);
    table.appendChild(thead);

    const tbody = document.createElement("tbody");
    const sorted = sortRouteDrives(this._routeDetail.drives, this._sortKey, this._sortDir);
    for (const drive of sorted) {
      const tr = document.createElement("tr");
      tr.className = driveKey(drive) === this._selectedDriveId ? "selected" : "";
      if (this._multi) {
        const carCell = this._cell(this._vehicleLetter(drive.vin));
        carCell.style.fontWeight = "600";
        carCell.style.color = this._vehicleColor(
          drive.vin,
          !!(this._hass && this._hass.themes && this._hass.themes.darkMode)
        );
        tr.appendChild(carCell);
      }
      tr.appendChild(this._cell(formatShortDate(drive.sort_ts !== null && drive.sort_ts !== undefined ? drive.sort_ts : drive.start_ts)));
      const elapsedCell = this._cell(formatDuration(drive.duration_seconds));
      if (drive.outlier) {
        const flag = document.createElement("span");
        flag.className = "rrc-outlier-flag";
        flag.title = "Excluded from stats as an outlier (took far longer than usual)";
        flag.setAttribute("aria-label", "Outlier, excluded from stats");
        _escapeText(flag, "⚠");
        elapsedCell.appendChild(flag);
      }
      tr.appendChild(elapsedCell);
      tr.appendChild(this._cell(formatDuration(drive.moving_seconds)));
      tr.appendChild(
        this._cell(
          drive.efficiency_mi_kwh !== null && drive.efficiency_mi_kwh !== undefined
            ? drive.efficiency_mi_kwh.toFixed(2)
            : "--"
        )
      );
      tr.appendChild(
        this._cell(drive.temp_f !== null && drive.temp_f !== undefined ? `${Math.round(drive.temp_f)}°` : "--")
      );
      // Compared with the same car's own average on this route.
      const vsPct = drive.vin_vs_avg_pct !== undefined && drive.vin_vs_avg_pct !== null ? drive.vin_vs_avg_pct : drive.vs_avg_pct;
      const vsCell = this._cell(formatVsAvgPct(vsPct));
      vsCell.style.color = colorForVsAvg(vsPct);
      tr.appendChild(vsCell);
      tr.addEventListener("click", () => this._selectDrive(driveKey(drive)));
      tr.tabIndex = 0;
      tr.title = driveReadout(drive, this._multi ? `${this._vehicleLetter(drive.vin)} \u00b7 ` : "");
      tr.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          this._selectDrive(driveKey(drive));
        }
      });
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    this._tableWrap.appendChild(table);
  }

  _cell(text) {
    const td = document.createElement("td");
    _escapeText(td, text);
    return td;
  }

  // -- map --------------------------------------------------------------------

  async _ensureMap() {
    if (this._map) return;
    const [leafletModule, cssText] = await Promise.all([_loadLeaflet(), _loadLeafletCss()]);
    this._leaflet = leafletModule;
    this._leafletStyleEl.textContent = cssText;
    const L = this._leaflet;
    this._map = L.map(this._mapEl, {
      zoomControl: true,
      attributionControl: true,
      maxZoom: MAP_MAX_ZOOM,
    }).setView([0, 0], 2);
    this._applyTileLayer();
    this._applyMapTouchMode(this._stacked);
  }

  _applyTileLayer() {
    if (!this._map) return;
    const L = this._leaflet;
    for (const layer of this._tileLayers) this._map.removeLayer(layer);
    this._tileLayers = [];
    const dark = !!(this._hass && this._hass.themes && this._hass.themes.darkMode);
    const style = BASEMAPS[this._basemap] || BASEMAPS.map;
    for (const [url, maxNativeZoom] of style.layers(dark)) {
      const layer = L.tileLayer(url, {
        maxNativeZoom,
        maxZoom: MAP_MAX_ZOOM,
        attribution: ESRI_ATTRIBUTION,
      }).addTo(this._map);
      this._tileLayers.push(layer);
    }
  }

  _setBasemap(key) {
    if (!BASEMAPS[key] || key === this._basemap) return;
    this._basemap = key;
    try {
      window.localStorage.setItem(BASEMAP_STORAGE_KEY, key);
    } catch (_err) {
      // Not persisted; the choice still applies for this session.
    }
    this._applyTileLayer();
    this._markActiveBasemap();
  }

  _markActiveBasemap() {
    if (!this._basemapEl) return;
    for (const btn of this._basemapEl.querySelectorAll("button")) {
      btn.classList.toggle("active", btn.dataset.basemap === this._basemap);
      btn.setAttribute("aria-pressed", String(btn.dataset.basemap === this._basemap));
    }
  }

  _renderMap(preserveView = false) {
    if (!this._map) return;
    const L = this._leaflet;
    for (const line of this._polylines.values()) this._map.removeLayer(line);
    this._polylines.clear();

    if (!this._routeDetail) return;
    const marks = fastestSlowestKeys(this._routeDetail.drives);
    const bounds = [];
    const dark = !!(this._hass && this._hass.themes && this._hass.themes.darkMode);
    const vehicles = this._vehicleList.map((v) => ({
      ...v,
      color: dark ? v.color_dark || v.color : v.color,
    }));

    // Draw plain/gray lines first, then fastest/slowest, then the selected
    // drive last (on top, thicker), so it never hides under the others. With
    // several vehicles each drive is drawn in its own vehicle's color, the
    // fastest / slowest outlined green / red.
    const styled = this._routeDetail.drives.map((drive) => ({
      drive,
      style: routeDriveStyle(drive, {
        multi: this._multi,
        selectedKey: this._selectedDriveId,
        fastestKey: marks.fastest,
        slowestKey: marks.slowest,
        vehicles,
      }),
    }));
    styled.sort((a, b) => a.style.z - b.style.z);

    for (const { drive, style } of styled) {
      const preview = drive.preview;
      if (!preview || !preview.lat || !preview.lat.length) continue;
      const latlngs = preview.lat.map((lat, i) => [lat, preview.lon[i]]);
      const key = driveKey(drive);
      if (style.casing) {
        const casing = L.polyline(latlngs, {
          color: style.casing.color,
          weight: style.casing.weight,
          opacity: 0.9,
          interactive: false,
        }).addTo(this._map);
        this._polylines.set(`__casing__${key}`, casing);
      }
      const line = L.polyline(latlngs, {
        color: style.color,
        weight: style.weight,
        opacity: style.opacity,
      }).addTo(this._map);
      line.on("click", () => this._selectDrive(key));
      line.bindTooltip(driveReadout(drive, this._multi ? `${this._vehicleLetter(drive.vin)} \u00b7 ` : ""), { sticky: true });
      this._polylines.set(key, line);
      for (const ll of latlngs) bounds.push(ll);
    }

    const startPlace = this._routeDetail.start_place;
    const endPlace = this._routeDetail.end_place;
    if (startPlace && typeof startPlace.lat === "number") {
      const marker = L.circleMarker([startPlace.lat, startPlace.lon], {
        radius: 7,
        color: "#ffffff",
        weight: 2,
        fillColor: "#2e7d32",
        fillOpacity: 1,
      }).addTo(this._map);
      marker.bindTooltip(startPlace.label || "Start");
      this._polylines.set("__start__", marker);
      bounds.push([startPlace.lat, startPlace.lon]);
    }
    if (endPlace && typeof endPlace.lat === "number") {
      const marker = L.circleMarker([endPlace.lat, endPlace.lon], {
        radius: 7,
        color: "#ffffff",
        weight: 2,
        fillColor: "#c62828",
        fillOpacity: 1,
      }).addTo(this._map);
      marker.bindTooltip(endPlace.label || "End");
      this._polylines.set("__end__", marker);
      bounds.push([endPlace.lat, endPlace.lon]);
    }

    if (bounds.length && (!preserveView || this._fittedRouteId !== this._routeDetail.id)) {
      this._fittedRouteId = this._routeDetail.id;
      this._map.fitBounds(bounds, { padding: [40, 40], maxZoom: 16 });
    }
  }
}

if (typeof customElements !== "undefined" && !customElements.get("rivian-routes-card")) {
  customElements.define("rivian-routes-card", RivianRoutesCard);
}
