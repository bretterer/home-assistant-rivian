/**
 * rivian-vehicle-bar.js
 *
 * Shared module for every Rivian card: the household's vehicle list, the
 * per-user vehicle SELECTION (which vehicles the dashboard currently shows),
 * and the `<rivian-vehicle-bar>` chip bar that edits it.
 *
 * Cards load this module dynamically so it carries their own `?v=` cache
 * buster (see CLAUDE.md):
 *   import(new URL("./rivian-vehicle-bar.js" + new URL(import.meta.url).search, import.meta.url))
 *
 * The selection is persisted per Home Assistant user through HA core's
 * `frontend/get_user_data` / `frontend/set_user_data` (key
 * `rivian_vehicle_selection`, value `{vins: [...]}`), falling back to
 * localStorage when user data is unavailable. Every change is broadcast on
 * `window` as a `rivian-vehicles-changed` CustomEvent (`detail.vins`), which
 * is how all cards on the page follow one selection.
 *
 * Pure helpers and the store are exported so Node tests can exercise them
 * without a DOM.
 */

export const SELECTION_KEY = "rivian_vehicle_selection";
export const CHANGE_EVENT = "rivian-vehicles-changed";
const VEHICLES_TTL_MS = 5 * 60 * 1000;
const FALLBACK_COLOR = "#8a8a8a";

// -- pure helpers ----------------------------------------------------------

/** The vins of a vehicle list, in list order. */
export function vehicleVins(vehicles) {
  return (Array.isArray(vehicles) ? vehicles : []).map((v) => v.vin).filter(Boolean);
}

/**
 * Normalize a requested selection against the known vehicles: unknown VINs
 * and duplicates are dropped, the result follows the vehicle list's order,
 * and anything empty/invalid becomes "all vehicles" (a selection is never
 * empty).
 */
export function normalizeSelection(vins, vehicles) {
  const all = vehicleVins(vehicles);
  if (!Array.isArray(vins)) return all;
  const wanted = new Set(vins);
  const picked = all.filter((vin) => wanted.has(vin));
  return picked.length ? picked : all;
}

/** True when `a` and `b` hold the same vins (order-insensitive). */
export function sameSelection(a, b) {
  if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
  const set = new Set(a);
  return b.every((vin) => set.has(vin));
}

/**
 * Toggle one vehicle in the selection. Deselecting the last selected vehicle
 * is blocked (the selection is returned unchanged): there is always at least
 * one vehicle in view.
 */
export function toggleVin(selected, vin, vehicles) {
  const current = normalizeSelection(selected, vehicles);
  if (!vehicleVins(vehicles).includes(vin)) return current;
  if (current.includes(vin)) {
    if (current.length === 1) return current;
    return current.filter((v) => v !== vin);
  }
  return normalizeSelection([...current, vin], vehicles);
}

/** A selection of just `vin` (all vehicles when `vin` is unknown). */
export function onlyVin(vin, vehicles) {
  return normalizeSelection([vin], vehicles);
}

/** A stable string for a set of vins (storage keys, cache dimensions). */
export function selectionKey(vins) {
  return [...(Array.isArray(vins) ? vins : [])].sort().join(",");
}

/** The vehicle with `vin`, or null. */
export function vehicleByVin(vehicles, vin) {
  return (Array.isArray(vehicles) ? vehicles : []).find((v) => v.vin === vin) || null;
}

/** A vehicle's categorical color for the light or dark theme. */
export function vehicleColor(vehicle, dark = false) {
  if (!vehicle) return FALLBACK_COLOR;
  const color = dark ? vehicle.color_dark || vehicle.color : vehicle.color;
  return typeof color === "string" && color ? color : FALLBACK_COLOR;
}

/** The vehicle's letter (A, B, ...), or "" when unknown. */
export function vehicleLetter(vehicle) {
  return vehicle && typeof vehicle.letter === "string" ? vehicle.letter : "";
}

/** Readable text color (white or near-black) on a filled `#rrggbb` color. */
export function inkOn(hex) {
  const m = /^#?([0-9a-f]{6})$/i.exec(String(hex || ""));
  if (!m) return "#ffffff";
  const n = parseInt(m[1], 16);
  const lin = (c) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
  };
  const lum = 0.2126 * lin((n >> 16) & 255) + 0.7152 * lin((n >> 8) & 255) + 0.0722 * lin(n & 255);
  // Contrast against white vs. near-black (#111): pick the better one.
  const white = 1.05 / (lum + 0.05);
  const black = (lum + 0.05) / 0.0668;
  return white >= black ? "#ffffff" : "#111111";
}

