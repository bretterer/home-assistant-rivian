/**
 * rivian-drive-explorer-card.js
 *
 * The "Drives" tab card: a calendar tree (All time -> years -> months -> days
 * -> segments, where "segment" means one drive within a calendar day) on the
 * left, and a Leaflet map on the right with three modes driven by the tree
 * selection:
 *   - Heat (All time / a year / a month): a road heat map tiled from
 *     `rivian/analytics/heat_tile`.
 *   - Day: every segment of the selected day, speed-colored on one shared
 *     scale, with start/stop/end markers.
 *   - Segment: the selected segment highlighted on its own speed scale, the
 *     day's other segments dimmed to a clickable white line.
 *
 * Backend calls (all via `hass.callWS`, all take `vin`): `rivian/analytics/
 * calendar`, `rivian/analytics/day`, `rivian/analytics/heat`, `rivian/
 * analytics/heat_tile`, `rivian/analytics/drives` (used here only for the
 * storage footer), and `rivian/analytics/subscribe` for live updates.
 *
 * Leaflet is vendored at ./leaflet/leaflet-src.esm.js + leaflet.css and is
 * loaded lazily (dynamic import + a fetched stylesheet injected into this
 * card's shadow root) the first time a card instance actually needs a map,
 * so dashboards that don't use this card never pay for Leaflet.
 *
 * A number of pure helpers are exported as named exports purely so a Node
 * smoke test (tests/frontend/explorer_card.test.mjs) can import and exercise
 * them without a DOM or customElements environment. The module guards every
 * top-level use of HTMLElement/customElements/window/document so it can be
 * imported under plain Node.
 */

const MPS_TO_MPH = 2.236936;

// Continuous route color by speed, scaled to each drive (or day): red when
// stopped, through orange (slow) and yellow (medium), to green at the scale's
// top speed. [fraction, [r, g, b]]
const SPEED_COLOR_STOPS = [
  [0, [211, 47, 47]],
  [0.3, [245, 124, 0]],
  [0.57, [251, 192, 45]],
  [0.79, [156, 204, 101]],
  [1, [46, 160, 67]],
];
// The top of a scale is its 98th-percentile speed, so a single GPS spike
// can't squash the rest of the route into red; the floor keeps a parking-lot
// shuffle from painting everything green.
const SPEED_SCALE_PERCENTILE = 0.98;
const SPEED_SCALE_MIN_MPH = 5;
const NEUTRAL_COLOR = "#888888";

// Heat map color scale: a continuous blue -> green -> yellow -> red ramp on a
// log scale of drive count, so a handful of well-worn commute roads don't
// wash out every side street into the same color.
const HEAT_COLOR_STOPS = [
  [0, [37, 99, 235]],
  [0.4, [22, 163, 74]],
  [0.7, [250, 204, 21]],
  [1, [220, 38, 38]],
];

const MONTH_NAMES = [
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December",
];
const MONTH_ABBR = MONTH_NAMES.map((m) => m.slice(0, 3));
const WEEKDAY_ABBR = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

// Esri's public basemaps need no API key (CARTO's free tiles now return an
// "API key required" watermark). Each style is a base layer plus an optional
// label layer; maxNativeZoom is the deepest level Esri actually has imagery
// for, and Leaflet upscales beyond it instead of showing "no data" tiles.
const ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services";
const ESRI_ATTRIBUTION =
  'Tiles &copy; Esri | Map data &copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors';
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
const BASEMAP_STORAGE_KEY = "rivian-drive-explorer-basemap";
const MAP_MAX_ZOOM = 20;
const STACK_BREAKPOINT_PX = 700;
/** One-shot "open this drive" request left by another card (see `rivian-efficiency-card.js`). */
const OPEN_REQUEST_KEY = "rivian-drive-explorer-open";
/** An open request older than this is ignored (the user went elsewhere first). */
const OPEN_REQUEST_MAX_AGE_MS = 120000;
const HEAT_TILE_SIZE = 256;
// Each neighbour pair once: right, down, down-right, down-left.
const HEAT_NEIGHBOUR_OFFSETS = [
  [1, 0],
  [0, 1],
  [1, 1],
  [-1, 1],
];
const HEAT_LINE_MIN_PX = 2.5;
const HEAT_LINE_MAX_PX = 9;
// Cells fetched around each tile so lines join across tile edges.
const HEAT_TILE_MARGIN = 1;

/** Read the basemap this browser last chose, falling back to the config default. */
function _initialBasemap(configDefault) {
  try {
    const saved = window.localStorage.getItem(BASEMAP_STORAGE_KEY);
    if (saved && BASEMAPS[saved]) return saved;
  } catch (_err) {
    // Storage can be unavailable (private mode, blocked site data).
  }
  return BASEMAPS[configDefault] ? configDefault : "map";
}

const _hex = (v) => Math.round(Math.max(0, Math.min(255, v))).toString(16).padStart(2, "0");

function _lerpColor(stops, frac) {
  const clamped = Math.max(0, Math.min(1, frac));
  for (let i = 1; i < stops.length; i++) {
    const [hiFrac, hi] = stops[i];
    if (clamped <= hiFrac) {
      const [loFrac, lo] = stops[i - 1];
      const span = hiFrac - loFrac;
      const f = span > 0 ? (clamped - loFrac) / span : 0;
      return [0, 1, 2].map((c) => lo[c] + (hi[c] - lo[c]) * f);
    }
  }
  return stops[stops.length - 1][1];
}

const _mphAt = (track, i) => {
  const mps = Array.isArray(track.speed_mps) ? track.speed_mps[i] : null;
  return mps === null || mps === undefined || Number.isNaN(mps) ? null : mps * MPS_TO_MPH;
};

function _percentileTopMph(speeds) {
  if (!speeds.length) return SPEED_SCALE_MIN_MPH;
  const sorted = speeds.slice().sort((a, b) => a - b);
  const top = sorted[Math.min(sorted.length - 1, Math.floor(SPEED_SCALE_PERCENTILE * sorted.length))];
  return Math.max(SPEED_SCALE_MIN_MPH, Math.round(top));
}

/** Top of a single track's color scale in whole mph (see SPEED_SCALE_PERCENTILE). */
export function speedScaleMax(track) {
  const speeds = [];
  for (let i = 0; track && Array.isArray(track.lat) && i < track.lat.length; i++) {
    const mph = _mphAt(track, i);
    if (mph !== null) speeds.push(mph);
  }
  return _percentileTopMph(speeds);
}

/**
 * Top of one *shared* color scale across every segment's track in a day, so
 * a day's drives can all be compared on the map at once.
 */
export function dayScaleMax(segments) {
  const speeds = [];
  for (const seg of segments || []) {
    const track = seg && seg.track;
    if (!track || !Array.isArray(track.lat)) continue;
    for (let i = 0; i < track.lat.length; i++) {
      const mph = _mphAt(track, i);
      if (mph !== null) speeds.push(mph);
    }
  }
  return _percentileTopMph(speeds);
}

/** Color for a speed on a 0..maxMph scale (red to green); gray when unknown. */
export function speedColor(speedMph, maxMph) {
  if (speedMph === null || speedMph === undefined || Number.isNaN(speedMph)) {
    return NEUTRAL_COLOR;
  }
  const frac = speedMph / Math.max(maxMph, SPEED_SCALE_MIN_MPH);
  const [r, g, b] = _lerpColor(SPEED_COLOR_STOPS, frac);
  return `#${_hex(r)}${_hex(g)}${_hex(b)}`;
}

/** CSS linear-gradient matching speedColor, for the legend bar. */
export function speedGradientCss() {
  const stops = SPEED_COLOR_STOPS.map(
    ([frac, [r, g, b]]) => `rgb(${r}, ${g}, ${b}) ${frac * 100}%`
  );
  return `linear-gradient(to right, ${stops.join(", ")})`;
}

/**
 * Color for a heat-map cell's drive count on a log scale of 1..scaleMax,
 * continuous blue -> green -> yellow -> red; null (transparent) for zero.
 */
/**
 * Turn a heat tile's cells into round-capped strokes, so roads draw as
 * continuous lines instead of square blocks: a dot on every cell plus a line
 * to each of its 8 neighbours. A line carries the cooler count of its two
 * ends, and strokes are sorted coolest first, so a busy road paints over the
 * side streets that join it instead of bleeding its color into them.
 * `cells` is `[[dx, dy, count], ...]` from `heat_tile` (it may include a
 * margin ring outside 0..size-1); coordinates returned are cell centers, in
 * cell units.
 */
export function heatStrokes(cells) {
  const counts = new Map();
  for (const [dx, dy, count] of cells || []) counts.set(`${dx},${dy}`, count);
  const strokes = [];
  for (const [dx, dy, count] of cells || []) {
    const x0 = dx + 0.5;
    const y0 = dy + 0.5;
    strokes.push({ value: count, x0, y0, x1: x0, y1: y0 });
    for (const [ox, oy] of HEAT_NEIGHBOUR_OFFSETS) {
      const other = counts.get(`${dx + ox},${dy + oy}`);
      if (other === undefined) continue;
      strokes.push({ value: Math.min(count, other), x0, y0, x1: x0 + ox, y1: y0 + oy });
    }
  }
  strokes.sort((a, b) => a.value - b.value);
  return strokes;
}

/** Stroke width (px) for heat lines: a little wider than a cell, capped. */
export function heatLineWidth(cellPx) {
  return Math.min(HEAT_LINE_MAX_PX, Math.max(HEAT_LINE_MIN_PX, cellPx * 1.25));
}

export function heatColor(count, scaleMax) {
  if (!count || count <= 0) return null;
  const max = Math.max(scaleMax || 1, 2);
  const t = Math.log(Math.max(count, 1)) / Math.log(max);
  const [r, g, b] = _lerpColor(HEAT_COLOR_STOPS, t);
  return `rgb(${Math.round(r)}, ${Math.round(g)}, ${Math.round(b)})`;
}

/** CSS linear-gradient matching heatColor, for the heat legend bar. */
export function heatGradientCss() {
  const stops = HEAT_COLOR_STOPS.map(
    ([frac, [r, g, b]]) => `rgb(${r}, ${g}, ${b}) ${frac * 100}%`
  );
  return `linear-gradient(to right, ${stops.join(", ")})`;
}

/**
 * Split a track into polyline pieces colored continuously by speed.
 *
 * `track` is the `{t, lat, lon, speed_mps, ...}` columnar payload shape from
 * DriveTrack.to_payload(). Each piece between two fixes is colored by their
 * mean speed (whole mph, so the gradient is smooth to the eye) on a 0..maxMph
 * scale, and consecutive pieces of the same color are merged. Pieces share
 * boundary points, so the line has no gaps.
 * Returns `[{color, points: [[lat, lon], ...]}]`.
 */
export function speedPieces(track, maxMph) {
  if (!track || !Array.isArray(track.lat) || track.lat.length < 2) return [];

  const pieces = [];
  let current = null;
  for (let i = 1; i < track.lat.length; i++) {
    const known = [_mphAt(track, i - 1), _mphAt(track, i)].filter((v) => v !== null);
    const mph = known.length ? known.reduce((a, b) => a + b, 0) / known.length : null;
    const color = speedColor(mph === null ? null : Math.round(mph), maxMph);
    const next = [track.lat[i], track.lon[i]];
    if (current && current.color === color) {
      current.points.push(next);
    } else {
      current = { color, points: [[track.lat[i - 1], track.lon[i - 1]], next] };
      pieces.push(current);
    }
  }
  return pieces;
}

const METERS_TO_MILES = 1 / 1609.344;

// Efficiency "rolling window" method: at each track point, look back for the
// most recent point whose SoC was at least this many percentage points
// higher, and divide the distance traveled by the energy used between them.
// Kept as its own constant so it's trivial to retune (see the dataviz
// comparison screenshots taken at 0.3 and 0.5).
export const ROLLING_EFF_MIN_SOC_DROP_PCT = 0.3;

function _haversineMiles(lat1, lon1, lat2, lon2) {
  const R_MI = 3958.7613;
  const toRad = (d) => (d * Math.PI) / 180;
  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);
  const a =
    Math.sin(dLat / 2) ** 2 + Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
  return 2 * R_MI * Math.asin(Math.min(1, Math.sqrt(a)));
}

/** The key `continuousDriveSeries` tags a `prior_tail` part's points with. */
export const PRIOR_TAIL_KEY = "__prior_tail__";

/** Fallback pack size (kWh) when a segment/tail carries no `battery_capacity_kwh`. */
export const DEFAULT_BATTERY_CAPACITY_KWH = 135;

/** Distance (miles) between two adjacent track points: `odo_m` delta if both have it, else haversine. */
function _stepMiles(track, i) {
  const odo = track.odo_m || [];
  if (odo[i - 1] !== null && odo[i - 1] !== undefined && odo[i] !== null && odo[i] !== undefined) {
    return Math.abs(odo[i] - odo[i - 1]) * METERS_TO_MILES;
  }
  const lat = track.lat || [];
  const lon = track.lon || [];
  if (lat[i - 1] == null || lon[i - 1] == null || lat[i] == null || lon[i] == null) return 0;
  return _haversineMiles(lat[i - 1], lon[i - 1], lat[i], lon[i]);
}

/**
 * Concatenate a chronological sequence of drive tracks into one continuous
 * series for cross-drive rolling-efficiency and 3-min-chunk math, so a
 * drive's first minutes aren't blank just because it hasn't yet dropped
 * enough SoC on its own.
 *
 * `parts` is `[{key, track}, ...]` in chronological order -- typically a
 * `prior_tail` part (`key: PRIOR_TAIL_KEY`) followed by one or more drives'
 * tracks (`key`: that drive's index). A part with no track, or an empty
 * one, is skipped.
 *
 * Returns `{t, lat, lon, soc, distance_m, partOf, ranges}`:
 *  - `soc` is rebased so a parked gap between parts (charging or vampire
 *    drain) never counts as driving energy: each part after the first has
 *    its SoC series shifted so its first known value continues from the
 *    previous part's last known value.
 *  - `distance_m` is cumulative *driving* distance in meters: it
 *    accumulates within a part (`odo_m` delta when both ends have it, else
 *    haversine) and carries the running total across a part boundary
 *    without ever adding the parked-gap distance itself.
 *  - `partOf[i]` is the source part's `key` for continuous index `i`.
 *  - `ranges` maps each part's `key` to `[startIndex, endIndexExclusive)`
 *    in the continuous arrays (a part's own point `i` is at
 *    `ranges[key][0] + i`).
 */
export function continuousDriveSeries(parts) {
  const t = [];
  const lat = [];
  const lon = [];
  const soc = [];
  const distance_m = [];
  const partOf = [];
  const ranges = {};

  let socOffset = 0;
  let lastKnownSoc = null;
  let distanceOffset = 0;
  let haveParts = false;

  for (const part of parts || []) {
    const track = part && part.track;
    const n = track && Array.isArray(track.t) ? track.t.length : 0;
    if (!n) continue;

    const rawSoc = track.soc || [];
    let firstRawSoc = null;
    for (let i = 0; i < n; i++) {
      if (rawSoc[i] !== null && rawSoc[i] !== undefined) {
        firstRawSoc = rawSoc[i];
        break;
      }
    }
    if (haveParts && firstRawSoc !== null && lastKnownSoc !== null) {
      socOffset = lastKnownSoc - firstRawSoc;
    } else if (!haveParts) {
      socOffset = 0;
    } // else: no overlap data to rebase against -- keep the running offset.

    const start = t.length;
    let localDistance = 0;
    for (let i = 0; i < n; i++) {
      t.push(track.t[i]);
      lat.push(track.lat ? track.lat[i] : null);
      lon.push(track.lon ? track.lon[i] : null);
      const rs = rawSoc[i];
      const rebased = rs === null || rs === undefined ? null : rs + socOffset;
      if (rebased !== null) lastKnownSoc = rebased;
      soc.push(rebased);
      if (i > 0) localDistance += _stepMiles(track, i) / METERS_TO_MILES;
      distance_m.push(distanceOffset + localDistance);
      partOf.push(part.key);
    }
    ranges[part.key] = [start, t.length];
    distanceOffset += localDistance;
    haveParts = true;
  }

  return { t, lat, lon, soc, distance_m, partOf, ranges };
}

/**
 * Rolling-window efficiency (mi/kWh) over a `continuousDriveSeries` result:
 * for continuous index `i`, walk back to the most recent index `j` whose
 * (rebased) SoC was at least `minSocDropPct` points higher -- possibly in
 * an earlier part -- and divide the distance traveled between `j` and `i`
 * by the energy that drop represents. `capacityKwh` is a constant number,
 * or a `(i) => number` function for a series spanning parts with different
 * pack sizes (the ending index `i`'s own capacity is used). Null wherever
 * no such `j` exists yet or SoC/capacity is missing.
 */
export function rollingEfficiencySeries(series, capacityKwh, minSocDropPct = ROLLING_EFF_MIN_SOC_DROP_PCT) {
  const n = series && Array.isArray(series.t) ? series.t.length : 0;
  const out = new Array(n).fill(null);
  if (!n) return out;
  const capacityAt = typeof capacityKwh === "function" ? capacityKwh : () => capacityKwh;
  const soc = series.soc;
  const dist = series.distance_m;

  for (let i = 0; i < n; i++) {
    const capacity = capacityAt(i);
    if (typeof capacity !== "number" || !(capacity > 0)) continue;
    if (soc[i] === null || soc[i] === undefined) continue;
    let j = -1;
    for (let k = i - 1; k >= 0; k--) {
      if (soc[k] === null || soc[k] === undefined) continue;
      if (soc[k] - soc[i] >= minSocDropPct) {
        j = k;
        break;
      }
    }
    if (j < 0) continue;
    const energyKwh = ((soc[j] - soc[i]) / 100) * capacity;
    if (!(energyKwh > 0)) continue;
    const distMiles = Math.abs(dist[i] - dist[j]) * METERS_TO_MILES;
    out[i] = distMiles / energyKwh;
  }
  return out;
}

/**
 * Rolling-window efficiency at every point of a single track, in mi/kWh.
 * A thin wrapper around `continuousDriveSeries`/`rollingEfficiencySeries`
 * for a lone drive with no cross-drive context (e.g. the map's route
 * coloring). Distance comes from `odo_m` when both ends have it, else
 * summed haversine over the intervening fixes.
 */
export function rollingEfficiency(track, capacityKwh, minSocDropPct = ROLLING_EFF_MIN_SOC_DROP_PCT) {
  const series = continuousDriveSeries([{ key: 0, track }]);
  return rollingEfficiencySeries(series, capacityKwh, minSocDropPct);
}

const CHUNK_WINDOW_SECONDS = 180;

/**
 * "3-min chunks" efficiency steps computed client-side from a
 * `continuousDriveSeries` result: consecutive 3-minute windows of *driving*
 * time (a part boundary's parked gap doesn't count towards the 3 minutes,
 * so a window can straddle a drive boundary), merging a window whose
 * (rebased) SoC didn't drop into the next until one does.
 * `capacityFor(i)` returns the pack size (kWh) to use for the chunk ending
 * at continuous index `i`. Returns `[{t0, t1, value, seg}]` -- a chunk that
 * spans more than one part emits one piece per part (same `value`, split at
 * the real timestamp boundary) so the day timeline can place each piece on
 * its own drive; pieces falling in a `PRIOR_TAIL_KEY` part are dropped
 * (there's nothing on screen to draw them against).
 */
export function continuousChunkSteps(series, capacityFor) {
  const n = series && Array.isArray(series.t) ? series.t.length : 0;
  const steps = [];
  if (n < 2) return steps;

  const rawWindows = [];
  let windowStart = 0;
  let accum = 0;
  for (let i = 1; i < n; i++) {
    const dt = series.partOf[i] === series.partOf[i - 1] ? series.t[i] - series.t[i - 1] : 0;
    accum += dt;
    if (accum >= CHUNK_WINDOW_SECONDS) {
      rawWindows.push([windowStart, i]);
      windowStart = i;
      accum = 0;
    }
  }
  if (windowStart < n - 1) rawWindows.push([windowStart, n - 1]);

  const merged = [];
  let cur = null;
  for (const [s, e] of rawWindows) {
    cur = cur === null ? [s, e] : [cur[0], e];
    const soc0 = series.soc[cur[0]];
    const soc1 = series.soc[cur[1]];
    const drop = soc0 !== null && soc0 !== undefined && soc1 !== null && soc1 !== undefined ? soc0 - soc1 : null;
    if (drop !== null && drop > 0) {
      merged.push(cur);
      cur = null;
    }
  }
  if (cur !== null) merged.push(cur);

  const capFn = typeof capacityFor === "function" ? capacityFor : () => capacityFor;
  for (const [s, e] of merged) {
    const soc0 = series.soc[s];
    const soc1 = series.soc[e];
    const drop = soc0 !== null && soc0 !== undefined && soc1 !== null && soc1 !== undefined ? soc0 - soc1 : null;
    let value = null;
    if (drop !== null && drop > 0) {
      const distMiles = Math.abs(series.distance_m[e] - series.distance_m[s]) * METERS_TO_MILES;
      const capacity = capFn(e);
      if (typeof capacity === "number" && capacity > 0) {
        const energyKwh = (drop / 100) * capacity;
        value = energyKwh > 0 ? distMiles / energyKwh : null;
      }
    }

    let pieceStart = s;
    for (let i = s + 1; i <= e; i++) {
      if (i === e || series.partOf[i] !== series.partOf[pieceStart]) {
        const key = series.partOf[pieceStart];
        if (key !== PRIOR_TAIL_KEY) {
          steps.push({ t0: series.t[pieceStart], t1: series.t[i], value, seg: key });
        }
        pieceStart = i;
      }
    }
  }
  return steps;
}

/** Step-line pieces for the "3-min chunks" efficiency view: `[{t0, t1, value}]`. */
export function chunkSteps(chunks) {
  const out = [];
  for (const c of chunks || []) {
    if (!c || typeof c.start_ts !== "number") continue;
    const duration = typeof c.duration_seconds === "number" ? c.duration_seconds : 0;
    const value = typeof c.efficiency_mi_kwh === "number" ? c.efficiency_mi_kwh : null;
    out.push({ t0: c.start_ts, t1: c.start_ts + duration, value });
  }
  return out;
}

/** Binary search a track's ascending `t` array for the index nearest to `t`. */
export function nearestPointIndex(track, t) {
  const arr = track && Array.isArray(track.t) ? track.t : null;
  if (!arr || !arr.length) return -1;
  let lo = 0;
  let hi = arr.length - 1;
  if (t <= arr[0]) return 0;
  if (t >= arr[hi]) return hi;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (arr[mid] === t) return mid;
    if (arr[mid] < t) lo = mid + 1;
    else hi = mid;
  }
  const before = Math.max(0, lo - 1);
  return t - arr[before] <= arr[lo] - t ? before : lo;
}

// Sequential blue ramp (palette.md steps 100 / 350 / 550 / 700): low->high elevation.
const ELEVATION_COLOR_STOPS = [
  [0, [205, 226, 251]],
  [0.33, [85, 152, 231]],
  [0.66, [28, 92, 171]],
  [1, [13, 54, 107]],
];

/** Sequential elevation color: low (light blue) -> high (dark blue); gray for unknown. */
export function elevationColor(value, min, max) {
  if (value === null || value === undefined || Number.isNaN(value)) return NEUTRAL_COLOR;
  const span = max - min;
  const frac = span > 0 ? (value - min) / span : 0.5;
  const [r, g, b] = _lerpColor(ELEVATION_COLOR_STOPS, frac);
  return `#${_hex(r)}${_hex(g)}${_hex(b)}`;
}

// The fixed status scale (palette.md), read low->high: critical -> serious ->
// warning -> good. Efficiency is a performance judgment (worse/better), which
// is what that scale means, unlike the plain speed/elevation magnitudes.
const EFFICIENCY_COLOR_STOPS = [
  [0, [208, 59, 59]],
  [0.33, [236, 131, 90]],
  [0.66, [250, 178, 25]],
  [1, [12, 163, 12]],
];

/** Efficiency color: low (red) -> high (green) on a robust range; gray for unknown. */
export function efficiencyColor(value, min, max) {
  if (value === null || value === undefined || Number.isNaN(value)) return NEUTRAL_COLOR;
  const span = max - min;
  const frac = span > 0 ? (value - min) / span : 0.5;
  const [r, g, b] = _lerpColor(EFFICIENCY_COLOR_STOPS, frac);
  return `#${_hex(r)}${_hex(g)}${_hex(b)}`;
}

const TIMELINE_GAP_PX_DEFAULT = 6;

/**
 * Lay a day's drives back to back on a shared pixel axis: each drive's width
 * proportional to its own real duration, with a fixed-width break standing
 * in for every gap between them (so a two-hour lunch stop doesn't dwarf a
 * five-minute errand). `segments` is any chronological array of
 * `{start_ts, end_ts, drive_id}` -- a single-entry array works for a
 * segment/drive view. `tToX`/`xToT` stay monotonic across breaks: a break's
 * real-time span, however long, maps onto exactly its fixed pixel width.
 */
export function dayTimeline(segments, gapPx = TIMELINE_GAP_PX_DEFAULT, widthPx = 0) {
  const segs = (segments || []).filter(
    (s) => s && typeof s.start_ts === "number" && typeof s.end_ts === "number" && s.end_ts >= s.start_ts
  );
  if (!segs.length) {
    return {
      totalPx: Math.max(0, widthPx),
      pieces: [],
      breaks: [],
      tToX: () => 0,
      xToT: () => null,
    };
  }
  const gapCount = segs.length - 1;
  const totalGapPx = gapCount * gapPx;
  const plotPx = Math.max(1, widthPx - totalGapPx);
  const totalDuration = segs.reduce((sum, s) => sum + Math.max(0, s.end_ts - s.start_ts), 0);
  const pieces = [];
  const breaks = [];
  const knotsT = [];
  const knotsX = [];
  let x = 0;
  segs.forEach((seg, i) => {
    const dur = Math.max(0, seg.end_ts - seg.start_ts);
    const w = totalDuration > 0 ? (dur / totalDuration) * plotPx : plotPx / segs.length;
    const x0 = x;
    const x1 = x + w;
    pieces.push({ index: i, driveId: seg.drive_id, startTs: seg.start_ts, endTs: seg.end_ts, x0, x1 });
    knotsT.push(seg.start_ts, seg.end_ts);
    knotsX.push(x0, x1);
    x = x1;
    if (i < segs.length - 1) {
      const bx0 = x;
      const bx1 = x + gapPx;
      breaks.push({ afterIndex: i, beforeIndex: i + 1, x0: bx0, x1: bx1 });
      x = bx1;
    }
  });
  const totalPx = x;
  const lastKnot = knotsT.length - 1;

  function tToX(t) {
    if (t <= knotsT[0]) return knotsX[0];
    if (t >= knotsT[lastKnot]) return knotsX[lastKnot];
    for (let i = 1; i < knotsT.length; i++) {
      if (t <= knotsT[i]) {
        const t0 = knotsT[i - 1];
        const t1 = knotsT[i];
        const x0 = knotsX[i - 1];
        const x1 = knotsX[i];
        const frac = t1 > t0 ? (t - t0) / (t1 - t0) : 0;
        return x0 + frac * (x1 - x0);
      }
    }
    return knotsX[lastKnot];
  }

  function xToT(xPos) {
    if (xPos <= knotsX[0]) return knotsT[0];
    if (xPos >= knotsX[lastKnot]) return knotsT[lastKnot];
    for (let i = 1; i < knotsX.length; i++) {
      if (xPos <= knotsX[i]) {
        const x0 = knotsX[i - 1];
        const x1 = knotsX[i];
        const t0 = knotsT[i - 1];
        const t1 = knotsT[i];
        const frac = x1 > x0 ? (xPos - x0) / (x1 - x0) : 0;
        return t0 + frac * (t1 - t0);
      }
    }
    return knotsT[lastKnot];
  }

  return { totalPx, pieces, breaks, tToX, xToT };
}

