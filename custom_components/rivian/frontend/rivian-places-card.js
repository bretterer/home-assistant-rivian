/**
 * rivian-places-card.js
 *
 * The "Places" tab card: a list pane (Suggestions, Places, a collapsed
 * Hidden fold) beside a Leaflet map showing one circle per place, sized to
 * its radius and colored by category.
 *
 * Places belong to the household, not to a vehicle: one shared list for the
 * vehicles selected in the shared vehicle bar, with each place's visits
 * broken down per car ("79 A · 12 B"). The category list comes from the
 * server (`categories` in the `rivian/places/list` reply).
 *
 * Backend calls (all via `hass.callWS`; places take a `dataset`, 'real' or
 * 'demo' -- every dataset represented in the selection is queried and the
 * results are concatenated, an edit goes to the place's own dataset):
 *   - `rivian/places/list {dataset, vins}` -- open to all users.
 *   - `rivian/places/update`, `rivian/places/create`, `rivian/places/merge`,
 *     `rivian/places/delete` -- admin only (enforced server-side; this card
 *     also hides the controls from non-admins via `hass.user.is_admin`).
 *   - `rivian/analytics/subscribe` -- refreshes the list on live updates.
 *
 * A number of pure helpers are exported purely so a Node smoke test
 * (tests/frontend/places_card.test.mjs) can import and exercise them without
 * a DOM or customElements environment. The module guards every top-level use
 * of HTMLElement/customElements/window/document so it can be imported under
 * plain Node, mirroring rivian-drive-explorer-card.js.
 */

const STACK_BREAKPOINT_PX = 700;

// Categorical palette, one color per place category (shop/other share a
// visual family since both are low-signal). Suggestions are drawn with a
// dashed outline in the same color as their (still unknown) category, or
// gray when no category has been guessed yet.
const CATEGORY_COLORS = {
  home: "#2e7d32",
  work: "#1565c0",
  school: "#6a1b9a",
  shop: "#ef6c00",
  dining: "#c62828",
  charging: "#00897b",
  friends: "#ad1457",
  family: "#d81b60",
  gym: "#37474f",
  swim: "#0288d1",
  mountain_biking: "#8d6e63",
  park: "#558b2f",
  medical: "#e53935",
  other: "#757575",
};
// Used only when the server's `categories` list is absent (an older backend):
// the same keys/labels/icons the server serves from `places.PLACE_CATEGORY_DEFS`.
export const FALLBACK_CATEGORIES = [
  { key: "home", label: "Home", icon: "mdi:home" },
  { key: "work", label: "Work", icon: "mdi:briefcase" },
  { key: "school", label: "School", icon: "mdi:school" },
  { key: "shop", label: "Shopping", icon: "mdi:cart" },
  { key: "charging", label: "Charging", icon: "mdi:ev-station" },
  { key: "other", label: "Other", icon: "mdi:map-marker" },
];

/** The category list of a `rivian/places/list` reply, else the fallback. */
export function categoryList(result) {
  const list = result && Array.isArray(result.categories) ? result.categories : null;
  return list && list.length ? list : FALLBACK_CATEGORIES;
}

/** The mdi icon for a place's category (server list first), else a plain pin. */
export function categoryIcon(category, categories = FALLBACK_CATEGORIES) {
  const found = (Array.isArray(categories) ? categories : []).find((c) => c.key === category);
  return (found && found.icon) || "mdi:map-marker";
}

/**
 * The datasets a vehicle selection spans, as `[{dataset, vins}]`: real
 * vehicles' places and demo vehicles' places never mix, so each dataset
 * represented in the selection is queried separately (real first).
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

/**
 * A place's per-vehicle visits for the selected vehicles, most visits first:
 * `[{vin, letter, color, count}]`. Empty unless the place was visited by
 * at least one selected vehicle.
 */
export function visitsByVehicle(place, vehicles, selected) {
  const by = (place && place.visits_by_vin) || {};
  const list = Array.isArray(vehicles) ? vehicles : [];
  const wanted = Array.isArray(selected) ? new Set(selected) : null;
  const out = [];
  for (const [vin, count] of Object.entries(by)) {
    if (!count || (wanted && !wanted.has(vin))) continue;
    const vehicle = list.find((v) => v.vin === vin);
    out.push({
      vin,
      letter: (vehicle && vehicle.letter) || "",
      color: (vehicle && vehicle.color) || "#8a8a8a",
      count,
    });
  }
  return out.sort((a, b) => b.count - a.count || a.letter.localeCompare(b.letter));
}

/** "79 A · 12 B" for a `visitsByVehicle` result ("" when empty). */
export function formatVisitsByVehicle(parts) {
  return (Array.isArray(parts) ? parts : [])
    .map((p) => `${p.count.toLocaleString()}${p.letter ? ` ${p.letter}` : ""}`)
    .join(" · ");
}

/** The map/legend color for a place's category, falling back to neutral gray. */
export function categoryColor(category) {
  return CATEGORY_COLORS[category] || CATEGORY_COLORS.other;
}

/** The place ids of `places` that live in `dataset` (merging stays within one dataset). */
export function sameDataset(a, b) {
  return !!a && !!b && (a.dataset || "real") === (b.dataset || "real");
}

/** "12 visits" / "1 visit" / "No visits yet". */
export function formatVisits(visits) {
  const n = typeof visits === "number" ? visits : 0;
  if (n === 0) return "No visits yet";
  return `${n.toLocaleString()} visit${n === 1 ? "" : "s"}`;
}

const DAY_SECONDS = 86400;

/** A short relative date ("today", "3 days ago", "in 2026"), or "never" when `ts` is null. */
export function formatLastVisit(ts, nowTs = Date.now() / 1000) {
  if (ts === null || ts === undefined || Number.isNaN(ts)) return "never";
  const deltaDays = Math.floor((nowTs - ts) / DAY_SECONDS);
  if (deltaDays <= 0) return "today";
  if (deltaDays === 1) return "yesterday";
  if (deltaDays < 7) return `${deltaDays} days ago`;
  if (deltaDays < 30) {
    const weeks = Math.floor(deltaDays / 7);
    return `${weeks} week${weeks === 1 ? "" : "s"} ago`;
  }
  if (deltaDays < 365) {
    const months = Math.floor(deltaDays / 30);
    return `${months} month${months === 1 ? "" : "s"} ago`;
  }
  const years = Math.floor(deltaDays / 365);
  return `${years} year${years === 1 ? "" : "s"} ago`;
}

// -- filters, charging spots and destination stats ------------------------------

export const ALL_FILTER = "all";
export const CHARGING_FILTER = "charging";
/** Charging sessions outside any place group into one spot within this many metres. */
export const SPOT_RADIUS_M = 100;
export const FILTER_STORAGE_KEY = "rivian-places-filter";

function _distM(lat1, lon1, lat2, lon2) {
  const rad = Math.PI / 180;
  const dLat = (lat2 - lat1) * rad;
  const dLon = (lon2 - lon1) * rad;
  const a = Math.sin(dLat / 2) ** 2 + Math.cos(lat1 * rad) * Math.cos(lat2 * rad) * Math.sin(dLon / 2) ** 2;
  return 2 * 6371008.8 * Math.asin(Math.min(1, Math.sqrt(a)));
}

function _isNum(v) {
  return typeof v === "number" && Number.isFinite(v);
}

/** The charging sessions the Destinations tab counts: every recorded one, never an inferred one. */
export function chargingSessions(sessions) {
  return (Array.isArray(sessions) ? sessions : []).filter((s) => s && !s.inferred && s.source !== "inferred");
}

/** Was this a charge at home (a home charger, the Home zone or a home-category place)? */
export function isHomeCharge(session) {
  return !!(session && (session.is_home_charge || session.is_home === true));
}

/** The Home place: the most-visited visible `home` place, else the `zone.home` place, else null. */
export function homePlaceId(places) {
  const visible = (Array.isArray(places) ? places : []).filter((p) => p && !p.hidden);
  const homes = visible.filter((p) => p.category === "home").sort((a, b) => (b.visits || 0) - (a.visits || 0));
  if (homes.length) return homes[0].id;
  const zone = visible.find((p) => p.zone_entity_id === "zone.home");
  return zone ? zone.id : null;
}

function _mostCommon(values) {
  const counts = new Map();
  for (const v of values) if (v) counts.set(v, (counts.get(v) || 0) + 1);
  let best = null;
  for (const [v, n] of counts) if (best === null || n > best[1]) best = [v, n];
  return best ? best[0] : null;
}

/** A charging spot's label: its most common station name, else network, else "Charging spot". */
export function spotLabel(sessions) {
  const list = Array.isArray(sessions) ? sessions : [];
  return _mostCommon(list.map((s) => s && s.station_name)) || _mostCommon(list.map((s) => s && s.brand_label)) || "Charging spot";
}

/**
 * Where each charge happened: `{byPlace: Map<placeId, session[]>, spots, unlocated}`.
 * A session in a visible place belongs to it; a home charge with no location
 * belongs to the Home place; any other located session joins the first spot
 * whose center is within SPOT_RADIUS_M (centers are running means), else
 * starts one (`{key: "spot:N", lat, lon, label, sessions}`); the rest are
 * counted in `unlocated`. Real and demo data never mix: `datasetOf(vin)`
 * ('real' | 'demo') partitions the sessions, each matched only against the
 * places of its own dataset (`place.dataset`, 'real' when absent).
 */
export function groupChargeSpots(sessions, places, datasetOf = () => "real") {
  const out = { byPlace: new Map(), spots: [], unlocated: 0 };
  const allPlaces = Array.isArray(places) ? places : [];
  const allSessions = Array.isArray(sessions) ? sessions : [];
  for (const ds of ["real", "demo"]) {
    const part = _groupOneDataset(
      allSessions.filter((s) => s && (datasetOf(s.vin) || "real") === ds),
      allPlaces.filter((p) => p && (p.dataset || "real") === ds),
      ds
    );
    for (const [id, list] of part.byPlace) out.byPlace.set(id, list);
    out.spots.push(...part.spots);
    out.unlocated += part.unlocated;
  }
  return out;
}

function _groupOneDataset(sessions, places, ds) {
  const visible = new Set(places.filter((p) => !p.hidden).map((p) => p.id));
  const homeId = homePlaceId(places);
  const byPlace = new Map();
  const spots = [];
  let unlocated = 0;
  const addTo = (id, s) => {
    if (!byPlace.has(id)) byPlace.set(id, []);
    byPlace.get(id).push(s);
  };
  for (const s of Array.isArray(sessions) ? sessions : []) {
    const pid = s && s.place && s.place.id;
    if (pid !== undefined && pid !== null && visible.has(pid)) {
      addTo(pid, s);
    } else if (_isNum(s.lat) && _isNum(s.lon)) {
      const spot = spots.find((g) => _distM(g.lat, g.lon, s.lat, s.lon) <= SPOT_RADIUS_M);
      if (spot) {
        const n = spot.sessions.length;
        spot.lat = (spot.lat * n + s.lat) / (n + 1);
        spot.lon = (spot.lon * n + s.lon) / (n + 1);
        spot.sessions.push(s);
      } else {
        spots.push({ key: `spot:${ds}:${spots.length}`, lat: s.lat, lon: s.lon, label: "", sessions: [s] });
      }
    } else if (isHomeCharge(s) && homeId !== null) {
      addTo(homeId, s);
    } else {
      unlocated += 1;
    }
  }
  for (const spot of spots) spot.label = spotLabel(spot.sessions);
  return { byPlace, spots, unlocated };
}

/** Does a place show under the filter chip? Hidden places only under All. */
export function placeMatchesFilter(place, filter, byPlace) {
  if (!place) return false;
  if (!filter || filter === ALL_FILTER) return true;
  if (place.hidden) return false;
  if (filter === CHARGING_FILTER) {
    const charges = byPlace && byPlace.get(place.id);
    return !!(charges && charges.length) || place.category === CHARGING_FILTER;
  }
  return place.category === filter;
}

/**
 * The filter chips: All, one per category present among the visible places
 * (server order; the `charging` category folds into the Charging chip), then
 * Charging when any place has charges or that category, or a spot exists.
 */