/** The chip's visible name. */
export function chipLabel(vehicle) {
  if (!vehicle) return "Vehicle";
  return String(vehicle.name || vehicle.model || "Rivian");
}

/** The chip's tooltip: "A · Rivi (R1S)". */
export function chipTitle(vehicle) {
  if (!vehicle) return "";
  const letter = vehicleLetter(vehicle);
  const name = chipLabel(vehicle);
  const model = vehicle.model && vehicle.model !== name ? ` (${vehicle.model})` : "";
  return `${letter ? `${letter} · ` : ""}${name}${model}`;
}

/** The chip's full hover/tap hint: identity plus what its two click targets do. */
export function chipHint(vehicle) {
  if (!vehicle) return "";
  return `${chipTitle(vehicle)} — tap to toggle, tap name to show only this vehicle`;
}

/** A vehicle's picture URL from live hass state, else its public render URL, else null. */
export function vehiclePictureUrl(vehicle, hass) {
  if (!vehicle) return null;
  if (vehicle.picture_entity && hass && hass.states) {
    const stateObj = hass.states[vehicle.picture_entity];
    const url = stateObj && stateObj.attributes && stateObj.attributes.entity_picture;
    if (url) return url;
  }
  return vehicle.picture_url || null;
}

/** Selected vehicles in selection order. */
export function selectedVehicles(vehicles, vins) {
  const list = Array.isArray(vehicles) ? vehicles : [];
  return normalizeSelection(vins, list)
    .map((vin) => vehicleByVin(list, vin))
    .filter(Boolean);
}

// -- selection store -------------------------------------------------------

const store = {
  vehicles: null,
  vehiclesAt: 0,
  vehiclesPromise: null,
  selection: null,
  selectionPromise: null,
};

/** Forget everything (tests, and a hard refresh of the vehicle list). */
export function _resetStore() {
  store.vehicles = null;
  store.vehiclesAt = 0;
  store.vehiclesPromise = null;
  store.selection = null;
  store.selectionPromise = null;
}

function _storage() {
  try {
    const s = globalThis.localStorage;
    return s && typeof s.getItem === "function" ? s : null;
  } catch (_err) {
    return null;
  }
}

function _events() {
  return typeof window !== "undefined" && window && typeof window.dispatchEvent === "function"
    ? window
    : null;
}

/** The household's vehicles (`rivian/vehicles/list`), cached for a few minutes. */
export async function getVehicles(hass, { refresh = false } = {}) {
  const fresh = store.vehicles && Date.now() - store.vehiclesAt < VEHICLES_TTL_MS;
  if (!refresh && fresh) return store.vehicles;
  if (!store.vehiclesPromise) {
    store.vehiclesPromise = (async () => {
      try {
        const list = await hass.callWS({ type: "rivian/vehicles/list" });
        store.vehicles = Array.isArray(list) ? list : [];
        store.vehiclesAt = Date.now();
      } catch (err) {
        console.warn("rivian-vehicle-bar: could not load the vehicle list", err);
        if (!store.vehicles) store.vehicles = [];
      } finally {
        store.vehiclesPromise = null;
      }
      return store.vehicles;
    })();
  }
  return store.vehiclesPromise;
}

async function _loadStoredVins(hass) {
  try {
    const result = await hass.callWS({ type: "frontend/get_user_data", key: SELECTION_KEY });
    const value = result && result.value;
    if (value && Array.isArray(value.vins)) return value.vins;
    return null;
  } catch (_err) {
    // User data unavailable: fall back to this browser's copy.
  }
  try {
    const s = _storage();
    const raw = s ? s.getItem(SELECTION_KEY) : null;
    const parsed = raw ? JSON.parse(raw) : null;
    return parsed && Array.isArray(parsed.vins) ? parsed.vins : null;
  } catch (_err) {
    return null;
  }
}

/** The current selection (vins, vehicle-list order). Defaults to every vehicle. */
export async function getSelection(hass) {
  const vehicles = await getVehicles(hass);
  if (store.selection) return normalizeSelection(store.selection, vehicles);
  if (!store.selectionPromise) {
    store.selectionPromise = (async () => {
      try {
        const stored = await _loadStoredVins(hass);
        if (!store.selection) store.selection = normalizeSelection(stored, vehicles);
      } finally {
        store.selectionPromise = null;
      }
      return store.selection;
    })();
  }
  const sel = await store.selectionPromise;
  return normalizeSelection(sel, vehicles);
}

/**
 * Change the selection: normalize, remember, broadcast, then persist (user
 * data, else localStorage). Returns the normalized selection.
 */