/** Format a duration in seconds as "1:23" (h:mm) or "45 min". */
export function formatDuration(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return "–";
  const totalMinutes = Math.round(seconds / 60);
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, "0")}`;
  }
  return `${minutes} min`;
}

/** Hover/tap explanations for the stat tiles, by label. */
export const STAT_TILE_TITLES = {
  Distance: "Total distance driven",
  Drives: "Number of recorded drives (very short moves are not counted)",
  "Driving time": "Total time spent driving",
  Energy: "Battery energy used, in kilowatt-hours (kWh)",
  Efficiency: "Miles per kilowatt-hour (mi/kWh): higher is better",
  "With route": "Drives that have a stored GPS route (only these appear on the heat map)",
  Stops: "Times the car stopped for 20 seconds or more mid-drive (not counting the very start and end)",
  "Day start": "When the first drive of the day began",
  "Day end": "When the last drive of the day ended",
  "Moving time": "Time spent actually moving (stopped time excluded)",
  Climb: "Total uphill climbed, with GPS noise filtered out",
  Duration: "Total time from start to end of the drive, including stops",
  "Avg speed": "Distance divided by total duration, stops included",
  "Max speed": "Highest speed the car reported during the drive",
  MPGe: "Miles per gallon equivalent (33.7 kWh = 1 gallon): higher is better",
  Temp: "Outside temperature during the drive, from weather data",
  "Elevation \u0394": "Elevation at the end of the drive minus the start (negative = ended lower)",
  SoC: "Battery percentage (state of charge) at the start and end of the drive",
  Start: "When the drive started",
  End: "When the drive ended",
  "Climb / Descent": "Total uphill and downhill, in feet, with GPS noise filtered out",
  "Route max speed": "Maximum speed from the GPS route (99th percentile, so brief GPS spikes are ignored)",
  "Range used": "Estimated driving range, in miles, the car used up during the drive",
  "Drive mode": "Drive mode(s) selected during the drive",
  Driver: "The driver profile that was active",
  Trailer: "Whether the car reported a trailer attached",
  Route: "Rank among your drives on this repeated route (a favorite drive), and how it compares with that route's average time",
  Vehicle: "Which vehicle made this drive",
  From: "Where the drive started (a named place, when known)",
  To: "Where the drive ended (a named place, when known)",
};

/** The title for a stat tile by its label ("Busiest month \u00b7 80 mi" matches by prefix). */
export function statTileTitle(label) {
  const text = String(label === null || label === undefined ? "" : label);
  if (Object.hasOwn(STAT_TILE_TITLES, text)) return STAT_TILE_TITLES[text];
  if (text.startsWith("Busiest")) return "The period with the most miles driven inside this selection";
  return "";
}

/** Hover/tap explanations for the route color toggle. */
export const ROUTE_COLOR_TITLES = {
  speed: "Color the route by speed (red = slow, green = fast)",
  elevation: "Color the route by elevation above sea level",
  efficiency: "Color the route by efficiency in mi/kWh (red = low, green = high)",
};

/** Hover/tap explanations for the efficiency method toggle. */
export const EFF_METHOD_TITLES = {
  chunks: "Efficiency of each fixed 3-minute piece of the drive, as a step line",
  rolling: "Efficiency over the last stretch with at least a 0.3% battery drop, computed in the card",
  model: "Estimate from the energy model fitted to your drives; dots are the measured battery-step values",
};

/**
 * Move a chart cursor from `x` by `delta` steps of `stepPx` pixels, clamped
 * to [0, plotWidth] (keyboard stepping for the time charts).
 */
export function stepChartX(x, delta, plotWidth, stepPx = 8) {
  const next = (Number.isFinite(x) ? x : 0) + delta * stepPx;
  return Math.max(0, Math.min(plotWidth, next));
}

/**
 * The heat count under a point of a heat tile: `counts` is a Map of
 * "dx,dy" -> passes, (fx, fy) the point in cell units. Takes the busiest cell
 * whose centre is within one cell of the point (roads draw a little wider than
 * one cell), or 0.
 */
export function heatCountAt(counts, fx, fy) {
  if (!counts || !Number.isFinite(fx) || !Number.isFinite(fy)) return 0;
  const cx = Math.floor(fx);
  const cy = Math.floor(fy);
  let best = 0;
  for (let ox = -1; ox <= 1; ox++) {
    for (let oy = -1; oy <= 1; oy++) {
      const n = counts.get(`${cx + ox},${cy + oy}`);
      if (!n) continue;
      const d = Math.hypot(cx + ox + 0.5 - fx, cy + oy + 0.5 - fy);
      if (d <= 1 && n > best) best = n;
    }
  }
  return best;
}

/** The heat map tooltip: "Driven 12 times" (passes, so an out-and-back counts twice). */
export function heatTipText(count) {
  if (!count || count < 1) return "";
  return count === 1 ? "Driven 1 time" : `Driven ${count.toLocaleString()} times`;
}

/** "#3 of 15 · 8% faster than avg" for a segment's `route` field (see analytics_db.day()). */
export function _routeTileText(route) {
  if (!route) return "–";
  const rankText = route.rank ? `#${route.rank} of ${route.count}` : `${route.count} drives`;
  if (route.vs_avg_pct === null || route.vs_avg_pct === undefined) return rankText;
  // vs_avg_pct is a percentage of average elapsed time; approximate the
  // seconds delta isn't available here, so word it by percentage instead
  // when no absolute delta is provided by the backend.
  const direction = route.vs_avg_pct < 0 ? "faster" : "slower";
  return `${rankText} · ${Math.abs(Math.round(route.vs_avg_pct))}% ${direction} than avg`;
}

/** Format a duration in seconds as "1 h 12 min" or "12 min", for map tooltips. */
/** "0.8 mi" / "450 ft" for a stretch that wasn't recorded. */
export function formatGapDistance(meters) {
  if (typeof meters !== "number" || !isFinite(meters)) return "distance unknown";
  const miles = meters / 1609.344;
  return miles >= 0.1 ? `${miles.toFixed(1)} mi` : `${Math.round(meters * 3.28084)} ft`;
}

export function formatParkedDuration(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return "Parked";
  const totalMinutes = Math.max(0, Math.round(seconds / 60));
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  return hours > 0 ? `Parked ${hours} h ${minutes} min` : `Parked ${minutes} min`;
}

/** Format a byte count as a human string, e.g. "3.4 MB". */
export function formatBytes(bytes) {
  if (bytes === null || bytes === undefined || Number.isNaN(bytes)) return "–";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex++;
  }
  return `${value.toFixed(1)} ${units[unitIndex]}`;
}

/** "September" from a "YYYY-MM" calendar key, without any time-zone shift. */
export function formatMonthLabel(monthKey) {
  if (typeof monthKey !== "string") return String(monthKey);
  const month = Number(monthKey.slice(5, 7));
  return MONTH_NAMES[month - 1] || monthKey;
}

/** "Tue, Sep 23" from a "YYYY-MM-DD" calendar key, without any time-zone shift. */
export function formatDayLabel(dayKey) {
  if (typeof dayKey !== "string") return String(dayKey);
  const year = Number(dayKey.slice(0, 4));
  const month = Number(dayKey.slice(5, 7));
  const day = Number(dayKey.slice(8, 10));
  if (!year || !month || !day) return dayKey;
  // A pure day-of-week computation (Sakamoto's algorithm) avoids any
  // Date/Intl time-zone interpretation of the key.
  const t = [0, 3, 2, 5, 0, 3, 5, 1, 4, 6, 2, 4];
  let y = year;
  if (month < 3) y -= 1;
  const weekday =
    (y + Math.floor(y / 4) - Math.floor(y / 100) + Math.floor(y / 400) + t[month - 1] + day) % 7;
  return `${WEEKDAY_ABBR[weekday]}, ${MONTH_ABBR[month - 1]} ${day}`;
}

/**
 * The `window.confirm` text for deleting one drive ("segment"), e.g.
 * "Delete the 3:35 PM drive on Wed, Sep 16 (6.1 mi)? Its route, stats and
 * efficiency data are removed. This can't be undone."
 */
export function deleteDriveMessage(seg, tz) {
  const time = _timeLabel(seg && seg.start_ts, tz);
  const dayKey = _dayKeyFromTs(seg && seg.start_ts, tz);
  const dateLabel = dayKey ? formatDayLabel(dayKey) : "–";
  const miles = typeof (seg && seg.distance_miles) === "number" ? seg.distance_miles.toFixed(1) : "?";
  return `Delete the ${time} drive on ${dateLabel} (${miles} mi)? Its route, stats and efficiency data are removed. This can't be undone.`;
}

/** The `window.confirm` text for deleting a whole day, e.g. "Delete all 7 drives on Wed, Sep 16?" */
export function deleteDayMessage(dayData, _tz) {
  const count = ((dayData && dayData.segments) || []).length;
  const label = formatDayLabel(dayData && dayData.date);
  return `Delete all ${count} drives on ${label}?`;
}

/** The local "YYYY-MM-DD" calendar day an epoch timestamp falls on, in `tz`. */
function _dayKeyFromTs(ts, tz) {
  if (ts === null || ts === undefined || Number.isNaN(ts)) return null;
  try {
    const parts = new Intl.DateTimeFormat("en-CA", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      timeZone: tz,
    }).formatToParts(new Date(ts * 1000));
    const get = (type) => parts.find((p) => p.type === type).value;
    return `${get("year")}-${get("month")}-${get("day")}`;
  } catch (_err) {
    return null;
  }
}

/** "Sep 6" from a "YYYY-MM-DD" calendar key -- formatDayLabel() without the weekday, for narrow stat tiles. */
function _shortDayLabel(dayKey) {
  if (typeof dayKey !== "string") return String(dayKey);
  const month = Number(dayKey.slice(5, 7));
  const day = Number(dayKey.slice(8, 10));
  if (!month || !day) return dayKey;
  return `${MONTH_ABBR[month - 1]} ${day}`;
}

// Markers closer than this share one number badge ("4, 7"), or sit beside
// the green/red marker instead of on top of it. Wide enough to treat two
// spots in one parking lot as one place (they'd overlap on a whole-day view).
const MARKER_MERGE_M = 100;

function _metersApart(a, b) {
  const kx = 111320 * Math.cos((((a.lat + b.lat) / 2) * Math.PI) / 180);
  return Math.hypot((a.lon - b.lon) * kx, (a.lat - b.lat) * 110540);
}

/**
 * Where to put a day's map markers. Drives ("segments") are numbered 1..x in
 * order; each starts where the car was parked before it (drive 1 at the
 * day's start, drive n at the stop after drive n-1).
 *
 * - Day view (`selectedDriveId` null): a green start for drive 1, a red end
 *   for drive x, and a numbered badge where each of drives 2..x starts.
 * - Segment view: green and red at the selected drive's own start and end,
 *   and numbered badges where every other drive starts.
 *
 * Badges within MARKER_MERGE_M of each other merge (`numbers: [6, 9]`), and a
 * badge that near the green or red marker is flagged `beside` so it can be
 * drawn next to it rather than covering it.
 * Returns `{start, end, badges}`: start/end are `{lat, lon, number}` or null,
 * each badge `{lat, lon, numbers, parked, beside}` (`parked`: seconds parked
 * before each numbered drive, or null).
 */
export function dayMarkerSpecs(dayData, selectedDriveId = null) {
  const segments = (dayData && dayData.segments) || [];
  const count = segments.length;
  if (!count) return { start: null, end: null, badges: [] };
  const stops = new Map();
  for (const stop of dayData.stops || []) {
    if (stop.lat != null && stop.lon != null) stops.set(stop.after_index, stop);
  }
  const at = (p) => (p && p.lat != null && p.lon != null ? { lat: p.lat, lon: p.lon } : null);
  // Drive n (1-based) starts at the day start, or at the stop after drive n-1.
  const startOf = (n) => (n === 1 ? at(dayData.start) : at(stops.get(n - 2)));
  const endOf = (n) => (n === count ? at(dayData.end) : at(stops.get(n - 1)));
  const parkedBefore = (n) => {
    const stop = n > 1 ? stops.get(n - 2) : null;
    return stop && typeof stop.duration_seconds === "number" ? stop.duration_seconds : null;
  };

  const selectedIndex =
    selectedDriveId == null ? -1 : segments.findIndex((seg) => seg.drive_id === selectedDriveId);
  const selected = selectedIndex >= 0 ? selectedIndex + 1 : null;
  const startNumber = selected || 1;
  const endNumber = selected || count;
  const startPos = startOf(startNumber);
  const endPos = endOf(endNumber);
  const start = startPos ? { ...startPos, number: startNumber } : null;
  const end = endPos ? { ...endPos, number: endNumber } : null;

  const badges = [];
  for (let n = 1; n <= count; n++) {
    if (n === startNumber) continue;
    const pos = startOf(n);
    if (!pos) continue;
    const near = badges.find((b) => _metersApart(b, pos) <= MARKER_MERGE_M);
    if (near) {
      near.numbers.push(n);
      near.parked.push(parkedBefore(n));
      continue;
    }
    badges.push({ ...pos, numbers: [n], parked: [parkedBefore(n)], beside: false });
  }
  for (const badge of badges) {
    badge.beside = [start, end].some((m) => m && _metersApart(m, badge) <= MARKER_MERGE_M);
  }
  return { start, end, badges };
}

function _vehicleIndex(vehicles) {
  const map = new Map();
  for (const v of Array.isArray(vehicles) ? vehicles : []) if (v && v.vin) map.set(v.vin, v);
  return map;
}

/**
 * A single vehicle's view of a combined (`vins`) `analytics/day` response,
 * shaped like a single-vehicle day payload so the single-drive view (charts,
 * stats, markers) can run on it unchanged: that vehicle's segments, stops,
 * gaps, start/end, prior_tail and totals. `others` holds every other
 * vehicle's segments (drawn gray behind the selected drive).
 */
export function projectDay(day, vin) {
  const d = day || {};
  const info = (d.vehicles && d.vehicles[vin]) || {};
  const segments = d.segments || [];
  return {
    date: d.date,
    vin,
    totals: info.totals || {},
    segments: segments.filter((s) => s.vin === vin),
    stops: info.stops || [],
    gaps: info.gaps || [],
    start: info.start == null ? null : info.start,
    end: info.end == null ? null : info.end,
    prior_tail: info.prior_tail == null ? null : info.prior_tail,
    others: segments.filter((s) => s.vin !== vin),
  };
}

/**
 * Place categories the naming forms offer when the server's list isn't
 * available (an older backend). The server's `rivian/places/list` reply
 * carries the real list (`categories`), shared with the Places card.
 */
export const FALLBACK_PLACE_CATEGORIES = [
  { key: "home", label: "Home" },
  { key: "work", label: "Work" },
  { key: "school", label: "School" },
  { key: "shop", label: "Shopping" },
  { key: "charging", label: "Charging" },
  { key: "other", label: "Other" },
];

/** The category list of a `rivian/places/list` reply, else the fallback. */
export function placeCategoriesFrom(result) {
  const list = result && Array.isArray(result.categories) ? result.categories : null;
  return list && list.length ? list : FALLBACK_PLACE_CATEGORIES;
}

/**
 * Per-vehicle drive numbers for a combined day's segments ("1A", "2A", "1B"):
 * each vehicle's drives are numbered 1.. in time order and carry the
 * vehicle's letter. Returns a Map from each segment object to its label.
 */
export function multiSegmentLabels(segments, vehicles) {
  const vmap = _vehicleIndex(vehicles);
  const counts = new Map();
  const labels = new Map();
  for (const seg of segments || []) {
    const n = (counts.get(seg.vin) || 0) + 1;
    counts.set(seg.vin, n);
    const v = vmap.get(seg.vin);
    labels.set(seg, `${n}${v && v.letter ? v.letter : ""}`);
  }
  return labels;
}

/**
 * Map markers for a combined (several-vehicle) day. Every vehicle's drives
 * are numbered per vehicle in time order -- 1A, 2A, 1B... -- and each
 * vehicle keeps its own parked-position chain (its own stops, start and end).
 *
 * - No selection (`selected` null): per vehicle a start marker (its drive 1)
 *   and an end marker (its last drive), plus a badge where each later drive
 *   starts.
 * - `selected` `{vin, driveId}`: the selected drive gets `selected: true`
 *   start/end markers at its own endpoints; every other drive of every
 *   vehicle gets a badge where it starts.
 *
 * Badges within MARKER_MERGE_M merge across vehicles ("1A, 3B", `labels`) and
 * a badge that near a start/end marker is flagged `beside`. Returns
 * `{starts, ends, badges}`: markers are `{vin, letter, color, colorDark, lat,
 * lon, number, label, driveId, selected}`, badges `{lat, lon, items[], labels[],
 * beside}` (each item the marker fields plus `parked`).
 */
export function multiDayMarkerSpecs(day, vehicles, selected = null) {
  const segments = (day && day.segments) || [];
  const info = (day && day.vehicles) || {};
  const vmap = _vehicleIndex(vehicles);
  const vins = [];
  for (const v of Array.isArray(vehicles) ? vehicles : []) {
    if (segments.some((s) => s.vin === v.vin)) vins.push(v.vin);
  }
  for (const s of segments) if (!vins.includes(s.vin)) vins.push(s.vin);

  const at = (p) => (p && p.lat != null && p.lon != null ? { lat: p.lat, lon: p.lon } : null);
  const starts = [];
  const ends = [];
  const pending = [];
  for (const vin of vins) {
    const segs = segments.filter((s) => s.vin === vin);
    const count = segs.length;
    const vi = info[vin] || {};
    const stops = new Map();
    for (const stop of vi.stops || []) {
      if (stop.lat != null && stop.lon != null) stops.set(stop.after_index, stop);
    }
    const startOf = (n) => (n === 1 ? at(vi.start) : at(stops.get(n - 2)));
    const endOf = (n) => (n === count ? at(vi.end) : at(stops.get(n - 1)));
    const parkedBefore = (n) => {
      const stop = n > 1 ? stops.get(n - 2) : null;
      return stop && typeof stop.duration_seconds === "number" ? stop.duration_seconds : null;
    };
    const v = vmap.get(vin) || {};
    const letter = v.letter || "";
    const meta = { vin, letter, color: v.color || null, colorDark: v.color_dark || v.color || null };
    const marker = (n, pos, isSelected) => ({
      ...meta,
      ...pos,
      number: n,
      label: `${n}${letter}`,
      driveId: segs[n - 1] ? segs[n - 1].drive_id : null,
      selected: isSelected,
    });

    const selIdx =
      selected && selected.vin === vin ? segs.findIndex((s) => s.drive_id === selected.driveId) : -1;
    const wholeDay = !selected;
    if (wholeDay || selIdx >= 0) {
      const sn = selIdx >= 0 ? selIdx + 1 : 1;
      const en = selIdx >= 0 ? selIdx + 1 : count;
      const sp = startOf(sn);
      const ep = endOf(en);
      if (sp) starts.push(marker(sn, sp, selIdx >= 0));
      if (ep) ends.push(marker(en, ep, selIdx >= 0));
    }
    for (let n = 1; n <= count; n++) {
      if (wholeDay && n === 1) continue;
      if (selIdx >= 0 && n === selIdx + 1) continue;
      const pos = startOf(n);
      if (!pos) continue;
      pending.push({ ...pos, item: { ...marker(n, pos, false), parked: parkedBefore(n) } });
    }
  }

  const badges = [];
  for (const p of pending) {
    const near = badges.find((b) => _metersApart(b, p) <= MARKER_MERGE_M);
    if (near) near.items.push(p.item);
    else badges.push({ lat: p.lat, lon: p.lon, items: [p.item], labels: [], beside: false });
  }
  for (const badge of badges) {
    badge.labels = badge.items.map((i) => i.label);
    badge.beside = [...starts, ...ends].some((m) => _metersApart(m, badge) <= MARKER_MERGE_M);
  }
  return { starts, ends, badges };
}

/**
 * The per-vehicle summary table under a combined day's map: one row per
 * vehicle that drove that day (vehicle-list order) plus a combined total.
 * Rows: `{vin, letter, name, color, colorDark, drives, miles, movingSeconds,
 * energyKwh, efficiency}` (numbers or null).
 */
export function vehicleSummaryRows(day, vehicles) {
  const info = (day && day.vehicles) || {};
  const segments = (day && day.segments) || [];
  const num = (x) => (typeof x === "number" && !Number.isNaN(x) ? x : null);
  const rows = [];
  for (const v of Array.isArray(vehicles) ? vehicles : []) {
    const t = info[v.vin] && info[v.vin].totals;
    if (!t || !t.drives) continue;
    const moving = segments
      .filter((s) => s.vin === v.vin && typeof s.moving_seconds === "number")
      .map((s) => s.moving_seconds);
    rows.push({
      vin: v.vin,
      letter: v.letter || "",
      name: v.name || v.model || "Vehicle",
      color: v.color || null,
      colorDark: v.color_dark || v.color || null,
      drives: t.drives,
      miles: num(t.miles),
      movingSeconds: moving.length ? moving.reduce((a, b) => a + b, 0) : null,
      energyKwh: num(t.energy_kwh),
      efficiency: num(t.efficiency_mi_kwh),
    });
  }
  const totals = (day && day.totals) || {};
  const allMoving = segments.filter((s) => typeof s.moving_seconds === "number").map((s) => s.moving_seconds);
  const total = {
    vin: null,
    letter: "",
    name: "All vehicles",
    color: null,
    colorDark: null,
    drives: totals.drives || 0,
    miles: num(totals.miles),
    movingSeconds: allMoving.length ? allMoving.reduce((a, b) => a + b, 0) : null,
    energyKwh: num(totals.energy_kwh),
    efficiency: num(totals.efficiency_mi_kwh),
  };
  return { rows, total };
}

/**
 * Colored per-vehicle drive counts for a tree node's `by_vin` map, in
 * vehicle-list order, skipping vehicles with no drives:
 * `[{vin, letter, color, colorDark, drives, miles}]`.
 */
export function vinCounts(byVin, vehicles) {
  const out = [];
  for (const v of Array.isArray(vehicles) ? vehicles : []) {
    const c = byVin && byVin[v.vin];
    if (!c || !c.drives) continue;
    out.push({
      vin: v.vin,
      letter: v.letter || "",
      color: v.color || null,
      colorDark: v.color_dark || v.color || null,
      drives: c.drives,
      miles: typeof c.miles === "number" ? c.miles : 0,
    });
  }
  return out;
}

/** Readable text color (white or near-black) on a filled `#rrggbb` color. */
export function inkOn(hex) {
  const m = /^#?([0-9a-f]{6})$/i.exec(String(hex || ""));
  if (!m) return "#ffffff";
  const n = parseInt(m[1], 16);
  const lin = (c) => {
    const x = c / 255;
    return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4);
  };
  const lum = 0.2126 * lin((n >> 16) & 255) + 0.7152 * lin((n >> 8) & 255) + 0.0722 * lin(n & 255);
  return 1.05 / (lum + 0.05) >= (lum + 0.05) / 0.0668 ? "#ffffff" : "#111111";
}

/** Load the shared vehicle bar/selection module with this module's own cache-buster. */
function _loadBarModule() {
  return import(new URL("./rivian-vehicle-bar.js" + new URL(import.meta.url).search, import.meta.url));
}

/** Format an epoch-seconds timestamp as a time-of-day in the given IANA time zone. */
function _timeLabel(ts, tz) {
  if (ts === null || ts === undefined || Number.isNaN(ts)) return "–";
  try {
    return new Intl.DateTimeFormat(undefined, {
      hour: "numeric",
      minute: "2-digit",
      timeZone: tz,
    }).format(new Date(ts * 1000));
  } catch (_err) {
    return new Date(ts * 1000).toLocaleTimeString();
  }
}

function _fmtMiles(value) {
  return typeof value === "number" ? `${value.toFixed(1)} mi` : "–";
}

function _fmtNum(value) {
  return typeof value === "number" ? value.toLocaleString() : "0";
}

function _aggMeta(agg) {
  if (!agg) return "";
  const miles = typeof agg.miles === "number" ? agg.miles.toFixed(1) : "0.0";
  const drives = _fmtNum(agg.drives || 0);
  const eff = typeof agg.efficiency_mi_kwh === "number" ? agg.efficiency_mi_kwh.toFixed(2) : null;
  return eff ? `${miles} mi · ${drives} drives · ${eff} mi/kWh` : `${miles} mi · ${drives} drives`;
}

function _dayMeta(agg) {
  if (!agg) return "";
  const drives = agg.drives || 0;
  const miles = typeof agg.miles === "number" ? agg.miles.toFixed(1) : "0.0";
  return `${drives} drive${drives === 1 ? "" : "s"} · ${miles} mi`;
}

/**
 * "Home → Work" style suffix for a segment's tree row, from its start/end
 * place refs ({id, label, category} or null, as `analytics/day` sends them).
 * Only the known side is shown when the other is unplaced; "" when neither is.
 */
export function segmentPlaceLabel(seg) {
  const start = seg && seg.start_place ? seg.start_place.label : null;
  const end = seg && seg.end_place ? seg.end_place.label : null;
  if (start && end) return `${start} → ${end}`;
  if (start) return `${start} →`;
  if (end) return `→ ${end}`;
  return "";
}

function _segmentMeta(seg) {
  const miles = typeof seg.distance_miles === "number" ? seg.distance_miles.toFixed(1) : "–";
  const mins =
    typeof seg.duration_seconds === "number" ? `${Math.round(seg.duration_seconds / 60)} min` : "–";
  const eff = typeof seg.efficiency_mi_kwh === "number" ? seg.efficiency_mi_kwh.toFixed(2) : "–";
  return `${miles} mi · ${mins} · ${eff} mi/kWh`;
}

function _segmentBadges(seg) {
  if (!seg.has_track) return ["no route"];
  if (seg.track_source === "backfill") return ["backfilled"];
  return [];
}

/** The ancestor chain (inclusive) of a tree selection, root first. */
function _selectionPath(selection) {
  const path = [{ level: "all", key: null }];
  const sel = selection || { level: "all" };
  if (sel.level === "all" || !sel.key) return path;
  const yearKey = sel.key.slice(0, 4);
  path.push({ level: "year", key: yearKey });
  if (sel.level === "year") return path;
  const monthKey = sel.key.slice(0, 7);
  path.push({ level: "month", key: monthKey });
  if (sel.level === "month") return path;
  path.push({ level: "day", key: sel.key });
  if (sel.level === "day") return path;
  path.push({ level: "segment", key: sel.key, driveId: sel.driveId });
  return path;
}

/**
 * Compute the rendered tree rows from cached calendar/day responses and the
 * current selection. `cache` is `{root, years: {[yearKey]: resp}, months:
 * {[monthKey]: resp}, days: {[dayKey]: resp}}`, each `resp` shaped like the
 * matching `rivian/analytics/calendar` or `rivian/analytics/day` result.
 * Only the path from "All time" to the current selection is expanded;
 * siblings along that path are listed but collapsed (no grandchildren).
 */
