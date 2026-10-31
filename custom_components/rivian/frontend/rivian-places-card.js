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
 * Backend calls (all via `hass.callWS`; places take a `dataset`, 'real' by
 * default -- every dataset represented in the selection is queried and the
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
 * vehicles' places never mix with other datasets', so each dataset
 * represented in the selection is queried separately.
 */
export function datasetGroups(vehicles, vins) {
  const list = Array.isArray(vehicles) ? vehicles : [];
  const real = [];
  for (const vin of Array.isArray(vins) ? vins : []) {
    if (list.some((v) => v.vin === vin)) real.push(vin);
  }
  return real.length ? [{ dataset: "real", vins: real }] : [];
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
`;

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
    const height = (this._config && this._config.height) || null;
    if (!height) this._card.style.height = stacked ? "auto" : "calc(100vh - var(--header-height, 56px) - 32px)";
    this._card.style.minHeight = stacked ? "" : "420px";
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
    this._places = places;
    this._categories = categoryList(results[0]);
    this._renderList();
    this._renderDetail();
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
    const groups = groupPlaces(this._places);

    if (!groups.suggestions.length && !groups.named.length) {
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

    row.addEventListener("click", () => this._selectPlace(place.id));
    row.tabIndex = 0;
    row.setAttribute("role", "button");
    row.setAttribute("aria-pressed", String(place.id === this._selectedId));
    row.title = `${placeTooltip(place)}. Tap to show it on the map.`;
    row.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        this._selectPlace(place.id);
      }
    });
    return row;
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
    this._renderList();
    this._renderDetail();
    this._renderMap();
    this._zoomToPlace(id);
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

    const visible = this._places.filter((p) => !p.hidden);
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
    this._renderEditHandles();
    if (bounds.length && !this._fitted) {
      this._fitted = true;
      const region = homeRegionPlaces(visible).map((p) => [p.lat, p.lon]);
      if (region.length === 1) this._map.setView(region[0], 15);
      else this._map.fitBounds(region, { padding: [40, 40], maxZoom: 16 });
    }
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