export function filterChips(places, categories, spots, byPlace = new Map()) {
  const visible = (Array.isArray(places) ? places : []).filter((p) => p && !p.hidden);
  const chips = [{ key: ALL_FILTER, label: "All", icon: "mdi:map-marker-multiple", count: visible.length }];
  const cats = Array.isArray(categories) && categories.length ? categories : FALLBACK_CATEGORIES;
  for (const c of cats) {
    if (c.key === CHARGING_FILTER) continue;
    const count = visible.filter((p) => p.category === c.key).length;
    if (count) chips.push({ key: c.key, label: c.label, icon: c.icon, count });
  }
  const charging = visible.filter((p) => placeMatchesFilter(p, CHARGING_FILTER, byPlace)).length + (Array.isArray(spots) ? spots.length : 0);
  if (charging) chips.push({ key: CHARGING_FILTER, label: "Charging", icon: "mdi:ev-station", count: charging });
  return chips;
}

const _hourFormatters = new Map();

/** The local hour of day (0-24, fractional) of epoch seconds `ts` in time zone `tz`. */
export function hourOfDay(ts, tz) {
  const key = tz || "";
  if (!_hourFormatters.has(key)) {
    _hourFormatters.set(
      key,
      new Intl.DateTimeFormat("en-US", { timeZone: tz || undefined, hour: "numeric", minute: "numeric", second: "numeric", hourCycle: "h23" })
    );
  }
  const parts = {};
  for (const p of _hourFormatters.get(key).formatToParts(new Date(ts * 1000))) parts[p.type] = Number(p.value);
  return ((parts.hour || 0) % 24) + (parts.minute || 0) / 60 + (parts.second || 0) / 3600;
}

/** The mean of hours on a 24-hour clock (23:30 and 00:30 average to midnight), or null. */
export function circularMeanHour(hours) {
  const list = (Array.isArray(hours) ? hours : []).filter(_isNum);
  if (!list.length) return null;
  let x = 0;
  let y = 0;
  for (const h of list) {
    const a = (h / 24) * 2 * Math.PI;
    x += Math.cos(a);
    y += Math.sin(a);
  }
  if (Math.abs(x) < 1e-12 && Math.abs(y) < 1e-12) return null;
  const hour = (Math.atan2(y, x) / (2 * Math.PI)) * 24;
  return (hour + 24) % 24;
}

/** "5:40 pm" for a fractional hour of day. */
export function formatClock(hour) {
  let h = Math.floor(hour);
  let m = Math.round((hour - h) * 60);
  if (m === 60) {
    h += 1;
    m = 0;
  }
  h = ((h % 24) + 24) % 24;
  const h12 = h % 12 === 0 ? 12 : h % 12;
  return `${h12}:${String(m).padStart(2, "0")} ${h < 12 ? "am" : "pm"}`;
}

/** "34 min", "3 h 10 min", "2 d 4 h". */
export function formatStay(seconds) {
  const s = Math.max(0, seconds || 0);
  if (s < 3600) return `${Math.round(s / 60)} min`;
  if (s < 86400) {
    const h = Math.floor(s / 3600);
    const m = Math.round((s - h * 3600) / 60);
    return m ? `${h} h ${m} min` : `${h} h`;
  }
  const d = Math.floor(s / 86400);
  const h = Math.round((s - d * 86400) / 3600);
  return h ? `${d} d ${h} h` : `${d} d`;
}

/** Counts per hour of day (24 bins, 0 = midnight to 1 am). */
export function hourBins(hours) {
  const bins = new Array(24).fill(0);
  for (const h of Array.isArray(hours) ? hours : []) {
    if (!_isNum(h)) continue;
    bins[Math.min(23, Math.max(0, Math.floor(h)))] += 1;
  }
  return bins;
}

function _niceStep(max, target) {
  const rough = max / Math.max(1, target);
  const pow = Math.pow(10, Math.floor(Math.log10(rough)));
  const frac = rough / pow;
  return (frac <= 1 ? 1 : frac <= 2 ? 2 : frac <= 5 ? 5 : 10) * pow;
}

/**
 * Histogram of `values` in about `targetBins` nice-width bins from 0 (the top
 * bin includes its upper edge): `{step, max, bins: [{from, to, count, idx}]}`,
 * or null with no values. Same contract as the Charging card's.
 */
export function histogram(values, targetBins = 10) {
  const valid = (Array.isArray(values) ? values : []).map((v, i) => [v, i]).filter(([v]) => _isNum(v) && v >= 0);
  if (!valid.length) return null;
  const hi = Math.max(...valid.map(([v]) => v));
  const step = hi > 0 ? _niceStep(hi, targetBins) : 1;
  const n = Math.max(1, Math.ceil(hi / step - 1e-9));
  const bins = Array.from({ length: n }, (_v, i) => ({ from: +(i * step).toFixed(6), to: +((i + 1) * step).toFixed(6), count: 0, idx: [] }));
  for (const [v, i] of valid) {
    const b = bins[Math.min(n - 1, Math.floor(v / step + 1e-9))];
    b.count += 1;
    b.idx.push(i);
  }
  return { step, max: Math.max(...bins.map((b) => b.count)), bins };
}

/**
 * Visit stats from `rivian/places/stays`: visits (arrivals, else departures),
 * stay lengths (only stays with both ends), mean stay, and the typical
 * (circular-mean) arrival and departure hour in time zone `tz`.
 */
export function stayStats(stays, tz) {
  const list = Array.isArray(stays) ? stays : [];
  const arrivals = list.filter((s) => _isNum(s.arrive_ts));
  const departures = list.filter((s) => _isNum(s.depart_ts));
  const paired = list.filter((s) => _isNum(s.arrive_ts) && _isNum(s.depart_ts) && s.depart_ts >= s.arrive_ts);
  const lengths = paired.map((s) => s.depart_ts - s.arrive_ts);
  const arriveHours = arrivals.map((s) => hourOfDay(s.arrive_ts, tz));
  const departHours = departures.map((s) => hourOfDay(s.depart_ts, tz));
  return {
    visits: arrivals.length || departures.length,
    avgStayS: lengths.length ? lengths.reduce((a, b) => a + b, 0) / lengths.length : null,
    typicalArrive: circularMeanHour(arriveHours),
    typicalDepart: circularMeanHour(departHours),
    lengths,
    arriveHours,
    departHours,
    vins: { arrive: arrivals.map((s) => s.vin), depart: departures.map((s) => s.vin), length: paired.map((s) => s.vin) },
  };
}

/** Why a place has no visit stats to fetch (a hidden place labels no drives), or null. */
export function staysUnavailableReason(place) {
  return place && place.hidden ? "Hidden places don't label drives, so they have no visit stats. Unhide it to see them." : null;
}

function _nearestRank(sorted, q) {
  return sorted[Math.max(0, Math.ceil(q * sorted.length) - 1)];
}

/**
 * Bins for the length-of-stay histogram (`lengthsS` in seconds). Stays beyond
 * the upper fence (Q3 + 1.5 x IQR, e.g. a week away from home) go into one
 * final overflow bin so they don't flatten the everyday stays into one bar.
 * `{unit (60 | 3600 s), values (in unit), step, n (bins incl. overflow),
 * overflowFrom (in unit, or null), index(v)}`, or null with no lengths.
 */
export function stayHistogramSpec(lengthsS) {
  const secs = (Array.isArray(lengthsS) ? lengthsS : []).filter((v) => _isNum(v) && v >= 0);
  if (!secs.length) return null;
  const sorted = [...secs].sort((a, b) => a - b);
  const max = sorted[sorted.length - 1];
  const q1 = _nearestRank(sorted, 0.25);
  const q3 = _nearestRank(sorted, 0.75);
  const top = Math.min(max, q3 + 1.5 * (q3 - q1));
  const unit = top < 2 * 3600 ? 60 : 3600;
  const topU = top / unit;
  const step = topU > 0 ? _niceStep(topU, 10) : 1;
  const nBase = Math.max(1, Math.ceil(topU / step - 1e-9));
  const edge = +(nBase * step).toFixed(6);
  const values = secs.map((v) => v / unit);
  const overflow = values.some((v) => v > edge + 1e-9);
  return {
    unit,
    values,
    step,
    n: nBase + (overflow ? 1 : 0),
    overflowFrom: overflow ? edge : null,
    index: (v) => (overflow && v > edge + 1e-9 ? nBase : Math.min(nBase - 1, Math.floor(v / step + 1e-9))),
  };
}

/**
 * `n` histogram bins with each bin's count split by vehicle: `[{count, byVin}]`.
 * `binIndex(value)` picks the bin (clamped into range); `vins[i]` owns `values[i]`.
 */
export function stackBins(values, vins, binIndex, n) {
  const bins = Array.from({ length: n }, () => ({ count: 0, byVin: {} }));
  (Array.isArray(values) ? values : []).forEach((v, i) => {
    if (!_isNum(v)) return;
    const b = bins[Math.min(n - 1, Math.max(0, binIndex(v)))];
    const vin = (vins && vins[i]) || "";
    b.count += 1;
    b.byVin[vin] = (b.byVin[vin] || 0) + 1;
  });
  return bins;
}

/** "dc" | "ac_l2" | "ac_l1" | "ac": the server's charge_type, else from kind and average power (L1 under 2 kW). */
function _chargeType(s) {
  if (s.charge_type) return s.charge_type;
  if (s.kind === "dc") return "dc";
  return _isNum(s.avg_power_kw) && s.avg_power_kw > 0 && s.avg_power_kw < 2 ? "ac_l1" : "ac_l2";
}

/** Charging stats for a place or spot: count, kWh, SoC added, charges per type, and each session's average kW. */
export function chargingStats(sessions) {
  const list = Array.isArray(sessions) ? sessions : [];
  const withKwh = list.filter((s) => _isNum(s.energy_added_kwh));
  const totalKwh = withKwh.reduce((a, s) => a + s.energy_added_kwh, 0);
  const socAdded = list.filter((s) => _isNum(s.start_soc) && _isNum(s.end_soc)).map((s) => s.end_soc - s.start_soc);
  const byType = { dc: 0, ac_l2: 0, ac_l1: 0, ac: 0 };
  for (const s of list) {
    const t = _chargeType(s);
    byType[t in byType ? t : "ac"] += 1;
  }
  const rateSessions = list.filter((s) => _isNum(s.avg_power_kw) && s.avg_power_kw > 0);
  return {
    count: list.length,
    totalKwh,
    avgKwh: withKwh.length ? totalKwh / withKwh.length : null,
    avgSocAdded: socAdded.length ? socAdded.reduce((a, b) => a + b, 0) / socAdded.length : null,
    byType,
    rates: rateSessions.map((s) => s.avg_power_kw),
    rateVins: rateSessions.map((s) => s.vin),
    noRate: list.length - rateSessions.length,
  };
}

/**
 * Groups and sorts the raw `rivian/places/list` result for the list pane:
 * Suggestions (unnamed autodetected places, by visits desc), Places (named
 * or zone-backed places, by visits desc), and Hidden (any hidden place, by
 * visits desc), each ordered independently of the other groups.
 */
export function groupPlaces(places) {
  const list = Array.isArray(places) ? places : [];
  const byVisitsDesc = (a, b) => (b.visits || 0) - (a.visits || 0);
  const visible = list.filter((p) => !p.hidden);
  const hidden = list.filter((p) => p.hidden);
  const suggestions = visible
    .filter((p) => !p.name && p.source !== "zone")
    .sort(byVisitsDesc);
  const named = visible
    .filter((p) => p.name || p.source === "zone")
    .sort(byVisitsDesc);
  return {
    suggestions,
    named,
    hidden: [...hidden].sort(byVisitsDesc),
  };
}

function _haversineKm(lat1, lon1, lat2, lon2) {
  const r = Math.PI / 180;
  const a =
    Math.sin(((lat2 - lat1) * r) / 2) ** 2 +
    Math.cos(lat1 * r) * Math.cos(lat2 * r) * Math.sin(((lon2 - lon1) * r) / 2) ** 2;
  return 2 * 6371 * Math.asin(Math.sqrt(a));
}

/**
 * The places to frame on first load: the visible ones within `radiusKm` of
 * the most-visited place. One road-trip stop hundreds of km away would
 * otherwise zoom the map out to the whole region and shrink every local
 * place to a dot. Falls back to every visible place.
 */