export function visibleTreeRows(cache, selection, tz, opts = {}) {
  const data = cache || {};
  const sel = selection || { level: "all" };
  // Several vehicles: nodes carry colored per-vehicle counts, and a day's
  // drives interleave by time as "1B 8:02 AM · Home → Library".
  const multi = !!opts.multi;
  const vehicles = opts.vehicles || [];
  const vmap = _vehicleIndex(vehicles);
  const counts = (agg) => (multi ? { vinCounts: vinCounts(agg && agg.by_vin, vehicles) } : {});
  const path = _selectionPath(sel);
  const onPath = (level, key) => path.some((p) => p.level === level && p.key === key);

  const rows = [];
  const root = data.root;
  rows.push({
    level: "all",
    key: null,
    driveId: null,
    ariaLevel: 1,
    label: "All time",
    meta: root ? _aggMeta(root.totals) : "",
    expanded: true,
    selected: sel.level === "all",
    hasChevron: false,
    badges: [],
    ...counts(root && root.totals),
  });

  const years = (root && root.years) || [];
  for (const y of years) {
    const yearExpanded = onPath("year", y.key);
    rows.push({
      level: "year",
      key: y.key,
      driveId: null,
      ariaLevel: 2,
      label: y.key,
      meta: _aggMeta(y),
      expanded: yearExpanded,
      selected: sel.level === "year" && sel.key === y.key,
      hasChevron: true,
      badges: [],
      ...counts(y),
    });
    if (!yearExpanded) continue;
    const yearData = data.years && data.years[y.key];
    const months = (yearData && yearData.months) || [];
    for (const m of months) {
      const monthExpanded = onPath("month", m.key);
      rows.push({
        level: "month",
        key: m.key,
        driveId: null,
        ariaLevel: 3,
        label: formatMonthLabel(m.key),
        meta: _aggMeta(m),
        expanded: monthExpanded,
        selected: sel.level === "month" && sel.key === m.key,
        hasChevron: true,
        badges: [],
        ...counts(m),
      });
      if (!monthExpanded) continue;
      const monthData = data.months && data.months[m.key];
      const days = (monthData && monthData.days) || [];
      for (const d of days) {
        const dayExpanded = onPath("day", d.key);
        rows.push({
          level: "day",
          key: d.key,
          driveId: null,
          ariaLevel: 4,
          label: formatDayLabel(d.key),
          meta: _dayMeta(d),
          expanded: dayExpanded,
          selected: sel.level === "day" && sel.key === d.key,
          hasChevron: true,
          badges: [],
          ...counts(d),
        });
        if (!dayExpanded) continue;
        const dayData = data.days && data.days[d.key];
        const segments = (dayData && dayData.segments) || [];
        const multiLabels = multi ? multiSegmentLabels(segments, vehicles) : null;
        for (const seg of segments) {
          const placeLabel = segmentPlaceLabel(seg);
          const vehicle = multi ? vmap.get(seg.vin) : null;
          rows.push({
            level: "segment",
            key: d.key,
            driveId: seg.drive_id,
            ariaLevel: 5,
            label: placeLabel
              ? `${_timeLabel(seg.start_ts, tz)} · ${placeLabel}`
              : _timeLabel(seg.start_ts, tz),
            // Numbered 1..x in the day's order, matching the map badges
            // (per vehicle, with its letter, when several are in view).
            number: multi
              ? multiLabels.get(seg)
              : (typeof seg.index === "number" ? seg.index : segments.indexOf(seg)) + 1,
            ...(multi
              ? {
                  vin: seg.vin,
                  color: (vehicle && vehicle.color) || null,
                  colorDark: (vehicle && (vehicle.color_dark || vehicle.color)) || null,
                }
              : {}),
            meta: _segmentMeta(seg),
            expanded: false,
            selected:
              sel.level === "segment" &&
              sel.key === d.key &&
              sel.driveId === seg.drive_id &&
              (sel.vin ?? null) === (seg.vin ?? null),
            hasChevron: false,
            badges: _segmentBadges(seg),
          });
        }
      }
    }
  }
  return rows;
}

/**
 * Phone-width tree: a drill-down list instead of the whole expanded path.
 * Shows the selected node's children (the breadcrumb covers the way back up),
 * or, for a selected drive, the rest of that day's drives. A node with no
 * children shows just itself.
 */
export function drillDownRows(rows) {
  const selIdx = rows.findIndex((r) => r.selected);
  if (selIdx < 0) return rows;
  const sel = rows[selIdx];
  if (sel.level === "segment") return rows.filter((r) => r.level === "segment");
  const children = [];
  for (let i = selIdx + 1; i < rows.length && rows[i].ariaLevel > sel.ariaLevel; i++) {
    if (rows[i].ariaLevel === sel.ariaLevel + 1) children.push(rows[i]);
  }
  return children.length ? children : [sel];
}

/** The clickable breadcrumb parts ("All time" -> ... -> the selection), inclusive. */
export function breadcrumbParts(cache, selection, tz, opts = {}) {
  const data = cache || {};
  const sel = selection || { level: "all" };
  const parts = [{ level: "all", key: null, driveId: null, label: "All time" }];
  if (sel.level === "all" || !sel.key) return parts;

  const yearKey = sel.key.slice(0, 4);
  parts.push({ level: "year", key: yearKey, driveId: null, label: yearKey });
  if (sel.level === "year") return parts;

  const monthKey = sel.key.slice(0, 7);
  parts.push({ level: "month", key: monthKey, driveId: null, label: formatMonthLabel(monthKey) });
  if (sel.level === "month") return parts;

  const dayKey = sel.key;
  parts.push({ level: "day", key: dayKey, driveId: null, label: formatDayLabel(dayKey) });
  if (sel.level === "day") return parts;

  const dayData = data.days && data.days[dayKey];
  const seg =
    dayData && dayData.segments
      ? dayData.segments.find((s) => s.drive_id === sel.driveId && (s.vin ?? null) === (sel.vin ?? null))
      : null;
  let label = seg ? _timeLabel(seg.start_ts, tz) : "…";
  if (seg && opts.multi) {
    const number = multiSegmentLabels(dayData.segments, opts.vehicles || []).get(seg);
    label = `${number} · ${label}`;
  }
  parts.push({
    level: "segment",
    key: dayKey,
    driveId: sel.driveId,
    ...(sel.vin ? { vin: sel.vin } : {}),
    label,
  });
  return parts;
}

function _escapeText(el, text) {
  el.textContent = text === null || text === undefined ? "" : String(text);
  return el;
}

let _leafletModulePromise = null;
let _leafletCssPromise = null;

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

/** A touch-first device (phone/tablet), where one-finger drags should scroll the page. */
function _isTouchDevice() {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia("(pointer: coarse)").matches;
}

function _loadLeafletCss() {
  if (!_leafletCssPromise) {
    _leafletCssPromise = fetch(new URL("./leaflet/leaflet.css", import.meta.url)).then((r) =>
      r.text()
    );
  }
  return _leafletCssPromise;
}

const _CARD_STYLE = `
  :host { display: block; }
  ha-card {
    display: flex;
    flex-direction: column;
    overflow: hidden;
    padding: 0;
    background: var(--ha-card-background, var(--card-background-color, #fff));
    color: var(--primary-text-color, #212121);
  }
  .rde-body {
    display: flex;
    flex: 1;
    min-height: 0;
  }
  .rde-body.rde-stacked {
    flex-direction: column;
  }
  .rde-list-pane {
    width: 340px;
    min-width: 260px;
    flex-shrink: 0;
    display: flex;
    flex-direction: column;
    border-right: 1px solid var(--divider-color, #e0e0e0);
    overflow: hidden;
  }
  /* Phone width: the card grows to its content and the page does all the
     scrolling -- nothing inside the card scrolls on its own. The list pane
     dissolves (display: contents) so the map, charts and stats come first,
     then the list's header, breadcrumb and drill-down list together. */
  .rde-stacked .rde-list-pane {
    display: contents;
  }
  .rde-stacked .rde-list-header {
    order: 2;
    border-top: 1px solid var(--divider-color, #e0e0e0);
  }
  .rde-stacked .rde-breadcrumb {
    order: 3;
  }
  .rde-stacked .rde-tree {
    order: 4;
    flex: none;
    overflow: visible;
  }
  .rde-stacked .rde-footer {
    order: 5;
  }
  /* The page's main title: larger and bolder than section headings. */
  .rde-page-title {
    font-size: 20px; font-weight: 700; letter-spacing: 0.01em; color: var(--primary-text-color);
  }
  .rde-list-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 8px 12px;
    border-bottom: 1px solid var(--divider-color, #e0e0e0);
    gap: 8px;
  }
  .rde-list-header label {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 0.85em;
    color: var(--secondary-text-color);
    cursor: pointer;
  }
  .rde-breadcrumb {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 2px;
    padding: 6px 10px;
    font-size: 0.78em;
    border-bottom: 1px solid var(--divider-color, #e0e0e0);
  }
  .rde-breadcrumb-part {
    background: none;
    border: none;
    color: var(--primary-color, #03a9f4);
    cursor: pointer;
    font: inherit;
    padding: 2px;
  }
  .rde-breadcrumb-part.current {
    color: var(--primary-text-color);
    font-weight: 500;
    cursor: default;
  }
  .rde-breadcrumb-sep {
    color: var(--secondary-text-color);
  }
  .rde-tree {
    flex: 1;
    overflow-y: auto;
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .rde-tree-row {
    display: flex;
    align-items: flex-start;
    gap: 4px;
    padding: 6px 10px 6px calc(10px + var(--rde-indent, 0) * 16px);
    cursor: pointer;
    border-bottom: 1px solid var(--divider-color, #e0e0e0);
  }
  .rde-tree-row:hover {
    background: rgba(127, 127, 127, 0.08);
  }
  .rde-tree-row.selected {
    background: rgba(var(--rgb-primary-color, 3, 169, 244), 0.15);
  }
  .rde-charts-body svg:focus-visible, .rde-charts-body g[role="button"]:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: 2px;
  }
  .rde-tree-row:focus {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: -2px;
  }
  .rde-chevron, .rde-chevron-spacer {
    display: inline-block;
    width: 14px;
    flex-shrink: 0;
    text-align: center;
    margin-top: 2px;
    color: var(--secondary-text-color);
    transition: transform 0.15s ease;
  }
  .rde-chevron.expanded {
    transform: rotate(90deg);
  }
  .rde-row-text {
    min-width: 0;
    flex: 1;
  }
  .rde-row-label {
    font-weight: 500;
    font-size: 0.92em;
  }
  .rde-tree-row.selected .rde-row-label {
    color: var(--primary-color, #03a9f4);
  }
  .rde-row-meta {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    font-size: 0.8em;
    color: var(--secondary-text-color);
    margin-top: 2px;
  }
  .rde-seg-num {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 18px;
    height: 18px;
    padding: 0 4px;
    margin-right: 6px;
    box-sizing: border-box;
    border-radius: 9px;
    background: #1565c0;
    color: #ffffff;
    font-size: 11px;
    font-weight: 600;
    vertical-align: 1px;
  }
  .rde-stop-icon {
    background: none;
    border: none;
  }
  .rde-stop-marker {
    position: absolute;
    left: 0;
    top: 0;
    transform: translate(-50%, -50%);
    display: flex;
    align-items: center;
    justify-content: center;
    min-width: 20px;
    height: 20px;
    padding: 0 5px;
    box-sizing: border-box;
    border-radius: 10px;
    border: 2px solid #ffffff;
    background: #1565c0;
    color: #ffffff;
    font: 600 11px/1 Roboto, "Noto Sans", sans-serif;
    white-space: nowrap;
    box-shadow: 0 0 2px rgba(0, 0, 0, 0.6);
    cursor: pointer;
  }
  /* Next to a green/red marker at the same spot, not on top of it. */
  .rde-stop-marker.beside {
    transform: translate(10px, -130%);
  }
  .rde-badge {
    display: inline-block;
    font-size: 0.72em;
    padding: 1px 6px;
    border-radius: 8px;
    background: var(--divider-color, #ccc);
    color: var(--secondary-text-color);
  }
  .rde-map-pane {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-width: 0;
    min-height: 0;
  }
  .rde-stacked .rde-map-pane {
    order: 1;
    flex: none;
  }
  .rde-stacked .rde-map-container {
    flex: none;
    height: 40vh;
    min-height: 240px;
    max-height: 420px;
  }
  .rde-stacked .rde-charts-panel {
    padding-bottom: 8px;
  }
  .rde-stacked .rde-stat {
    flex-basis: 78px;
    min-width: 78px;
    padding: 3px 4px;
  }
  .rde-map-container {
    position: relative;
    flex: 1;
    min-height: 300px;
  }
  .rde-map {
    position: absolute;
    inset: 0;
  }
  /* Leaflet sizes the element from iconSize; border-box keeps the border
     inside it, so the square stays small enough to sit within the start
     circle's green ring when a day starts and ends at the same spot. */
  .rde-end-marker {
    background: #c62828;
    border: 2px solid #ffffff;
    box-shadow: 0 0 2px rgba(0, 0, 0, 0.6);
    box-sizing: border-box;
  }
  .rde-map-overlay {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    text-align: center;
    padding: 16px;
    color: var(--secondary-text-color);
    background: rgba(0, 0, 0, 0.02);
    pointer-events: none;
    font-size: 0.95em;
  }
  .rde-legend-wrap {
    position: absolute;
    bottom: 8px;
    left: 8px;
    z-index: 1000;
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 6px;
  }
  .rde-legend {
    background: var(--card-background-color, #fff);
    border-radius: 6px;
    padding: 6px 8px;
    font-size: 0.72em;
    color: var(--primary-text-color);
    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.3);
    display: flex;
    flex-direction: column;
    gap: 3px;
    width: 180px;
    box-sizing: border-box;
  }
  .rde-legend-bar {
    height: 8px;
    border-radius: 4px;
  }
  .rde-legend-labels {
    display: flex;
    justify-content: space-between;
  }
  .rde-route-toggle {
    display: flex;
    border-radius: 6px;
    overflow: hidden;
    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.35);
  }
  .rde-route-toggle button {
    font: inherit;
    font-size: 11px;
    padding: 4px 8px;
    border: none;
    cursor: pointer;
    background: var(--card-background-color, #fff);
    color: var(--primary-text-color, #212121);
  }
  .rde-route-toggle button + button {
    border-left: 1px solid var(--divider-color, rgba(0, 0, 0, 0.12));
  }
  .rde-route-toggle button.active {
    background: var(--primary-color, #03a9f4);
    color: var(--text-primary-color, #fff);
  }
  .rde-charts-panel {
    flex-shrink: 0;
    border-top: 1px solid var(--divider-color, #e0e0e0);
    padding: 6px 10px 2px;
  }
  .rde-charts-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    margin-bottom: 2px;
  }
  .rde-charts-toggle {
    background: none;
    border: none;
    cursor: pointer;
    font: inherit;
    font-size: 0.82em;
    font-weight: 500;
    color: var(--primary-text-color);
    padding: 2px 4px;
  }
  .rde-eff-switch {
    display: flex;
    border-radius: 6px;
    overflow: hidden;
    border: 1px solid var(--divider-color, #e0e0e0);
  }
  .rde-eff-switch button {
    font: inherit;
    font-size: 11px;
    padding: 3px 8px;
    border: none;
    cursor: pointer;
    background: var(--card-background-color, #fff);
    color: var(--secondary-text-color);
  }
  .rde-eff-switch button + button {
    border-left: 1px solid var(--divider-color, #e0e0e0);
  }
  .rde-eff-switch button.active {
    background: var(--primary-color, #03a9f4);
    color: var(--text-primary-color, #fff);
  }
  .rde-eff-switch button:disabled,
  .rde-eff-switch button.disabled {
    cursor: not-allowed;
    opacity: 0.45;
  }
  .rde-eff-model-legend {
    display: flex;
    gap: 12px;
    font-size: 0.72em;
    color: var(--secondary-text-color);
    margin-bottom: 2px;
  }
  .rde-cursor-readout {
    font-size: 0.75em;
    color: var(--secondary-text-color);
    min-height: 16px;
    margin-bottom: 2px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .rde-charts-body {
    width: 100%;
  }
  .rde-charts-body svg {
    display: block;
    width: 100%;
    height: auto;
    cursor: crosshair;
    touch-action: pan-y;
  }
  .rde-charts-empty {
    padding: 8px 0 10px;
    font-size: 0.8em;
    color: var(--secondary-text-color);
  }
  .rde-basemaps {
    position: absolute;
    top: 10px;
    right: 10px;
    z-index: 1000;
    display: flex;
    border-radius: 6px;
    overflow: hidden;
    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.35);
  }
  .rde-basemaps button {
    font: inherit;
    font-size: 12px;
    padding: 5px 10px;
    border: none;
    cursor: pointer;
    background: var(--card-background-color, #fff);
    color: var(--primary-text-color, #212121);
  }
  .rde-basemaps button + button {
    border-left: 1px solid var(--divider-color, rgba(0, 0, 0, 0.12));
  }
  .rde-basemaps button.active {
    background: var(--primary-color, #03a9f4);
    color: var(--text-primary-color, #fff);
  }
  .rde-stats {
    display: flex;
    flex-wrap: wrap;
    gap: 0;
    border-top: 1px solid var(--divider-color, #e0e0e0);
    padding: 8px 4px;
  }
  .rde-stat {
    flex: 1 1 90px;
    min-width: 90px;
    padding: 4px 8px;
    text-align: center;
  }
  .rde-stat-wide {
    flex-basis: 160px;
  }
  .rde-stat-wide .rde-stat-value {
    white-space: normal;
  }
  .rde-stat-value {
    font-size: 1.05em;
    font-weight: 500;
    color: var(--primary-text-color);
    white-space: nowrap;
  }
  .rde-stat-label {
    font-size: 0.72em;
    color: var(--secondary-text-color);
  }
  .rde-stat-place {
    position: relative;
  }
  .rde-delete-row {
    flex-basis: 100%;
    display: flex;
    justify-content: center;
    padding: 6px 8px 2px;
  }
  .rde-danger-btn {
    font: inherit;
    font-size: 0.82em;
    padding: 5px 12px;
    border-radius: 4px;
    border: 1px solid var(--error-color, #db4437);
    background: var(--card-background-color, #fff);
    color: var(--error-color, #db4437);
    cursor: pointer;
  }
  .rde-place-edit-btn {
    position: absolute;
    top: 2px;
    right: 2px;
    border: none;
    background: none;
    cursor: pointer;
    font-size: 0.8em;
    color: var(--secondary-text-color);
    padding: 2px;
  }
  .rde-place-form {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    justify-content: center;
    margin-top: 6px;
    width: 100%;
  }
  .rde-place-form input,
  .rde-place-form select,
  .rde-place-form button {
    font: inherit;
    font-size: 0.78em;
    padding: 3px 5px;
    border-radius: 4px;
    border: 1px solid var(--divider-color, #e0e0e0);
    background: var(--card-background-color, #fff);
    color: var(--primary-text-color);
  }
  .rde-place-form button {
    cursor: pointer;
  }
  option { background-color: inherit; color: inherit; }
  /* Customizable <select> (Chromium 135+, incl. HA's Android app): the open
     list is drawn by the page, so it follows the theme -- the native list
     ignored it (white frame, wrong size and scrollbar in dark mode). Other
     browsers keep the native control. */
  @supports (appearance: base-select) {
    .rde-place-form select, .rde-map-place-popup select, .rde-place-form select::picker(select), .rde-map-place-popup select::picker(select) { appearance: base-select; }
    .rde-place-form select, .rde-map-place-popup select { display: inline-flex; align-items: center; gap: 6px; cursor: pointer; }
    .rde-place-form select::picker-icon, .rde-map-place-popup select::picker-icon { color: var(--secondary-text-color, #727272); font-size: 0.8em; }
    .rde-place-form select::picker(select), .rde-map-place-popup select::picker(select) {
      background: var(--card-background-color, var(--primary-background-color, #fff));
      color: var(--primary-text-color, #212121);
      border: 1px solid var(--divider-color, #e0e0e0);
      border-radius: 8px;
      box-shadow: 0 6px 18px rgba(0, 0, 0, 0.35);
      padding: 4px 0;
      margin-block: 2px;
      max-height: min(320px, 60vh);
      overflow-y: auto;
      scrollbar-width: thin;
      scrollbar-color: var(--divider-color, #e0e0e0) transparent;
      font-family: inherit;
      font-size: 14px;
    }
    .rde-place-form select option, .rde-map-place-popup select option { padding: 6px 12px; background: transparent; color: inherit; min-height: 0; }
    .rde-place-form select option:hover, .rde-place-form select option:focus-visible, .rde-map-place-popup select option:hover, .rde-map-place-popup select option:focus-visible { background: var(--secondary-background-color, rgba(127, 127, 127, 0.18)); outline: none; }
    .rde-place-form select option:checked, .rde-map-place-popup select option:checked { font-weight: 600; }
    .rde-place-form select option::checkmark, .rde-map-place-popup select option::checkmark { color: var(--primary-color, #03a9f4); }
  }
  .rde-map-place-popup {
    display: flex;
    flex-direction: column;
    gap: 6px;
    min-width: 160px;
  }
  .rde-map-place-popup-title {
    font-weight: 500;
  }
  .rde-map-place-popup input,
  .rde-map-place-popup select,
  .rde-map-place-popup button {
    font: inherit;
    font-size: 0.85em;
    padding: 4px 6px;
  }
  .rde-footer {
    padding: 4px 12px 8px;
    font-size: 0.72em;
    color: var(--secondary-text-color);
    border-top: 1px solid var(--divider-color, #e0e0e0);
  }
  .rde-topbar rivian-vehicle-bar {
    padding: 8px 12px;
  }
  .rde-topbar {
    border-bottom: 1px solid var(--divider-color, #e0e0e0);
  }
  .rde-vcount {
    display: inline-flex;
    align-items: center;
    gap: 3px;
    font-variant-numeric: tabular-nums;
  }
  .rde-vcount i, .rde-vtable i, .rde-vlegend-row i {
    display: inline-block;
    width: 9px;
    height: 9px;
    border-radius: 50%;
    flex: none;
  }
  .rde-vtable i, .rde-vlegend-row i { margin-right: 6px; }
  .rde-vstart-marker {
    position: absolute;
    left: 0;
    top: 0;
    transform: translate(-50%, -50%);
    display: flex;
    align-items: center;
    justify-content: center;
    min-width: 24px;
    height: 24px;
    padding: 0 5px;
    box-sizing: border-box;
    border-radius: 12px;
    border: 3px solid #ffffff;
    font: 700 11px/1 Roboto, "Noto Sans", sans-serif;
    white-space: nowrap;
    box-shadow: 0 0 3px rgba(0, 0, 0, 0.7);
  }
  .rde-vend-marker {
    position: absolute;
    left: 0;
    top: 0;
    transform: translate(-50%, -50%);
    width: 12px;
    height: 12px;
    box-sizing: border-box;
    border: 2px solid #ffffff;
    box-shadow: 0 0 2px rgba(0, 0, 0, 0.7);
  }
  .rde-vlegend-row {
    display: flex;
    align-items: center;
    white-space: nowrap;
  }
  .rde-vtable-wrap {
    flex-basis: 100%;
    padding: 0 8px;
    box-sizing: border-box;
  }
  .rde-vtable {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.88em;
  }
  .rde-vtable th, .rde-vtable td {
    padding: 5px 6px;
    text-align: right;
    border-bottom: 1px solid var(--divider-color, #e0e0e0);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }
  .rde-vtable th:first-child, .rde-vtable td:first-child {
    text-align: left;
  }
  .rde-vtable th {
    color: var(--secondary-text-color);
    font-weight: 500;
    font-size: 0.85em;
  }
  .rde-vtable-total td {
    font-weight: 600;
    border-bottom: none;
  }
  .rde-stacked .rde-vtable { font-size: 0.8em; }
  .rde-empty, .rde-loading, .rde-error {
    padding: 16px;
    color: var(--secondary-text-color);
    text-align: center;
  }
  .rde-error {
    color: var(--error-color, #db4437);
  }
`;

// -- charts: preferences, constants, small shared helpers --------------------

const CHARTS_COLLAPSED_KEY = "rivian-drive-explorer-charts-collapsed";
// v2: Model became the default; a fresh key so everyone starts there once,
// after which their own choice sticks.
const EFF_METHOD_KEY = "rivian-drive-explorer-eff-method-v2";
const ROUTE_COLOR_KEY = "rivian-drive-explorer-route-color";

function _readBoolPref(key, fallback) {
  try {
    const raw = window.localStorage.getItem(key);
    return raw === null ? fallback : raw === "1";
  } catch (_err) {
    return fallback;
  }
}
function _writeBoolPref(key, value) {
  try {
    window.localStorage.setItem(key, value ? "1" : "0");
  } catch (_err) {
    // Not persisted; the choice still applies for this session.
  }
}
function _readEnumPref(key, fallback, allowed) {
  try {
    const raw = window.localStorage.getItem(key);
    return allowed.includes(raw) ? raw : fallback;
  } catch (_err) {
    return fallback;
  }
}
function _writeEnumPref(key, value) {
  try {
    window.localStorage.setItem(key, value);
  } catch (_err) {
    // Not persisted; the choice still applies for this session.
  }
}

const METERS_TO_FEET = 3.28084;
const CHART_ROW_HEIGHT_PX = 64;
const CHART_AXIS_HEIGHT_PX = 20;
// A dedicated lane above the rows for a break's numbered badge, so it never
// collides with the topmost row's own corner label.
const CHART_BADGE_STRIP_PX = 16;
// A gutter on the right of each chart row reserved for its min/max labels,
// so the plotted line (which stops at the gutter's left edge) never runs
// under them.
const CHART_RIGHT_GUTTER_PX = 46;

// Categorical slots 1-4 (palette.md), assigned in fixed order -- distinct
// from the red->green speed gradient used on the map by default.
const CHART_METRICS = [
  { key: "speed", label: "Speed", unit: "mph", color: { light: "#2a78d6", dark: "#3987e5" } },
  { key: "elevation", label: "Elevation", unit: "ft", color: { light: "#eb6834", dark: "#d95926" } },
  { key: "efficiency", label: "Efficiency", unit: "mi/kWh", color: { light: "#1baf7a", dark: "#199e70" } },
  { key: "battery", label: "Battery", unit: "%", color: { light: "#eda100", dark: "#c98500" } },
];

/** A chart metric's bare number, rounded per its kind (no unit suffix). */
function _formatMetricNumber(key, v) {
  if (key === "elevation") return Math.round(v).toLocaleString();
  if (key === "efficiency") return (Math.round(v * 10) / 10).toFixed(1);
  return String(Math.round(v)); // speed, battery
}

/** A chart metric's value with its unit, for the hover readout. */
function _formatReadoutValue(metric, v) {
  const num = _formatMetricNumber(metric.key, v);
  return metric.key === "battery" ? `${num}%` : `${num} ${metric.unit}`;
}

function _stopsGradientCss(stops) {
  const parts = stops.map(([frac, [r, g, b]]) => `rgb(${r}, ${g}, ${b}) ${frac * 100}%`);
  return `linear-gradient(to right, ${parts.join(", ")})`;
}

/**
 * Split a track into polyline pieces colored by an arbitrary per-point value
 * array (elevation/efficiency route coloring) -- the same merge-adjacent-
 * same-color approach as `speedPieces`, generalized past speed.
 */
function _colorPieces(track, values, colorFn) {
  if (!track || !Array.isArray(track.lat) || track.lat.length < 2) return [];
  const pieces = [];
  let current = null;
  for (let i = 1; i < track.lat.length; i++) {
    const known = [values[i - 1], values[i]].filter(
      (v) => v !== null && v !== undefined && !Number.isNaN(v)
    );
    const mean = known.length ? known.reduce((a, b) => a + b, 0) / known.length : null;
    const color = colorFn(mean);
    const next = [track.lat[i], track.lon[i]];
    if (current && current.color === color) {
      current.points.push(next);
    } else {
      current = { color, points: [[track.lat[i - 1], track.lon[i - 1]], next] };
      pieces.push(current);
    }
  }
  return pieces;
}

