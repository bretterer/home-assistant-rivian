/**
 * rivian-overview-card.js
 *
 * Overview page of the Rivian dashboard: one ha-card per configured vehicle
 * with a hero image + live status, a compact lifetime-stats table, and a
 * "last drive" summary line. Live status comes straight from `hass.states`;
 * stats come from `rivian/analytics/summary` over the WebSocket API, kept
 * fresh via `rivian/analytics/subscribe` and a 15-minute rolling refresh.
 *
 * Pure helpers (formatNumber, formatHours, formatEnergy, socColor,
 * locationLabel, formatDuration, formatDriveDateTime, lastDriveText,
 * summaryRows) are exported as named exports purely so a Node smoke test can
 * import and exercise them without a DOM or customElements environment.
 */

const WINDOW_KEYS = ["7d", "30d", "365d", "all"];
const WINDOW_HEADERS = ["7 days", "30 days", "Year", "Lifetime"];
const REFRESH_INTERVAL_MS = 15 * 60 * 1000;
const STACK_BREAKPOINT_PX = 500;
const NARROW_BREAKPOINT_PX = 420;
// Card width at/above which the vehicles sit side by side in a scrolling row.
export const ROW_BREAKPOINT_PX = 900;

/** Load the shared vehicle bar/selection module with this module's own cache-buster. */
function _loadBarModule() {
  return import(new URL("./rivian-vehicle-bar.js" + new URL(import.meta.url).search, import.meta.url));
}

/** "row" (horizontal, scroll-snapping) on wide roots, else "column". */
export function overviewLayout(width) {
  return typeof width === "number" && width >= ROW_BREAKPOINT_PX ? "row" : "column";
}

/** Whether a vehicle's card is dimmed (not part of the current selection). */
export function isDimmed(vin, selection) {
  return Array.isArray(selection) && !selection.includes(vin);
}

/**
 * Rows of the "Household" strip from `summary {vins}`'s `combined` windows:
 * `[{label, values: [7d, 30d, 365d, lifetime]}]`, formatted for display.
 */
export function householdRows(combined) {
  const labels = { Miles: "Miles", Drives: "Drives", "Energy (kWh)": "kWh", "Efficiency (mi/kWh)": "mi/kWh" };
  return summaryRows(combined)
    .filter((row) => Object.hasOwn(labels, row.label))
    .map((row) => ({ label: labels[row.label], values: row.values }));
}

/** Hover/tap explanations for the stats table's row labels (stat-table and household labels). */
export const STAT_ROW_TITLES = {
  Miles: "Distance driven in the period",
  Drives: "Number of recorded drives (very short moves are not counted)",
  Hours: "Time spent driving",
  "Energy (kWh)": "Battery energy used while driving, in kilowatt-hours",
  kWh: "Battery energy used while driving, in kilowatt-hours",
  "Efficiency (mi/kWh)": "Miles per kilowatt-hour: higher is better",
  "mi/kWh": "Miles per kilowatt-hour: higher is better",
  MPGe: "Miles per gallon equivalent (33.7 kWh = 1 gallon): higher is better",
};

/** Hover/tap explanations for the stats table's column headers. */
export const STAT_WINDOW_TITLES = {
  "7 days": "The last 7 days",
  "30 days": "The last 30 days",
  Year: "The last 365 days",
  Lifetime: "Everything recorded for this vehicle",
};

/** Tooltip for a status chip: its text plus, when it opens details, what a click does. */
export function overviewChipTitle(chip) {
  if (!chip) return "";
  return chip.entityId ? `${chip.text} — tap for details` : String(chip.text || "");
}