export async function setSelection(hass, vins) {
  const vehicles = await getVehicles(hass);
  const next = normalizeSelection(vins, vehicles);
  const changed = !sameSelection(store.selection, next);
  store.selection = next;
  if (changed) {
    const target = _events();
    if (target && typeof CustomEvent !== "undefined") {
      target.dispatchEvent(new CustomEvent(CHANGE_EVENT, { detail: { vins: [...next] } }));
    }
  }
  try {
    await hass.callWS({
      type: "frontend/set_user_data",
      key: SELECTION_KEY,
      value: { vins: next },
    });
  } catch (_err) {
    try {
      const s = _storage();
      if (s) s.setItem(SELECTION_KEY, JSON.stringify({ vins: next }));
    } catch (_err2) {
      // Not persisted; the choice still applies for this session.
    }
  }
  return next;
}

/** Listen for selection changes; returns an unsubscribe function. */
export function onSelectionChange(callback) {
  const target = _events();
  if (!target || typeof target.addEventListener !== "function") return () => {};
  const handler = (ev) => callback((ev && ev.detail && ev.detail.vins) || []);
  target.addEventListener(CHANGE_EVENT, handler);
  return () => target.removeEventListener(CHANGE_EVENT, handler);
}

// -- the <rivian-vehicle-bar> element --------------------------------------

const _BAR_STYLE = `
  :host { display: block; }
  .vb {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    color: var(--primary-text-color, #212121);
  }
  .vb-chip {
    --vb-c: #888;
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 4px 12px 4px 5px;
    border-radius: 999px;
    border: 1.5px solid var(--divider-color, #cfcfcf);
    background: transparent;
    cursor: pointer;
    max-width: 100%;
    box-sizing: border-box;
    transition: background 0.12s ease, border-color 0.12s ease, opacity 0.12s ease;
  }
  .vb-chip.on {
    border-color: var(--vb-c);
    background: color-mix(in srgb, var(--vb-c) 14%, transparent);
  }
  .vb-chip:not(.on) { opacity: 0.62; }
  .vb-chip:hover { opacity: 1; }
  .vb-dot {
    flex: none;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 24px;
    height: 24px;
    padding: 0;
    border-radius: 50%;
    border: 2px solid var(--vb-c);
    background: transparent;
    color: var(--primary-text-color, #212121);
    font: inherit;
    font-size: 12px;
    font-weight: 700;
    cursor: pointer;
    box-sizing: border-box;
  }
  .vb-chip.on .vb-dot { background: var(--vb-c); color: var(--vb-ink, #fff); }
  .vb-thumb {
    flex: none;
    position: relative;
    width: 44px;
    height: 22px;
    overflow: hidden;
    border-radius: 6px;
    background: var(--divider-color, rgba(127, 127, 127, 0.18));
  }
  .vb-thumb img {
    position: absolute;
    width: 140%;
    max-width: none;
    left: -18.6%;
    top: -92%;
  }
  .vb-thumb.full img {
    width: 100%;
    height: 100%;
    left: 0;
    top: 0;
    object-fit: contain;
  }
  .vb-name {
    min-width: 0;
    padding: 2px 0;
    border: none;
    background: none;
    font: inherit;
    font-size: 0.92em;
    font-weight: 500;
    color: var(--primary-text-color, #212121);
    cursor: pointer;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .vb-name:hover { text-decoration: underline; }
  .vb-all {
    border: none;
    background: none;
    padding: 4px 6px;
    font: inherit;
    font-size: 0.85em;
    color: var(--primary-color, #03a9f4);
    cursor: pointer;
  }
  .vb-all[disabled] { color: var(--disabled-text-color, #9e9e9e); cursor: default; }
  button:focus-visible, .vb-chip:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: 2px;
  }
  @media (max-width: 600px) {
    .vb { gap: 6px; }
    .vb-thumb { display: none; }
    .vb-chip { padding: 3px 10px 3px 4px; gap: 6px; }
    .vb-name { max-width: 9em; }
  }
`;

const BaseElement = typeof HTMLElement === "undefined" ? class {} : HTMLElement;

class RivianVehicleBar extends BaseElement {
  constructor() {
    super();
    this._vehicles = [];
    this._selection = [];
    this._ready = false;
    this._unsub = null;
    this._built = false;
  }

  get hass() {
    return this._hass;
  }

  set hass(hass) {
    const darkChanged =
      this._hass && hass && !!this._hass.themes?.darkMode !== !!hass.themes?.darkMode;
    const first = !this._hass;
    this._hass = hass;
    if (first && this.isConnected) this._load();
    if (darkChanged) this._render();
    else if (this._ready) this._refreshPictures();
  }