/**
 * Split a track's points into contiguous runs by its `filled` flag (points
 * reconstructed along roads during a GPS dropout, not measured). Consecutive
 * runs share their boundary point index so a polyline drawn per run still
 * joins with its neighbours. A track with no `filled` array (or all-false)
 * comes back as one run covering every point. Returns `[{filled, from, to}]`
 * (inclusive point indices); `[]` for a track with under 2 points.
 */
export function trackFilledRuns(track) {
  const n = track && Array.isArray(track.lat) ? track.lat.length : 0;
  if (n < 2) return [];
  const filledArr = Array.isArray(track.filled) ? track.filled : null;
  const runs = [];
  let runStart = 0;
  let runFilled = filledArr ? !!filledArr[0] : false;
  for (let i = 1; i < n; i++) {
    const f = filledArr ? !!filledArr[i] : false;
    if (f !== runFilled) {
      runs.push({ filled: runFilled, from: runStart, to: i });
      runStart = i;
      runFilled = f;
    }
  }
  runs.push({ filled: runFilled, from: runStart, to: n - 1 });
  return runs;
}

/**
 * Split an ordered, possibly-gapped chart-point array (`{t, value, seg,
 * filled}`) into contiguous runs of the same `filled` flag, for dashing a
 * chart line's estimated stretches. A run breaks on a null value or a `seg`
 * change (a new drive); adjacent runs of the same seg share their boundary
 * point so the line still joins visually. Returns `[{filled, points}]`.
 */
export function splitFilledRuns(points) {
  const runs = [];
  let current = null;
  let prevSeg = null;
  let prevPoint = null;
  for (const p of points || []) {
    if (p.value === null || p.value === undefined) {
      current = null;
      prevSeg = null;
      prevPoint = null;
      continue;
    }
    const brokeSeg = prevSeg !== null && p.seg !== prevSeg;
    const filled = !!p.filled;
    if (!brokeSeg && current && current.filled === filled) {
      current.points.push(p);
    } else {
      const pts = !brokeSeg && prevPoint ? [prevPoint, p] : [p];
      current = { filled, points: pts };
      runs.push(current);
    }
    prevSeg = p.seg;
    prevPoint = p;
  }
  return runs;
}

// A DOM-less base class lets this module (and its pure exports above) be
// imported under plain Node for the smoke test.
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

class RivianDriveExplorerCard extends BaseElement {
  static getStubConfig() {
    return {};
  }

  /** The config's fixed vehicle set (`vin`, else `vins`), or null to follow the shared selection. */
  static _fixedVins(config) {
    if (config && config.vin) return [config.vin];
    if (config && Array.isArray(config.vins) && config.vins.length) return [...config.vins];
    return null;
  }

  setConfig(config) {
    const next = config || {};
    const fixed = RivianDriveExplorerCard._fixedVins;
    const scopeChanged =
      this._config && JSON.stringify(fixed(this._config)) !== JSON.stringify(fixed(next));
    this._config = next;
    if (!this._built) {
      this._build();
    } else if (scopeChanged) {
      // Card edited to point at other vehicles: drop everything and reload.
      this._unsubscribe();
      this._resetState();
      this._started = false;
      if (this._hass) this.hass = this._hass;
    }
  }

  getCardSize() {
    return 12;
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
    const themeChanged =
      this._hass && hass && this._hass.themes?.darkMode !== hass.themes?.darkMode;
    this._hass = hass;
    if (this._built) this._syncColorScheme(hass);
    if (!this._built) return;
    if (this._barEl) this._barEl.hass = hass;
    if (!this._started) {
      this._started = true;
      this._start().catch((err) => this._showError(err));
    }
    if (themeChanged) {
      this._applyTileLayer();
      this._redrawChartsForCurrentSelection();
      if (this._multi && this._treeRows) {
        // Vehicle colors differ per theme.
        this._renderTree();
        this._renderMapForSelection().catch((err) => this._showError(err));
      }
    }
  }

  _resetState() {
    this._cache = { root: null, years: {}, months: {}, days: {} };
    this._selection = { level: "all" };
    this._focusedRowKey = null;
    this._renderToken = 0;
    this._storage = null;
    this._clearRoute();
    this._clearHeatLayer();
    this._hideCharts();
  }

  _build() {
    this._built = true;
    this._started = false;
    this._includeMicro = false;
    this._map = null;
    this._tileLayers = [];
    this._basemap = _initialBasemap(this._config && this._config.basemap);
    this._routeLayers = [];
    this._markerLayers = [];
    this._heatLayer = null;
    this._heatLayerClass = null;
    this._resizeObserver = null;
    this._leaflet = null;
    this._cursorMapMarker = null;
    this._lastFitBounds = null;
    this._lastFitOptions = null;
    this._lastSetView = null;
    this._chartsCollapsed = _readBoolPref(CHARTS_COLLAPSED_KEY, false);
    // The method the user chose (default Model). `_effMethod` is what is shown,
    // which falls back to chunks only while a selection has no model estimate.
    this._effMethodPref = _readEnumPref(EFF_METHOD_KEY, "model", ["chunks", "rolling", "model"]);
    this._effMethod = this._effMethodPref;
    this._routeColorMode = _readEnumPref(ROUTE_COLOR_KEY, "speed", ["speed", "elevation", "efficiency"]);
    this._chartsData = null;
    this._cursorT = null;
    this._vehicleList = [];
    this._vins = [];
    this._multi = false;
    this._followStore = false;
    this._bar = null;
    this._barEl = null;
    this._unsubSelection = null;
    this._resetState();

    this.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = _CARD_STYLE;
    this.shadowRoot.appendChild(style);

    this._leafletStyleEl = document.createElement("style");
    this.shadowRoot.appendChild(this._leafletStyleEl);

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

    this._renderShell();
  }

  _renderShell() {
    this._card.textContent = "";
    this._applyCardHeight(false);

    // Vehicle bar slot (filled once the card follows the shared selection).
    this._topbarEl = document.createElement("div");
    this._topbarEl.className = "rde-topbar";
    this._topbarEl.style.display = "none";
    this._card.appendChild(this._topbarEl);

    this._body = document.createElement("div");
    this._body.className = "rde-body";
    this._card.appendChild(this._body);

    // List pane
    this._listPane = document.createElement("div");
    this._listPane.className = "rde-list-pane";
    this._body.appendChild(this._listPane);

    const header = document.createElement("div");
    header.className = "rde-list-header";
    const title = document.createElement("span");
    title.className = "rde-page-title";
    _escapeText(title, "Drives");
    header.appendChild(title);

    const label = document.createElement("label");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = this._includeMicro;
    checkbox.addEventListener("change", () => {
      this._includeMicro = checkbox.checked;
      this._refreshData().catch((err) => this._showError(err));
    });
    label.appendChild(checkbox);
    label.appendChild(document.createTextNode("Show short trips"));
    header.appendChild(label);
    this._listPane.appendChild(header);

    this._breadcrumbEl = document.createElement("div");
    this._breadcrumbEl.className = "rde-breadcrumb";
    this._listPane.appendChild(this._breadcrumbEl);

    this._treeEl = document.createElement("ul");
    this._treeEl.className = "rde-tree";
    this._treeEl.setAttribute("role", "tree");
    this._listPane.appendChild(this._treeEl);

    this._footerEl = document.createElement("div");
    this._footerEl.className = "rde-footer";
    this._listPane.appendChild(this._footerEl);

    // Map pane
    this._mapPane = document.createElement("div");
    this._mapPane.className = "rde-map-pane";
    this._body.appendChild(this._mapPane);

    this._mapContainer = document.createElement("div");
    this._mapContainer.className = "rde-map-container";
    this._mapPane.appendChild(this._mapContainer);

    this._mapEl = document.createElement("div");
    this._mapEl.className = "rde-map";
    this._mapContainer.appendChild(this._mapEl);

    this._overlayEl = document.createElement("div");
    this._overlayEl.className = "rde-map-overlay";
    this._overlayEl.style.display = "none";
    this._mapContainer.appendChild(this._overlayEl);

    this._legendWrapEl = document.createElement("div");
    this._legendWrapEl.className = "rde-legend-wrap";
    this._mapContainer.appendChild(this._legendWrapEl);

    // Route color toggle: only shown (in day/segment mode) once a track
    // exists to color. Sits above the legend it controls.
    this._routeToggleEl = document.createElement("div");
    this._routeToggleEl.className = "rde-route-toggle";
    this._routeToggleEl.style.display = "none";
    for (const [key, label] of [
      ["speed", "Speed"],
      ["elevation", "Elevation"],
      ["efficiency", "Efficiency"],
    ]) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.dataset.mode = key;
      _escapeText(btn, label);
      btn.title = ROUTE_COLOR_TITLES[key] || label;
      btn.setAttribute("aria-label", ROUTE_COLOR_TITLES[key] || label);
      btn.addEventListener("click", () => {
        if (this._routeColorMode === key) return;
        this._routeColorMode = key;
        _writeEnumPref(ROUTE_COLOR_KEY, key);
        this._markActiveRouteColorMode();
        this._renderMapForSelection().catch((err) => this._showError(err));
      });
      this._routeToggleEl.appendChild(btn);
    }
    this._legendWrapEl.appendChild(this._routeToggleEl);
    this._markActiveRouteColorMode();

    this._legendEl = document.createElement("div");
    this._legendEl.className = "rde-legend";
    this._legendEl.style.display = "none";
    this._legendWrapEl.appendChild(this._legendEl);

    // Basemap switcher; hidden when the config pins its own tile_url.
    if (!(this._config && this._config.tile_url)) {
      this._basemapEl = document.createElement("div");
      this._basemapEl.className = "rde-basemaps";
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

    // Charts panel: time-series charts for the selected day/drive, between
    // the map and the stats row. Hidden by _hideCharts() until a day or
    // segment selection populates it.
    this._chartsPanelEl = document.createElement("div");
    this._chartsPanelEl.className = "rde-charts-panel";
    this._chartsPanelEl.style.display = "none";
    this._mapPane.appendChild(this._chartsPanelEl);
    this._buildChartsChrome();

    this._statsEl = document.createElement("div");
    this._statsEl.className = "rde-stats";
    this._mapPane.appendChild(this._statsEl);

    this._applyLayout();
    this._observeResize();
    this._renderTree();
    this._renderBreadcrumb();
  }

  _observeResize() {
    if (this._resizeObserver || typeof ResizeObserver === "undefined") return;
    this._resizeObserver = new ResizeObserver(() => {
      this._applyLayout();
      this._reapplyMapView();
      if (this._chartsData && !this._chartsCollapsed) this._drawCharts();
    });
    this._resizeObserver.observe(this);
  }

  _applyLayout() {
    const width = this.getBoundingClientRect ? this.getBoundingClientRect().width : 0;
    const stacked = width > 0 && width < STACK_BREAKPOINT_PX;
    this._body.classList.toggle("rde-stacked", stacked);
    this._applyCardHeight(stacked);
    this._applyMapTouchMode();
    if (stacked !== this._stacked) {
      this._stacked = stacked;
      if (this._treeRows) this._renderTree();
    }
  }

  /** Stacked on a touch screen, one-finger drags scroll the page instead of panning the map (pinch still pans and zooms it). */
  _applyMapTouchMode() {
    if (!this._map || !this._map.dragging) return;
    const pageScrolls = !!this._stacked && _isTouchDevice();
    if (pageScrolls && this._map.dragging.enabled()) this._map.dragging.disable();
    else if (!pageScrolls && !this._map.dragging.enabled()) this._map.dragging.enable();
  }

  /** Side by side, the card fills the view; stacked (phone width), it grows to its content so the page scrolls to the charts. A configured `height` always wins. */
  _applyCardHeight(stacked) {
    const height = (this._config && this._config.height) || null;
    if (height) this._card.style.height = height;
    else this._card.style.height = stacked ? "auto" : "calc(100vh - var(--header-height, 56px) - 32px)";
    this._card.style.minHeight = stacked ? "" : "480px";
  }

  connectedCallback() {
    if (!this._built) return;
    // HA detaches a view when switching tabs and re-attaches it on return.
    // The Leaflet map survives that; it only needs its size re-measured.
    this._observeResize();
    this._applyLayout();
    if (this._map) requestAnimationFrame(() => this._reapplyMapView());
    if (this._hass && !this._started) {
      this._started = true;
      this._start().catch((err) => this._showError(err));
    }
    if (this._hass) this._subscribe();
    this._listenSelection();
    this._applyOpenRequest().catch((err) => this._showError(err));
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

  /** The WebSocket scope for the current selection: `{vin}` for one vehicle, else `{vins}`. */
  _scope() {
    return this._multi ? { vins: [...this._vins] } : { vin: this._vins[0] };
  }

  /** The vehicle a single-vehicle action (delete, place edit) applies to when a segment carries no `vin`. */
  _primaryVin() {
    return this._vins[0];
  }

  _subscribe() {
    if (this._unsubPromise || !this._hass || !this._vins.length) return;
    this._unsubPromise = this._hass.connection
      .subscribeMessage(
        () => {
          this._refreshData().catch((err) => this._showError(err));
        },
        { type: "rivian/analytics/subscribe", ...this._scope() }
      )
      .catch((err) => {
        // Live refresh is a nicety; the card still works without it.
        console.warn("rivian-drive-explorer-card: live updates unavailable", err);
        return null;
      });
  }

  _unsubscribe() {
    if (!this._unsubPromise) return;
    this._unsubPromise.then((unsub) => unsub && unsub()).catch(() => {});
    this._unsubPromise = null;
  }

  // -- selection persistence -----------------------------------------------

  _selectionStorageKey() {
    // One vehicle keeps the historic per-VIN key; a vehicle set gets its own.
    return `rivian-drive-explorer-selection:${[...this._vins].sort().join(",")}`;
  }

  _saveSelection() {
    try {
      window.localStorage.setItem(this._selectionStorageKey(), JSON.stringify(this._selection));
    } catch (_err) {
      // Not persisted; the choice still applies for this session.
    }
  }

  /**
   * A one-shot "open this drive" request another card (Efficiency) left in
   * localStorage, consumed so it applies once. Null when absent or stale.
   */
  _takeOpenRequest() {
    try {
      const raw = window.localStorage.getItem(OPEN_REQUEST_KEY);
      if (!raw) return null;
      window.localStorage.removeItem(OPEN_REQUEST_KEY);
      const parsed = JSON.parse(raw);
      if (!parsed || !parsed.selection || !parsed.selection.level) return null;
      if (!(Date.now() - Number(parsed.ts) < OPEN_REQUEST_MAX_AGE_MS)) return null;
      return parsed.selection;
    } catch (_err) {
      return null;
    }
  }

  /** Jump to a pending open request when the view is shown again with the card already loaded. */
  async _applyOpenRequest() {
    if (!this._cache || !this._cache.root) return;
    const request = this._takeOpenRequest();
    if (!request) return;
    const selection = await this._validateSelection(request);
    if (selection) await this._selectNode(selection);
  }

  _loadStoredSelection() {
    try {
      const raw = window.localStorage.getItem(this._selectionStorageKey());
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== "object" || !parsed.level) return null;
      return parsed;
    } catch (_err) {
      return null;
    }
  }

  // -- calendar/day data loading --------------------------------------------

  async _loadYearRaw(yearKey) {
    if (!this._cache.years[yearKey]) {
      this._cache.years[yearKey] = await this._hass.callWS({
        type: "rivian/analytics/calendar",
        ...this._scope(),
        year: Number(yearKey),
        include_micro: this._includeMicro,
      });
    }
    return this._cache.years[yearKey];
  }

  async _loadMonthRaw(yearKey, monthKey) {
    if (!this._cache.months[monthKey]) {
      this._cache.months[monthKey] = await this._hass.callWS({
        type: "rivian/analytics/calendar",
        ...this._scope(),
        year: Number(yearKey),
        month: Number(monthKey.slice(5, 7)),
        include_micro: this._includeMicro,
      });
    }
    return this._cache.months[monthKey];
  }

  async _loadDayRaw(dayKey) {
    if (!this._cache.days[dayKey]) {
      this._cache.days[dayKey] = await this._hass.callWS({
        type: "rivian/analytics/day",
        ...this._scope(),
        date: dayKey,
        include_micro: this._includeMicro,
      });
    }
    return this._cache.days[dayKey];
  }

  /** Fetch whatever cache levels `selection`'s path needs, re-rendering the tree as each lands. */
  async _ensurePath(selection) {
    const token = this._renderToken;
    if (selection.level === "all") return;
    const yearKey = selection.key.slice(0, 4);
    await this._loadYearRaw(yearKey);
    if (token !== this._renderToken) return;
    this._renderTree();
    if (selection.level === "year") return;
    const monthKey = selection.key.slice(0, 7);
    await this._loadMonthRaw(yearKey, monthKey);
    if (token !== this._renderToken) return;
    this._renderTree();
    if (selection.level === "month") return;
    const dayKey = selection.key;
    await this._loadDayRaw(dayKey);
    if (token !== this._renderToken) return;
    this._renderTree();
  }

  /** Confirm `candidate` still exists in cache, loading down its path as needed. Null if stale. */
  async _validateSelection(candidate) {
    if (!candidate || candidate.level === "all") return { level: "all" };
    if (!this._multi && candidate.vin !== undefined) {
      // One vehicle: segments carry no `vin`, so a vin-tagged selection would never match.
      const { vin: _vin, ...rest } = candidate;
      candidate = rest;
    }
    const root = this._cache.root;
    if (!root) return null;
    const yearKey = candidate.key ? candidate.key.slice(0, 4) : null;
    if (!yearKey || !root.years.some((y) => y.key === yearKey)) return null;
    if (candidate.level === "year") return candidate;
    const yearData = await this._loadYearRaw(yearKey);
    const monthKey = candidate.key.slice(0, 7);
    if (!yearData.months || !yearData.months.some((m) => m.key === monthKey)) return null;
    if (candidate.level === "month") return candidate;
    const monthData = await this._loadMonthRaw(yearKey, monthKey);
    const dayKey = candidate.key;
    if (!monthData.days || !monthData.days.some((d) => d.key === dayKey)) return null;
    if (candidate.level === "day") return candidate;
    const dayData = await this._loadDayRaw(dayKey);
    if (
      !dayData.segments ||
      !dayData.segments.some(
        (s) => s.drive_id === candidate.driveId && (s.vin ?? null) === (candidate.vin ?? null)
      )
    ) {
      return null;
    }
    return candidate;
  }

  /** The most recent day (newest year -> newest month -> newest day), or a shallower fallback. */
  async _computeDefaultSelection() {
    const root = this._cache.root;
    if (!root || !root.years || !root.years.length) return { level: "all" };
    const yearKey = root.years[0].key;
    const yearData = await this._loadYearRaw(yearKey);
    if (!yearData.months || !yearData.months.length) return { level: "year", key: yearKey };
    const monthKey = yearData.months[0].key;
    const monthData = await this._loadMonthRaw(yearKey, monthKey);
    if (!monthData.days || !monthData.days.length) return { level: "month", key: monthKey };
    return { level: "day", key: monthData.days[0].key };
  }

  async _start() {
    await this._initScope();
    if (!this._vins.length) {
      this._showMessage("No vehicles to show.");
      return;
    }
    this._subscribe();
    await this._loadAll();
  }

  /** Work out which vehicles to show: the config's fixed set, else the shared selection (with the vehicle bar). */
  async _initScope() {
    const fixed = RivianDriveExplorerCard._fixedVins(this._config);
    try {
      this._bar = await _loadBarModule();
      this._vehicleList = await this._bar.getVehicles(this._hass);
    } catch (err) {
      console.warn("rivian-drive-explorer-card: vehicle list unavailable", err);
    }
    if (fixed) {
      this._followStore = false;
      this._vins = fixed;
    } else {
      this._followStore = true;
      try {
        this._vins = this._bar ? await this._bar.getSelection(this._hass) : [];
      } catch (err) {
        console.warn("rivian-drive-explorer-card: vehicle selection unavailable", err);
        this._vins = [];
      }
      this._listenSelection();
      this._mountBar();
    }
    this._multi = this._vins.length > 1;
  }

  _mountBar() {
    if (this._barEl || !this._bar) return;
    const el = document.createElement("rivian-vehicle-bar");
    el.hass = this._hass;
    this._topbarEl.appendChild(el);
    this._topbarEl.style.display = "";
    this._barEl = el;
  }

  _listenSelection() {
    if (this._unsubSelection || !this._followStore || !this._bar) return;
    this._unsubSelection = this._bar.onSelectionChange((vins) => this._onSelectionChanged(vins));
  }

  _onSelectionChanged(vins) {
    if (!this._followStore || !this._bar) return;
    const next = this._bar.normalizeSelection(vins, this._vehicleList);
    if (this._bar.sameSelection(next, this._vins)) return;
    this._unsubscribe();
    this._vins = next;
    this._multi = next.length > 1;
    this._resetState();
    this._statsEl.textContent = "";
    this._renderTree();
    this._renderBreadcrumb();
    this._subscribe();
    this._loadAll().catch((err) => this._showError(err));
  }

  /** `color` (light) or `colorDark` for the current theme. */
  _pickColor(color, colorDark) {
    const dark = !!(this._hass && this._hass.themes && this._hass.themes.darkMode);
    return (dark ? colorDark || color : color) || null;
  }

  _vehicleOf(vin) {
    return this._vehicleList.find((v) => v.vin === vin) || null;
  }

  _colorOfVin(vin) {
    const v = this._vehicleOf(vin);
    return this._pickColor(v && v.color, v && v.color_dark) || "#888888";
  }

  async _loadAll() {
    try {
      this._cache.root = await this._hass.callWS({
        type: "rivian/analytics/calendar",
        ...this._scope(),
        include_micro: this._includeMicro,
      });
    } catch (err) {
      this._showError(err);
      return;
    }
    const stored = this._takeOpenRequest() || this._loadStoredSelection();
    let selection = await this._validateSelection(stored);
    if (!selection) selection = await this._computeDefaultSelection();
    await this._selectNode(selection);
    this._loadFooter();
  }

  /** Re-fetch everything (a live update, or the "show short trips" toggle), keeping the selection if it still exists. */
  async _refreshData() {
    const previous = this._selection;
    this._cache = { root: null, years: {}, months: {}, days: {} };
    try {
      this._cache.root = await this._hass.callWS({
        type: "rivian/analytics/calendar",
        ...this._scope(),
        include_micro: this._includeMicro,
      });
    } catch (err) {
      this._showError(err);
      return;
    }
    let selection = await this._validateSelection(previous);
    if (!selection) selection = await this._computeDefaultSelection();
    await this._selectNode(selection);
    this._loadFooter();
  }

  async _selectNode(selection) {
    this._selection = selection;
    this._saveSelection();
    const token = ++this._renderToken;
    this._renderTree();
    this._renderBreadcrumb();
    try {
      await this._ensurePath(selection);
    } catch (err) {
      if (token === this._renderToken) this._showError(err);
      return;
    }
    if (token !== this._renderToken) return;
    this._renderTree();
    this._renderBreadcrumb();
    await this._renderMapForSelection();
  }

  async _loadFooter() {
    try {
      const result = await this._hass.callWS({
        type: "rivian/analytics/drives",
        ...this._scope(),
        limit: 1,
      });
      this._storage = result.storage || null;
      this._renderFooter();
    } catch (_err) {
      // The footer is a nicety; the tree/map still work without it.
    }
  }

  _renderFooter() {
    this._footerEl.textContent = "";
    if (!this._storage) return;
    const trackCount = this._storage.track_count ?? 0;
    const dbBytes = formatBytes(this._storage.db_bytes);
    _escapeText(this._footerEl, `Routes stored: ${trackCount} drives · ${dbBytes}`);
  }

  // -- tree rendering ---------------------------------------------------------

  _rowKey(row) {
    return `${row.level}:${row.key}:${row.driveId || ""}:${row.vin || ""}`;
  }

  _renderTree() {
    const tz = this._hass && this._hass.config ? this._hass.config.time_zone : undefined;
    const allRows = visibleTreeRows(this._cache, this._selection, tz, {
      multi: this._multi,
      vehicles: this._vehicleList,
    });
    const rows = this._stacked ? drillDownRows(allRows) : allRows;
    this._treeRows = rows;
    this._treeEl.textContent = "";

    let focusIndex = rows.findIndex((r) => this._rowKey(r) === this._focusedRowKey);
    if (focusIndex < 0) focusIndex = rows.findIndex((r) => r.selected);
    if (focusIndex < 0) focusIndex = 0;

    rows.forEach((row, i) => {
      const li = document.createElement("li");
      li.setAttribute("role", "treeitem");
      li.setAttribute("aria-level", String(row.ariaLevel));
      li.setAttribute("aria-expanded", row.hasChevron ? String(row.expanded) : "false");
      li.setAttribute("aria-selected", String(row.selected));
      li.tabIndex = i === focusIndex ? 0 : -1;
      li.className = "rde-tree-row" + (row.selected ? " selected" : "");
      li.title = [row.label, row.meta].filter(Boolean).join(" \u00b7 ");
      li.style.setProperty("--rde-indent", String(this._stacked ? 0 : row.ariaLevel - 1));

      const chevron = document.createElement("span");
      if (row.hasChevron) {
        chevron.className = "rde-chevron" + (row.expanded ? " expanded" : "");
        chevron.textContent = "▸";
      } else {
        chevron.className = "rde-chevron-spacer";
      }
      li.appendChild(chevron);

      const text = document.createElement("div");
      text.className = "rde-row-text";
      const labelEl = document.createElement("div");
      labelEl.className = "rde-row-label";
      if (row.number) {
        const num = document.createElement("span");
        num.className = "rde-seg-num";
        _escapeText(num, String(row.number));
        if (row.color) {
          // Several vehicles: the drive's number wears its vehicle's color.
          const c = this._pickColor(row.color, row.colorDark);
          num.style.background = c;
          num.style.color = inkOn(c);
        }
        labelEl.appendChild(num);
        labelEl.appendChild(document.createTextNode(row.label));
      } else {
        _escapeText(labelEl, row.label);
      }
      text.appendChild(labelEl);

      if (row.meta || row.badges.length || (row.vinCounts && row.vinCounts.length)) {
        const metaEl = document.createElement("div");
        metaEl.className = "rde-row-meta";
        if (row.meta) {
          const span = document.createElement("span");
          _escapeText(span, row.meta);
          metaEl.appendChild(span);
        }
        for (const c of row.vinCounts || []) {
          const chip = document.createElement("span");
          chip.className = "rde-vcount";
          const dot = document.createElement("i");
          dot.style.background = this._pickColor(c.color, c.colorDark) || "#888";
          chip.appendChild(dot);
          chip.appendChild(document.createTextNode(`${c.letter} ${c.drives}`));
          chip.title = `${c.drives} drives · ${c.miles.toFixed(1)} mi`;
          metaEl.appendChild(chip);
        }
        for (const badge of row.badges) {
          const b = document.createElement("span");
          b.className = "rde-badge";
          _escapeText(b, badge);
          b.title = String(badge);
          metaEl.appendChild(b);
        }
        text.appendChild(metaEl);
      }
      li.appendChild(text);

      li.addEventListener("click", () => this._onRowActivate(row));
      li.addEventListener("keydown", (ev) => this._onRowKeydown(ev, i));
      li.addEventListener("focus", () => {
        this._focusedRowKey = this._rowKey(row);
      });
      this._treeEl.appendChild(li);
    });

    if (!rows.length) {
      const empty = document.createElement("li");
      empty.className = "rde-empty";
      _escapeText(empty, "No drives recorded yet.");
      this._treeEl.appendChild(empty);
    }
  }