export function homeRegionPlaces(places, radiusKm = 60) {
  const visible = (Array.isArray(places) ? places : []).filter((p) => !p.hidden);
  if (!visible.length) return [];
  const anchor = visible.reduce((a, b) => ((b.visits || 0) > (a.visits || 0) ? b : a));
  const near = visible.filter((p) => _haversineKm(anchor.lat, anchor.lon, p.lat, p.lon) <= radiusKm);
  return near.length ? near : visible;
}

export const MIN_RADIUS_M = 25;
export const MAX_RADIUS_M = 500;

/** A radius in metres, rounded to 5 m and clamped to the editable range. */
export function clampRadius(m) {
  const v = Math.round(Number(m) / 5) * 5;
  if (!Number.isFinite(v)) return MIN_RADIUS_M;
  return Math.min(MAX_RADIUS_M, Math.max(MIN_RADIUS_M, v));
}

/** Where the resize handle sits: due east of the centre, on the circle's edge. */
export function radiusHandleLatLng(lat, lon, radiusM) {
  const metresPerDegLon = 111320 * Math.cos((lat * Math.PI) / 180);
  return [lat, lon + radiusM / Math.max(1, metresPerDegLon)];
}

/** One place's tooltip: "Home \u00b7 79 visits \u00b7 last today \u00b7 100 m radius \u00b7 zone". */
export function placeTooltip(place) {
  if (!place) return "";
  const parts = [place.label || "Place"];
  parts.push(formatVisits(place.visits));
  parts.push(`last ${formatLastVisit(place.last_visit_ts)}`);
  if (typeof place.radius_m === "number") parts.push(`${Math.round(place.radius_m)} m radius`);
  if (place.source === "zone") parts.push("Home Assistant zone");
  else if (!place.name) parts.push("suggestion (not yet named)");
  return parts.join(" \u00b7 ");
}

/** The human sentence for how a place came to exist, for the detail panel. */
export function sourceDescription(source) {
  if (source === "zone") return "From an HA zone";
  if (source === "user") return "Added by you";
  return "Detected from your stops";
}

/**
 * The `window.confirm` text for deleting a place, which depends on its
 * source: a `user` place is deleted outright, an `auto` suggestion is only
 * hidden (so it can be restored from Hidden). Never called for a `zone`
 * place -- those aren't offered a delete control at all.
 */
export function deletePlaceMessage(place) {
  if (place && place.source === "auto") {
    return "Remove this suggestion? It won't be suggested again; you can restore it from Hidden.";
  }
  const label = (place && (place.name || place.label)) || "this place";
  return `Delete “${label}”? Drives that started or ended here will no longer be labeled with it. This can't be undone.`;
}

// -- DOM-dependent card -------------------------------------------------------

const ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services";
const ESRI_ATTRIBUTION =
  'Tiles &copy; Esri | Map data &copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors';
// Copied from rivian-drive-explorer-card.js's BASEMAPS (nothing shared
// between the two modules, to avoid a stale-cache risk from an unversioned
// shared import -- see that file's header comment).
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
const BASEMAP_STORAGE_KEY = "rivian-places-basemap";
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
    _leafletCssPromise = fetch(new URL("./leaflet/leaflet.css", import.meta.url)).then((r) =>
      r.text()
    );
  }
  return _leafletCssPromise;
}

/** A touch-first device (phone/tablet), where one-finger drags should scroll the page. */
function _isTouchDevice() {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia("(pointer: coarse)").matches;
}

const _CARD_STYLE = `
  :host { display: block; }
  .rpc-topbar {
    border-bottom: 1px solid var(--divider-color, #e0e0e0);
  }
  .rpc-topbar rivian-vehicle-bar {
    padding: 8px 12px;
  }
  .rpc-vb {
    display: inline-flex;
    align-items: center;
    gap: 3px;
    margin-left: 6px;
    font-weight: 600;
  }
  .rpc-vb::before {
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
  .rpc-body {
    display: flex;
    flex: 1;
    min-height: 0;
  }
  .rpc-body.rpc-stacked {
    flex-direction: column;
  }
  .rpc-list-pane {
    width: 320px;
    min-width: 260px;
    flex-shrink: 0;
    display: flex;
    flex-direction: column;
    border-right: 1px solid var(--divider-color, #e0e0e0);
    overflow: hidden;
  }
  .rpc-stacked .rpc-list-pane {
    display: contents;
  }
  .rpc-stacked .rpc-list {
    order: 2;
    flex: none;
    overflow: visible;
  }
  .rpc-stacked .rpc-detail {
    order: 3;
    max-height: none;
  }
  .rpc-list-header {
    padding: 8px 12px;
    font-size: 20px; font-weight: 700; letter-spacing: 0.01em; color: var(--primary-text-color);
    border-bottom: 1px solid var(--divider-color, #e0e0e0);
    flex-shrink: 0;
  }
  .rpc-list {
    flex: 1;
    overflow: auto;
    min-height: 0;
  }
  .rpc-group-title {
    padding: 8px 12px 2px;
    font-size: 0.78em;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--secondary-text-color);
  }
  .rpc-row:focus-visible, .rpc-hidden-toggle:focus-visible, .rpc-basemaps button:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: -2px;
  }
  .rpc-row {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 12px;
    cursor: pointer;
    border-bottom: 1px solid var(--divider-color, rgba(0, 0, 0, 0.06));
  }
  .rpc-row:hover, .rpc-row.selected {
    background: var(--secondary-background-color, rgba(0, 0, 0, 0.04));
  }
  .rpc-row ha-icon {
    color: var(--secondary-text-color);
    flex-shrink: 0;
  }
  .rpc-row-text {
    min-width: 0;
    flex: 1;
  }
  .rpc-row-label {
    font-size: 0.95em;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .rpc-row-meta {
    font-size: 0.75em;
    color: var(--secondary-text-color);
  }
  .rpc-chip {
    font-size: 0.68em;
    padding: 1px 6px;
    border-radius: 10px;
    background: var(--secondary-background-color, rgba(0, 0, 0, 0.08));
    color: var(--secondary-text-color);
    flex-shrink: 0;
  }
  .rpc-hidden-toggle {
    padding: 8px 12px;
    font-size: 0.85em;
    color: var(--secondary-text-color);
    cursor: pointer;
    border-bottom: 1px solid var(--divider-color, #e0e0e0);
  }
  .rpc-empty {
    padding: 16px 12px;
    color: var(--secondary-text-color);
    font-size: 0.9em;
  }
  .rpc-map-pane {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-width: 0;
    min-height: 0;
  }
  .rpc-map-container {
    position: relative;
    flex: 1;
    min-height: 260px;
  }
  .rpc-map {
    position: absolute;
    inset: 0;
  }
  .rpc-basemaps {
    position: absolute;
    top: 10px;
    right: 10px;
    z-index: 1000;
    display: flex;
    border-radius: 6px;
    overflow: hidden;
    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.35);
  }
  .rpc-basemaps button {
    font: inherit;
    font-size: 12px;
    padding: 5px 10px;
    border: none;
    cursor: pointer;
    background: var(--card-background-color, #fff);
    color: var(--primary-text-color, #212121);
  }
  .rpc-basemaps button + button {
    border-left: 1px solid var(--divider-color, rgba(0, 0, 0, 0.12));
  }
  .rpc-basemaps button.active {
    background: var(--primary-color, #03a9f4);
    color: var(--text-primary-color, #fff);
  }
  .rpc-detail {
    border-top: 1px solid var(--divider-color, #e0e0e0);
    padding: 10px 14px;
    font-size: 0.88em;
    flex-shrink: 0;
    max-height: 45%;
    overflow: auto;
  }
  .rpc-detail-title {
    font-size: 1.05em;
    font-weight: 500;
    margin-bottom: 2px;
  }
  .rpc-detail-sub {
    color: var(--secondary-text-color);
    margin-bottom: 8px;
  }
  .rpc-field {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 6px;
  }
  .rpc-field label {
    width: 72px;
    flex-shrink: 0;
    color: var(--secondary-text-color);
    font-size: 0.85em;
  }
  .rpc-field input[type="text"],
  .rpc-field select {
    flex: 1;
    font: inherit;
    padding: 4px 6px;
    border-radius: 4px;
    border: 1px solid var(--divider-color, #e0e0e0);
    background: var(--card-background-color, #fff);
    color: var(--primary-text-color);
  }
  /* Customizable <select> (Chromium 135+, incl. HA's Android app): the open
     list is drawn by the page, so it follows the theme -- the native list
     ignored it (white frame, wrong size and scrollbar in dark mode). Other
     browsers keep the native control. */
  @supports (appearance: base-select) {
    .rpc-field select, .rpc-field select::picker(select) { appearance: base-select; }
    .rpc-field select { display: inline-flex; align-items: center; gap: 6px; cursor: pointer; }
    .rpc-field select::picker-icon { color: var(--secondary-text-color, #727272); font-size: 0.8em; }
    .rpc-field select::picker(select) {
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
    .rpc-field select option { padding: 6px 12px; background: transparent; color: inherit; min-height: 0; }
    .rpc-field select option:hover, .rpc-field select option:focus-visible { background: var(--secondary-background-color, rgba(127, 127, 127, 0.18)); outline: none; }
    .rpc-field select option:checked { font-weight: 600; }
    .rpc-field select option::checkmark { color: var(--primary-color, #03a9f4); }
  }
  .rpc-field input[type="range"] {
    flex: 1;
  }
  option { background-color: inherit; color: inherit; }
  .rpc-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-top: 8px;
  }
  .rpc-actions button {
    font: inherit;
    font-size: 0.85em;
    padding: 5px 10px;
    border-radius: 4px;
    border: 1px solid var(--divider-color, #e0e0e0);
    background: var(--card-background-color, #fff);
    color: var(--primary-text-color);
    cursor: pointer;
  }
  .rpc-actions button.rpc-primary {
    background: var(--primary-color, #03a9f4);
    color: var(--text-primary-color, #fff);
    border-color: transparent;
  }
  .rpc-actions button.rpc-danger {
    color: var(--error-color, #b00020);
    border-color: var(--error-color, #b00020);
  }
  .rpc-status {
    margin-top: 6px;
    font-size: 0.82em;
    color: var(--secondary-text-color);
  }
  .rpc-link {
    color: var(--primary-color, #03a9f4);
    cursor: pointer;
  }
  /* Map edit handles (Leaflet divIcons): a white-ringed centre dot to move
     the place and a square edge handle to resize it. */
  .rpc-handle {
    box-sizing: border-box;
    border: 3px solid #ffffff;
    box-shadow: 0 0 3px rgba(0, 0, 0, 0.6);
    background: var(--primary-color, #03a9f4);
    cursor: grab;
  }
  .rpc-handle-center {
    border-radius: 50%;
  }
  .rpc-handle-edge {
    border-radius: 3px;
    cursor: ew-resize;
  }
  .rpc-merge-hint {
    margin-top: 4px;
    font-size: 0.82em;
    color: var(--primary-color, #03a9f4);
  }
  .rpc-error {
    padding: 16px;
    color: var(--error-color, #b00020);
  }
  /* Filter chips (category / Charging). */
  .rpc-filters {
    display: flex;
    gap: 6px;
    padding: 8px 12px;
    overflow-x: auto;
    flex-shrink: 0;
    scrollbar-width: thin;
    border-bottom: 1px solid var(--divider-color, #e0e0e0);
  }
  .rpc-fchip {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font: inherit;
    font-size: 12px;
    padding: 3px 10px;
    min-height: 28px;
    border-radius: 999px;
    border: 1px solid var(--divider-color, #e0e0e0);
    background: transparent;
    color: var(--primary-text-color, #212121);
    cursor: pointer;
    white-space: nowrap;
  }
  .rpc-fchip ha-icon { --mdc-icon-size: 16px; color: var(--secondary-text-color); }
  .rpc-fchip small { color: var(--secondary-text-color); }
  .rpc-fchip[aria-pressed="true"] {
    background: color-mix(in srgb, var(--primary-color, #03a9f4) 18%, transparent);
    border-color: var(--primary-color, #03a9f4);
  }
  .rpc-fchip:focus-visible, .rpc-hist:focus-visible, .rpc-stats-close:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: 2px;
  }
  .rpc-row-bolt { color: #00897b; font-weight: 600; margin-left: 4px; }
  .rpc-note {
    padding: 6px 12px 10px;
    font-size: 0.78em;
    color: var(--secondary-text-color);
  }
  /* Charging-spot markers on the map (Leaflet divIcons). */
  .rpc-bolt {
    display: flex;
    align-items: center;
    justify-content: center;
    box-sizing: border-box;
    font-size: 13px;
    line-height: 1;
    border-radius: 50%;
    background: #ffffff;
    border: 2px solid #00897b;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.45);
    cursor: pointer;
  }
  .rpc-bolt.selected { background: #00897b; border-color: #ffffff; }
  .rpc-bolt-badge { font-size: 10px; border-width: 1.5px; }
  /* Stats section below the list and map. */
  .rpc-stats {
    position: relative;
    border-top: 1px solid var(--divider-color, #e0e0e0);
    padding: 12px 14px 16px;
    font-size: 0.92em;
  }
  .rpc-stats-head { display: flex; align-items: center; gap: 8px; }
  .rpc-stats-title { font-size: 1.15em; font-weight: 600; flex: 1; min-width: 0; }
  .rpc-stats-close {
    font: inherit;
    font-size: 18px;
    line-height: 1;
    width: 30px;
    height: 30px;
    border-radius: 50%;
    border: 1px solid var(--divider-color, #e0e0e0);
    background: transparent;
    color: var(--primary-text-color);
    cursor: pointer;
  }
  .rpc-stats-sec + .rpc-stats-sec { margin-top: 14px; padding-top: 10px; border-top: 1px solid var(--divider-color, #e0e0e0); }
  .rpc-stats-h { font-weight: 600; margin: 4px 0 2px; }
  .rpc-stats-note { font-size: 0.85em; color: var(--secondary-text-color); margin: 4px 0; }
  .rpc-tiles { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; margin: 8px 0 10px; }
  .rpc-narrow .rpc-tiles { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .rpc-tile b { display: block; font-size: 1.3em; font-weight: 600; line-height: 1.25; }
  .rpc-tile span { font-size: 0.78em; color: var(--secondary-text-color); }
  .rpc-hists { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; }
  .rpc-narrow .rpc-hists { grid-template-columns: minmax(0, 1fr); }
  .rpc-hist-title { font-size: 0.85em; font-weight: 600; margin-bottom: 2px; }
  .rpc-hist { position: relative; touch-action: pan-y; }
  .rpc-hist svg { display: block; width: 100%; height: auto; overflow: visible; }
  .rpc-hist text { fill: var(--secondary-text-color, #727272); font-size: 10px; font-family: inherit; }
  .rpc-hist .rpc-grid { stroke: var(--divider-color, #e0e0e0); stroke-width: 1; }
  .rpc-hist .rpc-axis { stroke: var(--secondary-text-color, #727272); stroke-width: 1; opacity: 0.6; }
  .rpc-hist .rpc-mid { stroke: var(--primary-text-color, #212121); stroke-width: 1.2; stroke-dasharray: 4 3; opacity: 0.75; }
  .rpc-hist text.rpc-midlabel { fill: var(--primary-text-color, #212121); font-weight: 600; }
  .rpc-hit { fill: transparent; }
  .rpc-hit.on { fill: var(--primary-text-color, #212121); fill-opacity: 0.08; }
  .rpc-legend { display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 0.8em; margin-top: 6px; }
  .rpc-legend span { display: inline-flex; align-items: center; gap: 5px; }
  .rpc-legend i { width: 10px; height: 10px; border-radius: 2px; background: var(--sw, #888); }
  .rpc-tip {
    position: absolute;
    z-index: 5;
    display: none;
    pointer-events: none;
    max-width: 260px;
    padding: 6px 9px;
    border-radius: 8px;
    font-size: 12px;
    line-height: 1.4;
    background: var(--card-background-color, #fff);
    color: var(--primary-text-color, #212121);
    border: 1px solid var(--divider-color, #e0e0e0);
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.25);
  }
`;