  connectedCallback() {
    if (!this._built) {
      this._built = true;
      this.attachShadow({ mode: "open" });
      const style = document.createElement("style");
      style.textContent = _BAR_STYLE;
      this.shadowRoot.appendChild(style);
      this._root = document.createElement("div");
      this._root.className = "vb";
      this._root.setAttribute("role", "group");
      this._root.setAttribute("aria-label", "Vehicles");
      this.shadowRoot.appendChild(this._root);
    }
    this._unsub = onSelectionChange((vins) => {
      this._selection = normalizeSelection(vins, this._vehicles);
      this._render();
    });
    if (this._hass) this._load();
  }

  disconnectedCallback() {
    if (this._unsub) this._unsub();
    this._unsub = null;
  }

  async _load() {
    if (!this._hass) return;
    const hass = this._hass;
    try {
      this._vehicles = await getVehicles(hass);
      this._selection = await getSelection(hass);
    } catch (err) {
      console.warn("rivian-vehicle-bar:", err);
    }
    this._ready = true;
    this._render();
  }

  async _apply(vins) {
    this._selection = normalizeSelection(vins, this._vehicles);
    this._render();
    try {
      await setSelection(this._hass, this._selection);
    } catch (err) {
      console.warn("rivian-vehicle-bar: could not save the selection", err);
    }
  }

  _refreshPictures() {
    for (const img of this._root.querySelectorAll("img[data-vin]")) {
      const url = vehiclePictureUrl(vehicleByVin(this._vehicles, img.dataset.vin), this._hass);
      if (url && img.getAttribute("src") !== url) img.src = url;
    }
  }

  _render() {
    if (!this._root) return;
    const dark = !!(this._hass && this._hass.themes && this._hass.themes.darkMode);
    this._root.textContent = "";
    // A single vehicle needs no selector.
    this.style.display = this._vehicles.length < 2 ? "none" : "";
    if (this._vehicles.length < 2) return;
    const selected = new Set(this._selection);

    for (const v of this._vehicles) {
      const on = selected.has(v.vin);
      const color = vehicleColor(v, dark);
      const chip = document.createElement("div");
      chip.className = "vb-chip" + (on ? " on" : "");
      chip.style.setProperty("--vb-c", color);
      chip.style.setProperty("--vb-ink", inkOn(color));
      chip.title = chipHint(v);
      chip.addEventListener("click", () => this._apply(toggleVin(this._selection, v.vin, this._vehicles)));

      const dot = document.createElement("button");
      dot.type = "button";
      dot.className = "vb-dot";
      dot.setAttribute("aria-pressed", String(on));
      dot.setAttribute("aria-label", `${on ? "Hide" : "Show"} ${chipLabel(v)}`);
      dot.title = `${on ? "Hide" : "Show"} ${chipLabel(v)} (${on ? "selected" : "not selected"}); the last vehicle can't be hidden`;
      dot.textContent = vehicleLetter(v);
      chip.appendChild(dot);

      const url = vehiclePictureUrl(v, this._hass);
      if (url) {
        const thumb = document.createElement("span");
        // Configurator renders are wide scenes (crop in on the car); a
        // bundled SVG illustration is shown whole.
        thumb.className = "vb-thumb" + (/\.svg(\?|$)/i.test(url) ? " full" : "");
        const img = document.createElement("img");
        img.dataset.vin = v.vin;
        img.alt = "";
        img.src = url;
        thumb.appendChild(img);
        chip.appendChild(thumb);
      }

      const name = document.createElement("button");
      name.type = "button";
      name.className = "vb-name";
      name.textContent = chipLabel(v);
      name.title = `Show only ${chipLabel(v)}`;
      name.setAttribute("aria-label", `Show only ${chipLabel(v)}`);
      name.addEventListener("click", (ev) => {
        ev.stopPropagation();
        this._apply(onlyVin(v.vin, this._vehicles));
      });
      chip.appendChild(name);

      this._root.appendChild(chip);
    }

    const all = document.createElement("button");
    all.type = "button";
    all.className = "vb-all";
    all.textContent = "All";
    all.title = "Show all vehicles";
    all.setAttribute("aria-label", "Show all vehicles");
    all.disabled = this._selection.length === this._vehicles.length;
    all.addEventListener("click", () => this._apply(vehicleVins(this._vehicles)));
    this._root.appendChild(all);
  }
}

if (typeof customElements !== "undefined" && !customElements.get("rivian-vehicle-bar")) {
  customElements.define("rivian-vehicle-bar", RivianVehicleBar);
}