  _onRowActivate(row) {
    const selection =
      row.level === "segment"
        ? { level: "segment", key: row.key, driveId: row.driveId, ...(row.vin ? { vin: row.vin } : {}) }
        : { level: row.level, key: row.key };
    this._focusedRowKey = this._rowKey(row);
    this._selectNode(selection).catch((err) => this._showError(err));
    // Phone width: the list is below the map, charts and stats, so bring the
    // top of the card (the map) back into view.
    if (this._stacked && this._card.scrollIntoView) {
      const top = this._card.getBoundingClientRect().top;
      if (top < 0) this._card.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }

  _focusRowAt(index) {
    const items = this._treeEl.querySelectorAll('[role="treeitem"]');
    const el = items[index];
    if (!el) return;
    items.forEach((it, i) => {
      it.tabIndex = i === index ? 0 : -1;
    });
    this._focusedRowKey = this._rowKey(this._treeRows[index]);
    el.focus();
  }

  _onRowKeydown(ev, index) {
    const rows = this._treeRows;
    if (ev.key === "ArrowDown") {
      ev.preventDefault();
      this._focusRowAt(Math.min(rows.length - 1, index + 1));
    } else if (ev.key === "ArrowUp") {
      ev.preventDefault();
      this._focusRowAt(Math.max(0, index - 1));
    } else if (ev.key === "Enter" || ev.key === " ") {
      ev.preventDefault();
      this._onRowActivate(rows[index]);
    } else if (ev.key === "ArrowRight") {
      ev.preventDefault();
      const row = rows[index];
      if (row.hasChevron && !row.expanded) {
        this._onRowActivate(row);
      } else if (row.expanded && index + 1 < rows.length && rows[index + 1].ariaLevel > row.ariaLevel) {
        this._focusRowAt(index + 1);
      }
    } else if (ev.key === "ArrowLeft") {
      ev.preventDefault();
      const row = rows[index];
      for (let i = index - 1; i >= 0; i--) {
        if (rows[i].ariaLevel < row.ariaLevel) {
          this._onRowActivate(rows[i]);
          break;
        }
      }
    }
  }

  _renderBreadcrumb() {
    const tz = this._hass && this._hass.config ? this._hass.config.time_zone : undefined;
    const parts = breadcrumbParts(this._cache, this._selection, tz, {
      multi: this._multi,
      vehicles: this._vehicleList,
    });
    this._breadcrumbEl.textContent = "";
    parts.forEach((part, i) => {
      if (i > 0) {
        const sep = document.createElement("span");
        sep.className = "rde-breadcrumb-sep";
        _escapeText(sep, "›");
        this._breadcrumbEl.appendChild(sep);
      }
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "rde-breadcrumb-part" + (i === parts.length - 1 ? " current" : "");
      _escapeText(btn, part.label);
      if (i < parts.length - 1) btn.title = `Go back to ${part.label}`;
      else btn.setAttribute("aria-current", "location");
      if (i < parts.length - 1) {
        btn.addEventListener("click", () => {
          const selection =
            part.level === "segment"
              ? { level: "segment", key: part.key, driveId: part.driveId, ...(part.vin ? { vin: part.vin } : {}) }
              : { level: part.level, key: part.key };
          this._selectNode(selection).catch((err) => this._showError(err));
        });
      }
      this._breadcrumbEl.appendChild(btn);
    });
  }

  // -- stats tiles ------------------------------------------------------------

  _setStatTiles(tiles) {
    this._statsEl.textContent = "";
    for (const [label, value, wide] of tiles) {
      const tile = document.createElement("div");
      tile.className = wide ? "rde-stat rde-stat-wide" : "rde-stat";
      const tileTitle = statTileTitle(label);
      if (tileTitle) tile.title = tileTitle;
      const valEl = document.createElement("div");
      valEl.className = "rde-stat-value";
      _escapeText(valEl, value);
      const labEl = document.createElement("div");
      labEl.className = "rde-stat-label";
      _escapeText(labEl, label);
      tile.appendChild(valEl);
      tile.appendChild(labEl);
      this._statsEl.appendChild(tile);
    }
  }

  _findAgg(level, key) {
    if (level === "all") return this._cache.root && this._cache.root.totals;
    if (level === "year") {
      return this._cache.root && this._cache.root.years.find((y) => y.key === key);
    }
    if (level === "month") {
      const yearData = this._cache.years[key.slice(0, 4)];
      return yearData && yearData.months && yearData.months.find((m) => m.key === key);
    }
    if (level === "day") {
      const monthData = this._cache.months[key.slice(0, 7)];
      return monthData && monthData.days && monthData.days.find((d) => d.key === key);
    }
    return null;
  }

  // Value is the short label (a bare year/month/day), and the "Busiest X"
  // phrase plus rounded mileage go in the *label* instead -- keeps the tile
  // narrow enough to never wrap, even at 400px, unlike a single long value
  // like "Sun, Sep 6 · 241.0 mi".
  _busiestChild(period, key) {
    if (period === "all") {
      const years = (this._cache.root && this._cache.root.years) || [];
      const top = years.reduce((a, b) => (!a || b.miles > a.miles ? b : a), null);
      return top ? [`Busiest year · ${Math.round(top.miles)} mi`, top.key] : null;
    }
    if (period === "year") {
      const yearData = this._cache.years[key];
      const months = (yearData && yearData.months) || [];
      const top = months.reduce((a, b) => (!a || b.miles > a.miles ? b : a), null);
      return top ? [`Busiest month · ${Math.round(top.miles)} mi`, formatMonthLabel(top.key)] : null;
    }
    if (period === "month") {
      const monthData = this._cache.months[key];
      const days = (monthData && monthData.days) || [];
      const top = days.reduce((a, b) => (!a || b.miles > a.miles ? b : a), null);
      return top ? [`Busiest day · ${Math.round(top.miles)} mi`, _shortDayLabel(top.key)] : null;
    }
    return null;
  }

  _renderHeatStats(period, key) {
    const agg = this._findAgg(period, key) || {};
    const withRoute =
      typeof agg.with_route === "number" && typeof agg.drives === "number"
        ? `${agg.with_route} of ${agg.drives}`
        : "–";
    const tiles = [
      ["Distance", _fmtMiles(agg.miles)],
      ["Drives", _fmtNum(agg.drives || 0)],
      ["Driving time", formatDuration((agg.hours || 0) * 3600)],
      ["Energy", typeof agg.energy_kwh === "number" ? `${agg.energy_kwh.toFixed(1)} kWh` : "–"],
      [
        "Efficiency",
        typeof agg.efficiency_mi_kwh === "number" ? `${agg.efficiency_mi_kwh.toFixed(2)} mi/kWh` : "–",
      ],
      ["With route", withRoute],
    ];
    const busiest = this._busiestChild(period, key);
    if (busiest) tiles.push(busiest);
    this._setStatTiles(tiles);
  }

  _renderDayStats(dayData) {
    const totals = dayData.totals || {};
    const tz = this._hass && this._hass.config ? this._hass.config.time_zone : undefined;
    const tiles = [
      ["Distance", _fmtMiles(totals.miles)],
      ["Drives", _fmtNum(totals.drives || 0)],
      ["Driving time", formatDuration((totals.hours || 0) * 3600)],
      ["Energy", typeof totals.energy_kwh === "number" ? `${totals.energy_kwh.toFixed(1)} kWh` : "–"],
      [
        "Efficiency",
        typeof totals.efficiency_mi_kwh === "number"
          ? `${totals.efficiency_mi_kwh.toFixed(2)} mi/kWh`
          : "–",
      ],
      ["Stops", String((dayData.stops || []).length)],
      // Two tiles rather than one "Start → End" value, which wraps at 1400px.
      ["Day start", dayData.start ? _timeLabel(dayData.start.ts, tz) : "–"],
      ["Day end", dayData.end ? _timeLabel(dayData.end.ts, tz) : "–"],
    ];
    const segments = dayData.segments || [];
    const movingValues = segments.map((s) => s.moving_seconds).filter((v) => typeof v === "number");
    if (movingValues.length) {
      tiles.push(["Moving time", formatDuration(movingValues.reduce((a, b) => a + b, 0))]);
    }
    const climbValues = segments.map((s) => s.climb_ft).filter((v) => typeof v === "number");
    if (climbValues.length) {
      tiles.push(["Climb", `${Math.round(climbValues.reduce((a, b) => a + b, 0))} ft`]);
    }
    this._setStatTiles(tiles);
    this._appendDeleteButton("Delete day", () => deleteDayMessage(dayData, tz), () =>
      this._deleteDay(dayData.date)
    );
  }

  _renderSegmentStats(seg) {
    if (!seg) {
      this._statsEl.textContent = "";
      return;
    }
    const tz = this._hass && this._hass.config ? this._hass.config.time_zone : undefined;
    const tiles = [
      ["Distance", _fmtMiles(seg.distance_miles)],
      ["Duration", formatDuration(seg.duration_seconds)],
      ["Avg speed", typeof seg.avg_speed_mph === "number" ? `${seg.avg_speed_mph.toFixed(0)} mph` : "–"],
      ["Max speed", typeof seg.max_speed_mph === "number" ? `${seg.max_speed_mph.toFixed(0)} mph` : "–"],
      ["Energy", typeof seg.energy_kwh === "number" ? `${seg.energy_kwh.toFixed(1)} kWh` : "–"],
      [
        "Efficiency",
        typeof seg.efficiency_mi_kwh === "number" ? `${seg.efficiency_mi_kwh.toFixed(2)} mi/kWh` : "–",
      ],
      ["MPGe", typeof seg.mpge === "number" ? seg.mpge.toFixed(1) : "–"],
      ["Temp", typeof seg.temp_f === "number" ? `${seg.temp_f.toFixed(0)}°F` : "–"],
      [
        "Elevation Δ",
        typeof seg.elevation_change_ft === "number" ? `${Math.round(seg.elevation_change_ft)} ft` : "–",
      ],
      [
        "SoC",
        seg.start_soc != null && seg.end_soc != null
          ? `${Math.round(seg.start_soc)}% → ${Math.round(seg.end_soc)}%`
          : "–",
      ],
      ["Start", _timeLabel(seg.start_ts, tz)],
      ["End", _timeLabel(seg.end_ts, tz)],
    ];
    if (typeof seg.moving_seconds === "number") {
      tiles.push(["Moving time", formatDuration(seg.moving_seconds)]);
    }
    if (typeof seg.stop_count === "number") {
      tiles.push(["Stops", _fmtNum(seg.stop_count)]);
    }
    if (typeof seg.climb_ft === "number" || typeof seg.descent_ft === "number") {
      const climb = typeof seg.climb_ft === "number" ? Math.round(seg.climb_ft) : "–";
      const descent = typeof seg.descent_ft === "number" ? Math.round(seg.descent_ft) : "–";
      tiles.push(["Climb / Descent", `${climb} / ${descent} ft`]);
    }
    if (typeof seg.track_max_speed_mph === "number") {
      tiles.push(["Route max speed", `${Math.round(seg.track_max_speed_mph)} mph`]);
    }
    if (typeof seg.range_used_mi === "number") {
      tiles.push(["Range used", `${seg.range_used_mi.toFixed(1)} mi`]);
    }
    if (Array.isArray(seg.drive_modes) && seg.drive_modes.length) {
      tiles.push(["Drive mode", seg.drive_modes.join(", ")]);
    }
    if (seg.driver) {
      tiles.push(["Driver", seg.driver]);
    }
    if (seg.trailer !== null && seg.trailer !== undefined) {
      tiles.push(["Trailer", seg.trailer ? "Yes" : "No"]);
    }
    if (seg.route) {
      tiles.push(["Route", _routeTileText(seg.route), true]);
    }
    if (this._multi && seg.vin) {
      const vehicle = this._vehicleOf(seg.vin);
      if (vehicle) tiles.unshift(["Vehicle", `${vehicle.letter} · ${vehicle.name || vehicle.model || ""}`]);
    }
    this._setStatTiles(tiles);
    this._appendPlaceTiles(seg);
    this._appendDeleteButton("Delete drive", () => deleteDriveMessage(seg, tz), () =>
      this._deleteDrive(seg.drive_id, seg.vin)
    );
  }

  /** Admin-only red "Delete..." button appended under the stat tiles, with a confirm prompt. */
  _appendDeleteButton(label, buildMessage, onConfirmed) {
    const isAdmin = !!(this._hass && this._hass.user && this._hass.user.is_admin);
    if (!isAdmin) return;
    const row = document.createElement("div");
    row.className = "rde-delete-row";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "rde-danger-btn";
    _escapeText(btn, label);
    btn.title = `${label} (asks for confirmation first; this can't be undone)`;
    btn.addEventListener("click", () => {
      if (!window.confirm(buildMessage())) return;
      Promise.resolve(onConfirmed()).catch((err) => this._showError(err));
    });
    row.appendChild(btn);
    this._statsEl.appendChild(row);
  }

  /** Delete one drive, refresh the calendar/day caches, and select the day it was on. */
  async _deleteDrive(driveId, vin) {
    const dayKey = this._selection && this._selection.key;
    await this._hass.callWS({
      type: "rivian/analytics/delete_drive",
      vin: vin || this._primaryVin(),
      drive_id: driveId,
    });
    this._invalidateAfterDelete(dayKey);
    await this._selectNode(dayKey ? { level: "day", key: dayKey } : { level: "all" });
  }

  /** Delete every drive on a day, refresh the calendar caches, and select its month. */
  async _deleteDay(dayKey) {
    await this._hass.callWS({
      type: "rivian/analytics/delete_day",
      vin: this._primaryVin(),
      date: dayKey,
    });
    this._invalidateAfterDelete(dayKey);
    const monthKey = typeof dayKey === "string" ? dayKey.slice(0, 7) : null;
    await this._selectNode(monthKey ? { level: "month", key: monthKey } : { level: "all" });
  }

  /** Drop the deleted day's detail cache and every aggregate cache (counts/mileage changed). */
  _invalidateAfterDelete(dayKey) {
    if (dayKey && this._cache.days) delete this._cache.days[dayKey];
    this._cache.root = null;
    this._cache.years = {};
    this._cache.months = {};
  }

  /** From/To tiles appended after the plain stat tiles, with an admin "name this place" pencil. */
  _appendPlaceTiles(seg) {
    const isAdmin = !!(this._hass && this._hass.user && this._hass.user.is_admin);
    for (const side of ["start", "end"]) {
      const place = side === "start" ? seg.start_place : seg.end_place;
      const tile = document.createElement("div");
      tile.className = "rde-stat rde-stat-place";
      tile.title = STAT_TILE_TITLES[side === "start" ? "From" : "To"];
      const valEl = document.createElement("div");
      valEl.className = "rde-stat-value";
      _escapeText(valEl, place ? place.label : "—");
      const labEl = document.createElement("div");
      labEl.className = "rde-stat-label";
      _escapeText(labEl, side === "start" ? "From" : "To");
      tile.appendChild(valEl);
      tile.appendChild(labEl);
      if (isAdmin) {
        const pencil = document.createElement("button");
        pencil.type = "button";
        pencil.className = "rde-place-edit-btn";
        pencil.title = `Name or rename the ${side === "start" ? "starting" : "ending"} place`;
        pencil.setAttribute("aria-label", pencil.title);
        _escapeText(pencil, "✎");
        pencil.addEventListener("click", () => this._togglePlaceForm(tile, seg, side, place));
        tile.appendChild(pencil);
      }
      this._statsEl.appendChild(tile);
    }
  }

  /**
   * Fill a category <select> from the server's category list (shared with the
   * Places card), starting from the fallback list until it arrives. The list
   * is fetched once per card from `rivian/places/list`.
   */
  _fillCategorySelect(select, current, vin) {
    const fill = (categories) => {
      const wanted = select.value || current || "";
      select.textContent = "";
      const blank = document.createElement("option");
      blank.value = "";
      _escapeText(blank, "–");
      select.appendChild(blank);
      for (const category of categories) {
        const opt = document.createElement("option");
        opt.value = category.key;
        _escapeText(opt, category.label);
        select.appendChild(opt);
      }
      select.value = wanted;
    };
    fill(this._placeCategories || FALLBACK_PLACE_CATEGORIES);
    if (!this._placeCategories && !this._placeCategoriesPromise && this._hass) {
      this._placeCategoriesPromise = this._hass
        .callWS({ type: "rivian/places/list", ...(vin ? { vin } : {}) })
        .then((result) => {
          this._placeCategories = placeCategoriesFrom(result);
        })
        .catch(() => {
          this._placeCategories = null;
        })
        .finally(() => {
          this._placeCategoriesPromise = null;
        });
    }
    if (!this._placeCategories && this._placeCategoriesPromise) {
      this._placeCategoriesPromise.then(() => {
        if (this._placeCategories) fill(this._placeCategories);
      });
    }
  }

  /** Shows (or hides, if already open for this tile) an inline name/category form for a drive endpoint. */
  _togglePlaceForm(tile, seg, side, place) {
    if (this._placeFormEl && this._placeFormEl.parentElement === tile) {
      this._placeFormEl.remove();
      this._placeFormEl = null;
      return;
    }
    if (this._placeFormEl) this._placeFormEl.remove();

    const form = document.createElement("div");
    form.className = "rde-place-form";

    const nameInput = document.createElement("input");
    nameInput.type = "text";
    nameInput.placeholder = "Name this place";
    nameInput.value = place && place.name ? place.name : "";
    nameInput.setAttribute("aria-label", "Place name");

    const categorySelect = document.createElement("select");
    categorySelect.setAttribute("aria-label", "Place category");
    categorySelect.title = "Category: sets the icon and color of the place";
    this._fillCategorySelect(categorySelect, place && place.category, seg.vin || this._primaryVin());

    const saveBtn = document.createElement("button");
    saveBtn.type = "button";
    _escapeText(saveBtn, "Save");
    saveBtn.title = "Save this place name";
    saveBtn.addEventListener("click", () => {
      this._savePlaceName(seg, side, place, nameInput.value.trim(), categorySelect.value || null).catch(
        (err) => this._showError(err)
      );
    });

    const cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    _escapeText(cancelBtn, "Cancel");
    cancelBtn.title = "Close without saving";
    cancelBtn.addEventListener("click", () => {
      form.remove();
      this._placeFormEl = null;
    });

    form.appendChild(nameInput);
    form.appendChild(categorySelect);
    form.appendChild(saveBtn);
    form.appendChild(cancelBtn);
    tile.appendChild(form);
    this._placeFormEl = form;
    nameInput.focus();
  }

  /** Names a drive endpoint: updates its existing place, or creates one at that endpoint's coordinates. */
  async _savePlaceName(seg, side, place, name, category) {
    if (!name) return;
    const vin = seg.vin || this._primaryVin();
    if (place) {
      await this._hass.callWS({
        type: "rivian/places/update",
        vin,
        place_id: place.id,
        name,
        category,
      });
    } else {
      const lat = side === "start" ? seg.start_lat : seg.end_lat;
      const lon = side === "start" ? seg.start_lon : seg.end_lon;
      if (lat == null || lon == null) return;
      await this._hass.callWS({
        type: "rivian/places/create",
        vin,
        lat,
        lon,
        name,
        category,
      });
    }
    if (this._placeFormEl) {
      this._placeFormEl.remove();
      this._placeFormEl = null;
    }
    // The day's places changed: bypass the cached response so labels refresh.
    const dayKey = this._selection && this._selection.key;
    if (dayKey && this._cache.days) delete this._cache.days[dayKey];
    await this._selectNode(this._selection);
  }

  // -- charts: time-series panel under the map -----------------------------

  _buildChartsChrome() {
    const header = document.createElement("div");
    header.className = "rde-charts-header";

    this._chartsToggleBtn = document.createElement("button");
    this._chartsToggleBtn.type = "button";
    this._chartsToggleBtn.className = "rde-charts-toggle";
    this._chartsToggleBtn.title = "Show or hide the time charts under the map";
    this._chartsToggleBtn.addEventListener("click", () => {
      this._chartsCollapsed = !this._chartsCollapsed;
      _writeBoolPref(CHARTS_COLLAPSED_KEY, this._chartsCollapsed);
      this._applyChartsCollapsed();
    });
    header.appendChild(this._chartsToggleBtn);

    this._effSwitchEl = document.createElement("div");
    this._effSwitchEl.className = "rde-eff-switch";
    for (const [key, label] of [
      ["chunks", "3-min chunks"],
      ["rolling", "Rolling"],
      ["model", "Model"],
    ]) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.dataset.method = key;
      _escapeText(btn, label);
      btn.title = EFF_METHOD_TITLES[key] || label;
      btn.addEventListener("click", () => {
        if (btn.disabled || this._effMethod === key) return;
        this._effMethod = key;
        this._effMethodPref = key;
        _writeEnumPref(EFF_METHOD_KEY, key);
        this._markActiveEffMethod();
        this._redrawChartsForCurrentSelection();
      });
      this._effSwitchEl.appendChild(btn);
    }
    header.appendChild(this._effSwitchEl);
    this._chartsPanelEl.appendChild(header);

    this._effModelLegendEl = document.createElement("div");
    this._effModelLegendEl.className = "rde-eff-model-legend";
    this._effModelLegendEl.style.display = "none";
    const dash = document.createElement("span");
    dash.className = "rde-eff-model-legend-line";
    _escapeText(dash, "— estimate");
    const dot = document.createElement("span");
    dot.className = "rde-eff-model-legend-dot";
    _escapeText(dot, "● measured");
    this._effModelLegendEl.appendChild(dash);
    this._effModelLegendEl.appendChild(dot);
    this._chartsPanelEl.appendChild(this._effModelLegendEl);

    this._cursorReadoutEl = document.createElement("div");
    this._cursorReadoutEl.className = "rde-cursor-readout";
    _escapeText(this._cursorReadoutEl, "Hover or tap the charts or map to inspect a moment (arrow keys on a focused chart).");
    this._cursorReadoutEl.setAttribute("aria-live", "polite");
    this._chartsPanelEl.appendChild(this._cursorReadoutEl);

    this._chartsBodyEl = document.createElement("div");
    this._chartsBodyEl.className = "rde-charts-body";
    this._chartsPanelEl.appendChild(this._chartsBodyEl);

    this._markActiveEffMethod();
    this._applyChartsCollapsed();
  }

  _applyChartsCollapsed() {
    const collapsed = this._chartsCollapsed;
    _escapeText(this._chartsToggleBtn, collapsed ? "Charts ▸" : "Charts ▾");
    this._chartsToggleBtn.setAttribute("aria-expanded", String(!collapsed));
    this._chartsBodyEl.style.display = collapsed ? "none" : "";
    this._cursorReadoutEl.style.display = collapsed ? "none" : "";
    this._effSwitchEl.style.display = collapsed ? "none" : "";
    if (!collapsed) this._redrawChartsForCurrentSelection();
    // The map's own height changes either way (it grows when collapsed,
    // shrinks when expanded); refit once that layout has settled.
    requestAnimationFrame(() => this._reapplyMapView());
  }

  _markActiveEffMethod() {
    for (const btn of this._effSwitchEl.querySelectorAll("button")) {
      const active = btn.dataset.method === this._effMethod;
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-pressed", String(active));
    }
    if (this._effModelLegendEl) {
      this._effModelLegendEl.style.display = this._effMethod === "model" ? "flex" : "none";
    }
  }

  /**
   * Enable/disable the "Model" efficiency-method button for the current
   * chart selection (it needs at least one segment with a `model` estimate),
   * showing "chunks" while it's unavailable and returning to the user's
   * chosen method (default Model) once a selection has an estimate again.
   */
  _updateEffSwitchAvailability() {
    const data = this._chartsData;
    const hasModel = !!(data && data.segments && data.segments.some((s) => s && s.model));
    const btn = this._effSwitchEl && this._effSwitchEl.querySelector('button[data-method="model"]');
    if (!btn) return;
    btn.disabled = !hasModel;
    btn.classList.toggle("disabled", !hasModel);
    btn.title = hasModel ? EFF_METHOD_TITLES.model : "No model estimate available for this selection.";
    if (!hasModel && this._effMethod === "model") {
      this._effMethod = "chunks";
    } else if (hasModel && this._effMethodPref === "model") {
      this._effMethod = "model";
    }
    this._markActiveEffMethod();
  }

  /** The rolling-efficiency SoC-drop threshold, overridable by a harness/debug hook. */
  _socDropPct() {
    if (typeof window !== "undefined" && typeof window.__RDE_MIN_SOC_DROP_PCT === "number") {
      return window.__RDE_MIN_SOC_DROP_PCT;
    }
    return ROLLING_EFF_MIN_SOC_DROP_PCT;
  }

  _hideCharts() {
    this._chartsData = null;
    this._chartsSvg = null;
    this._chartsRows = null;
    if (this._chartsPanelEl) this._chartsPanelEl.style.display = "none";
    if (this._chartsBodyEl) this._chartsBodyEl.textContent = "";
  }

  _showChartsForDay(dayKey, dayData) {
    const segments = (dayData.segments || []).filter(
      (s) => s && typeof s.start_ts === "number" && typeof s.end_ts === "number"
    );
    if (!segments.length) {
      this._hideCharts();
      return;
    }
    this._chartsData = {
      mode: "day",
      dayKey,
      vin: dayData.vin,
      segments,
      allSegments: segments,
      priorTail: dayData.prior_tail || null,
    };
    this._chartsPanelEl.style.display = "";
    this._updateEffSwitchAvailability();
    if (!this._chartsCollapsed) this._drawCharts();
  }

  _showChartsForSegment(dayKey, dayData, seg) {
    if (!seg || typeof seg.start_ts !== "number" || typeof seg.end_ts !== "number") {
      this._hideCharts();
      return;
    }
    const allSegments = (dayData.segments || []).filter(
      (s) => s && typeof s.start_ts === "number" && typeof s.end_ts === "number"
    );
    const selectedIndex = allSegments.findIndex((s) => s.drive_id === seg.drive_id);
    this._chartsData = {
      mode: "segment",
      dayKey,
      vin: dayData.vin,
      segments: [seg],
      allSegments,
      selectedIndex: selectedIndex >= 0 ? selectedIndex : allSegments.length - 1,
      priorTail: dayData.prior_tail || null,
    };
    this._chartsPanelEl.style.display = "";
    this._updateEffSwitchAvailability();
    if (!this._chartsCollapsed) this._drawCharts();
  }

  /**
   * The chronological `{key, track, capacityKwh}` parts feeding
   * `continuousDriveSeries` for the current chart selection: an optional
   * `prior_tail` part, followed by every day segment with a track up to (in
   * segment mode) the selected drive -- so rolling/chunks can look back
   * across a drive boundary or into `prior_tail` instead of going blank at
   * a drive's own start.
   */
  _buildContinuousParts(data) {
    const parts = [];
    const priorTail = data.priorTail;
    if (priorTail && priorTail.track && Array.isArray(priorTail.track.t) && priorTail.track.t.length) {
      parts.push({ key: PRIOR_TAIL_KEY, track: priorTail.track, capacityKwh: priorTail.battery_capacity_kwh });
    }
    const allSegments = data.allSegments || data.segments || [];
    const upto =
      data.mode === "segment" && typeof data.selectedIndex === "number"
        ? data.selectedIndex
        : allSegments.length - 1;
    for (let i = 0; i <= upto; i++) {
      const seg = allSegments[i];
      if (seg && seg.track && Array.isArray(seg.track.t) && seg.track.t.length) {
        parts.push({ key: i, track: seg.track, capacityKwh: seg.battery_capacity_kwh });
      }
    }
    return parts;
  }