function _esc(text) {
  return String(text === null || text === undefined ? "" : text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function _median(values) {
  const v = values.filter((x) => Number.isFinite(x)).sort((a, b) => a - b);
  if (!v.length) return NaN;
  const mid = Math.floor(v.length / 2);
  return v.length % 2 ? v[mid] : (v[mid - 1] + v[mid]) / 2;
}

/** Bin-edge axis ticks for `n` bins of `step`, thinned to stay ~30 px apart. */
function _edgeTicks(n, step, width, fmt) {
  const every = Math.max(1, Math.ceil(30 / Math.max(1, (width - 36) / n)));
  const out = [];
  for (let i = 0; i <= n; i += every) out.push({ v: i * step, label: fmt(i * step) });
  return out;
}

/** The remembered filter chip (localStorage may be unavailable). */
function _loadFilter() {
  try {
    return (typeof window !== "undefined" && window.localStorage.getItem(FILTER_STORAGE_KEY)) || ALL_FILTER;
  } catch (_err) {
    return ALL_FILTER;
  }
}

function _saveFilter(key) {
  try {
    if (typeof window !== "undefined") window.localStorage.setItem(FILTER_STORAGE_KEY, key);
  } catch (_err) {
    /* storage unavailable: not remembered */
  }
}

/** Load the shared vehicle bar/selection module with this module's own cache-buster. */
function _loadBarModule() {
  return import(new URL("./rivian-vehicle-bar.js" + new URL(import.meta.url).search, import.meta.url));
}

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

class RivianPlacesCard extends BaseElement {
  static getStubConfig() {
    return {};
  }

  setConfig(config) {
    const next = config || {};
    // A configured `vin` pins the card; otherwise it follows the shared
    // vehicle selection.
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
    if (themeChanged) this._applyTileLayer();
  }

  _resetState() {
    this._places = [];
    this._categories = FALLBACK_CATEGORIES;
    this._selectedId = null;
    this._showHidden = false;
    this._mergeMode = null; // { intoId } while picking a target to merge into
    this._circles = new Map();
    this._charge = { byPlace: new Map(), spots: [], unlocated: 0 };
    this._selectedSpot = null; // a charging spot's key while one is selected
    this._stays = null; // the selected place's stays, "loading", or {error}
    this._statsToken = 0;
  }

  _build() {
    this._built = true;
    this._started = false;
    this._map = null;
    this._tileLayers = [];
    this._basemap = _initialBasemap(this._config && this._config.basemap);
    this._leaflet = null;
    this._vins = [];
    this._selection = [];
    this._vehicleList = [];
    this._selection = [];
    this._bar = null;
    this._barEl = null;
    this._unsubSelection = null;
    this._resizeObserver = null;
    this._bolts = new Map();
    this._filter = _loadFilter();
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
    const height = (this._config && this._config.height) || null;
    if (height) this._card.style.height = height;
    else this._card.style.height = "calc(100vh - var(--header-height, 56px) - 32px)";
    this._card.style.minHeight = "420px";

    // Vehicle bar slot (filled once the card follows the shared selection),
    // with a note while several vehicles are selected.
    this._topbarEl = document.createElement("div");
    this._topbarEl.className = "rpc-topbar";
    this._topbarEl.style.display = "none";
    this._card.appendChild(this._topbarEl);

    // Category / Charging filter chips (filled once places load).
    this._filtersEl = document.createElement("div");
    this._filtersEl.className = "rpc-filters";
    this._filtersEl.setAttribute("role", "group");
    this._filtersEl.setAttribute("aria-label", "Filter destinations");
    this._filtersEl.style.display = "none";
    this._card.appendChild(this._filtersEl);

    this._body = document.createElement("div");
    this._body.className = "rpc-body";
    this._card.appendChild(this._body);

    this._listPane = document.createElement("div");
    this._listPane.className = "rpc-list-pane";
    this._body.appendChild(this._listPane);

    const header = document.createElement("div");
    header.className = "rpc-list-header";
    _escapeText(header, "Destinations");
    this._listPane.appendChild(header);

    this._listEl = document.createElement("div");
    this._listEl.className = "rpc-list";
    this._listPane.appendChild(this._listEl);

    this._detailEl = document.createElement("div");
    this._detailEl.className = "rpc-detail";
    this._listPane.appendChild(this._detailEl);

    this._mapPane = document.createElement("div");
    this._mapPane.className = "rpc-map-pane";
    this._body.appendChild(this._mapPane);

    this._mapContainer = document.createElement("div");
    this._mapContainer.className = "rpc-map-container";
    this._mapPane.appendChild(this._mapContainer);

    this._mapEl = document.createElement("div");
    this._mapEl.className = "rpc-map";
    this._mapContainer.appendChild(this._mapEl);

    if (!(this._config && this._config.tile_url)) {
      this._basemapEl = document.createElement("div");
      this._basemapEl.className = "rpc-basemaps";
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

    // Visit and charging stats for the selected place or charging spot.
    this._statsEl = document.createElement("div");
    this._statsEl.className = "rpc-stats";
    this._statsEl.style.display = "none";
    this._card.appendChild(this._statsEl);
    this._statsEl.addEventListener("click", (ev) => {
      if (ev.target.closest && ev.target.closest("[data-act='close']")) this._deselect();
    });
    this._statsEl.addEventListener("pointermove", (ev) => this._onStatsPointer(ev));
    this._statsEl.addEventListener("pointerdown", (ev) => this._onStatsPointer(ev));
    this._statsEl.addEventListener("pointerleave", () => this._hideStatsTip());
    this._statsEl.addEventListener("keydown", (ev) => this._onStatsKey(ev));
    this._card.addEventListener("keydown", (ev) => {
      if (ev.key !== "Escape" || this._mergeMode) return;
      const t = ev.composedPath ? ev.composedPath()[0] : ev.target;
      if (t && (t.tagName === "INPUT" || t.tagName === "SELECT" || t.tagName === "TEXTAREA")) return;
      if (this._selectedId !== null || this._selectedSpot) this._deselect();
    });

    this._applyLayout();
    this._observeResize();
    this._renderList();
    this._renderDetail();
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
    this._body.classList.toggle("rpc-stacked", stacked);
    if (this._statsEl) this._statsEl.classList.toggle("rpc-narrow", stacked);
    const height = (this._config && this._config.height) || null;
    // While stats are open on a wide card, list+map shrink to a fixed height
    // and the card grows so the stats sit right below them.
    const open = this._selectedId !== null || !!this._selectedSpot;
    const fixed = !stacked && open;
    if (fixed) this._card.style.height = "auto";
    else if (height) this._card.style.height = height;
    else this._card.style.height = stacked ? "auto" : "calc(100vh - var(--header-height, 56px) - 32px)";
    this._body.style.flex = fixed ? "none" : "";
    this._body.style.height = fixed ? "min(55vh, 520px)" : "";
    this._card.style.minHeight = stacked || fixed ? "" : "420px";
    this._applyMapTouchMode(stacked);
    this._stacked = stacked;
  }

  /** Stacked on a touch screen, one-finger drags scroll the page instead of panning the map (pinch still pans and zooms it). Copied from rivian-drive-explorer-card.js's _applyMapTouchMode. */
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
        console.warn("rivian-places-card: live updates unavailable", err);
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
      console.warn("rivian-places-card: vehicle list unavailable", err);
    }
    if (this._config && this._config.vin) {
      this._vins = [this._config.vin];
      this._selection = [...this._vins];
      return;
    }
    try {
      this._selection = this._bar ? await this._bar.getSelection(this._hass) : [];
    } catch (err) {
      console.warn("rivian-places-card: vehicle selection unavailable", err);
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
    if (this._map) for (const layer of this._circles.values()) this._map.removeLayer(layer);
    this._resetState();
    this._applyLayout();
    this._subscribe();
    this._refresh().catch((err) => this._showError(err));
  }

  _showMessage(text) {
    this._card.textContent = "";
    const el = document.createElement("div");
    el.className = "rpc-error";
    _escapeText(el, text);
    this._card.appendChild(el);
  }

  async _refresh() {
    const groups = datasetGroups(this._vehicleList, this._vins);
    // A card pinned to a vehicle the list doesn't know still asks for it.
    const queries = groups.length
      ? groups
      : this._vins.length
        ? [{ dataset: undefined, vins: [...this._vins] }]
        : [];
    const results = await Promise.all(
      queries.map((g) =>
        this._hass.callWS({
          type: "rivian/places/list",
          ...(g.dataset ? { dataset: g.dataset } : {}),
          vins: g.vins,
        })
      )
    );
    const places = [];
    results.forEach((result, i) => {
      for (const place of (result && result.places) || []) {
        places.push({ ...place, dataset: (result && result.dataset) || queries[i].dataset || "real" });
      }
    });
    let sessions = [];
    try {
      const reply = this._vins.length
        ? await this._hass.callWS({ type: "rivian/charging/sessions", vins: [...this._vins] })
        : null;
      sessions = (reply && reply.sessions) || [];
    } catch (err) {
      if (!this._chargeWarned) console.warn("rivian-places-card: charging sessions unavailable", err);
      this._chargeWarned = true;
    }
    this._places = places;
    this._categories = categoryList(results[0]);
    const demoVins = new Set(this._vehicleList.filter((v) => v.is_demo).map((v) => v.vin));
    this._charge = groupChargeSpots(chargingSessions(sessions), places, (vin) => (demoVins.has(vin) ? "demo" : "real"));
    if (!filterChips(places, this._categories, this._charge.spots, this._charge.byPlace).some((c) => c.key === this._filter)) {
      this._filter = ALL_FILTER;
    }
    if (this._selectedSpot && !this._charge.spots.some((sp) => sp.key === this._selectedSpot)) this._selectedSpot = null;
    if (this._selectedId !== null && !places.some((p) => p.id === this._selectedId)) this._selectedId = null;
    this._applyLayout();
    this._renderFilters();
    this._renderList();
    this._renderDetail();
    this._renderMap();
    if (this._selectedId !== null) this._loadStats(false);
    else this._renderStats();
  }

  // -- filter chips -----------------------------------------------------------

  _renderFilters() {
    const chips = filterChips(this._places, this._categories, this._charge.spots, this._charge.byPlace);
    this._filtersEl.textContent = "";
    this._filtersEl.style.display = chips.length > 1 ? "" : "none";
    for (const chip of chips) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "rpc-fchip";
      btn.setAttribute("aria-pressed", String(chip.key === this._filter));
      const icon = document.createElement("ha-icon");
      icon.setAttribute("icon", chip.icon || "mdi:map-marker");
      btn.appendChild(icon);
      btn.appendChild(document.createTextNode(chip.label + " "));
      const count = document.createElement("small");
      _escapeText(count, String(chip.count));
      btn.appendChild(count);
      btn.title =
        chip.key === CHARGING_FILTER
          ? "Every place you've charged, home included, plus charging spots outside your destinations"
          : chip.key === ALL_FILTER
            ? "Every destination"
            : `Destinations in the ${chip.label} category`;
      btn.addEventListener("click", () => this._setFilter(chip.key));
      this._filtersEl.appendChild(btn);
    }
  }

  _setFilter(key) {
    if (key === this._filter) return;
    this._filter = key;
    _saveFilter(key);
    const place = this._selectedId !== null ? this._places.find((p) => p.id === this._selectedId) : null;
    const hidesPlace = place && !placeMatchesFilter(place, key, this._charge.byPlace);
    const hidesSpot = this._selectedSpot && key !== ALL_FILTER && key !== CHARGING_FILTER;
    if (hidesPlace || hidesSpot) {
      this._deselect();
      this._renderFilters();
      return;
    }
    this._renderFilters();
    this._renderList();
    this._renderMap();
  }

  _showError(err) {
    console.error("rivian-places-card", err);
    this._card.textContent = "";
    const el = document.createElement("div");
    el.className = "rpc-error";
    _escapeText(el, `rivian-places-card: ${err && err.message ? err.message : err}`);
    this._card.appendChild(el);
  }

  // -- list pane ------------------------------------------------------------

  _renderList() {
    this._listEl.textContent = "";
    const shown = this._places.filter((p) => placeMatchesFilter(p, this._filter, this._charge.byPlace));
    const groups = groupPlaces(shown);
    const charging = this._filter === CHARGING_FILTER;

    if (this._filter !== ALL_FILTER && !groups.named.length && !groups.suggestions.length && !(charging && this._charge.spots.length)) {
      const empty = document.createElement("div");
      empty.className = "rpc-empty";
      _escapeText(empty, charging ? "No charging recorded yet." : "No destinations in this category.");
      this._listEl.appendChild(empty);
    } else if (!groups.suggestions.length && !groups.named.length && !charging) {
      const empty = document.createElement("div");
      empty.className = "rpc-empty";
      _escapeText(
        empty,
        "No places yet. Places are detected automatically after a few visits, or you can name a parked spot from the Drives tab."
      );
      this._listEl.appendChild(empty);
    }

    if (groups.suggestions.length) {
      const title = document.createElement("div");
      title.className = "rpc-group-title";
      _escapeText(title, "Suggestions");
      title.title = "Frequently visited spots detected from your drives that you haven't named yet (dashed circles on the map)";
      this._listEl.appendChild(title);
      for (const place of groups.suggestions) this._listEl.appendChild(this._buildRow(place));
    }
    if (groups.named.length) {
      const title = document.createElement("div");
      title.className = "rpc-group-title";
      _escapeText(title, "Saved destinations");
      title.title = "Places you named, plus your Home Assistant zones";
      this._listEl.appendChild(title);
      for (const place of groups.named) this._listEl.appendChild(this._buildRow(place));
    }
    if (charging && this._charge.spots.length) {
      const title = document.createElement("div");
      title.className = "rpc-group-title";
      _escapeText(title, "Charging spots");
      title.title = "Places you charged that aren't one of your destinations, grouped within 100 m";
      this._listEl.appendChild(title);
      const spots = [...this._charge.spots].sort((a, b) => b.sessions.length - a.sessions.length);
      for (const spot of spots) this._listEl.appendChild(this._buildSpotRow(spot));
    }
    if (charging && this._charge.unlocated) {
      const note = document.createElement("div");
      note.className = "rpc-note";
      const n = this._charge.unlocated;
      _escapeText(note, `${n} charge${n === 1 ? " has" : "s have"} no location and ${n === 1 ? "isn't" : "aren't"} on the map.`);
      this._listEl.appendChild(note);
    }
    if (groups.hidden.length) {
      const toggle = document.createElement("div");
      toggle.className = "rpc-hidden-toggle";
      _escapeText(toggle, this._showHidden ? `▾ Hidden (${groups.hidden.length})` : `▸ Hidden (${groups.hidden.length})`);
      toggle.addEventListener("click", () => {
        this._showHidden = !this._showHidden;
        this._renderList();
      });
      toggle.tabIndex = 0;
      toggle.setAttribute("role", "button");
      toggle.setAttribute("aria-expanded", String(!!this._showHidden));
      toggle.title = "Hidden places label no drives; click to show or hide the list";
      toggle.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          toggle.click();
        }
      });
      this._listEl.appendChild(toggle);
      if (this._showHidden) {
        for (const place of groups.hidden) this._listEl.appendChild(this._buildRow(place));
      }
    }
  }

  _buildRow(place) {
    const row = document.createElement("div");
    row.className = "rpc-row" + (place.id === this._selectedId ? " selected" : "");
    const icon = document.createElement("ha-icon");
    icon.setAttribute("icon", categoryIcon(place.category, this._categories));
    row.appendChild(icon);

    const text = document.createElement("div");
    text.className = "rpc-row-text";
    const label = document.createElement("div");
    label.className = "rpc-row-label";
    _escapeText(label, place.label);
    const meta = document.createElement("div");
    meta.className = "rpc-row-meta";
    _escapeText(meta, `${formatVisits(place.visits)} · last ${formatLastVisit(place.last_visit_ts)}`);
    const charges = this._charge.byPlace.get(place.id);
    if (charges && charges.length) {
      const bolt = document.createElement("span");
      bolt.className = "rpc-row-bolt";
      bolt.title = `${charges.length} charge${charges.length === 1 ? "" : "s"} here`;
      _escapeText(bolt, `⚡ ${charges.length}`);
      meta.appendChild(bolt);
    }
    this._appendVehicleVisits(meta, place);
    text.appendChild(label);
    text.appendChild(meta);
    row.appendChild(text);

    if (place.source === "zone") {
      const chip = document.createElement("span");
      chip.className = "rpc-chip";
      _escapeText(chip, "zone");
      chip.title = "Backed by a Home Assistant zone; edit it in Settings > Areas & zones > Zones";
      row.appendChild(chip);
    }

    const toggle = () => (place.id === this._selectedId && !this._mergeMode ? this._deselect() : this._selectPlace(place.id));
    row.addEventListener("click", toggle);
    row.tabIndex = 0;
    row.setAttribute("role", "button");
    row.setAttribute("aria-pressed", String(place.id === this._selectedId));
    row.title = `${placeTooltip(place)}. Tap to show it on the map and see its stats; tap again to close them.`;
    row.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        toggle();
      }
    });
    return row;
  }

  /** A list row for a charging spot outside every destination. */
  _buildSpotRow(spot) {
    const row = document.createElement("div");
    const on = spot.key === this._selectedSpot;
    row.className = "rpc-row" + (on ? " selected" : "");
    const icon = document.createElement("ha-icon");
    icon.setAttribute("icon", "mdi:ev-station");
    row.appendChild(icon);
    const text = document.createElement("div");
    text.className = "rpc-row-text";
    const label = document.createElement("div");
    label.className = "rpc-row-label";
    _escapeText(label, spot.label);
    const meta = document.createElement("div");
    meta.className = "rpc-row-meta";
    const n = spot.sessions.length;
    _escapeText(meta, `⚡ ${n} charge${n === 1 ? "" : "s"}`);
    text.appendChild(label);
    text.appendChild(meta);
    row.appendChild(text);
    const toggle = () => (on ? this._deselect() : this._selectSpot(spot.key));
    row.addEventListener("click", toggle);
    row.tabIndex = 0;
    row.setAttribute("role", "button");
    row.setAttribute("aria-pressed", String(on));
    row.title = `${spot.label}: ${n} charge${n === 1 ? "" : "s"}. Tap to show it on the map and see its charging stats.`;
    row.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        toggle();
      }
    });
    return row;
  }

  _selectSpot(key, fromMap = false) {
    if (this._mergeMode) return;
    const spot = this._charge.spots.find((sp) => sp.key === key);
    if (!spot) return;
    this._selectedSpot = key;
    this._selectedId = null;
    this._stays = null;
    this._applyLayout();
    this._renderList();
    this._renderDetail();
    this._renderMap();
    if (this._map) {
      requestAnimationFrame(() => {
        this._map.invalidateSize();
        this._map.setView([spot.lat, spot.lon], Math.max(this._map.getZoom(), 15));
      });
    }
    this._renderStats(true);
    if (fromMap && this._stacked && this._statsEl.scrollIntoView) {
      this._statsEl.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }

  /** Close the stats: no place or spot selected, full-height layout back. */
  _deselect() {
    this._selectedId = null;
    this._selectedSpot = null;
    this._stays = null;
    this._statsToken += 1;
    this._applyLayout();
    this._renderList();
    this._renderDetail();
    this._renderMap();
    this._renderStats();
    if (this._map) requestAnimationFrame(() => this._map.invalidateSize());
  }

  /** With several vehicles selected, append each car's own visit count ("79 A · 12 B"). */
  _appendVehicleVisits(el, place) {
    if (this._vins.length < 2) return;
    const parts = visitsByVehicle(place, this._vehicleList, this._vins);
    if (!parts.length) return;
    for (const part of parts) {
      const chip = document.createElement("span");
      chip.className = "rpc-vb";
      chip.style.setProperty("--vb-c", part.color);
      chip.title = `${part.count} visit${part.count === 1 ? "" : "s"}${part.letter ? ` by ${part.letter}` : ""}`;
      _escapeText(chip, `${part.count.toLocaleString()}${part.letter ? ` ${part.letter}` : ""}`);
      el.appendChild(chip);
    }
  }

  _datasetOf(placeId) {
    const place = this._places.find((p) => p.id === placeId);
    return (place && place.dataset) || "real";
  }

  _selectPlace(id, fromMap = false) {
    if (this._mergeMode) {
      const into = this._places.find((p) => p.id === this._mergeMode.intoId);
      const other = this._places.find((p) => p.id === id);
      if (into && other && !sameDataset(into, other)) return;
      if (this._mergeMode.intoId !== id) {
        this._doMerge(this._mergeMode.intoId, id).catch((err) => this._showError(err));
      }
      return;
    }
    this._selectedId = id;
    this._selectedSpot = null;
    this._applyLayout();
    this._renderList();
    this._renderDetail();
    this._renderMap();
    if (this._map) this._map.invalidateSize();
    this._zoomToPlace(id);
    this._loadStats(true);
    // Phone width: the edit panel sits below the list, out of view of a tap
    // on the map, so bring it up.
    if (fromMap && this._stacked && this._detailEl.scrollIntoView) {
      this._detailEl.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }

  /** Frame one place's circle (with room around it), never closer than street level. */
  _zoomToPlace(id) {
    const circle = this._circles.get(id);
    if (!this._map || !circle) return;
    this._map.fitBounds(circle.getBounds(), { padding: [60, 60], maxZoom: 17 });
  }

  // -- detail/edit panel ------------------------------------------------------

  _renderDetail() {
    this._detailEl.textContent = "";
    const place = this._places.find((p) => p.id === this._selectedId);
    if (!place) {
      if (this._mergeMode) {
        const hint = document.createElement("div");
        hint.className = "rpc-merge-hint";
        _escapeText(hint, "Pick a place in the list or on the map to merge into it.");
        this._detailEl.appendChild(hint);
      }
      return;
    }

    const title = document.createElement("div");
    title.className = "rpc-detail-title";
    _escapeText(title, place.label);
    this._detailEl.appendChild(title);

    const sub = document.createElement("div");
    sub.className = "rpc-detail-sub";
    let subText = sourceDescription(place.source);
    if (place.geocode_name && place.geocode_name !== place.label) {
      subText += ` · ${place.geocode_name}`;
    }
    _escapeText(sub, subText);
    this._detailEl.appendChild(sub);

    if (!this._isAdmin) {
      const meta = document.createElement("div");
      meta.className = "rpc-detail-sub";
      _escapeText(
        meta,
        `${formatVisits(place.visits)} · last ${formatLastVisit(place.last_visit_ts)} · ${Math.round(place.radius_m)} m radius`
      );
      this._appendVehicleVisits(meta, place);
      this._detailEl.appendChild(meta);
      return;
    }

    this._buildEditForm(place);
  }

  _buildEditForm(place) {
    const nameField = document.createElement("div");
    nameField.className = "rpc-field";
    const nameLabel = document.createElement("label");
    _escapeText(nameLabel, "Name");
    const nameInput = document.createElement("input");
    nameInput.type = "text";
    nameInput.value = place.name || "";
    nameInput.setAttribute("aria-label", "Place name");
    nameInput.title = "Place name (leave empty to keep it an unnamed suggestion)";
    nameField.appendChild(nameLabel);
    nameField.appendChild(nameInput);
    this._detailEl.appendChild(nameField);

    const catField = document.createElement("div");
    catField.className = "rpc-field";
    const catLabel = document.createElement("label");
    _escapeText(catLabel, "Category");
    const catSelect = document.createElement("select");
    catSelect.setAttribute("aria-label", "Place category");
    catSelect.title = "Category: sets the icon and color of the place";
    const blank = document.createElement("option");
    blank.value = "";
    _escapeText(blank, "–");
    catSelect.appendChild(blank);
    for (const category of this._categories) {
      const opt = document.createElement("option");
      opt.value = category.key;
      _escapeText(opt, category.label);
      catSelect.appendChild(opt);
    }
    catSelect.value = place.category || "";
    catField.appendChild(catLabel);
    catField.appendChild(catSelect);
    this._detailEl.appendChild(catField);

    const isZone = place.source === "zone";
    const radiusField = document.createElement("div");
    radiusField.className = "rpc-field";
    const radiusLabel = document.createElement("label");
    _escapeText(radiusLabel, "Radius");
    const radiusInput = document.createElement("input");
    radiusInput.type = "range";
    radiusInput.min = String(MIN_RADIUS_M);
    radiusInput.max = String(MAX_RADIUS_M);
    radiusInput.step = "5";
    radiusInput.value = String(Math.round(place.radius_m));
    radiusInput.disabled = isZone;
    radiusInput.setAttribute("aria-label", "Place radius in meters");
    radiusInput.title = isZone
      ? "Radius comes from the Home Assistant zone"
      : "How close a drive start or stop must be to count as this place (saved on release)";
    radiusField.appendChild(radiusLabel);
    radiusField.appendChild(radiusInput);
    const radiusValue = document.createElement("span");
    radiusValue.className = "rpc-radius-value";
    _escapeText(radiusValue, `${Math.round(place.radius_m)} m`);
    radiusField.appendChild(radiusValue);
    this._detailEl.appendChild(radiusField);
    radiusInput.addEventListener("input", () => {
      _escapeText(radiusValue, `${radiusInput.value} m`);
      this._previewRadius(place.id, Number(radiusInput.value));
    });
    radiusInput.addEventListener("change", () => {
      this._updatePlace(place.id, { radius_m: Number(radiusInput.value) }).catch((err) =>
        this._showError(err)
      );
    });

    if (isZone) {
      const note = document.createElement("div");
      note.className = "rpc-status";
      _escapeText(note, "Its location and radius come from its Home Assistant zone. ");
      const link = document.createElement("a");
      link.href = "/config/zone";
      link.className = "rpc-link";
      _escapeText(link, "Edit it in Settings → Areas & zones → Zones");
      link.addEventListener("click", (ev) => {
        // Navigate inside the HA frontend (no full page reload).
        ev.preventDefault();
        window.history.pushState(null, "", "/config/zone");
        window.dispatchEvent(new CustomEvent("location-changed", { detail: { replace: false } }));
      });
      note.appendChild(link);
      this._detailEl.appendChild(note);
    } else {
      const hint = document.createElement("div");
      hint.className = "rpc-status";
      _escapeText(hint, "Drag the centre dot on the map to move it, or the edge handle to resize.");
      this._detailEl.appendChild(hint);
    }

    const statusEl = document.createElement("div");
    statusEl.className = "rpc-status";
    this._detailEl.appendChild(statusEl);

    const actions = document.createElement("div");
    actions.className = "rpc-actions";

    const saveBtn = document.createElement("button");
    saveBtn.type = "button";
    saveBtn.className = "rpc-primary";
    _escapeText(saveBtn, "Save");
    saveBtn.title = "Save the name and category";
    saveBtn.addEventListener("click", () => {
      this._updatePlace(place.id, {
        name: nameInput.value.trim() || null,
        category: catSelect.value || null,
      }).catch((err) => this._showError(err));
    });
    actions.appendChild(saveBtn);

    const hideBtn = document.createElement("button");
    hideBtn.type = "button";
    _escapeText(hideBtn, place.hidden ? "Unhide" : "Hide");
    hideBtn.title = place.hidden
      ? "Show this place again; its drives will be labeled with it"
      : "Hide this place: it labels no drives and disappears from the map (restore it from Hidden)";
    hideBtn.addEventListener("click", () => {
      this._updatePlace(place.id, { hidden: !place.hidden }).catch((err) => this._showError(err));
    });
    actions.appendChild(hideBtn);

    if (!isZone) {
      const mergeBtn = document.createElement("button");
      mergeBtn.type = "button";
      _escapeText(mergeBtn, "Merge into…");
      mergeBtn.title = "Move this place's drives into another place, then remove this one";
      mergeBtn.addEventListener("click", () => {
        this._mergeMode = { intoId: null, fromId: place.id };
        _escapeText(statusEl, "Click another place to merge this one into it.");
        this._armMergePick(place.id);
      });
      actions.appendChild(mergeBtn);
    }

    if (!isZone) {
      const zoneBtn = document.createElement("button");
      zoneBtn.type = "button";
      _escapeText(zoneBtn, "Create HA zone");
      zoneBtn.title = "Create a Home Assistant zone here, so automations can use it too";
      zoneBtn.addEventListener("click", () => {
        this._createZone(place, statusEl).catch((err) => this._showError(err));
      });
      actions.appendChild(zoneBtn);
    }

    if (!isZone) {
      const deleteBtn = document.createElement("button");
      deleteBtn.type = "button";
      deleteBtn.className = "rpc-danger";
      _escapeText(deleteBtn, "Delete place");
      deleteBtn.title = place.source === "auto"
        ? "Remove this suggestion (it can be restored from Hidden)"
        : "Delete this place (asks for confirmation)";
      deleteBtn.addEventListener("click", () => {
        if (!window.confirm(deletePlaceMessage(place))) return;
        this._deletePlace(place.id).catch((err) => this._showError(err));
      });
      actions.appendChild(deleteBtn);
    }

    this._detailEl.appendChild(actions);
  }

  async _deletePlace(placeId) {
    await this._hass.callWS({
      type: "rivian/places/delete",
      dataset: this._datasetOf(placeId),
      place_id: placeId,
    });
    if (this._selectedId === placeId) this._selectedId = null;
    await this._refresh();
  }

  /** "Merge into..." picks the *target* from the clicked place; this arms that pick mode. */
  _armMergePick(fromId) {
    this._mergeMode = { intoId: fromId, pickMode: true };
  }

  async _doMerge(intoId, otherId) {
    const into = this._places.find((p) => p.id === intoId);
    const other = this._places.find((p) => p.id === otherId);
    const ok = window.confirm(
      `Merge "${other ? other.label : otherId}" into "${into ? into.label : intoId}"? Its visits will count toward the target; this can't be undone.`
    );
    if (!ok) {
      this._mergeMode = null;
      this._renderDetail();
      return;
    }
    await this._hass.callWS({
      type: "rivian/places/merge",
      dataset: this._datasetOf(intoId),
      into: intoId,
      place_ids: [otherId],
    });
    this._mergeMode = null;
    this._selectedId = intoId;
    await this._refresh();
  }

  async _updatePlace(placeId, fields) {
    await this._hass.callWS({
      type: "rivian/places/update",
      dataset: this._datasetOf(placeId),
      place_id: placeId,
      ...fields,
    });
    await this._refresh();
  }

  async _createZone(place, statusEl) {
    _escapeText(statusEl, "Creating zone…");
    try {
      await this._hass.callWS({
        type: "zone/create",
        name: place.name || place.label,
        latitude: place.lat,
        longitude: place.lon,
        radius: place.radius_m,
        passive: false,
      });
    } catch (err) {
      _escapeText(statusEl, `Couldn't create the zone: ${err && err.message ? err.message : err}`);
      return;
    }
    // Zone resync is debounced ~10s server-side; poll briefly for the new
    // zone place, then merge the old place into it so there's only one.
    const targetName = place.name || place.label;
    const deadline = Date.now() + 15000;
    while (Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 1500));
      const result = await this._hass.callWS({
        type: "rivian/places/list",
        dataset: place.dataset || "real",
      });
      const zonePlace = (result.places || []).find(
        (p) => p.source === "zone" && p.name === targetName && p.id !== place.id
      );
      if (zonePlace) {
        _escapeText(statusEl, "Linking to the new zone…");
        await this._hass.callWS({
          type: "rivian/places/merge",
          dataset: place.dataset || "real",
          into: zonePlace.id,
          place_ids: [place.id],
        });
        this._selectedId = zonePlace.id;
        await this._refresh();
        return;
      }
    }
    _escapeText(statusEl, "Zone created, but it hasn't linked back yet. Try refreshing shortly.");
    await this._refresh();
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

  _renderMap() {
    if (!this._map) return;
    const L = this._leaflet;
    for (const circle of this._circles.values()) this._map.removeLayer(circle);
    this._circles.clear();
    for (const dot of (this._dots || new Map()).values()) this._map.removeLayer(dot);
    this._dots = new Map();

    for (const marker of this._bolts.values()) this._map.removeLayer(marker);
    this._bolts = new Map();

    const visible = this._places.filter((p) => !p.hidden && placeMatchesFilter(p, this._filter, this._charge.byPlace));
    const bounds = [];
    for (const place of visible) {
      const isSuggestion = !place.name && place.source !== "zone";
      const color = categoryColor(place.category);
      const circle = L.circle([place.lat, place.lon], {
        radius: place.radius_m,
        color,
        weight: place.id === this._selectedId ? 3 : 2,
        fillColor: color,
        fillOpacity: place.id === this._selectedId ? 0.35 : 0.18,
        dashArray: isSuggestion ? "4 4" : null,
      }).addTo(this._map);
      circle.bindTooltip(placeTooltip(place), { sticky: true });
      circle.on("click", () => this._selectPlace(place.id, true));
      this._circles.set(place.id, circle);
      // A fixed-size dot at the centre, so a place stays visible (and
      // tappable) when zoomed out far enough that its radius is sub-pixel.
      const dot = L.circleMarker([place.lat, place.lon], {
        radius: place.id === this._selectedId ? 7 : 5,
        color: "#ffffff",
        weight: 1.5,
        fillColor: color,
        fillOpacity: 1,
      }).addTo(this._map);
      dot.bindTooltip(placeTooltip(place));
      dot.on("click", () => this._selectPlace(place.id, true));
      this._dots.set(place.id, dot);
      bounds.push([place.lat, place.lon]);
    }
    // Charging: a bolt for every charging spot, and a small bolt badge on
    // each destination with charges.
    if (this._filter === ALL_FILTER || this._filter === CHARGING_FILTER) {
      for (const spot of this._charge.spots) {
        const n = spot.sessions.length;
        const on = spot.key === this._selectedSpot;
        const marker = L.marker([spot.lat, spot.lon], {
          icon: L.divIcon({ className: "rpc-bolt" + (on ? " selected" : ""), html: "⚡", iconSize: on ? [28, 28] : [22, 22] }),
          keyboard: false,
          zIndexOffset: on ? 1000 : 500,
        }).addTo(this._map);
        marker.bindTooltip(`${spot.label} · ${n} charge${n === 1 ? "" : "s"}`);
        marker.on("click", () => this._selectSpot(spot.key, true));
        this._bolts.set(spot.key, marker);
        bounds.push([spot.lat, spot.lon]);
      }
      for (const place of visible) {
        const charges = this._charge.byPlace.get(place.id);
        if (!charges || !charges.length) continue;
        const n = charges.length;
        const badge = L.marker([place.lat, place.lon], {
          icon: L.divIcon({ className: "rpc-bolt rpc-bolt-badge", html: "⚡", iconSize: [16, 16], iconAnchor: [-3, 19] }),
          keyboard: false,
          zIndexOffset: 400,
        }).addTo(this._map);
        badge.bindTooltip(`${place.label} · ${n} charge${n === 1 ? "" : "s"}`);
        badge.on("click", () => this._selectPlace(place.id, true));
        this._bolts.set(`place:${place.id}`, badge);
      }
    }
    this._renderEditHandles();
    if (bounds.length && !this._fitted) {
      this._fitted = true;
      const region = homeRegionPlaces(visible).map((p) => [p.lat, p.lon]);
      const pts = region.length ? region : bounds;
      if (pts.length === 1) this._map.setView(pts[0], 15);
      else this._map.fitBounds(pts, { padding: [40, 40], maxZoom: 16 });
    }
  }

  // -- stats section ------------------------------------------------------------

  /** Fetch the selected place's stays, then draw the stats (`scroll`: bring them into view). */
  async _loadStats(scroll) {
    const place = this._places.find((p) => p.id === this._selectedId);
    if (!place) return this._renderStats();
    const token = ++this._statsToken;
    const reason = staysUnavailableReason(place);
    if (reason) {
      this._stays = { note: reason };
      this._renderStats(scroll);
      return;
    }
    if (scroll || !Array.isArray(this._stays)) {
      this._stays = "loading";
      this._renderStats(scroll);
    }
    const dataset = place.dataset || "real";
    const group = datasetGroups(this._vehicleList, this._vins).find((g) => g.dataset === dataset);
    const vins = group ? group.vins : [...this._vins];
    try {
      const reply = await this._hass.callWS({
        type: "rivian/places/stays",
        dataset,
        ...(vins.length ? { vins } : {}),
        place_id: place.id,
      });
      if (token !== this._statsToken) return;
      this._stays = (reply && reply.stays) || [];
    } catch (err) {
      if (token !== this._statsToken) return;
      this._stays = { error: (err && (err.message || err.code)) || String(err) };
    }
    this._renderStats(false);
  }

  _vehicleColor(vin) {
    const v = this._vehicleList.find((x) => x.vin === vin);
    const dark = !!(this._hass && this._hass.themes && this._hass.themes.darkMode);
    return (v && ((dark && v.color_dark) || v.color)) || "#2a7de1";
  }

  _vehicleTag(vin) {
    const v = this._vehicleList.find((x) => x.vin === vin);
    return (v && (v.letter || v.name)) || "";
  }

  /** "A · R1T" for the legend. */
  _vehicleName(vin) {
    const v = this._vehicleList.find((x) => x.vin === vin);
    if (!v) return vin;
    return [v.letter, v.name || v.model].filter(Boolean).join(" · ");
  }

  _histWidth() {
    const w = (this._statsEl && this._statsEl.clientWidth) || (this.getBoundingClientRect ? this.getBoundingClientRect().width : 0) || 600;
    const cols = this._stacked ? 1 : 3;
    return Math.max(220, Math.floor((w - 28 - (cols - 1) * 16) / cols));
  }

  _renderStats(scroll = false) {
    const el = this._statsEl;
    if (!el) return;
    const place = this._selectedId !== null ? this._places.find((p) => p.id === this._selectedId) : null;
    const spot = !place && this._selectedSpot ? this._charge.spots.find((sp) => sp.key === this._selectedSpot) : null;
    if (!place && !spot) {
      el.style.display = "none";
      el.innerHTML = "";
      return;
    }
    el.style.display = "";
    const W = this._histWidth();
    let html =
      `<div class="rpc-stats-head"><div class="rpc-stats-title">${_esc(place ? place.label : spot.label)}</div>` +
      `<button type="button" class="rpc-stats-close" data-act="close" title="Close the stats (Esc)" aria-label="Close the stats">×</button></div>`;
    if (place) html += this._visitStatsHtml(W);
    const charges = place ? this._charge.byPlace.get(place.id) || [] : spot.sessions;
    if (charges.length) html += this._chargeStatsHtml(charges, W, !!place);
    if (this._vins.length > 1) {
      html +=
        '<div class="rpc-legend">' +
        this._vins.map((vin) => `<span><i style="--sw:${_esc(this._vehicleColor(vin))}"></i>${_esc(this._vehicleName(vin))}</span>`).join("") +
        "</div>";
    }
    el.innerHTML = html + '<div class="rpc-tip" role="tooltip"></div>';
    if (scroll && el.getBoundingClientRect && typeof window !== "undefined") {
      const top = el.getBoundingClientRect().top;
      if (top > window.innerHeight - 80) el.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }

  _tilesHtml(tiles) {
    return (
      '<div class="rpc-tiles">' +
      tiles.map(([value, label, title]) => `<div class="rpc-tile" title="${_esc(title || label)}"><b>${_esc(value)}</b><span>${_esc(label)}</span></div>`).join("") +
      "</div>"
    );
  }

  _visitStatsHtml(W) {
    const tz = this._hass && this._hass.config ? this._hass.config.time_zone : undefined;
    let html = '<div class="rpc-stats-sec"><div class="rpc-stats-h">Visits</div>';
    const stays = this._stays;
    if (stays === "loading" || stays === null) return html + '<div class="rpc-stats-note">Loading visits…</div></div>';
    if (!Array.isArray(stays)) {
      const text = stays.note || `Couldn't load visits: ${stays.error}`;
      return html + `<div class="rpc-stats-note">${_esc(text)}</div></div>`;
    }
    const st = stayStats(stays, tz);
    if (!st.visits) return html + '<div class="rpc-stats-note">No visits in the saved drive history yet.</div></div>';
    html += this._tilesHtml([
      [st.visits.toLocaleString(), "visits", "Arrivals recorded in the saved drive history"],
      [st.avgStayS === null ? "–" : formatStay(st.avgStayS), "average stay", "From arriving to the next drive leaving here"],
      [st.typicalArrive === null ? "–" : formatClock(st.typicalArrive), "typical arrival", "Average arrival time of day"],
      [st.typicalDepart === null ? "–" : formatClock(st.typicalDepart), "typical departure", "Average departure time of day"],
    ]);
    if (st.lengths.length < st.visits) {
      html += `<div class="rpc-stats-note">${st.lengths.length} of ${st.visits} visits have both an arrival and a departure; only those count toward the stay length.</div>`;
    }
    html += '<div class="rpc-hists">';
    if (st.lengths.length) {
      // Stays past the upper fence (a week away) share one last "N+" bin.
      const spec = stayHistogramSpec(st.lengths);
      const minutes = spec.unit === 60;
      const u = minutes ? "min" : "h";
      const bins = stackBins(spec.values, st.vins.length, spec.index, spec.n);
      const med = _median(st.lengths);
      const fmt = (v) => `${+v.toFixed(1)}`;
      const base = spec.overflowFrom === null ? spec.n : spec.n - 1;
      let ticks = _edgeTicks(base, spec.step, W, fmt);
      if (spec.overflowFrom !== null) {
        // The overflow bin's "N+" label replaces the plain edge label beside it.
        ticks = ticks.filter((t) => Math.abs(t.v - spec.overflowFrom) > 1e-9);
        ticks.push({ v: (base + 0.5) * spec.step, label: `${fmt(spec.overflowFrom)}+` });
      }
      html += this._histHtml({
        id: "stay",
        title: "Length of stay",
        bins,
        step: spec.step,
        axis: minutes ? "Minutes" : "Hours",
        ticks,
        line: { v: med / spec.unit, label: `median ${formatStay(med)}` },
        unit: "visit",
        binLabel: (i) =>
          spec.overflowFrom !== null && i === base ? `${fmt(spec.overflowFrom)} ${u} or more` : `${fmt(i * spec.step)}–${fmt((i + 1) * spec.step)} ${u}`,
        W,
      });
    } else {
      html += '<div><div class="rpc-hist-title">Length of stay</div><div class="rpc-stats-note">No completed stays yet.</div></div>';
    }
    const clockTicks = [0, 6, 12, 18, 24].map((v) => ({ v, label: ["12a", "6a", "12p", "6p", "12a"][v / 6] }));
    const hourLabel = (i) => `${formatClock(i).replace(":00", "")}–${formatClock((i + 1) % 24).replace(":00", "")}`;
    for (const [id, title, hours, vins, typical] of [
      ["arrive", "Arrival time", st.arriveHours, st.vins.arrive, st.typicalArrive],
      ["depart", "Departure time", st.departHours, st.vins.depart, st.typicalDepart],
    ]) {
      if (!hours.length) {
        html += `<div><div class="rpc-hist-title">${title}</div><div class="rpc-stats-note">None recorded yet.</div></div>`;
        continue;
      }
      html += this._histHtml({
        id,
        title,
        bins: stackBins(hours, vins, Math.floor, 24),
        step: 1,
        axis: "Time of day",
        ticks: clockTicks,
        line: typical === null ? null : { v: typical, label: `typical ${formatClock(typical)}` },
        unit: "visit",
        binLabel: hourLabel,
        W,
      });
    }
    return html + "</div></div>";
  }

  _chargeStatsHtml(sessions, W, underPlace) {
    const st = chargingStats(sessions);
    let html = `<div class="rpc-stats-sec">${underPlace ? '<div class="rpc-stats-h">⚡ Charging here</div>' : ""}`;
    const types = [
      ["ac_l2", "AC L2"],
      ["ac_l1", "AC L1"],
      ["dc", "DC Fast"],
      ["ac", "AC"],
    ]
      .filter(([k]) => st.byType[k])
      .map(([k, label]) => `${st.byType[k]} ${label}`)
      .join(" · ");
    html += this._tilesHtml([
      [st.count.toLocaleString(), "charges", "Recorded charging sessions here (inferred charges aren't counted)"],
      [`${Math.round(st.totalKwh).toLocaleString()} kWh`, st.avgKwh === null ? "added" : `added · ${st.avgKwh.toFixed(1)} per charge`, "Energy added in total"],
      [st.avgSocAdded === null ? "–" : `+${Math.round(st.avgSocAdded)} %`, "average battery added", "Average battery percentage added per charge"],
      [types || "–", "by type", "Charges per charging type"],
    ]);
    html += '<div class="rpc-hists">';
    if (st.rates.length) {
      const h = histogram(st.rates, 8);
      const fmt = (v) => `${+v.toFixed(1)}`;
      const med = _median(st.rates);
      html += this._histHtml({
        id: "rate",
        title: "Charging rate",
        bins: stackBins(st.rates, st.rateVins, (v) => Math.floor(v / h.step + 1e-9), h.bins.length),
        step: h.step,
        axis: "Average kW per charge",
        ticks: _edgeTicks(h.bins.length, h.step, W, fmt),
        line: { v: med, label: `median ${fmt(med)} kW` },
        unit: "charge",
        binLabel: (i) => `${fmt(i * h.step)}–${fmt((i + 1) * h.step)} kW`,
        W,
      });
    } else {
      html += '<div><div class="rpc-hist-title">Charging rate</div><div class="rpc-stats-note">No charging rate recorded here.</div></div>';
    }
    html += "</div>";
    if (st.rates.length && st.noRate) {
      html += `<div class="rpc-stats-note">${st.noRate} charge${st.noRate === 1 ? " has" : "s have"} no recorded rate and ${st.noRate === 1 ? "isn't" : "aren't"} in the rate histogram.</div>`;
    }
    return html + "</div>";
  }

  /**
   * One histogram as an inline SVG: bars 2 px apart with rounded tops, split
   * by vehicle color when several are selected, a dashed median/typical line
   * and a hit area per bin carrying its tooltip text.
   */
  _histHtml(o) {
    const H = 150;
    const g = { x0: 28, x1: o.W - 8, y0: 24, y1: H - 30 };
    g.w = g.x1 - g.x0;
    g.h = g.y1 - g.y0;
    const n = o.bins.length;
    const xs = (v) => g.x0 + (v / (n * o.step)) * g.w;
    const max = Math.max(1, ...o.bins.map((b) => b.count));
    const yStep = Math.max(1, Math.ceil(_niceStep(max, 3)));
    const yTop = Math.ceil(max / yStep) * yStep;
    const ys = (c) => g.y1 - (c / yTop) * g.h;
    const binW = g.w / n;
    const parts = [];
    for (let v = 0; v <= yTop; v += yStep) {
      parts.push(`<line class="rpc-grid" x1="${g.x0}" x2="${g.x1}" y1="${ys(v).toFixed(1)}" y2="${ys(v).toFixed(1)}"/>`);
      parts.push(`<text x="${g.x0 - 4}" y="${(ys(v) + 3.5).toFixed(1)}" text-anchor="end">${v}</text>`);
    }
    for (const t of o.ticks) {
      parts.push(`<text x="${xs(t.v).toFixed(1)}" y="${g.y1 + 13}" text-anchor="middle">${_esc(t.label)}</text>`);
    }
    parts.push(`<text x="${(g.x0 + g.x1) / 2}" y="${g.y1 + 26}" text-anchor="middle">${_esc(o.axis)}</text>`);
    const order = this._vins.length ? this._vins : Object.keys(o.bins[0] ? o.bins[0].byVin : {});
    o.bins.forEach((b, i) => {
      if (!b.count) return;
      const x = g.x0 + i * binW + (binW > 4 ? 1 : 0);
      const w = Math.max(1, binW - (binW > 4 ? 2 : 0));
      const segs = order.filter((vin) => b.byVin[vin]).map((vin) => ({ vin, c: b.byVin[vin] }));
      for (const vin of Object.keys(b.byVin)) if (!order.includes(vin)) segs.push({ vin, c: b.byVin[vin] });
      let acc = 0;
      segs.forEach((sg, k) => {
        const yBot = ys(acc) - (k ? 1 : 0);
        acc += sg.c;
        const yTopSeg = ys(acc);
        const h = Math.max(0.5, yBot - yTopSeg);
        const color = _esc(this._vehicleColor(sg.vin));
        if (k === segs.length - 1) {
          const r = Math.min(3, w / 2, h);
          const y = yBot - h;
          parts.push(
            `<path d="M${x.toFixed(1)},${yBot.toFixed(1)}V${(y + r).toFixed(1)}Q${x.toFixed(1)},${y.toFixed(1)} ${(x + r).toFixed(1)},${y.toFixed(1)}H${(x + w - r).toFixed(1)}Q${(x + w).toFixed(1)},${y.toFixed(1)} ${(x + w).toFixed(1)},${(y + r).toFixed(1)}V${yBot.toFixed(1)}Z" fill="${color}"/>`
          );
        } else {
          parts.push(`<rect x="${x.toFixed(1)}" y="${(yBot - h).toFixed(1)}" width="${w.toFixed(1)}" height="${h.toFixed(1)}" fill="${color}"/>`);
        }
      });
    });
    parts.push(`<line class="rpc-axis" x1="${g.x0}" x2="${g.x1}" y1="${g.y1}" y2="${g.y1}"/>`);
    if (o.line && Number.isFinite(o.line.v)) {
      const mx = xs(Math.min(n * o.step, Math.max(0, o.line.v)));
      const fits = mx + 4 + o.line.label.length * 5.4 <= o.W;
      parts.push(`<line class="rpc-mid" x1="${mx.toFixed(1)}" x2="${mx.toFixed(1)}" y1="${g.y0 - 8}" y2="${g.y1}"/>`);
      parts.push(`<text class="rpc-midlabel" x="${(fits ? mx + 4 : mx - 4).toFixed(1)}" y="${g.y0 - 10}" text-anchor="${fits ? "start" : "end"}">${_esc(o.line.label)}</text>`);
    }
    // Hit areas last so they sit on top: the whole column is the target.
    o.bins.forEach((b, i) => {
      let tip = `${o.binLabel(i)}: ${b.count} ${o.unit}${b.count === 1 ? "" : "s"}`;
      if (this._vins.length > 1 && b.count) {
        tip += ` (${Object.entries(b.byVin)
          .map(([vin, c]) => `${this._vehicleTag(vin)} ${c}`)
          .join(", ")})`;
      }
      parts.push(
        `<rect class="rpc-hit" data-i="${i}" data-n="${b.count}" data-tip="${_esc(tip)}" x="${(g.x0 + i * binW).toFixed(1)}" y="${g.y0}" width="${binW.toFixed(1)}" height="${g.h}"/>`
      );
    });
    return (
      `<div><div class="rpc-hist-title">${_esc(o.title)}</div>` +
      `<div class="rpc-hist" data-hist="${o.id}" tabindex="0" role="group" aria-label="${_esc(o.title)} histogram: hover, tap, or use the arrow keys to read each bar">` +
      `<svg viewBox="0 0 ${o.W} ${H}" role="img" aria-label="${_esc(o.title)}">${parts.join("")}</svg></div></div>`
    );
  }

  _showStatsTip(text, clientX, clientY) {
    const tip = this._statsEl.querySelector(".rpc-tip");
    if (!tip) return;
    tip.textContent = text;
    tip.style.display = "block";
    const box = this._statsEl.getBoundingClientRect();
    let left = clientX - box.left + 12;
    if (left + tip.offsetWidth > box.width) left = Math.max(0, clientX - box.left - tip.offsetWidth - 12);
    tip.style.left = `${left}px`;
    tip.style.top = `${Math.max(0, clientY - box.top - tip.offsetHeight - 10)}px`;
  }

  _hideStatsTip() {
    const tip = this._statsEl && this._statsEl.querySelector(".rpc-tip");
    if (tip) tip.style.display = "none";
    for (const el of this._statsEl ? this._statsEl.querySelectorAll(".rpc-hit.on") : []) el.classList.remove("on");
  }

  _onStatsPointer(ev) {
    const hit = ev.target && ev.target.closest ? ev.target.closest(".rpc-hit") : null;
    for (const el of this._statsEl.querySelectorAll(".rpc-hit.on")) if (el !== hit) el.classList.remove("on");
    if (!hit) {
      if (ev.pointerType !== "touch") this._hideStatsTip();
      return;
    }
    hit.classList.add("on");
    this._showStatsTip(hit.dataset.tip, ev.clientX, ev.clientY);
  }

  _onStatsKey(ev) {
    const hist = ev.target && ev.target.classList && ev.target.classList.contains("rpc-hist") ? ev.target : null;
    if (!hist || (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight")) return;
    ev.preventDefault();
    const hits = [...hist.querySelectorAll(".rpc-hit")].filter((h) => Number(h.dataset.n) > 0);
    if (!hits.length) return;
    const cur = hits.findIndex((h) => h.classList.contains("on"));
    const next = cur < 0 ? (ev.key === "ArrowRight" ? 0 : hits.length - 1) : Math.min(hits.length - 1, Math.max(0, cur + (ev.key === "ArrowRight" ? 1 : -1)));
    for (const el of this._statsEl.querySelectorAll(".rpc-hit.on")) el.classList.remove("on");
    const hit = hits[next];
    hit.classList.add("on");
    const r = hit.getBoundingClientRect();
    this._showStatsTip(hit.dataset.tip, r.left + r.width / 2, r.top + r.height / 3);
  }

  _previewRadius(placeId, radius) {
    const circle = this._circles.get(placeId);
    if (circle) circle.setRadius(radius);
    if (placeId === this._selectedId && this._radiusHandle && circle) {
      const c = circle.getLatLng();
      this._radiusHandle.setLatLng(radiusHandleLatLng(c.lat, c.lng, radius));
    }
  }

  /**
   * Admins editing a non-zone place get two draggable handles: the centre
   * (moves the place) and a dot on the circle's east edge (resizes it). Each
   * saves once on release; while dragging, the circle follows live.
   */
  _renderEditHandles() {
    for (const h of [this._centerHandle, this._radiusHandle]) if (h) this._map.removeLayer(h);
    this._centerHandle = null;
    this._radiusHandle = null;
    const place = this._places.find((p) => p.id === this._selectedId);
    if (!place || place.hidden || !this._isAdmin || place.source === "zone" || this._mergeMode) return;
    const L = this._leaflet;
    const circle = this._circles.get(place.id);
    const dot = this._dots && this._dots.get(place.id);
    if (!circle) return;
    const handleIcon = (cls) =>
      L.divIcon({ className: `rpc-handle ${cls}`, iconSize: [18, 18], iconAnchor: [9, 9] });

    const center = L.marker([place.lat, place.lon], {
      draggable: true,
      icon: handleIcon("rpc-handle-center"),
      zIndexOffset: 1000,
      title: "Drag to move",
      alt: "Drag to move this place",
    }).addTo(this._map);
    const edge = L.marker(radiusHandleLatLng(place.lat, place.lon, place.radius_m), {
      draggable: true,
      icon: handleIcon("rpc-handle-edge"),
      zIndexOffset: 1000,
      title: "Drag to resize",
      alt: "Drag to resize this place",
    }).addTo(this._map);
    this._centerHandle = center;
    this._radiusHandle = edge;

    center.on("drag", () => {
      const c = center.getLatLng();
      circle.setLatLng(c);
      if (dot) dot.setLatLng(c);
      edge.setLatLng(radiusHandleLatLng(c.lat, c.lng, circle.getRadius()));
    });
    center.on("dragend", () => {
      const c = center.getLatLng();
      this._updatePlace(place.id, { lat: c.lat, lon: c.lng }).catch((err) => this._showError(err));
    });
    edge.on("drag", () => {
      const radius = clampRadius(this._map.distance(circle.getLatLng(), edge.getLatLng()));
      circle.setRadius(radius);
      const label = this._detailEl.querySelector(".rpc-radius-value");
      const slider = this._detailEl.querySelector('input[type="range"]');
      if (label) _escapeText(label, `${radius} m`);
      if (slider) slider.value = String(radius);
    });
    edge.on("dragend", () => {
      const radius = clampRadius(this._map.distance(circle.getLatLng(), edge.getLatLng()));
      this._updatePlace(place.id, { radius_m: radius }).catch((err) => this._showError(err));
    });
  }
}

if (typeof customElements !== "undefined" && !customElements.get("rivian-places-card")) {
  customElements.define("rivian-places-card", RivianPlacesCard);
}