/** Format a number with thousands separators and a fixed decimal count; "–" when missing. */
export function formatNumber(value, decimals = 0) {
  if (value === null || value === undefined || typeof value !== "number" || Number.isNaN(value)) {
    return "–";
  }
  return value.toLocaleString(undefined, {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

/** Format hours to one decimal place; "–" when missing. */
export function formatHours(value) {
  return formatNumber(value, 1);
}

/** Format an energy value in kWh: 0 dp at/above 100, else 1 dp. */
export function formatEnergy(kwh) {
  if (kwh === null || kwh === undefined || typeof kwh !== "number" || Number.isNaN(kwh)) return "–";
  return formatNumber(kwh, kwh >= 100 ? 0 : 1);
}

/** CSS color (with fallback) for a state-of-charge percentage. */
export function socColor(soc) {
  if (soc === null || soc === undefined || typeof soc !== "number" || Number.isNaN(soc)) {
    return "var(--disabled-text-color, #9e9e9e)";
  }
  if (soc >= 50) return "var(--success-color, #4caf50)";
  if (soc >= 20) return "var(--warning-color, #ff9800)";
  return "var(--error-color, #db4437)";
}

/**
 * Whether a typed confirmation (from `window.prompt`) matches a vehicle's
 * name, trimmed and case-insensitive. `null`/`undefined` (a cancelled
 * prompt) never matches.
 */
export function confirmMatches(input, name) {
  if (input === null || input === undefined) return false;
  if (typeof name !== "string") return false;
  return input.trim().toLowerCase() === name.trim().toLowerCase();
}

/**
 * Text for the "Delete vehicle history…" prompt. The vehicle keeps
 * recording new drives afterward.
 */
export function deleteVehicleMessage(name) {
  return `This permanently deletes ALL recorded drives, routes, charging sessions and statistics for “${name}”. New drives will still be recorded for a real vehicle. Type the vehicle name to confirm:`;
}

/** Human label for a device_tracker location state. */
export function locationLabel(state) {
  if (state === null || state === undefined) return null;
  if (state === "home") return "Home";
  if (state === "not_home") return "Away";
  return String(state);
}

/** Format a duration in seconds as "1:04" (h:mm) or "24 min". */
export function formatDuration(seconds) {
  if (seconds === null || seconds === undefined || typeof seconds !== "number" || Number.isNaN(seconds)) {
    return null;
  }
  const totalMinutes = Math.round(seconds / 60);
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (hours > 0) return `${hours}:${String(minutes).padStart(2, "0")}`;
  return `${minutes} min`;
}

/** Format an epoch-seconds timestamp as "Tue, Sep 23 · 8:12 AM" in local time. */
export function formatDriveDateTime(startTs) {
  if (startTs === null || startTs === undefined || typeof startTs !== "number" || Number.isNaN(startTs)) {
    return null;
  }
  const date = new Date(startTs * 1000);
  const datePart = date.toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
  });
  const timePart = date.toLocaleTimeString(undefined, {
    hour: "numeric",
    minute: "2-digit",
  });
  return `${datePart} · ${timePart}`;
}

/**
 * Build the "Last drive · Tue, Sep 23 · 8:12 AM · 11.4 mi · 24 min ·
 * 2.91 mi/kWh" summary line for a `last_drive` payload, skipping any part
 * that is missing or zero. Returns "" when there is no drive.
 */
export function lastDriveText(drive) {
  if (!drive) return "";
  const parts = ["Last drive"];
  const dt = formatDriveDateTime(drive.start_ts);
  if (dt) parts.push(dt);
  if (typeof drive.distance_miles === "number" && drive.distance_miles > 0) {
    parts.push(`${drive.distance_miles.toFixed(1)} mi`);
  }
  const duration = formatDuration(drive.duration_seconds);
  if (duration && drive.duration_seconds > 0) parts.push(duration);
  if (typeof drive.efficiency_mi_kwh === "number" && drive.efficiency_mi_kwh > 0) {
    parts.push(`${drive.efficiency_mi_kwh.toFixed(2)} mi/kWh`);
  }
  return parts.join(" · ");
}

/**
 * Build the stats-table rows from a `windows` map (`{"7d": S, "30d": S, ...}`).
 * Returns `[{label, values: [v7d, v30d, v365d, vAll]}]` with each value
 * already formatted for display.
 */
export function summaryRows(windows) {
  const w = windows || {};
  const at = (key) => w[key] || null;
  return [
    { label: "Miles", values: WINDOW_KEYS.map((k) => formatNumber(at(k) && at(k).miles, 0)) },
    { label: "Drives", values: WINDOW_KEYS.map((k) => formatNumber(at(k) && at(k).drives, 0)) },
    { label: "Hours", values: WINDOW_KEYS.map((k) => formatHours(at(k) && at(k).hours)) },
    { label: "Energy (kWh)", values: WINDOW_KEYS.map((k) => formatEnergy(at(k) && at(k).kwh)) },
    {
      label: "Efficiency (mi/kWh)",
      values: WINDOW_KEYS.map((k) => {
        const s = at(k);
        return s && s.drives ? formatNumber(s.efficiency_mi_kwh, 2) : "–";
      }),
      narrowSkip: false,
    },
    {
      label: "MPGe",
      values: WINDOW_KEYS.map((k) => {
        const s = at(k);
        return s && s.drives ? formatNumber(s.mpge, 0) : "–";
      }),
      narrowSkip: true,
    },
  ];
}

function _escapeText(el, text) {
  el.textContent = text === null || text === undefined ? "" : String(text);
  return el;
}

/** Read a live entity's display text, or null when missing/unavailable/unknown. */
function _stateText(hass, stateObj) {
  if (!stateObj) return null;
  if (stateObj.state === "unavailable" || stateObj.state === "unknown") return null;
  if (hass && typeof hass.formatEntityState === "function") {
    try {
      return hass.formatEntityState(stateObj);
    } catch (_err) {
      // Fall through to the manual formatting below.
    }
  }
  const unit = stateObj.attributes && stateObj.attributes.unit_of_measurement;
  return unit ? `${stateObj.state} ${unit}` : stateObj.state;
}

function _numericState(stateObj) {
  if (!stateObj || stateObj.state === "unavailable" || stateObj.state === "unknown") return null;
  const n = Number(stateObj.state);
  return Number.isNaN(n) ? null : n;
}

const _CARD_STYLE = `
  :host { display: block; }
  .roc-root {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }
  /* Wide: the vehicles sit side by side and scroll horizontally. */
  .roc-root.roc-row {
    flex-direction: row;
    align-items: flex-start;
    overflow-x: auto;
    scroll-snap-type: x mandatory;
    padding-bottom: 6px;
  }
  .roc-root.roc-row > ha-card {
    flex: 0 0 min(420px, 100%);
    scroll-snap-align: start;
  }
  ha-card {
    padding: 16px;
    position: relative;
    transition: opacity 0.15s ease;
  }
  ha-card.roc-dim {
    opacity: 0.45;
  }
  ha-card.roc-dim:hover {
    opacity: 0.8;
  }
  .roc-select {
    position: absolute;
    top: 10px;
    right: 10px;
    z-index: 1;
    display: flex;
    align-items: center;
    cursor: pointer;
  }
  .roc-select input {
    width: 20px;
    height: 20px;
    margin: 0;
    cursor: pointer;
    accent-color: var(--roc-vcolor, var(--primary-color, #03a9f4));
  }
  .roc-name-row {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
    padding-right: 28px;
  }
  .roc-badge {
    flex: none;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 22px;
    height: 22px;
    border-radius: 50%;
    background: var(--roc-vcolor, #888);
    color: var(--roc-vink, #fff);
    font-size: 12px;
    font-weight: 700;
  }
  .roc-household {
    margin-bottom: 16px;
  }
  .roc-household ha-card {
    padding: 12px 16px;
  }
  .roc-household-title {
    font-weight: 500;
    color: var(--primary-text-color);
    margin-bottom: 4px;
  }
  .roc-household-title span {
    font-weight: 400;
    font-size: 0.85em;
    color: var(--secondary-text-color);
    margin-left: 6px;
  }
  .roc-household .roc-stats {
    margin-top: 4px;
  }
  .roc-hero {
    display: flex;
    gap: 16px;
    align-items: center;
  }
  .roc-stacked .roc-hero {
    flex-direction: column;
    align-items: stretch;
  }
  .roc-image-wrap {
    flex: 0 0 40%;
    max-width: 40%;
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 100px;
  }
  .roc-stacked .roc-image-wrap {
    flex: 0 0 auto;
    max-width: 100%;
  }
  .roc-image-wrap img {
    max-width: 100%;
    max-height: 180px;
    object-fit: contain;
  }
  /* Configurator render: a wide studio scene with the car small in the
     middle, so crop in on the car rather than showing the whole room. */
  .roc-image-wrap.roc-render-wrap {
    position: relative;
    display: block;
    overflow: hidden;
    aspect-ratio: 2 / 1;
    min-height: 0;
    border-radius: 12px;
  }
  .roc-image-wrap.roc-render-wrap img {
    position: absolute;
    width: 140%;
    max-width: none;
    max-height: none;
    left: -18.6%;
    top: -92%;
  }
  .roc-image-wrap ha-icon {
    --mdc-icon-size: 96px;
    color: var(--secondary-text-color);
  }
  .roc-info {
    flex: 1 1 auto;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .roc-name {
    font-size: 1.3em;
    font-weight: 500;
    color: var(--primary-text-color);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .roc-model {
    font-size: 0.9em;
    color: var(--secondary-text-color);
    margin-top: -6px;
  }
  .roc-battery {
    cursor: pointer;
    background: none;
    border: none;
    padding: 0;
    text-align: left;
    font: inherit;
    color: inherit;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .roc-battery:not(.roc-battery-clickable) {
    cursor: default;
  }
  .roc-battery-bar {
    position: relative;
    height: 8px;
    border-radius: 4px;
    background: var(--divider-color, #e0e0e0);
    overflow: visible;
  }
  .roc-battery-fill {
    position: absolute;
    inset: 0;
    right: auto;
    border-radius: 4px;
    background: var(--disabled-text-color, #9e9e9e);
  }
  .roc-battery-tick {
    position: absolute;
    top: -2px;
    bottom: -2px;
    width: 2px;
    background: var(--primary-text-color);
    opacity: 0.6;
  }
  .roc-battery-text {
    font-size: 0.9em;
    color: var(--primary-text-color);
  }
  .roc-chips {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .roc-chip {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 3px 10px 3px 6px;
    border-radius: 12px;
    background: var(--divider-color, rgba(127, 127, 127, 0.16));
    color: var(--primary-text-color);
    font-size: 0.8em;
    border: none;
    cursor: pointer;
    font-family: inherit;
  }
  .roc-chip:not(.roc-chip-clickable) {
    cursor: default;
  }
  .roc-chip ha-icon {
    --mdc-icon-size: 16px;
  }
  .roc-chip-success { color: var(--success-color, #4caf50); }
  .roc-chip-warning { color: var(--warning-color, #ff9800); }
  .roc-stats {
    width: 100%;
    border-collapse: collapse;
    margin-top: 14px;
    font-size: 0.88em;
  }
  .roc-narrow .roc-stats {
    font-size: 0.78em;
  }
  .roc-stats th, .roc-stats td {
    padding: 4px 6px;
    text-align: right;
    border-bottom: 1px solid var(--divider-color, #e0e0e0);
  }
  .roc-stats th:first-child, .roc-stats td:first-child {
    text-align: left;
    color: var(--secondary-text-color);
  }
  .roc-stats th {
    color: var(--secondary-text-color);
    font-weight: 500;
  }
  .roc-stats td {
    font-variant-numeric: tabular-nums;
    color: var(--primary-text-color);
  }
  .roc-stats tr:last-child td {
    border-bottom: none;
  }
  .roc-narrow .roc-stats-mpge {
    display: none;
  }
  .roc-stats-error {
    margin-top: 14px;
    color: var(--secondary-text-color);
    font-size: 0.85em;
  }
  .roc-last-drive {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    margin-top: 12px;
    padding-top: 10px;
    border-top: 1px solid var(--divider-color, #e0e0e0);
    font-size: 0.85em;
    color: var(--secondary-text-color);
    background: none;
    border-left: none;
    border-right: none;
    border-bottom: none;
    width: 100%;
    text-align: left;
    font-family: inherit;
  }
  button.roc-last-drive {
    cursor: pointer;
  }
  .roc-last-drive ha-icon {
    --mdc-icon-size: 18px;
    flex-shrink: 0;
  }
  .roc-delete-link {
    display: block;
    margin: 10px 16px 14px;
    padding: 0;
    border: none;
    background: none;
    font: inherit;
    font-size: 0.78em;
    color: var(--secondary-text-color);
    cursor: pointer;
    text-align: left;
  }
  .roc-delete-link:hover {
    color: var(--error-color, #db4437);
    text-decoration: underline;
  }
  .roc-empty {
    padding: 24px;
    text-align: center;
    color: var(--secondary-text-color);
  }
`;

// Guarded so this module (and its exported pure helpers) can be imported
// under plain Node with no DOM, for tests/frontend/overview_card.test.mjs.
const BaseElement = typeof HTMLElement === "undefined" ? class {} : HTMLElement;

class RivianOverviewCard extends BaseElement {
  static getStubConfig() {
    return { vehicles: [] };
  }

  setConfig(config) {
    if (!config || !Array.isArray(config.vehicles)) {
      throw new Error("rivian-overview-card: `vehicles` is required in config");
    }
    this._config = config;
    if (!this._shadowBuilt) this._buildShadow();
    this._teardownVehicles();
    this._buildVehicles();
    if (this._hass) this.hass = this._hass;
  }

  getCardSize() {
    const count = (this._config && this._config.vehicles && this._config.vehicles.length) || 1;
    return count * 6;
  }

  get hass() {
    return this._hass;
  }

  set hass(hass) {
    const darkChanged =
      this._hass && hass && !!this._hass.themes?.darkMode !== !!hass.themes?.darkMode;
    this._hass = hass;
    if (!this._shadowBuilt) return;
    if (!this._storeStarted && hass) this._startStore();
    if (darkChanged) this._applyVehicleStyle();
    for (const v of this._vehicles) {
      // The image depends on hass (theme + image entity), which may not have
      // existed when the card was built; _renderImage skips unchanged images.
      this._renderImage(v, darkChanged);
      this._maybeUpdateStatus(v);
      this._renderDeleteLink(v);
      if (!v.started && v.vin) {
        v.started = true;
        this._fetchStats(v);
        this._subscribe(v);
      }
    }
  }

  connectedCallback() {
    if (!this._shadowBuilt) return;
    this._observeResize();
    if (this._hass && !this._storeStarted) this._startStore();
    for (const v of this._vehicles) {
      if (this._hass && v.vin) this._subscribe(v);
    }
    if (!this._refreshTimer) {
      this._refreshTimer = setInterval(() => {
        for (const v of this._vehicles) {
          if (v.vin) this._fetchStats(v);
        }
      }, REFRESH_INTERVAL_MS);
    }
  }

  disconnectedCallback() {
    for (const v of this._vehicles) this._unsubscribe(v);
    if (this._unsubSelection) this._unsubSelection();
    this._unsubSelection = null;
    this._storeStarted = false;
    clearTimeout(this._householdTimer);
    if (this._refreshTimer) {
      clearInterval(this._refreshTimer);
      this._refreshTimer = null;
    }
    if (this._resizeObserver) {
      this._resizeObserver.disconnect();
      this._resizeObserver = null;
    }
  }

  _buildShadow() {
    this._shadowBuilt = true;
    this.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = _CARD_STYLE;
    this.shadowRoot.appendChild(style);
    this._household = document.createElement("div");
    this._household.className = "roc-household";
    this._household.style.display = "none";
    this.shadowRoot.appendChild(this._household);
    this._root = document.createElement("div");
    this._root.className = "roc-root";
    this.shadowRoot.appendChild(this._root);
    this._vehicles = [];
    this._vehicleList = [];
    this._selection = null;
  }

  // -- shared vehicle selection ------------------------------------------

  async _startStore() {
    this._storeStarted = true;
    const hass = this._hass;
    try {
      this._bar = await _loadBarModule();
      this._vehicleList = await this._bar.getVehicles(hass);
      this._selection = await this._bar.getSelection(hass);
    } catch (err) {
      console.warn("rivian-overview-card: vehicle selection unavailable", err);
      this._storeStarted = false;
      return;
    }
    if (this._unsubSelection) this._unsubSelection();
    this._unsubSelection = this._bar.onSelectionChange((vins) => {
      this._selection = vins;
      this._applySelection();
    });
    this._applyVehicleStyle();
    this._applySelection();
  }

  /** Letter badge + color for each card, from the shared vehicle list. */
  _applyVehicleStyle() {
    const bar = this._bar;
    if (!bar) return;
    const dark = !!(this._hass && this._hass.themes && this._hass.themes.darkMode);
    for (const v of this._vehicles) {
      const info = bar.vehicleByVin(this._vehicleList, v.vin);
      const color = bar.vehicleColor(info, dark);
      v.card.style.setProperty("--roc-vcolor", color);
      v.card.style.setProperty("--roc-vink", bar.inkOn(color));
      v.badgeEl.textContent = bar.vehicleLetter(info);
      v.badgeEl.title = info ? `Vehicle ${bar.vehicleLetter(info)}: its color and letter appear on every Rivian card` : "";
      v.badgeEl.style.display = info ? "" : "none";
    }
  }

  /** Dim unselected cards, sync the checkboxes, and refresh the household strip. */
  _applySelection() {
    const sel = this._selection;
    for (const v of this._vehicles) {
      const dimmed = isDimmed(v.vin, sel);
      v.card.classList.toggle("roc-dim", dimmed);
      v.selectInput.checked = !dimmed;
    }
    this._scheduleHousehold(0);
  }

  async _onSelectToggle(v) {
    const bar = this._bar;
    if (!bar || !this._hass) {
      v.selectInput.checked = !v.selectInput.checked;
      return;
    }
    const next = bar.toggleVin(this._selection, v.vin, this._vehicleList);
    // Blocked (the last selected vehicle): the checkbox snaps back.
    v.selectInput.checked = next.includes(v.vin);
    if (bar.sameSelection(next, this._selection)) return;
    this._selection = next;
    this._applySelection();
    await bar.setSelection(this._hass, next);
  }

  _scheduleHousehold(delayMs = 300) {
    clearTimeout(this._householdTimer);
    this._householdTimer = setTimeout(() => {
      this._fetchHousehold().catch((err) => console.warn("rivian-overview-card: household stats", err));
    }, delayMs);
  }

  /** The vins of the selection that this card actually shows. */
  _shownSelection() {
    const mine = new Set(this._vehicles.map((v) => v.vin).filter(Boolean));
    return (this._selection || []).filter((vin) => mine.has(vin));
  }

  async _fetchHousehold() {
    const vins = this._shownSelection();
    if (vins.length < 2 || !this._hass) {
      this._householdSeq = (this._householdSeq || 0) + 1;
      this._household.style.display = "none";
      this._household.textContent = "";
      return;
    }
    const seq = (this._householdSeq = (this._householdSeq || 0) + 1);
    const result = await this._hass.callWS({ type: "rivian/analytics/summary", vins });
    if (seq !== this._householdSeq) return;
    this._renderHousehold(vins, result && result.combined);
  }

  _renderHousehold(vins, combined) {
    const el = this._household;
    el.textContent = "";
    if (!combined) {
      el.style.display = "none";
      return;
    }
    el.style.display = "";
    const card = document.createElement("ha-card");
    const title = document.createElement("div");
    title.className = "roc-household-title";
    title.textContent = "Household";
    const sub = document.createElement("span");
    sub.textContent = `${vins.length} vehicles combined`;
    title.appendChild(sub);
    card.appendChild(title);

    const table = document.createElement("table");
    table.className = "roc-stats";
    const headRow = document.createElement("tr");
    headRow.appendChild(document.createElement("th"));
    for (const header of WINDOW_HEADERS) {
      const th = document.createElement("th");
      th.textContent = header;
      th.title = STAT_WINDOW_TITLES[header] || header;
      headRow.appendChild(th);
    }
    const thead = document.createElement("thead");
    thead.appendChild(headRow);
    table.appendChild(thead);
    const tbody = document.createElement("tbody");
    for (const row of householdRows(combined)) {
      const tr = document.createElement("tr");
      const label = document.createElement("td");
      label.textContent = row.label;
      label.title = STAT_ROW_TITLES[row.label] || row.label;
      tr.appendChild(label);
      for (const value of row.values) {
        const td = document.createElement("td");
        td.textContent = value;
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    card.appendChild(table);
    el.appendChild(card);
  }

  _teardownVehicles() {
    for (const v of this._vehicles || []) this._unsubscribe(v);
    this._vehicles = [];
  }

  _buildVehicles() {
    this._root.textContent = "";
    const vehicles = (this._config && this._config.vehicles) || [];
    if (!vehicles.length) {
      const empty = document.createElement("div");
      empty.className = "roc-empty";
      _escapeText(empty, "No vehicles configured.");
      this._root.appendChild(empty);
      return;
    }
    for (const vehicleConfig of vehicles) {
      const v = this._buildVehicleSection(vehicleConfig);
      this._vehicles.push(v);
      this._root.appendChild(v.card);
    }
    this._observeResize();
  }

  _buildVehicleSection(vehicleConfig) {
    const entities = vehicleConfig.entities || {};
    const v = {
      config: vehicleConfig,
      vin: vehicleConfig.vin || null,
      entities,
      entityIds: Array.from(new Set(Object.values(entities).filter(Boolean))),
      prevStates: new Map(),
      started: false,
      statsSeq: 0,
      unsubPromise: null,
      windows: null,
      lastDrive: null,
      statsError: null,
    };

    v.card = document.createElement("ha-card");

    v.hero = document.createElement("div");
    v.hero.className = "roc-hero";
    v.card.appendChild(v.hero);

    v.imageWrap = document.createElement("div");
    v.imageWrap.className = "roc-image-wrap";
    v.hero.appendChild(v.imageWrap);

    v.info = document.createElement("div");
    v.info.className = "roc-info";
    v.hero.appendChild(v.info);

    v.nameRow = document.createElement("div");
    v.nameRow.className = "roc-name-row";
    v.info.appendChild(v.nameRow);

    v.badgeEl = document.createElement("span");
    v.badgeEl.className = "roc-badge";
    v.badgeEl.style.display = "none";
    v.nameRow.appendChild(v.badgeEl);

    v.nameEl = document.createElement("div");
    v.nameEl.className = "roc-name";
    _escapeText(v.nameEl, vehicleConfig.name || "Vehicle");
    v.nameRow.appendChild(v.nameEl);

    // Selection checkbox in the hero corner, bound to the shared selection.
    v.selectLabel = document.createElement("label");
    v.selectLabel.className = "roc-select";
    v.selectLabel.title = "Include in the dashboard's vehicle selection";
    v.selectInput = document.createElement("input");
    v.selectInput.type = "checkbox";
    v.selectInput.checked = true;
    v.selectInput.setAttribute("aria-label", `Show ${vehicleConfig.name || "vehicle"}`);
    v.selectInput.title = "Include in the dashboard's vehicle selection";
    v.selectInput.addEventListener("change", () => {
      this._onSelectToggle(v).catch((err) => console.warn("rivian-overview-card: selection", err));
    });
    v.selectLabel.appendChild(v.selectInput);
    v.selectLabel.style.display = v.vin ? "" : "none";
    v.card.appendChild(v.selectLabel);

    v.modelEl = document.createElement("div");
    v.modelEl.className = "roc-model";
    _escapeText(v.modelEl, vehicleConfig.model || "");
    v.info.appendChild(v.modelEl);

    const batteryHasEntity = !!entities.soc;
    v.batteryEl = document.createElement(batteryHasEntity ? "button" : "div");
    if (batteryHasEntity) {
      v.batteryEl.type = "button";
      v.batteryEl.classList.add("roc-battery-clickable");
      v.batteryEl.addEventListener("click", () => this._openMoreInfo(entities.soc));
      v.batteryEl.title = "Battery level and estimated range — tap for details";
      v.batteryEl.setAttribute("aria-label", "Battery level and estimated range, tap for details");
    }
    v.batteryEl.className += " roc-battery";
    v.info.appendChild(v.batteryEl);

    v.batteryBar = document.createElement("div");
    v.batteryBar.className = "roc-battery-bar";
    v.batteryFill = document.createElement("div");
    v.batteryFill.className = "roc-battery-fill";
    v.batteryBar.appendChild(v.batteryFill);
    v.batteryTick = document.createElement("div");
    v.batteryTick.title = "Charge limit";
    v.batteryTick.className = "roc-battery-tick";
    v.batteryTick.style.display = "none";
    v.batteryBar.appendChild(v.batteryTick);
    v.batteryEl.appendChild(v.batteryBar);

    v.batteryText = document.createElement("div");
    v.batteryText.className = "roc-battery-text";
    v.batteryEl.appendChild(v.batteryText);

    v.chipsEl = document.createElement("div");
    v.chipsEl.className = "roc-chips";
    v.info.appendChild(v.chipsEl);

    v.statsEl = document.createElement("table");
    v.statsEl.className = "roc-stats";
    v.card.appendChild(v.statsEl);

    v.statsErrorEl = document.createElement("div");
    v.statsErrorEl.className = "roc-stats-error";
    v.statsErrorEl.style.display = "none";
    v.card.appendChild(v.statsErrorEl);

    // drives_path is card-level config (shared by every vehicle).
    const drivesPath = this._config && this._config.drives_path;
    v.lastDriveEl = document.createElement(drivesPath ? "button" : "div");
    if (drivesPath) {
      v.lastDriveEl.type = "button";
    }
    v.lastDriveEl.className = "roc-last-drive";
    v.lastDriveEl.style.display = "none";
    v.card.appendChild(v.lastDriveEl);

    v.deleteLinkEl = document.createElement("button");
    v.deleteLinkEl.type = "button";
    v.deleteLinkEl.className = "roc-delete-link";
    v.deleteLinkEl.title = "Permanently delete every recorded drive and charge for this vehicle (asks for confirmation)";
    v.deleteLinkEl.style.display = "none";
    _escapeText(v.deleteLinkEl, "Delete vehicle history…");
    v.deleteLinkEl.addEventListener("click", () => {
      this._confirmDeleteVehicleHistory(v).catch((err) =>
        console.error("rivian-overview-card: delete vehicle history failed", err)
      );
    });
    v.card.appendChild(v.deleteLinkEl);

    this._renderImage(v);
    this._renderStatus(v);
    this._renderStats(v);
    this._renderDeleteLink(v);
    return v;
  }

  /** Shows the admin-only "Delete vehicle history…" link once hass/vin are known. */
  _renderDeleteLink(v) {
    const isAdmin = !!(this._hass && this._hass.user && this._hass.user.is_admin);
    v.deleteLinkEl.style.display = isAdmin && v.vin ? "block" : "none";
  }

  async _confirmDeleteVehicleHistory(v) {
    if (!v.vin) return;
    const name = (v.config && v.config.name) || "this vehicle";
    const input = window.prompt(deleteVehicleMessage(name));
    if (!confirmMatches(input, name)) return;
    await this._hass.callWS({ type: "rivian/analytics/delete_vehicle_history", vin: v.vin });
    await this._fetchStats(v);
  }

  _observeResize() {
    if (this._resizeObserver || typeof ResizeObserver === "undefined") return;
    this._resizeObserver = new ResizeObserver((entries) => {
      for (const entry of entries) {
        if (entry.target === this) {
          this._applyRootLayout(entry.contentRect.width);
          continue;
        }
        const v = this._vehicles.find((x) => x.card === entry.target);
        if (v) this._applyCardLayout(v, entry.contentRect.width);
      }
    });
    this._resizeObserver.observe(this);
    for (const v of this._vehicles) this._resizeObserver.observe(v.card);
  }

  _applyRootLayout(width) {
    this._root.classList.toggle("roc-row", overviewLayout(width) === "row");
  }

  _applyCardLayout(v, width) {
    v.card.classList.toggle("roc-stacked", width > 0 && width < STACK_BREAKPOINT_PX);
    v.card.classList.toggle("roc-narrow", width > 0 && width < NARROW_BREAKPOINT_PX);
  }

  _openMoreInfo(entityId) {
    if (!entityId) return;
    this.dispatchEvent(
      new CustomEvent("hass-more-info", {
        detail: { entityId },
        bubbles: true,
        composed: true,
      })
    );
  }

  _navigateToDrives() {
    const path = this._config && this._config.drives_path;
    if (!path) return;
    history.pushState(null, "", path);
    window.dispatchEvent(new CustomEvent("location-changed", { detail: { replace: false } }));
  }

  // -- Live status -----------------------------------------------------

  _maybeUpdateStatus(v) {
    if (!this._hass) return;
    let changed = false;
    for (const id of v.entityIds) {
      const stateObj = this._hass.states[id];
      if (v.prevStates.get(id) !== stateObj) {
        v.prevStates.set(id, stateObj);
        changed = true;
      }
    }
    if (changed) this._renderStatus(v);
  }

  _renderImage(v, force = false) {
    const hass = this._hass;
    const dark = !!(hass && hass.themes && hass.themes.darkMode);
    const entities = v.entities;
    const preferredId = dark ? entities.image_dark : entities.image_light;
    const fallbackId = dark ? entities.image_light : entities.image_dark;
    let picture = null;
    let pictureKey = "placeholder";
    // The saved configurator render (the car as ordered) wins over Rivian's
    // legacy app images, which the API no longer returns for every vehicle.
    for (const id of [entities.picture, preferredId, fallbackId]) {
      if (!id || !hass) continue;
      const stateObj = hass.states[id];
      const url = stateObj && stateObj.attributes && stateObj.attributes.entity_picture;
      if (url) {
        picture = url;
        // entity_picture's access token rotates every few minutes; keying on
        // the image's own state (its last-updated time) avoids re-downloading
        // an unchanged image on every rotation.
        pictureKey = `${id}|${stateObj.state}`;
        break;
      }
    }
    const isRender = !!(picture && entities.picture && pictureKey.startsWith(`${entities.picture}|`));
    if (!force && v.pictureKey === pictureKey) return;
    v.pictureKey = pictureKey;
    v.imageWrap.textContent = "";
    v.imageWrap.classList.toggle("roc-render-wrap", isRender);
    if (picture) {
      const img = document.createElement("img");
      img.src = picture;
      img.alt = "";
      v.imageWrap.appendChild(img);
    } else {
      const icon = document.createElement("ha-icon");
      icon.setAttribute("icon", "mdi:car-electric");
      v.imageWrap.appendChild(icon);
    }
  }

  _renderStatus(v) {
    const hass = this._hass;
    const entities = v.entities;

    // Battery block
    const socObj = entities.soc && hass ? hass.states[entities.soc] : null;
    const socValue = _numericState(socObj);
    const limitObj = entities.soc_limit && hass ? hass.states[entities.soc_limit] : null;
    const limitValue = _numericState(limitObj);
    v.batteryFill.style.width = `${Math.max(0, Math.min(100, socValue ?? 0))}%`;
    v.batteryFill.style.background = socColor(socValue);
    if (limitValue !== null) {
      v.batteryTick.style.display = "block";
      v.batteryTick.style.left = `${Math.max(0, Math.min(100, limitValue))}%`;
    } else {
      v.batteryTick.style.display = "none";
    }
    const socText = socValue !== null ? `${Math.round(socValue)}%` : "–";
    const rangeObj = entities.range && hass ? hass.states[entities.range] : null;
    const rangeText = _stateText(hass, rangeObj);
    _escapeText(v.batteryText, rangeText ? `${socText}  ·  ${rangeText}` : socText);

    // Chips
    v.chipsEl.textContent = "";
    for (const chip of this._buildChips(v)) {
      v.chipsEl.appendChild(this._renderChip(chip));
    }
  }

  _buildChips(v) {
    const hass = this._hass;
    const entities = v.entities;
    const chips = [];

    if (entities.location && hass) {
      const stateObj = hass.states[entities.location];
      if (stateObj && stateObj.state !== "unavailable" && stateObj.state !== "unknown") {
        const label = locationLabel(stateObj.state);
        const icon =
          stateObj.state === "home"
            ? "mdi:home"
            : stateObj.state === "not_home"
              ? "mdi:map-marker-outline"
              : "mdi:map-marker";
        chips.push({ icon, text: label, entityId: entities.location });
      }
    }

    if (entities.locked && hass) {
      const stateObj = hass.states[entities.locked];
      if (stateObj && stateObj.state !== "unavailable" && stateObj.state !== "unknown") {
        // A lock-class binary sensor is "on" when UNLOCKED (HA convention;
        // the Rivian locked_state sensor uses on_value="unlocked").
        if (stateObj.state === "on") {
          chips.push({
            icon: "mdi:lock-open-variant",
            text: "Unlocked",
            entityId: entities.locked,
            variant: "warning",
          });
        } else {
          chips.push({ icon: "mdi:lock", text: "Locked", entityId: entities.locked });
        }
      }
    }

    const chargingObj = entities.charging && hass ? hass.states[entities.charging] : null;
    if (chargingObj && chargingObj.state === "on") {
      const rateObj = entities.charging_rate && hass ? hass.states[entities.charging_rate] : null;
      const rateText = _stateText(hass, rateObj);
      chips.push({
        icon: "mdi:ev-station",
        text: rateText ? `Charging · ${rateText}` : "Charging",
        entityId: entities.charging,
        variant: "success",
      });
    } else {
      const pluggedObj = entities.plugged_in && hass ? hass.states[entities.plugged_in] : null;
      if (pluggedObj && pluggedObj.state === "on") {
        chips.push({ icon: "mdi:power-plug", text: "Plugged in", entityId: entities.plugged_in });
      }
    }

    const driveStatusId = entities.drive_status || entities.gear;
    if (driveStatusId && hass) {
      const stateObj = hass.states[driveStatusId];
      const text = _stateText(hass, stateObj);
      if (text) {
        const driving = /driv/i.test(stateObj.state);
        chips.push({
          icon: driving ? "mdi:car-electric" : "mdi:parking",
          text,
          entityId: driveStatusId,
        });
      }
    }

    if (entities.odometer && hass) {
      const stateObj = hass.states[entities.odometer];
      const text = _stateText(hass, stateObj);
      if (text) {
        chips.push({ icon: "mdi:counter", text, entityId: entities.odometer });
      }
    }

    return chips;
  }

  _renderChip(chip) {
    const el = document.createElement(chip.entityId ? "button" : "span");
    el.className = "roc-chip";
    if (chip.entityId) {
      el.type = "button";
      el.classList.add("roc-chip-clickable");
      el.addEventListener("click", () => this._openMoreInfo(chip.entityId));
    }
    el.title = overviewChipTitle(chip);
    if (chip.variant === "success") el.classList.add("roc-chip-success");
    if (chip.variant === "warning") el.classList.add("roc-chip-warning");
    const icon = document.createElement("ha-icon");
    icon.setAttribute("icon", chip.icon);
    el.appendChild(icon);
    el.appendChild(document.createTextNode(chip.text));
    return el;
  }

  // -- Stats -------------------------------------------------------------

  _subscribe(v) {
    if (v.unsubPromise || !this._hass || !v.vin) return;
    v.unsubPromise = this._hass.connection
      .subscribeMessage(() => this._fetchStats(v), { type: "rivian/analytics/subscribe", vin: v.vin })
      .catch((err) => {
        console.warn("rivian-overview-card: live updates unavailable", err);
        return null;
      });
  }

  _unsubscribe(v) {
    if (!v.unsubPromise) return;
    v.unsubPromise.then((unsub) => unsub && unsub()).catch(() => {});
    v.unsubPromise = null;
  }

  async _fetchStats(v) {
    if (!v.vin || !this._hass) return;
    const seq = ++v.statsSeq;
    try {
      const result = await this._hass.callWS({ type: "rivian/analytics/summary", vin: v.vin });
      if (seq !== v.statsSeq) return;
      v.windows = result.windows || {};
      v.lastDrive = result.last_drive || null;
      v.statsError = null;
      this._scheduleHousehold();
      this._renderImage(v);
      this._renderStatus(v);
      this._renderStats(v);
    } catch (err) {
      if (seq !== v.statsSeq) return;
      console.error("rivian-overview-card:", err);
      v.windows = null;
      v.lastDrive = null;
      v.statsError = err;
      this._renderStats(v);
    }
  }

  _renderStats(v) {
    v.statsEl.textContent = "";
    v.lastDriveEl.textContent = "";
    v.lastDriveEl.style.display = "none";

    if (!v.vin) {
      v.statsErrorEl.style.display = "none";
      return;
    }

    if (v.statsError) {
      v.statsErrorEl.style.display = "block";
      _escapeText(v.statsErrorEl, "Stats unavailable");
      return;
    }
    if (!v.windows) {
      v.statsErrorEl.style.display = "none";
      return;
    }
    v.statsErrorEl.style.display = "none";

    const thead = document.createElement("thead");
    const headRow = document.createElement("tr");
    const corner = document.createElement("th");
    headRow.appendChild(corner);
    for (const header of WINDOW_HEADERS) {
      const th = document.createElement("th");
      _escapeText(th, header);
      th.title = STAT_WINDOW_TITLES[header] || header;
      headRow.appendChild(th);
    }
    thead.appendChild(headRow);
    v.statsEl.appendChild(thead);

    const tbody = document.createElement("tbody");
    for (const row of summaryRows(v.windows)) {
      const tr = document.createElement("tr");
      if (row.narrowSkip) tr.classList.add("roc-stats-mpge");
      const labelCell = document.createElement("td");
      _escapeText(labelCell, row.label);
      labelCell.title = STAT_ROW_TITLES[row.label] || row.label;
      tr.appendChild(labelCell);
      for (const value of row.values) {
        const td = document.createElement("td");
        _escapeText(td, value);
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
    v.statsEl.appendChild(tbody);

    const text = lastDriveText(v.lastDrive);
    if (text) {
      const clickable = !!(this._config && this._config.drives_path);
      if (clickable) v.lastDriveEl.type = "button";
      v.lastDriveEl.style.display = "flex";
      const label = document.createElement("span");
      _escapeText(label, text);
      v.lastDriveEl.appendChild(label);
      v.lastDriveEl.title = clickable ? "Last drive — tap to open the Drives tab" : "Last recorded drive";
      if (clickable) {
        const chevron = document.createElement("ha-icon");
        chevron.setAttribute("icon", "mdi:chevron-right");
        v.lastDriveEl.appendChild(chevron);
        v.lastDriveEl.onclick = () => this._navigateToDrives();
      }
    }
  }
}

if (typeof customElements !== "undefined") {
  if (!customElements.get("rivian-overview-card")) {
    customElements.define("rivian-overview-card", RivianOverviewCard);
  }
  window.customCards = window.customCards || [];
  if (!window.customCards.some((c) => c.type === "rivian-overview-card")) {
    window.customCards.push({
      type: "rivian-overview-card",
      name: "Rivian Overview",
      description: "Lists Rivian vehicles with a picture, live status, and driving stats.",
    });
  }
}