  /** A `(i) => capacityKwh` lookup over a continuous series, from its parts' own capacities. */
  _capacityForBuilder(series, parts) {
    const byKey = new Map();
    for (const part of parts) {
      const cap =
        typeof part.capacityKwh === "number" && part.capacityKwh > 0
          ? part.capacityKwh
          : DEFAULT_BATTERY_CAPACITY_KWH;
      byKey.set(part.key, cap);
    }
    return (i) => {
      const key = series.partOf[i];
      return byKey.has(key) ? byKey.get(key) : DEFAULT_BATTERY_CAPACITY_KWH;
    };
  }

  /** A drawn segment's real index into `data.allSegments` (day mode: same array; segment mode: `data.selectedIndex`). */
  _realSegIndex(data, segIdx) {
    return data.mode === "segment" ? data.selectedIndex : segIdx;
  }

  _redrawChartsForCurrentSelection() {
    if (this._chartsData) this._drawCharts();
  }

  /** Robust efficiency domain (mi/kWh): the 2nd-98th percentile, clamped to [0, 6]. */
  _efficiencyDomain(values) {
    if (!values.length) return [0, 6];
    const sorted = values.slice().sort((a, b) => a - b);
    const lo = sorted[Math.max(0, Math.floor(0.02 * (sorted.length - 1)))];
    const hi = sorted[Math.min(sorted.length - 1, Math.ceil(0.98 * (sorted.length - 1)))];
    const min = Math.max(0, lo);
    let max = Math.min(6, hi);
    if (max - min < 0.5) max = min + 0.5;
    return [min, max];
  }

  /** Plain min/max domain with a little padding; battery is clamped to [0, 100]. */
  _plainDomain(values, key) {
    let min = Infinity;
    let max = -Infinity;
    for (const v of values) {
      if (v < min) min = v;
      if (v > max) max = v;
    }
    if (max - min < 1e-6) {
      min -= 1;
      max += 1;
    }
    const pad = (max - min) * 0.08;
    let lo = min - pad;
    let hi = max + pad;
    // Speed and battery can't physically go negative; battery tops out at 100.
    if (key === "speed" || key === "battery") lo = Math.max(0, lo);
    if (key === "battery") hi = Math.min(100, hi);
    return [lo, hi];
  }

  /** One metric's series across every segment in the current chart selection. */
  _buildMetricSeries(key, segments, socDrop) {
    const data = this._chartsData;
    if (key === "efficiency" && this._effMethod === "chunks") {
      const parts = this._buildContinuousParts(data);
      const series = continuousDriveSeries(parts);
      const capacityFor = this._capacityForBuilder(series, parts);
      const contSteps = continuousChunkSteps(series, capacityFor);
      const contBySeg = new Map();
      for (const s of contSteps) {
        if (!contBySeg.has(s.seg)) contBySeg.set(s.seg, []);
        contBySeg.get(s.seg).push(s);
      }
      const steps = [];
      let any = false;
      segments.forEach((seg, segIdx) => {
        const realIdx = this._realSegIndex(data, segIdx);
        const hasTrackSoc =
          seg.track && Array.isArray(seg.track.soc) && seg.track.soc.some((v) => v !== null && v !== undefined);
        const segSteps =
          hasTrackSoc && contBySeg.has(realIdx)
            ? contBySeg.get(realIdx).map((s) => ({ ...s, seg: segIdx }))
            : chunkSteps(seg.chunks).map((s) => ({ ...s, seg: segIdx }));
        for (const s of segSteps) {
          if (s.value !== null) any = true;
          steps.push(s);
        }
      });
      if (!any) return { hasData: false };
      const values = steps.filter((s) => s.value !== null).map((s) => s.value);
      const [min, max] = this._efficiencyDomain(values);
      return { hasData: true, kind: "steps", steps, min, max };
    }

    // The Model method's smooth line comes from `model.eff` (per track
    // point, like rolling); its measured points come separately from
    // `model.points` ([t, mi/kWh] pairs between SoC steps) and are drawn as
    // a distinct dot overlay rather than folded into the line.
    const isModelEff = key === "efficiency" && this._effMethod === "model";
    const isRollingEff = key === "efficiency" && this._effMethod === "rolling";

    let contRolling = null;
    let contRanges = null;
    if (isRollingEff) {
      const parts = this._buildContinuousParts(data);
      const series = continuousDriveSeries(parts);
      const capacityFor = this._capacityForBuilder(series, parts);
      contRolling = rollingEfficiencySeries(series, capacityFor, socDrop);
      contRanges = series.ranges;
    }

    const points = [];
    const dots = isModelEff ? [] : null;
    let any = false;
    segments.forEach((seg, segIdx) => {
      const track = seg.track;
      if (!track || !Array.isArray(track.t)) return;
      const filledArr = Array.isArray(track.filled) ? track.filled : null;
      let values;
      if (key === "speed") values = track.t.map((_t, i) => _mphAt(track, i));
      else if (key === "elevation") {
        values = (track.alt_m || []).map((v) => (v === null || v === undefined ? null : v * METERS_TO_FEET));
      } else if (key === "battery") values = track.soc || [];
      else if (isModelEff) {
        values = seg.model && Array.isArray(seg.model.eff) ? seg.model.eff : [];
      } else if (isRollingEff) {
        const realIdx = this._realSegIndex(data, segIdx);
        const range = contRanges[realIdx];
        values = range ? contRolling.slice(range[0], range[1]) : new Array(track.t.length).fill(null);
      } else values = [];
      track.t.forEach((t, i) => {
        const filled = filledArr ? !!filledArr[i] : false;
        // A GPS-filled (reconstructed) stretch has no measured energy use,
        // so an efficiency estimate there isn't real -- leave it as a gap.
        const raw = key === "efficiency" && filled ? null : values[i];
        const value = raw === null || raw === undefined || Number.isNaN(raw) ? null : raw;
        if (value !== null) any = true;
        points.push({ t, value, seg: segIdx, filled });
      });
      if (isModelEff && seg.model && Array.isArray(seg.model.points)) {
        for (const pair of seg.model.points) {
          const t = pair && pair[0];
          const v = pair && pair[1];
          if (typeof t !== "number" || typeof v !== "number" || Number.isNaN(v)) continue;
          any = true;
          dots.push({ t, value: v, seg: segIdx });
        }
      }
    });
    if (!any) return { hasData: false };
    const finiteValues = points
      .filter((p) => p.value !== null)
      .map((p) => p.value)
      .concat(dots ? dots.map((d) => d.value) : []);
    const [min, max] =
      key === "efficiency" ? this._efficiencyDomain(finiteValues) : this._plainDomain(finiteValues, key);
    return { hasData: true, kind: "points", points, dots, min, max };
  }

  _drawCharts() {
    const data = this._chartsData;
    if (!data) return;
    const fullWidth = Math.max(200, (this._chartsPanelEl.clientWidth || 620) - 20);
    // A right-hand gutter the lines/steps never draw into, so a min/max
    // label at the row's right edge can't collide with the line it labels.
    const plotWidth = Math.max(120, fullWidth - CHART_RIGHT_GUTTER_PX);
    const gapPx = data.mode === "day" ? TIMELINE_GAP_PX_DEFAULT : 0;
    const timeline = dayTimeline(data.segments, gapPx, plotWidth);
    data.timeline = timeline;

    const socDrop = this._socDropPct();
    const rows = [];
    for (const metric of CHART_METRICS) {
      const built = this._buildMetricSeries(metric.key, data.segments, socDrop);
      if (built.hasData) rows.push({ metric, built });
    }

    this._chartsBodyEl.textContent = "";
    if (!rows.length) {
      const empty = document.createElement("div");
      empty.className = "rde-charts-empty";
      _escapeText(empty, "No chart data for this selection.");
      this._chartsBodyEl.appendChild(empty);
      this._chartsSvg = null;
      this._chartsRows = null;
      this._clearCursor();
      return;
    }

    const rowsHeight = rows.length * CHART_ROW_HEIGHT_PX;
    const cutHeight = CHART_BADGE_STRIP_PX + rowsHeight;
    const totalHeight = cutHeight + CHART_AXIS_HEIGHT_PX;
    const svgNS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("viewBox", `0 0 ${fullWidth} ${totalHeight}`);
    svg.setAttribute("width", "100%");
    svg.setAttribute("height", String(totalHeight));
    svg.setAttribute("preserveAspectRatio", "none");
    svg.setAttribute("tabindex", "0");
    svg.setAttribute("role", "group");
    svg.setAttribute(
      "aria-label",
      "Speed, elevation, efficiency and battery charts over time: hover or tap to read a moment, or use the left and right arrow keys"
    );

    const dark = !!(this._hass && this._hass.themes && this._hass.themes.darkMode);
    const pendingLabels = [];
    rows.forEach((row, i) =>
      this._drawChartRow(
        svg,
        row,
        timeline,
        CHART_BADGE_STRIP_PX + i * CHART_ROW_HEIGHT_PX,
        plotWidth,
        fullWidth,
        dark,
        pendingLabels
      )
    );
    for (const brk of timeline.breaks) this._drawBreakMark(svg, brk, cutHeight, data);
    // Day view: every drive's stretch gets a numbered badge, including the
    // first (the rest come from `_drawBreakMark` at each gap that follows).
    if (data.mode === "day" && timeline.pieces.length) {
      this._drawStartBadge(svg, timeline.pieces[0], data);
    }
    // Row labels/min/max are drawn last (on top of the break "cut" marks and
    // badges), so a short first drive can't hide a label behind either.
    for (const l of pendingLabels) this._drawChip(svg, l.text, l.x, l.y, l.anchor);
    const tz = this._hass && this._hass.config ? this._hass.config.time_zone : undefined;
    this._drawTimeAxis(svg, timeline, cutHeight, plotWidth, fullWidth, tz);

    if (data.mode === "day") {
      svg.addEventListener("click", (ev) => this._onChartsClick(ev, svg, fullWidth, plotWidth, timeline, data));
    }
    svg.addEventListener("keydown", (ev) => this._onChartsKey(ev, plotWidth, timeline));
    const onPointer = (ev) => this._onChartsPointerMove(ev, svg, fullWidth, plotWidth, timeline);
    svg.addEventListener("pointermove", onPointer);
    // A tap has no hover: show the cursor where the finger lands, and keep
    // it (and its readout) after the finger lifts instead of clearing it.
    svg.addEventListener("pointerdown", (ev) => {
      this._lastChartPointerType = ev.pointerType;
      onPointer(ev);
    });
    svg.addEventListener("pointerleave", (ev) => {
      if (ev.pointerType !== "touch" && ev.pointerType !== "pen") this._clearCursor();
    });

    this._chartsBodyEl.appendChild(svg);
    this._chartsSvg = svg;
    this._chartsPlotHeight = cutHeight;
    this._chartsPlotWidth = plotWidth;
    this._chartsRows = rows;
    this._cursorGroup = null;
    this._drawCursorOverlay();
    this._updateReadout();
  }

  /** A label with a translucent background chip, so a line (or a break's erase rect) crossing under it doesn't cut through the text. */
  _drawChip(svg, text, x, y, anchor) {
    const svgNS = "http://www.w3.org/2000/svg";
    const chipW = text.length * 5.4 + 6;
    const rect = document.createElementNS(svgNS, "rect");
    rect.setAttribute("x", String(anchor === "end" ? x - chipW : x - 2));
    rect.setAttribute("y", String(y - 8));
    rect.setAttribute("width", String(chipW));
    rect.setAttribute("height", "10");
    rect.setAttribute("fill", "var(--card-background-color, #fff)");
    rect.setAttribute("opacity", "0.9");
    svg.appendChild(rect);
    const el = document.createElementNS(svgNS, "text");
    el.setAttribute("x", String(x));
    el.setAttribute("y", String(y));
    el.setAttribute("text-anchor", anchor);
    el.setAttribute("font-size", "9");
    el.setAttribute("fill", "var(--secondary-text-color)");
    el.textContent = text;
    svg.appendChild(el);
  }

  _drawChartRow(svg, row, timeline, y0, plotWidth, fullWidth, dark, pendingLabels) {
    const svgNS = "http://www.w3.org/2000/svg";
    const { metric, built } = row;
    const color = dark ? metric.color.dark : metric.color.light;
    row.color = color;
    row.colorAt = null;
    const g = document.createElementNS(svgNS, "g");
    // Speed is colored the way the map colors it: red (slow) -> green (fast),
    // on the same scale (the day's for a day, the drive's own for a drive).
    let stroke = color;
    if (metric.key === "speed" && built.kind === "points") {
      const data = this._chartsData;
      const scaleMax =
        data.mode === "segment" && data.segments[0] && data.segments[0].track
          ? speedScaleMax(data.segments[0].track)
          : dayScaleMax(data.segments);
      row.colorAt = (v) => speedColor(v, scaleMax);
      const gradId = this._appendSpeedGradient(svg, built.points, timeline, plotWidth, row.colorAt);
      if (gradId) stroke = `url(#${gradId})`;
    }

    if (y0 > CHART_BADGE_STRIP_PX) {
      const divider = document.createElementNS(svgNS, "line");
      divider.setAttribute("x1", "0");
      divider.setAttribute("x2", String(fullWidth));
      divider.setAttribute("y1", String(y0));
      divider.setAttribute("y2", String(y0));
      divider.setAttribute("stroke", "var(--divider-color, #e0e0e0)");
      divider.setAttribute("stroke-width", "1");
      g.appendChild(divider);
    }

    const padTop = 14;
    const padBottom = 6;
    const plotTop = y0 + padTop;
    const plotBottom = y0 + CHART_ROW_HEIGHT_PX - padBottom;
    const span = built.max - built.min;
    // Values past the domain (efficiency's is a robust 2nd-98th percentile)
    // are pinned to the row's edge rather than drawn into the next row.
    const yScale = (v) => {
      const frac = span > 0 ? Math.max(0, Math.min(1, (v - built.min) / span)) : 0.5;
      return plotBottom - frac * (plotBottom - plotTop);
    };
    row.yScale = yScale;
    row.plotTop = plotTop;
    row.plotBottom = plotBottom;

    if (built.kind === "steps") this._appendStepPath(g, built.steps, timeline, yScale, color);
    else this._appendLinePath(g, built.points, timeline, yScale, stroke);
    if (built.dots && built.dots.length) this._appendModelDots(g, built.dots, timeline, yScale, color);
    svg.appendChild(g);

    // Labels are drawn later, on top of every row and break mark (see
    // `_drawCharts`) -- a short first drive can otherwise put a break's
    // erase rect right where this row's own label sits. Min/max sit in the
    // right gutter (past `plotWidth`), clear of the line, which stops there.
    pendingLabels.push({ text: `${metric.label} (${metric.unit})`, x: 4, y: y0 + 10, anchor: "start" });
    pendingLabels.push({
      text: _formatMetricNumber(metric.key, built.max),
      x: fullWidth - 4,
      y: plotTop + 3,
      anchor: "end",
    });
    pendingLabels.push({
      text: _formatMetricNumber(metric.key, built.min),
      x: fullWidth - 4,
      y: plotBottom,
      anchor: "end",
    });
  }

  /**
   * A horizontal gradient whose color at each x is `colorAt` of the series
   * value there, so one path can be colored point by point (the series is a
   * function of x, time order). Stops are thinned to ~1 per pixel. Returns
   * the gradient's id, or null when there's nothing to color.
   */
  _appendSpeedGradient(svg, points, timeline, plotWidth, colorAt) {
    const svgNS = "http://www.w3.org/2000/svg";
    const stops = [];
    let lastX = -Infinity;
    for (const p of points) {
      if (p.value === null) continue;
      const x = Math.max(0, Math.min(plotWidth, timeline.tToX(p.t)));
      if (x - lastX < 1 && stops.length) continue;
      stops.push([x / plotWidth, colorAt(p.value)]);
      lastX = x;
    }
    if (!stops.length) return null;
    const id = "rde-speed-gradient";
    let defs = svg.querySelector("defs");
    if (!defs) {
      defs = document.createElementNS(svgNS, "defs");
      svg.insertBefore(defs, svg.firstChild);
    }
    const grad = document.createElementNS(svgNS, "linearGradient");
    grad.setAttribute("id", id);
    grad.setAttribute("gradientUnits", "userSpaceOnUse");
    grad.setAttribute("x1", "0");
    grad.setAttribute("x2", String(plotWidth));
    grad.setAttribute("y1", "0");
    grad.setAttribute("y2", "0");
    for (const [offset, c] of stops) {
      const stop = document.createElementNS(svgNS, "stop");
      stop.setAttribute("offset", offset.toFixed(5));
      stop.setAttribute("stop-color", c);
      grad.appendChild(stop);
    }
    defs.appendChild(grad);
    return id;
  }

  /**
   * Draw a metric's line, split into runs by `splitFilledRuns` so a
   * GPS-filled (reconstructed) stretch of speed/elevation/battery draws
   * dashed instead of solid, while measured stretches stay solid.
   */
  _appendLinePath(g, points, timeline, yScale, color) {
    const svgNS = "http://www.w3.org/2000/svg";
    for (const run of splitFilledRuns(points)) {
      if (run.points.length < 2) continue;
      let d = "";
      run.points.forEach((p, idx) => {
        const x = timeline.tToX(p.t);
        const y = yScale(p.value);
        d += `${idx === 0 ? "M" : " L"} ${x.toFixed(1)} ${y.toFixed(1)}`;
      });
      const path = document.createElementNS(svgNS, "path");
      path.setAttribute("d", d);
      path.setAttribute("fill", "none");
      path.setAttribute("stroke", color);
      path.setAttribute("stroke-width", "2");
      path.setAttribute("stroke-linejoin", "round");
      path.setAttribute("stroke-linecap", "round");
      if (run.filled) path.setAttribute("stroke-dasharray", "6 4");
      g.appendChild(path);
    }
  }

  /**
   * The Model efficiency method's measured points: small dots in the line's
   * own hue but visually distinct (hollow, so they read apart from the
   * smooth estimate line under them).
   */
  _appendModelDots(g, dots, timeline, yScale, color) {
    const svgNS = "http://www.w3.org/2000/svg";
    for (const d of dots) {
      const x = timeline.tToX(d.t);
      const y = yScale(d.value);
      const dot = document.createElementNS(svgNS, "circle");
      dot.setAttribute("cx", x.toFixed(1));
      dot.setAttribute("cy", y.toFixed(1));
      dot.setAttribute("r", "2.5");
      dot.setAttribute("fill", "var(--card-background-color, #fff)");
      dot.setAttribute("stroke", color);
      dot.setAttribute("stroke-width", "1.5");
      g.appendChild(dot);
    }
  }

  _appendStepPath(g, steps, timeline, yScale, color) {
    const svgNS = "http://www.w3.org/2000/svg";
    let d = "";
    let prevSeg = null;
    let prevX1 = null;
    let prevY = null;
    for (const s of steps) {
      if (s.value === null) {
        prevSeg = null;
        prevX1 = null;
        prevY = null;
        continue;
      }
      const x0 = timeline.tToX(s.t0);
      const x1 = timeline.tToX(s.t1);
      const y = yScale(s.value);
      const contiguous = prevSeg === s.seg && prevX1 !== null && Math.abs(x0 - prevX1) < 0.5;
      d += contiguous
        ? ` L ${x0.toFixed(1)} ${prevY.toFixed(1)} L ${x0.toFixed(1)} ${y.toFixed(1)}`
        : ` M ${x0.toFixed(1)} ${y.toFixed(1)}`;
      d += ` L ${x1.toFixed(1)} ${y.toFixed(1)}`;
      prevSeg = s.seg;
      prevX1 = x1;
      prevY = y;
    }
    if (!d.trim()) return;
    const path = document.createElementNS(svgNS, "path");
    path.setAttribute("d", d.trim());
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", color);
    path.setAttribute("stroke-width", "2");
    path.setAttribute("stroke-linejoin", "round");
    g.appendChild(path);
  }

  /** The compressed-time "cut" for a gap between drives: an erase rect, a zigzag, and the next drive's number badge. */
  /** A small numbered blue badge (a drive's own number, at the top of the charts), optionally clickable to select that drive. */
  _drawBadgeCircle(svg, cx, number, onSelect) {
    const svgNS = "http://www.w3.org/2000/svg";
    const g = document.createElementNS(svgNS, "g");
    const badge = document.createElementNS(svgNS, "circle");
    badge.setAttribute("cx", String(cx));
    badge.setAttribute("cy", String(CHART_BADGE_STRIP_PX / 2));
    badge.setAttribute("r", "8");
    badge.setAttribute("fill", "#1565c0");
    g.appendChild(badge);
    const text = document.createElementNS(svgNS, "text");
    text.setAttribute("x", String(cx));
    text.setAttribute("y", String(CHART_BADGE_STRIP_PX / 2 + 3));
    text.setAttribute("text-anchor", "middle");
    text.setAttribute("font-size", "9");
    text.setAttribute("font-weight", "600");
    text.setAttribute("fill", "#ffffff");
    text.textContent = String(number);
    g.appendChild(text);
    const tip = document.createElementNS(svgNS, "title");
    tip.textContent = onSelect ? `Drive ${number}: click to select it` : `Drive ${number}`;
    g.appendChild(tip);
    g.setAttribute("aria-label", tip.textContent);
    if (onSelect) {
      g.style.cursor = "pointer";
      g.setAttribute("role", "button");
      g.setAttribute("tabindex", "0");
      g.addEventListener("click", (ev) => {
        ev.stopPropagation();
        onSelect();
      });
      g.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          ev.stopPropagation();
          onSelect();
        }
      });
    } else {
      g.setAttribute("role", "img");
    }
    svg.appendChild(g);
  }

  _drawBreakMark(svg, brk, plotHeight, data) {
    const svgNS = "http://www.w3.org/2000/svg";
    const g = document.createElementNS(svgNS, "g");
    const rect = document.createElementNS(svgNS, "rect");
    rect.setAttribute("x", String(brk.x0));
    rect.setAttribute("y", "0");
    rect.setAttribute("width", String(Math.max(0.5, brk.x1 - brk.x0)));
    rect.setAttribute("height", String(plotHeight));
    rect.setAttribute("fill", "var(--card-background-color, #fff)");
    g.appendChild(rect);

    const midX = (brk.x0 + brk.x1) / 2;
    const amp = Math.max(2, (brk.x1 - brk.x0) / 4);
    const step = 10;
    let d = `M ${(midX - amp).toFixed(1)} 0`;
    let y = 0;
    let sign = 1;
    while (y < plotHeight) {
      y = Math.min(plotHeight, y + step);
      d += ` L ${(midX + sign * amp).toFixed(1)} ${y.toFixed(1)}`;
      sign *= -1;
    }
    const zig = document.createElementNS(svgNS, "path");
    zig.setAttribute("d", d);
    zig.setAttribute("fill", "none");
    zig.setAttribute("stroke", "var(--divider-color, #bbb)");
    zig.setAttribute("stroke-width", "1.5");
    g.appendChild(zig);
    svg.appendChild(g);

    const nextSeg = data.segments[brk.beforeIndex];
    this._drawBadgeCircle(
      svg,
      midX,
      brk.beforeIndex + 1,
      nextSeg && nextSeg.drive_id
        ? () =>
            this._selectNode({ level: "segment", key: data.dayKey, driveId: nextSeg.drive_id, ...(data.vin ? { vin: data.vin } : {}) }).catch((err) =>
              this._showError(err)
            )
        : null
    );
  }

  /** The first drive's own badge (day view only) -- every other drive's comes from `_drawBreakMark` at the gap before it. */
  _drawStartBadge(svg, firstPiece, data) {
    const cx = Math.max(9, Math.min(firstPiece.x1 - 9, firstPiece.x0 + 9));
    const firstSeg = data.segments[0];
    this._drawBadgeCircle(
      svg,
      cx,
      1,
      firstSeg && firstSeg.drive_id
        ? () =>
            this._selectNode({ level: "segment", key: data.dayKey, driveId: firstSeg.drive_id, ...(data.vin ? { vin: data.vin } : {}) }).catch((err) =>
              this._showError(err)
            )
        : null
    );
  }

  _drawTimeAxis(svg, timeline, plotHeight, plotWidth, fullWidth, tz) {
    const svgNS = "http://www.w3.org/2000/svg";
    const g = document.createElementNS(svgNS, "g");
    const axisLine = document.createElementNS(svgNS, "line");
    axisLine.setAttribute("x1", "0");
    axisLine.setAttribute("x2", String(fullWidth));
    axisLine.setAttribute("y1", String(plotHeight));
    axisLine.setAttribute("y2", String(plotHeight));
    axisLine.setAttribute("stroke", "var(--divider-color, #e0e0e0)");
    axisLine.setAttribute("stroke-width", "1");
    g.appendChild(axisLine);

    const candidates = [];
    for (const piece of timeline.pieces) {
      const pieceWidth = piece.x1 - piece.x0;
      const count = Math.max(2, Math.min(4, Math.floor(pieceWidth / 70) + 1));
      for (let k = 0; k < count; k++) {
        const frac = count > 1 ? k / (count - 1) : 0;
        const t = piece.startTs + frac * (piece.endTs - piece.startTs);
        candidates.push({ x: timeline.tToX(t), t });
      }
    }
    const kept = [];
    for (const c of candidates) {
      if (kept.length && c.x - kept[kept.length - 1].x < 34) continue;
      kept.push(c);
    }
    for (const tick of kept) {
      const tickLine = document.createElementNS(svgNS, "line");
      tickLine.setAttribute("x1", String(tick.x));
      tickLine.setAttribute("x2", String(tick.x));
      tickLine.setAttribute("y1", String(plotHeight));
      tickLine.setAttribute("y2", String(plotHeight + 3));
      tickLine.setAttribute("stroke", "var(--divider-color, #e0e0e0)");
      g.appendChild(tickLine);
      const label = document.createElementNS(svgNS, "text");
      label.setAttribute("x", String(Math.min(plotWidth - 2, Math.max(2, tick.x))));
      label.setAttribute("y", String(plotHeight + CHART_AXIS_HEIGHT_PX - 4));
      label.setAttribute("font-size", "9");
      label.setAttribute("fill", "var(--secondary-text-color)");
      label.setAttribute("text-anchor", tick.x < 20 ? "start" : tick.x > plotWidth - 20 ? "end" : "middle");
      label.textContent = _timeLabel(tick.t, tz);
      g.appendChild(label);
    }
    svg.appendChild(g);
  }

  _onChartsPointerMove(ev, svg, fullWidth, plotWidth, timeline) {
    const rect = svg.getBoundingClientRect();
    if (!rect.width) return;
    const xFrac = (ev.clientX - rect.left) / rect.width;
    // Clamp into the plot region: hovering over the label gutter still
    // tracks the nearest (rightmost) real position instead of doing nothing.
    const x = Math.max(0, Math.min(plotWidth, xFrac * fullWidth));
    const t = timeline.xToT(x);
    if (t === null || t === undefined) return;
    this._setCursorT(t);
  }

  /** Keyboard path for the chart cursor: left/right step it (shift = 10x), Escape clears it. */
  _onChartsKey(ev, plotWidth, timeline) {
    if (ev.target && ev.target.getAttribute && ev.target.getAttribute("role") === "button") return;
    if (ev.key === "Escape") {
      ev.preventDefault();
      this._clearCursor();
      return;
    }
    if (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight") return;
    ev.preventDefault();
    const dir = ev.key === "ArrowRight" ? 1 : -1;
    const stride = ev.shiftKey ? 10 : 1;
    let x = this._cursorT === null || this._cursorT === undefined ? (dir > 0 ? 0 : plotWidth) : timeline.tToX(this._cursorT);
    const fresh = this._cursorT === null || this._cursorT === undefined;
    // A gap break has no time: keep stepping until we land on a real position.
    for (let tries = 0; tries < 12; tries++) {
      if (!(fresh && tries === 0)) x = stepChartX(x, dir * stride, plotWidth);
      const t = timeline.xToT(x);
      if (t !== null && t !== undefined) {
        this._setCursorT(t);
        return;
      }
    }
  }

  _onChartsClick(ev, svg, fullWidth, plotWidth, timeline, data) {
    // On a touch screen a tap is how you read a value, so it doesn't switch
    // drives (the map's numbered badges and the list still do).
    if (this._lastChartPointerType === "touch") return;
    const rect = svg.getBoundingClientRect();
    if (!rect.width) return;
    const xFrac = (ev.clientX - rect.left) / rect.width;
    const x = Math.max(0, Math.min(plotWidth, xFrac * fullWidth));
    const piece = timeline.pieces.find((p) => x >= p.x0 && x <= p.x1);
    if (piece && piece.driveId) {
      this._selectNode({ level: "segment", key: data.dayKey, driveId: piece.driveId, ...(data.vin ? { vin: data.vin } : {}) }).catch((err) =>
        this._showError(err)
      );
    }
  }

  /**
   * The value a chart row's series holds at time `t` (nearest sample, or the
   * containing chunk), as `{value, filled}` (`null` when there's nothing at
   * `t`). `filled` marks a GPS-reconstructed (estimated, not measured) point.
   */
  _valueAtT(row, t) {
    const built = row.built;
    if (built.kind === "steps") {
      for (const s of built.steps) {
        if (s.value !== null && t >= s.t0 && t <= s.t1) return { value: s.value, filled: false };
      }
      return null;
    }
    const pts = built.points;
    if (!pts.length) return null;
    let lo = 0;
    let hi = pts.length - 1;
    let idx;
    if (t <= pts[0].t) idx = 0;
    else if (t >= pts[hi].t) idx = hi;
    else {
      while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (pts[mid].t === t) {
          lo = mid;
          hi = mid;
          break;
        }
        if (pts[mid].t < t) lo = mid + 1;
        else hi = mid;
      }
      const before = Math.max(0, lo - 1);
      idx = t - pts[before].t <= pts[lo].t - t ? before : lo;
    }
    const p = pts[idx];
    if (p.value === null) return null;
    return { value: p.value, filled: !!p.filled };
  }

  _setCursorT(t) {
    this._cursorT = t;
    this._drawCursorOverlay();
    this._updateReadout();
    this._updateMapCursorDot();
  }

  _clearCursor() {
    if (this._cursorT === null) return;
    this._cursorT = null;
    this._drawCursorOverlay();
    this._updateReadout();
    this._updateMapCursorDot();
  }

  /** Reset hover state (called whenever the map switches to a new mode/selection). */
  _resetCursor() {
    this._cursorT = null;
    this._cursorGroup = null;
    if (this._cursorMapMarker && this._map) this._map.removeLayer(this._cursorMapMarker);
    this._cursorMapMarker = null;
    if (this._cursorReadoutEl) {
      _escapeText(this._cursorReadoutEl, "Hover or tap the charts or map to inspect a moment (arrow keys on a focused chart).");
    }
  }

  _drawCursorOverlay() {
    if (!this._chartsSvg || !this._chartsData) return;
    const svgNS = "http://www.w3.org/2000/svg";
    if (this._cursorGroup) {
      this._cursorGroup.remove();
      this._cursorGroup = null;
    }
    if (this._cursorT === null || !this._chartsRows || !this._chartsRows.length) return;
    const { timeline } = this._chartsData;
    const x = timeline.tToX(this._cursorT);
    const g = document.createElementNS(svgNS, "g");
    const line = document.createElementNS(svgNS, "line");
    line.setAttribute("x1", String(x));
    line.setAttribute("x2", String(x));
    line.setAttribute("y1", "0");
    line.setAttribute("y2", String(this._chartsPlotHeight));
    line.setAttribute("stroke", "var(--primary-text-color, #212121)");
    line.setAttribute("stroke-width", "1");
    line.setAttribute("stroke-dasharray", "3 2");
    line.setAttribute("opacity", "0.65");
    g.appendChild(line);
    for (const row of this._chartsRows) {
      const hit = this._valueAtT(row, this._cursorT);
      if (hit === null) continue;
      const dotY = row.yScale(hit.value);
      const dot = document.createElementNS(svgNS, "circle");
      dot.setAttribute("cx", String(x));
      dot.setAttribute("cy", String(dotY));
      dot.setAttribute("r", "3.5");
      dot.setAttribute("fill", row.colorAt ? row.colorAt(hit.value) : row.color);
      dot.setAttribute("stroke", "var(--card-background-color, #fff)");
      dot.setAttribute("stroke-width", "1.5");
      g.appendChild(dot);
      this._drawCursorValueLabel(g, x, dotY, row, hit.value, hit.filled);
    }
    this._chartsSvg.appendChild(g);
    this._cursorGroup = g;
  }

  /** The cursor's value at one chart row, as text right next to that row's cursor dot, in the series color. */
  _drawCursorValueLabel(g, x, dotY, row, v, filled = false) {
    const svgNS = "http://www.w3.org/2000/svg";
    const text = filled ? `${_formatReadoutValue(row.metric, v)} est.` : _formatReadoutValue(row.metric, v);
    const approxWidth = text.length * 5.6 + 4;
    const gutterStart = this._chartsPlotWidth != null ? this._chartsPlotWidth : x;
    const gap = 7;
    let anchor = "start";
    let lx = x + gap;
    if (lx + approxWidth > gutterStart) {
      anchor = "end";
      lx = x - gap;
    }
    const rowTop = row.plotTop !== undefined ? row.plotTop : 0;
    const rowBottom = row.plotBottom !== undefined ? row.plotBottom : this._chartsPlotHeight;
    const ly = Math.min(rowBottom, Math.max(rowTop + 3, dotY + 3));
    const label = document.createElementNS(svgNS, "text");
    label.setAttribute("x", String(lx));
    label.setAttribute("y", String(ly));
    label.setAttribute("text-anchor", anchor);
    label.setAttribute("font-size", "10");
    label.setAttribute("font-weight", "600");
    label.setAttribute("fill", row.colorAt ? row.colorAt(v) : row.color);
    label.setAttribute("stroke", "var(--card-background-color, #fff)");
    label.setAttribute("stroke-width", "3");
    label.setAttribute("stroke-linejoin", "round");
    label.style.paintOrder = "stroke";
    label.textContent = text;
    g.appendChild(label);
  }

  _updateReadout() {
    if (!this._cursorReadoutEl) return;
    if (this._cursorT === null || !this._chartsRows || !this._chartsRows.length) {
      _escapeText(this._cursorReadoutEl, "Hover or tap the charts or map to inspect a moment (arrow keys on a focused chart).");
      return;
    }
    _escapeText(this._cursorReadoutEl, this._readoutText(this._cursorT));
  }

  /** "3:42 PM · Speed 45 mph · Battery 62 %" for the chart time `t` (shared by the readout and the map tooltip). */
  _readoutText(t) {
    if (t === null || t === undefined || !this._chartsRows) return "";
    const tz = this._hass && this._hass.config ? this._hass.config.time_zone : undefined;
    const parts = [_timeLabel(t, tz)];
    for (const row of this._chartsRows) {
      const hit = this._valueAtT(row, t);
      if (hit === null) continue;
      const suffix = hit.filled ? " (est.)" : "";
      parts.push(`${row.metric.label} ${_formatReadoutValue(row.metric, hit.value)}${suffix}`);
    }
    return parts.join(" \u00b7 ");
  }

  /** Move (or hide) the map's cursor dot to the track point nearest the hovered time. */
  _updateMapCursorDot() {
    if (!this._map || !this._leaflet) return;
    if (this._cursorT === null || !this._chartsData) {
      if (this._cursorMapMarker) {
        this._map.removeLayer(this._cursorMapMarker);
        this._cursorMapMarker = null;
      }
      return;
    }
    let best = null;
    for (const seg of this._chartsData.segments) {
      const track = seg.track;
      if (!track || !Array.isArray(track.t) || !track.t.length) continue;
      const idx = nearestPointIndex(track, this._cursorT);
      if (idx < 0) continue;
      const dt = Math.abs(track.t[idx] - this._cursorT);
      if (!best || dt < best.dt) best = { lat: track.lat[idx], lon: track.lon[idx], dt };
    }
    if (!best) return;
    const L = this._leaflet;
    if (!this._cursorMapMarker) {
      this._cursorMapMarker = L.circleMarker([best.lat, best.lon], {
        radius: 6,
        color: "#ffffff",
        weight: 2,
        fillColor: "#e91e63",
        fillOpacity: 1,
        renderer: this._map.options.renderer,
        interactive: false,
      }).addTo(this._map);
    } else {
      this._cursorMapMarker.setLatLng([best.lat, best.lon]);
    }
  }

  // -- map: shared setup --------------------------------------------------------

  async _ensureMap() {
    if (this._map) return;
    const [leafletModule, cssText] = await Promise.all([_loadLeaflet(), _loadLeafletCss()]);
    this._leaflet = leafletModule;
    this._leafletStyleEl.textContent = cssText;

    const L = this._leaflet;
    this._map = L.map(this._mapEl, {
      preferCanvas: true,
      renderer: L.canvas(),
      zoomControl: true,
      maxZoom: MAP_MAX_ZOOM,
    });
    this._map.setView([37.0902, -95.7129], 4);
    this._applyTileLayer();
    this._applyMapTouchMode();
  }

  /** Fit to `bounds` and remember it, so a later layout change (the charts
   * panel appearing/collapsing, a card resize) can refit without a fresh
   * request. */
  _fitBounds(bounds, options) {
    this._lastFitBounds = bounds;
    this._lastFitOptions = options;
    this._lastSetView = null;
    this._map.fitBounds(bounds, options);
  }

  /** `setView` fallback (no route to fit) that's remembered the same way. */
  _setMapView(latlng, zoom) {
    this._lastSetView = { latlng, zoom };
    this._lastFitBounds = null;
    this._map.setView(latlng, zoom);
  }

  /**
   * Re-measure the map container and restore whatever view was last fit or
   * set -- needed because the charts panel (or a card resize, or expanding/
   * collapsing it) changes the map's own height *after* the initial
   * `fitBounds`/`setView`, which was computed against the old, larger
   * container. Never called on hover; only on layout changes.
   */
  _reapplyMapView() {
    if (!this._map) return;
    this._map.invalidateSize();
    // No pan/zoom animation here: this call is correcting the view for a
    // layout change (not a user-driven navigation), and animating risks the
    // map settling mid-transition if it's re-triggered again shortly after
    // (e.g. the charts panel's own layout still settling).
    if (this._lastFitBounds) {
      this._map.fitBounds(this._lastFitBounds, { ...this._lastFitOptions, animate: false });
    } else if (this._lastSetView) {
      this._map.setView(this._lastSetView.latlng, this._lastSetView.zoom, { animate: false });
    }
  }

  _applyTileLayer() {
    if (!this._map || !this._leaflet) return;
    const L = this._leaflet;
    const dark = !!(this._hass && this._hass.themes && this._hass.themes.darkMode);
    const cfg = this._config || {};
    for (const layer of this._tileLayers) this._map.removeLayer(layer);
    this._tileLayers = [];

    if (cfg.tile_url) {
      const layer = L.tileLayer(cfg.tile_url, {
        maxZoom: MAP_MAX_ZOOM,
        attribution: cfg.tile_attribution || "",
      });
      layer.addTo(this._map);
      this._tileLayers.push(layer);
      return;
    }

    const style = BASEMAPS[this._basemap] || BASEMAPS.map;
    style.layers(dark).forEach(([url, maxNativeZoom], index) => {
      const layer = L.tileLayer(url, {
        maxZoom: MAP_MAX_ZOOM,
        maxNativeZoom,
        // One attribution for the whole style; label layers add none.
        attribution: index === 0 ? cfg.tile_attribution || ESRI_ATTRIBUTION : "",
      });
      layer.addTo(this._map);
      this._tileLayers.push(layer);
    });
  }

  _setBasemap(key) {
    if (!BASEMAPS[key] || key === this._basemap) return;
    this._basemap = key;
    try {
      window.localStorage.setItem(BASEMAP_STORAGE_KEY, key);
    } catch (_err) {
      // Not persisted; the choice still applies for this session.
    }
    this._markActiveBasemap();
    this._applyTileLayer();
  }

  _markActiveBasemap() {
    if (!this._basemapEl) return;
    for (const btn of this._basemapEl.querySelectorAll("button")) {
      const active = btn.dataset.basemap === this._basemap;
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-pressed", String(active));
    }
  }

  _clearRoute() {
    if (this._map) {
      for (const layer of this._routeLayers) this._map.removeLayer(layer);
      for (const layer of this._markerLayers) this._map.removeLayer(layer);
      if (this._cursorMapMarker) this._map.removeLayer(this._cursorMapMarker);
    }
    this._routeLayers = [];
    this._markerLayers = [];
    this._cursorMapMarker = null;
  }

  _markActiveRouteColorMode() {
    if (!this._routeToggleEl) return;
    for (const btn of this._routeToggleEl.querySelectorAll("button")) {
      const active = btn.dataset.mode === this._routeColorMode;
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-pressed", String(active));
    }
  }

  _clearHeatLayer() {
    this._detachHeatHover();
    if (this._heatLayer && this._map) this._map.removeLayer(this._heatLayer);
    this._heatLayer = null;
    this._heatTiles = new Map();
  }

  /** Remember a heat tile's cell counts so hovering or tapping the map can read "Driven N times". */
  _storeHeatTile(coords, tile) {
    if (!this._heatTiles) this._heatTiles = new Map();
    const counts = new Map();
    for (const [dx, dy, count] of tile.cells || []) counts.set(`${dx},${dy}`, count);
    this._heatTiles.set(`${coords.z}/${coords.x}/${coords.y}`, { size: tile.size || 1, counts });
  }

  /** The pass count of the road under a map position (0 when nothing is drawn there). */
  _heatCountAtLatLng(latlng) {
    if (!this._map || !this._heatTiles) return 0;
    const z = Math.round(this._map.getZoom());
    const p = this._map.project(latlng, z);
    const tx = Math.floor(p.x / HEAT_TILE_SIZE);
    const ty = Math.floor(p.y / HEAT_TILE_SIZE);
    const tile = this._heatTiles.get(`${z}/${tx}/${ty}`);
    if (!tile) return 0;
    const step = HEAT_TILE_SIZE / tile.size;
    return heatCountAt(tile.counts, (p.x - tx * HEAT_TILE_SIZE) / step, (p.y - ty * HEAT_TILE_SIZE) / step);
  }

  /** Hover (mouse) or tap (touch) the heat map for a "Driven N times" tooltip. */
  _attachHeatHover() {
    this._detachHeatHover();
    if (!this._map || !this._leaflet) return;
    const tip = this._leaflet.tooltip({ direction: "top", offset: [0, -6], className: "rde-heat-tip" });
    const show = (ev) => {
      const text = heatTipText(this._heatCountAtLatLng(ev.latlng));
      if (!text) {
        this._map.closeTooltip(tip);
        return;
      }
      tip.setLatLng(ev.latlng).setContent(text);
      this._map.openTooltip(tip);
    };
    const hide = () => this._map.closeTooltip(tip);
    this._map.on("mousemove click", show);
    this._map.on("mouseout", hide);
    this._heatHover = { tip, show, hide };
  }

  _detachHeatHover() {
    const h = this._heatHover;
    if (!h) return;
    if (this._map) {
      this._map.off("mousemove click", h.show);
      this._map.off("mouseout", h.hide);
      this._map.closeTooltip(h.tip);
    }
    this._heatHover = null;
  }

  _clearLegend() {
    this._legendEl.style.display = "none";
    this._legendEl.textContent = "";
  }

  _clearOverlay() {
    this._overlayEl.style.display = "none";
    this._overlayEl.textContent = "";
  }

  _showOverlay(text) {
    this._overlayEl.style.display = "flex";
    _escapeText(this._overlayEl, text);
  }

  /**
   * Draw one highlighted track (day view: every segment; segment view: just
   * the selected one), colored by `this._routeColorMode` over `range`
   * (`[0, maxMph]` for speed; `[min, max]` for elevation/efficiency). `seg`
   * is that track's own drive summary, needed for efficiency coloring
   * (`battery_capacity_kwh`/`chunks`).
   */
  _drawTrack(track, seg, range, opts = {}) {
    const L = this._leaflet;
    const renderer = this._map.options.renderer;
    // Not interactive: this is either the day view (nothing to click) or the
    // highlighted segment in segment mode, which is drawn last (on top of
    // the dimmed tracks) purely for visibility -- it must never swallow
    // clicks meant for a dimmed segment's hit-line underneath it. (A
    // separate, wider hit-line is added alongside it for hover-cursor sync.)
    const allPoints = track.lat.map((lat, i) => [lat, track.lon[i]]);
    this._routeLayers.push(
      L.polyline(allPoints, {
        // A casing in the vehicle's color highlights the selected drive
        // when several vehicles are in view.
        color: opts.casing || "#000000",
        weight: opts.casing ? 9 : 7,
        opacity: opts.casing ? 0.9 : 0.55,
        renderer,
        interactive: false,
      }).addTo(this._map)
    );
    const solid = opts.solid || null;
    const mode = this._routeColorMode;
    const values = solid || mode === "speed" ? null : this._routeValues(track, seg);
    const colorFn =
      mode === "elevation"
        ? (v) => elevationColor(v, range[0], range[1])
        : mode === "efficiency"
          ? (v) => efficiencyColor(v, range[0], range[1])
          : null;
    // Draw run by run (not the whole track at once) so a GPS-filled
    // (reconstructed) stretch can be dashed and tooltipped separately from
    // the measured route around it.
    for (const run of trackFilledRuns(track)) {
      const subTrack = {
        lat: track.lat.slice(run.from, run.to + 1),
        lon: track.lon.slice(run.from, run.to + 1),
        speed_mps: Array.isArray(track.speed_mps) ? track.speed_mps.slice(run.from, run.to + 1) : undefined,
      };
      const pieces = solid
        ? [{ points: subTrack.lat.map((lat, i) => [lat, subTrack.lon[i]]), color: solid }]
        : mode === "speed"
          ? speedPieces(subTrack, range[1])
          : _colorPieces(subTrack, values.slice(run.from, run.to + 1), colorFn);
      for (const piece of pieces) {
        const layer = L.polyline(piece.points, {
          color: piece.color,
          weight: 4,
          opacity: 1,
          renderer,
          interactive: false,
          ...(run.filled ? { dashArray: "6 6" } : {}),
        }).addTo(this._map);
        if (run.filled) layer.bindTooltip("Estimated route (no GPS)");
        this._routeLayers.push(layer);
      }
    }
  }

  /** Per-point values for the current route color mode (see `_drawTrack`). */
  _routeValues(track, seg) {
    const mode = this._routeColorMode;
    if (mode === "speed") {
      return track.t.map((_t, i) => _mphAt(track, i));
    }
    if (mode === "elevation") {
      return (track.alt_m || []).map((v) => (v === null || v === undefined ? null : v * METERS_TO_FEET));
    }
    if (this._effMethod === "rolling") {
      return rollingEfficiency(track, seg && seg.battery_capacity_kwh, this._socDropPct());
    }
    if (this._effMethod === "model") {
      const eff = seg && seg.model && Array.isArray(seg.model.eff) ? seg.model.eff : null;
      return track.t.map((_t, i) => (eff ? eff[i] : null));
    }
    const steps = chunkSteps(seg && seg.chunks);
    return track.t.map((t) => {
      const step = steps.find((s) => t >= s.t0 && t <= s.t1);
      return step ? step.value : null;
    });
  }

  /** A wide, invisible hit-line so hovering a route on the map moves the chart cursor. */
  _addTrackHoverHitline(track) {
    if (!track || !Array.isArray(track.lat) || track.lat.length < 2) return;
    const L = this._leaflet;
    const points = track.lat.map((lat, i) => [lat, track.lon[i]]);
    const hit = L.polyline(points, {
      color: "#000000",
      weight: 16,
      opacity: 0.001,
      renderer: this._map.options.renderer,
    });
    hit.bindTooltip("Route", { sticky: true });
    const probe = (ev, open) => {
      const idx = this._nearestTrackIndexToLatLng(track, ev.latlng);
      if (idx >= 0 && track.t[idx] !== null && track.t[idx] !== undefined) {
        this._setCursorT(track.t[idx]);
        const text = this._readoutText(track.t[idx]);
        if (text) {
          hit.setTooltipContent(text);
          if (open) hit.openTooltip(ev.latlng);
        }
      }
    };
    hit.on("mousemove", (ev) => probe(ev, false));
    // A tap has no hover: it shows the same cursor and readout.
    hit.on("click", (ev) => probe(ev, true));
    hit.on("mouseout", () => this._clearCursor());
    hit.addTo(this._map);
    this._routeLayers.push(hit);
  }

  _nearestTrackIndexToLatLng(track, latlng) {
    let best = -1;
    let bestD = Infinity;
    for (let i = 0; i < track.lat.length; i++) {
      const dLat = track.lat[i] - latlng.lat;
      const dLon = track.lon[i] - latlng.lng;
      const d = dLat * dLat + dLon * dLon;
      if (d < bestD) {
        bestD = d;
        best = i;
      }
    }
    return best;
  }

  /** A wide invisible line over a drive's route that selects that drive when clicked. */
  _addSelectHit(track, dayKey, driveId, vin) {
    const L = this._leaflet;
    const points = track.lat.map((lat, i) => [lat, track.lon[i]]);
    const hit = L.polyline(points, {
      color: "#000000",
      weight: 14,
      opacity: 0.001,
      renderer: this._map.options.renderer,
    });
    hit.on("click", () => {
      this._selectNode({ level: "segment", key: dayKey, driveId, ...(vin ? { vin } : {}) }).catch((err) =>
        this._showError(err)
      );
    });
    hit.addTo(this._map);
    this._routeLayers.push(hit);
  }

  _drawDimTrack(track, dayKey, driveId, vin) {
    const L = this._leaflet;
    const renderer = this._map.options.renderer;
    const points = track.lat.map((lat, i) => [lat, track.lon[i]]);
    // Gray with a faint light casing, so it reads on light, dark and
    // satellite basemaps without competing with the selected drive's colors.
    this._routeLayers.push(
      L.polyline(points, { color: "#ffffff", weight: 7, opacity: 0.5, renderer }).addTo(this._map)
    );
    this._routeLayers.push(
      L.polyline(points, { color: "#7d7d7d", weight: 4, opacity: 0.95, renderer }).addTo(this._map)
    );
    const hit = L.polyline(points, { color: "#000000", weight: 14, opacity: 0.001, renderer });
    hit.on("click", () => {
      this._selectNode({ level: "segment", key: dayKey, driveId, ...(vin ? { vin } : {}) }).catch((err) =>
        this._showError(err)
      );
    });
    hit.addTo(this._map);
    this._routeLayers.push(hit);
  }

  // Stretches the car drove without reporting its position (it takes a minute
  // or two to connect after waking): a dashed line from where it was parked
  // to where recording resumed, so the day's start still sits at home.
  _drawGaps(dayData) {
    const L = this._leaflet;
    for (const gap of dayData.gaps || []) {
      if (!gap.from || !gap.to) continue;
      const line = L.polyline([gap.from, gap.to], {
        color: "#757575",
        weight: 3,
        opacity: 0.9,
        dashArray: "2 7",
        lineCap: "round",
        renderer: this._map.options.renderer,
      }).addTo(this._map);
      line.bindTooltip(`Not recorded (${formatGapDistance(gap.distance_m)}): the car hadn't reported its position yet`);
      this._routeLayers.push(line);
    }
  }

  _drawDayMarkers(dayData, selectedDriveId = null) {
    const L = this._leaflet;
    const renderer = this._map.options.renderer;
    const tz = this._hass && this._hass.config ? this._hass.config.time_zone : undefined;
    const segments = dayData.segments || [];
    const segmentTime = (n, key) => _timeLabel(segments[n - 1] ? segments[n - 1][key] : null, tz);
    const specs = dayMarkerSpecs(dayData, selectedDriveId);
    const inSegment = selectedDriveId != null;
    const isAdmin = !!(this._hass && this._hass.user && this._hass.user.is_admin);
    const placeAt = (n, key) => {
      const seg = segments[n - 1];
      return seg ? seg[key] : null;
    };

    // Badges: where each other drive starts. Clicking one selects that drive.
    for (const badge of specs.badges) {
      const el = document.createElement("div");
      el.className = "rde-stop-marker" + (badge.beside ? " beside" : "");
      el.textContent = badge.numbers.join(", ");
      el.setAttribute("role", "button");
      el.setAttribute("aria-label", `Drive ${badge.numbers.join(", ")} starts here: select it`);
      const icon = L.divIcon({ className: "rde-stop-icon", html: el, iconSize: null });
      const marker = L.marker([badge.lat, badge.lon], { icon, keyboard: true }).addTo(this._map);
      marker.bindTooltip(
        badge.numbers
          .map((n, k) => {
            const parked = badge.parked[k];
            const placeLabel = placeAt(n, "start_place");
            const where = placeLabel ? ` — ${placeLabel.label}` : "";
            const when = `Drive ${n} starts here${where}, ${segmentTime(n, "start_ts")}`;
            return parked == null ? when : `${when} (${formatParkedDuration(parked).toLowerCase()} before)`;
          })
          .join("<br>")
      );
      const first = segments[badge.numbers[0] - 1];
      if (first) {
        marker.on("click", () => {
          this._selectNode({
            level: "segment",
            key: dayData.date,
            driveId: first.drive_id,
            ...(first.vin ? { vin: first.vin } : {}),
          }).catch((err) => this._showError(err));
        });
      }
      this._markerLayers.push(marker);
    }
    if (specs.start) {
      // Larger than the end square (half-diagonal ~8 px), so when a drive or
      // day starts and ends at the same spot (usually home) a green ring
      // shows around the red square.
      const marker = L.circleMarker([specs.start.lat, specs.start.lon], {
        radius: 11,
        color: "#ffffff",
        weight: 2,
        fillColor: "#2e7d32",
        fillOpacity: 1,
        renderer,
      }).addTo(this._map);
      const startPlace = inSegment ? placeAt(specs.start.number, "start_place") : dayData.start && dayData.start.place;
      const startWhere = startPlace ? ` — ${startPlace.label}` : "";
      marker.bindTooltip(
        inSegment
          ? `Drive ${specs.start.number} start${startWhere}, ${segmentTime(specs.start.number, "start_ts")}`
          : `Day start: drive 1${startWhere}, ${segmentTime(1, "start_ts")}`
      );
      if (isAdmin && inSegment) {
        const seg = segments[specs.start.number - 1];
        if (seg) {
          marker.on("click", () => this._openMapPlacePopup(marker, seg, "start", startPlace));
        }
      }
      this._markerLayers.push(marker);
    }
    if (specs.end) {
      const icon = L.divIcon({
        className: "rde-end-marker",
        iconSize: [11, 11],
        iconAnchor: [5.5, 5.5],
      });
      const marker = L.marker([specs.end.lat, specs.end.lon], { icon, zIndexOffset: 1000 }).addTo(this._map);
      const endPlace = inSegment ? placeAt(specs.end.number, "end_place") : dayData.end && dayData.end.place;
      const endWhere = endPlace ? ` — ${endPlace.label}` : "";
      marker.bindTooltip(
        inSegment
          ? `Drive ${specs.end.number} end${endWhere}, ${segmentTime(specs.end.number, "end_ts")}`
          : `Day end: drive ${specs.end.number}${endWhere}, ${segmentTime(specs.end.number, "end_ts")}`
      );
      if (isAdmin && inSegment) {
        const seg = segments[specs.end.number - 1];
        if (seg) {
          marker.on("click", () => this._openMapPlacePopup(marker, seg, "end", endPlace));
        }
      }
      this._markerLayers.push(marker);
    }
  }

  /** Admin-only: a small Leaflet popup on the selected drive's own start/end marker to name that spot. */
  _openMapPlacePopup(marker, seg, side, place) {
    const wrap = document.createElement("div");
    wrap.className = "rde-map-place-popup";
    const title = document.createElement("div");
    title.className = "rde-map-place-popup-title";
    _escapeText(title, place ? place.label : "Name this place");
    wrap.appendChild(title);

    const nameInput = document.createElement("input");
    nameInput.type = "text";
    nameInput.placeholder = "Name this place";
    nameInput.setAttribute("aria-label", "Place name");
    nameInput.value = place && place.name ? place.name : "";

    const categorySelect = document.createElement("select");
    categorySelect.setAttribute("aria-label", "Place category");
    this._fillCategorySelect(categorySelect, place && place.category, seg.vin || this._primaryVin());

    const saveBtn = document.createElement("button");
    saveBtn.type = "button";
    _escapeText(saveBtn, "Save");
    saveBtn.title = "Save this place name";
    saveBtn.addEventListener("click", () => {
      this._savePlaceName(seg, side, place, nameInput.value.trim(), categorySelect.value || null)
        .then(() => marker.closePopup())
        .catch((err) => this._showError(err));
    });

    wrap.appendChild(nameInput);
    wrap.appendChild(categorySelect);
    wrap.appendChild(saveBtn);
    marker.bindPopup(wrap).openPopup();
  }

  _renderSpeedLegend(scaleMax) {
    this._legendEl.textContent = "";
    this._legendEl.style.display = "flex";
    const bar = document.createElement("div");
    bar.className = "rde-legend-bar";
    bar.style.background = speedGradientCss();
    this._legendEl.title = `Route color is speed: red is slow, green is fast, up to ${scaleMax} mph on this ${this._selection && this._selection.level === "segment" ? "drive's" : "day's"} own scale`;
    const labels = document.createElement("div");
    labels.className = "rde-legend-labels";
    // One consistent style for both day mode (a shared scale) and segment
    // mode (the selected drive's own scale): "0", the midpoint, "N mph".
    const texts = ["0", `${Math.round(scaleMax / 2)}`, `${scaleMax} mph`];
    for (const text of texts) {
      const span = document.createElement("span");
      _escapeText(span, text);
      labels.appendChild(span);
    }
    this._legendEl.appendChild(bar);
    this._legendEl.appendChild(labels);
  }

  _renderHeatLegend(scaleMax) {
    this._legendEl.textContent = "";
    this._legendEl.style.display = "flex";
    const bar = document.createElement("div");
    bar.className = "rde-legend-bar";
    bar.style.background = heatGradientCss();
    this._legendEl.title = `Road heat: how many times you drove each road (an out-and-back counts twice), up to ${scaleMax}+`;
    const labels = document.createElement("div");
    labels.className = "rde-legend-labels";
    // Counts are passes (an out-and-back counts twice), so "times", not drives.
    for (const text of ["1×", `${scaleMax}+ times`]) {
      const span = document.createElement("span");
      _escapeText(span, text);
      labels.appendChild(span);
    }
    this._legendEl.appendChild(bar);
    this._legendEl.appendChild(labels);
  }

  _renderElevationLegend(min, max) {
    this._legendEl.textContent = "";
    this._legendEl.style.display = "flex";
    const bar = document.createElement("div");
    bar.className = "rde-legend-bar";
    bar.style.background = _stopsGradientCss(ELEVATION_COLOR_STOPS);
    this._legendEl.title = "Route color is elevation above sea level, in feet";
    const labels = document.createElement("div");
    labels.className = "rde-legend-labels";
    for (const text of [`${Math.round(min)} ft`, `${Math.round(max)} ft`]) {
      const span = document.createElement("span");
      _escapeText(span, text);
      labels.appendChild(span);
    }
    this._legendEl.appendChild(bar);
    this._legendEl.appendChild(labels);
  }

  _renderEfficiencyLegend(min, max) {
    this._legendEl.textContent = "";
    this._legendEl.style.display = "flex";
    const bar = document.createElement("div");
    bar.className = "rde-legend-bar";
    bar.style.background = _stopsGradientCss(EFFICIENCY_COLOR_STOPS);
    this._legendEl.title = "Route color is efficiency in mi/kWh: red is low, green is high";
    const labels = document.createElement("div");
    labels.className = "rde-legend-labels";
    for (const text of [`${min.toFixed(1)} mi/kWh`, `${max.toFixed(1)} mi/kWh`]) {
      const span = document.createElement("span");
      _escapeText(span, text);
      labels.appendChild(span);
    }
    this._legendEl.appendChild(bar);
    this._legendEl.appendChild(labels);
  }

  _renderRouteLegend(mode, range) {
    if (mode === "elevation") this._renderElevationLegend(range[0], range[1]);
    else if (mode === "efficiency") this._renderEfficiencyLegend(range[0], range[1]);
    else this._renderSpeedLegend(range[1]);
  }

  /** Elevation route-color range (ft), shared across every track passed in. */
  _elevationRange(tracks) {
    const values = [];
    for (const track of tracks) {
      for (const v of (track && track.alt_m) || []) {
        if (v !== null && v !== undefined) values.push(v * METERS_TO_FEET);
      }
    }
    if (!values.length) return [0, 1];
    let min = Infinity;
    let max = -Infinity;
    for (const v of values) {
      if (v < min) min = v;
      if (v > max) max = v;
    }
    return [min, max];
  }

  /** Efficiency route-color range (mi/kWh): the same robust range as the chart. */
  _efficiencyRangeForRoute(segments) {
    const values = [];
    for (const seg of segments) {
      if (!seg.track) continue;
      for (const v of this._routeValues(seg.track, seg)) {
        if (v !== null && v !== undefined && !Number.isNaN(v)) values.push(v);
      }
    }
    return this._efficiencyDomain(values);
  }

  // -- map: mode dispatch -------------------------------------------------------

  async _renderMapForSelection() {
    await this._ensureMap();
    const sel = this._selection;
    if (sel.level === "all") return this._renderHeatMode("all", null);
    if (sel.level === "year") return this._renderHeatMode("year", sel.key);
    if (sel.level === "month") return this._renderHeatMode("month", sel.key);
    if (sel.level === "day") return this._renderDayMode(sel.key);
    if (sel.level === "segment") return this._renderSegmentMode(sel.key, sel.driveId);
  }

  _heatLayerClassFor(L) {
    if (!this._heatLayerClass) {
      // Read hass at request time: HA hands the card a new hass object on
      // every state change, so one captured here would go stale.
      const card = this;
      this._heatLayerClass = L.GridLayer.extend({
        initialize(scope, period, key, scaleMax, options) {
          L.GridLayer.prototype.initialize.call(this, options);
          this._rdeScope = scope;
          this._rdePeriod = period;
          this._rdeKey = key;
          this._rdeScaleMax = scaleMax;
        },
        createTile(coords, done) {
          const canvas = document.createElement("canvas");
          canvas.width = HEAT_TILE_SIZE;
          canvas.height = HEAT_TILE_SIZE;
          const ctx = canvas.getContext("2d");
          card._hass
            .callWS({
              type: "rivian/analytics/heat_tile",
              ...this._rdeScope,
              period: this._rdePeriod,
              ...(this._rdeKey ? { key: this._rdeKey } : {}),
              z: coords.z,
              x: coords.x,
              y: coords.y,
              margin: HEAT_TILE_MARGIN,
            })
            .then((tile) => {
              const size = tile.size || 1;
              card._storeHeatTile(coords, tile);
              const step = HEAT_TILE_SIZE / size;
              const scaleMax = tile.scale_max || this._rdeScaleMax;
              ctx.lineCap = "round";
              ctx.lineJoin = "round";
              ctx.lineWidth = heatLineWidth(step);
              // Consecutive strokes of one color share a path (they're sorted
              // by count), which keeps a busy tile to a few hundred stroke calls.
              let color = null;
              for (const stroke of heatStrokes(tile.cells)) {
                const next = heatColor(stroke.value, scaleMax);
                if (!next) continue;
                if (next !== color) {
                  if (color) ctx.stroke();
                  color = next;
                  ctx.strokeStyle = color;
                  ctx.beginPath();
                }
                ctx.moveTo(stroke.x0 * step, stroke.y0 * step);
                // A zero-length line with round caps draws the cell's dot.
                ctx.lineTo(stroke.x1 * step, stroke.y1 * step);
              }
              if (color) ctx.stroke();
              done(null, canvas);
            })
            .catch((err) => done(err, canvas));
          return canvas;
        },
      });
    }
    return this._heatLayerClass;
  }

  async _renderHeatMode(period, key) {
    this._clearRoute();
    this._clearLegend();
    this._clearOverlay();
    this._resetCursor();
    this._hideCharts();
    this._routeToggleEl.style.display = "none";
    this._lastFitBounds = null;
    this._lastSetView = null;
    let heat;
    try {
      // "All time" has no key; send none rather than null.
      heat = await this._hass.callWS({
        type: "rivian/analytics/heat",
        ...this._scope(),
        period,
        ...(key ? { key } : {}),
      });
    } catch (err) {
      this._clearHeatLayer();
      this._statsEl.textContent = "";
      this._showOverlay(
        err && err.code === "not_found" ? "Vehicle not found." : "The road heat map could not be loaded."
      );
      return;
    }
    if (this._selection.level !== period || (period !== "all" && this._selection.key !== key)) {
      // The selection moved on while this request was in flight.
      return;
    }
    this._clearHeatLayer();
    if (!heat.bbox) {
      const agg = this._findAgg(period, key);
      this._showOverlay(agg && agg.drives ? "No routes stored for this period." : "No drives recorded yet.");
      this._renderHeatStats(period, key);
      requestAnimationFrame(() => this._reapplyMapView());
      return;
    }
    const L = this._leaflet;
    const bounds = L.latLngBounds([
      [heat.bbox[0], heat.bbox[1]],
      [heat.bbox[2], heat.bbox[3]],
    ]);
    this._fitBounds(bounds, { padding: [24, 24], maxZoom: 15 });
    const HeatLayerClass = this._heatLayerClassFor(L);
    this._heatLayer = new HeatLayerClass(this._scope(), period, key, heat.scale_max, {
      tileSize: HEAT_TILE_SIZE,
      opacity: 0.85,
    });
    this._heatLayer.addTo(this._map);
    this._heatLayer.on("tileunload", (ev) => {
      if (this._heatTiles && ev.coords) this._heatTiles.delete(`${ev.coords.z}/${ev.coords.x}/${ev.coords.y}`);
    });
    this._attachHeatHover();
    this._renderHeatLegend(heat.scale_max);
    this._renderHeatStats(period, key);
    requestAnimationFrame(() => this._reapplyMapView());
  }

  async _renderDayMode(dayKey) {
    this._clearHeatLayer();
    this._clearRoute();
    this._clearOverlay();
    this._resetCursor();
    const dayData = this._cache.days[dayKey];
    if (!dayData) return;
    if (this._selection.level !== "day" || this._selection.key !== dayKey) return;
    if (this._multi) return this._renderMultiDayMode(dayKey, dayData);

    const segments = dayData.segments || [];
    const withTrack = segments.filter((s) => s.track && Array.isArray(s.track.lat) && s.track.lat.length > 1);
    const mode = this._routeColorMode;
    let range = [0, dayScaleMax(segments)];
    if (mode === "elevation") range = this._elevationRange(withTrack.map((s) => s.track));
    else if (mode === "efficiency") range = this._efficiencyRangeForRoute(withTrack);

    const boundsPts = [];
    for (const seg of segments) {
      if (seg.track && Array.isArray(seg.track.lat) && seg.track.lat.length > 1) {
        this._drawTrack(seg.track, seg, range);
        this._addTrackHoverHitline(seg.track);
        for (let i = 0; i < seg.track.lat.length; i++) boundsPts.push([seg.track.lat[i], seg.track.lon[i]]);
      }
    }
    this._drawGaps(dayData);
    for (const gap of dayData.gaps || []) {
      if (gap.from && gap.to) boundsPts.push(gap.from, gap.to);
    }
    this._drawDayMarkers(dayData);
    if (boundsPts.length) {
      this._fitBounds(this._leaflet.latLngBounds(boundsPts), { padding: [40, 40] });
    } else if (dayData.start) {
      this._setMapView([dayData.start.lat, dayData.start.lon], 13);
      this._showOverlay("No GPS route recorded for this day.");
    } else {
      this._lastFitBounds = null;
      this._lastSetView = null;
    }
    this._routeToggleEl.style.display = withTrack.length ? "flex" : "none";
    if (withTrack.length) this._renderRouteLegend(mode, range);
    else this._clearLegend();
    this._renderDayStats(dayData);
    this._showChartsForDay(dayKey, dayData);
    // The charts panel changes the map's height *after* the fit above (it's
    // appended/sized by _showChartsForDay just now), so refit once that
    // layout has settled instead of only invalidating the old view's size.
    requestAnimationFrame(() => this._reapplyMapView());
  }

  async _renderSegmentMode(dayKey, driveId) {
    this._clearHeatLayer();
    this._clearRoute();
    this._clearOverlay();
    this._resetCursor();
    const rawDay = this._cache.days[dayKey];
    if (!rawDay) return;
    const vin = this._selection.vin;
    if (
      this._selection.level !== "segment" ||
      this._selection.key !== dayKey ||
      this._selection.driveId !== driveId
    ) {
      return;
    }
    // Several vehicles: the single-drive view runs on that vehicle's own
    // slice of the combined day (its stops, gaps, charts and stats).
    const dayData = this._multi ? projectDay(rawDay, vin) : rawDay;

    const segments = dayData.segments || [];
    const selectedSeg = segments.find((s) => s.drive_id === driveId);
    let boundsPts = [];

    // Dimmed segments are drawn first so the highlighted one always renders
    // on top -- otherwise a later drive that retraces the same roads (e.g.
    // the evening commute over the morning one) would visually bury the
    // selection. `_drawTrack`'s layers are non-interactive, so this order
    // doesn't affect clickability: a dimmed segment's own hit-line is still
    // reachable even where the highlight visually covers it.
    for (const seg of [...(dayData.others || []), ...segments]) {
      if (seg.drive_id === driveId && (seg.vin ?? null) === (vin ?? null)) continue;
      if (!seg.track || !Array.isArray(seg.track.lat) || seg.track.lat.length < 2) continue;
      this._drawDimTrack(seg.track, dayKey, seg.drive_id, seg.vin);
    }
    const hasSelTrack =
      selectedSeg && selectedSeg.track && Array.isArray(selectedSeg.track.lat) && selectedSeg.track.lat.length >= 2;
    if (hasSelTrack) {
      const mode = this._routeColorMode;
      let range = [0, speedScaleMax(selectedSeg.track)];
      if (mode === "elevation") range = this._elevationRange([selectedSeg.track]);
      else if (mode === "efficiency") range = this._efficiencyRangeForRoute([selectedSeg]);
      this._drawTrack(
        selectedSeg.track,
        selectedSeg,
        range,
        this._multi ? { casing: this._colorOfVin(vin) } : {}
      );
      this._addTrackHoverHitline(selectedSeg.track);
      boundsPts = selectedSeg.track.lat.map((lat, i) => [lat, selectedSeg.track.lon[i]]);
      this._renderRouteLegend(mode, range);
    } else {
      this._clearLegend();
    }
    this._routeToggleEl.style.display = hasSelTrack ? "flex" : "none";
    this._drawGaps(dayData);
    if (this._multi) this._drawMultiMarkers(rawDay, { vin, driveId });
    else this._drawDayMarkers(dayData, driveId);

    if (boundsPts.length) {
      this._fitBounds(this._leaflet.latLngBounds(boundsPts), { padding: [40, 40] });
    } else {
      this._showOverlay("No route recorded for this drive.");
      if (selectedSeg && typeof selectedSeg.start_lat === "number") {
        this._setMapView([selectedSeg.start_lat, selectedSeg.start_lon], 13);
      } else {
        this._lastFitBounds = null;
        this._lastSetView = null;
      }
    }
    this._renderSegmentStats(selectedSeg);
    this._showChartsForSegment(dayKey, dayData, selectedSeg);
    // See _renderDayMode: refit after the charts panel's own layout settles.
    requestAnimationFrame(() => this._reapplyMapView());
  }

  // -- several vehicles: day view --------------------------------------------

  /**
   * Map markers for a combined day (see `multiDayMarkerSpecs`): numbered
   * badges filled with each drive's vehicle color, per-vehicle start and end
   * markers (the selected drive's own endpoints in green/red).
   */
  _drawMultiMarkers(raw, selected) {
    const L = this._leaflet;
    const renderer = this._map.options.renderer;
    const tz = this._hass && this._hass.config ? this._hass.config.time_zone : undefined;
    const isAdmin = !!(this._hass && this._hass.user && this._hass.user.is_admin);
    const specs = multiDayMarkerSpecs(raw, this._vehicleList, selected);
    const segOf = (item) => (raw.segments || []).find((s) => s.vin === item.vin && s.drive_id === item.driveId);
    const timeOf = (item, key) => {
      const seg = segOf(item);
      return _timeLabel(seg ? seg[key] : null, tz);
    };
    const whereOf = (item, key) => {
      const seg = segOf(item);
      const place = seg ? seg[key] : null;
      return place ? ` — ${place.label}` : "";
    };

    for (const badge of specs.badges) {
      const el = document.createElement("div");
      el.className = "rde-stop-marker" + (badge.beside ? " beside" : "");
      el.textContent = badge.labels.join(", ");
      el.setAttribute("role", "button");
      el.setAttribute("aria-label", `Drive ${badge.labels.join(", ")} starts here: select it`);
      const single = new Set(badge.items.map((i) => i.vin)).size === 1;
      const color = single ? this._colorOfVin(badge.items[0].vin) : "#455a64";
      el.style.background = color;
      el.style.color = inkOn(color);
      const icon = L.divIcon({ className: "rde-stop-icon", html: el, iconSize: null });
      const marker = L.marker([badge.lat, badge.lon], { icon, keyboard: true }).addTo(this._map);
      marker.bindTooltip(
        badge.items
          .map((item) => {
            const when = `Drive ${item.label} starts here${whereOf(item, "start_place")}, ${timeOf(item, "start_ts")}`;
            return item.parked == null
              ? when
              : `${when} (${formatParkedDuration(item.parked).toLowerCase()} before)`;
          })
          .join("<br>")
      );
      const first = badge.items[0];
      marker.on("click", () => {
        this._selectNode({ level: "segment", key: raw.date, driveId: first.driveId, vin: first.vin }).catch(
          (err) => this._showError(err)
        );
      });
      this._markerLayers.push(marker);
    }

    // Several vehicles starting (or ending) at the same spot, usually home,
    // would sit on top of each other: fan them out sideways, and tuck an end
    // square into the corner of its own start circle.
    const spreadIndex = (list, m, i) =>
      list.slice(0, i).filter((o) => _metersApart(o, m) <= MARKER_MERGE_M).length;

    for (const [i, m] of specs.starts.entries()) {
      const seg = segOf(m);
      const placeLabel = whereOf(m, "start_place");
      let marker;
      if (m.selected) {
        marker = L.circleMarker([m.lat, m.lon], {
          radius: 11,
          color: "#ffffff",
          weight: 2,
          fillColor: "#2e7d32",
          fillOpacity: 1,
          renderer,
        }).addTo(this._map);
        marker.bindTooltip(`Drive ${m.label} start${placeLabel}, ${timeOf(m, "start_ts")}`);
        if (isAdmin && seg) {
          marker.on("click", () => this._openMapPlacePopup(marker, seg, "start", seg.start_place));
        }
      } else {
        const color = this._colorOfVin(m.vin);
        const el = document.createElement("div");
        el.className = "rde-vstart-marker";
        el.textContent = m.label;
        el.style.background = color;
        el.style.color = inkOn(color);
        const dx = spreadIndex(specs.starts, m, i) * 26;
        if (dx) el.style.transform = `translate(calc(-50% + ${dx}px), -50%)`;
        const icon = L.divIcon({ className: "rde-stop-icon", html: el, iconSize: null });
        marker = L.marker([m.lat, m.lon], { icon, keyboard: false }).addTo(this._map);
        marker.bindTooltip(`${m.label} · day start${placeLabel}, ${timeOf(m, "start_ts")}`);
      }
      this._markerLayers.push(marker);
    }

    for (const [i, m] of specs.ends.entries()) {
      const seg = segOf(m);
      const placeLabel = whereOf(m, "end_place");
      let marker;
      if (m.selected) {
        const icon = L.divIcon({ className: "rde-end-marker", iconSize: [11, 11], iconAnchor: [5.5, 5.5] });
        marker = L.marker([m.lat, m.lon], { icon, zIndexOffset: 1000 }).addTo(this._map);
        marker.bindTooltip(`Drive ${m.label} end${placeLabel}, ${timeOf(m, "end_ts")}`);
        if (isAdmin && seg) {
          marker.on("click", () => this._openMapPlacePopup(marker, seg, "end", seg.end_place));
        }
      } else {
        const el = document.createElement("div");
        el.className = "rde-vend-marker";
        el.style.background = this._colorOfVin(m.vin);
        const nearStart = specs.starts.some((o) => _metersApart(o, m) <= MARKER_MERGE_M);
        const dx = spreadIndex(specs.ends, m, i) * 26 + (nearStart ? 11 : 0);
        const dy = nearStart ? 11 : 0;
        if (dx || dy) el.style.transform = `translate(calc(-50% + ${dx}px), calc(-50% + ${dy}px))`;
        const icon = L.divIcon({ className: "rde-stop-icon", html: el, iconSize: null });
        marker = L.marker([m.lat, m.lon], { icon, zIndexOffset: 900, keyboard: false }).addTo(this._map);
        marker.bindTooltip(`${m.label} · day end${placeLabel}, ${timeOf(m, "end_ts")}`);
      }
      this._markerLayers.push(marker);
    }
  }

  /** The combined day: every drive in its vehicle's color, no charts, a per-vehicle table below. */
  async _renderMultiDayMode(dayKey, raw) {
    this._hideCharts();
    this._routeToggleEl.style.display = "none";
    const segments = raw.segments || [];
    const boundsPts = [];
    for (const seg of segments) {
      const track = seg.track;
      if (!track || !Array.isArray(track.lat) || track.lat.length < 2) continue;
      this._drawTrack(track, seg, [0, 1], { solid: this._colorOfVin(seg.vin) });
      this._addSelectHit(track, dayKey, seg.drive_id, seg.vin);
      for (let i = 0; i < track.lat.length; i++) boundsPts.push([track.lat[i], track.lon[i]]);
    }
    for (const info of Object.values(raw.vehicles || {})) {
      this._drawGaps(info);
      for (const gap of info.gaps || []) {
        if (gap.from && gap.to) boundsPts.push(gap.from, gap.to);
      }
    }
    this._drawMultiMarkers(raw, null);
    if (boundsPts.length) {
      this._fitBounds(this._leaflet.latLngBounds(boundsPts), { padding: [40, 40] });
    } else {
      const first = Object.values(raw.vehicles || {}).find((v) => v.start);
      if (first) {
        this._setMapView([first.start.lat, first.start.lon], 13);
        this._showOverlay("No GPS route recorded for this day.");
      } else {
        this._lastFitBounds = null;
        this._lastSetView = null;
      }
    }
    this._renderVehicleLegend(raw);
    this._renderMultiDaySummary(raw);
    requestAnimationFrame(() => this._reapplyMapView());
  }

  /** Map legend for the combined day: a colored dot, letter and name per vehicle. */
  _renderVehicleLegend(raw) {
    this._legendEl.textContent = "";
    const present = new Set((raw.segments || []).map((s) => s.vin));
    const vehicles = this._vehicleList.filter((v) => present.has(v.vin));
    if (!vehicles.length) {
      this._legendEl.style.display = "none";
      return;
    }
    this._legendEl.style.display = "flex";
    for (const v of vehicles) {
      const row = document.createElement("div");
      row.className = "rde-vlegend-row";
      const dot = document.createElement("i");
      dot.style.background = this._colorOfVin(v.vin);
      row.appendChild(dot);
      row.appendChild(document.createTextNode(`${v.letter} · ${v.name || v.model || "Vehicle"}`));
      this._legendEl.appendChild(row);
    }
  }

  /** One colored row per vehicle (drives, miles, moving time, kWh, mi/kWh) plus a total, in place of the stat tiles. */
  _renderMultiDaySummary(raw) {
    const { rows, total } = vehicleSummaryRows(raw, this._vehicleList);
    this._statsEl.textContent = "";
    const wrap = document.createElement("div");
    wrap.className = "rde-vtable-wrap";
    const table = document.createElement("table");
    table.className = "rde-vtable";
    const head = document.createElement("tr");
    for (const text of ["Vehicle", "Drives", "Miles", "Moving", "kWh", "mi/kWh"]) {
      const th = document.createElement("th");
      th.textContent = text;
      head.appendChild(th);
    }
    const thead = document.createElement("thead");
    thead.appendChild(head);
    table.appendChild(thead);
    const tbody = document.createElement("tbody");
    const addRow = (r, isTotal) => {
      const tr = document.createElement("tr");
      if (isTotal) tr.className = "rde-vtable-total";
      const first = document.createElement("td");
      if (!isTotal) {
        const dot = document.createElement("i");
        dot.style.background = this._pickColor(r.color, r.colorDark) || "#888";
        first.appendChild(dot);
        first.appendChild(document.createTextNode(`${r.letter} · ${r.name}`));
      } else {
        first.textContent = r.name;
      }
      tr.appendChild(first);
      const cells = [
        String(r.drives),
        r.miles == null ? "–" : r.miles.toFixed(1),
        r.movingSeconds == null ? "–" : formatDuration(r.movingSeconds),
        r.energyKwh == null ? "–" : r.energyKwh.toFixed(1),
        r.efficiency == null ? "–" : r.efficiency.toFixed(2),
      ];
      for (const text of cells) {
        const td = document.createElement("td");
        td.textContent = text;
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    };
    for (const r of rows) addRow(r, false);
    if (rows.length > 1) addRow(total, true);
    table.appendChild(tbody);
    wrap.appendChild(table);
    this._statsEl.appendChild(wrap);
  }

  _showMessage(text) {
    this._treeEl.textContent = "";
    const box = document.createElement("li");
    box.className = "rde-empty";
    _escapeText(box, text);
    this._treeEl.appendChild(box);
  }

  _showError(err) {
    console.error("rivian-drive-explorer-card:", err);
    const message =
      err && err.code === "not_found" ? "Vehicle not found." : "Rivian drive data could not be loaded right now.";
    this._treeEl.textContent = "";
    const box = document.createElement("li");
    box.className = "rde-error";
    _escapeText(box, message);
    this._treeEl.appendChild(box);
  }
}

if (typeof customElements !== "undefined") {
  if (!customElements.get("rivian-drive-explorer-card")) {
    customElements.define("rivian-drive-explorer-card", RivianDriveExplorerCard);
  }
  window.customCards = window.customCards || [];
  if (!window.customCards.some((c) => c.type === "rivian-drive-explorer-card")) {
    window.customCards.push({
      type: "rivian-drive-explorer-card",
      name: "Rivian Drive Explorer",
      description:
        "Browse a vehicle's drives by calendar day, with a road heat map and GPS route playback.",
    });
  }
}
