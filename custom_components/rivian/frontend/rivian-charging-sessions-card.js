/**
 * rivian-charging-sessions-card.js
 *
 * The Charging tab's DC fast-charge session list: date/time, start -> end
 * SoC, kWh added, peak kW and duration, newest first, with an admin-only
 * per-row delete. Sits above the Charging tab's entity tiles and Plotly
 * charts (see dashboard_generator.py's `_build_charging_cards`).
 *
 * Backend calls (all via `hass.callWS`; reads take `vins`, the delete a single `vin`):
 *   - `rivian/analytics/series` with `series: ["dcfc"]` -- open to all users;
 *     reads `result.dcfc`, the same payload the DCFC Plotly charts use.
 *   - `rivian/charging/delete_session` -- admin only (enforced server-side;
 *     this card also hides the control from non-admins via
 *     `hass.user.is_admin`). Fires `rivian_analytics_updated`, which the
 *     Plotly DCFC charts (via rivian-series-card.js) already refetch on.
 *   - `rivian/analytics/subscribe` -- refreshes this list on live updates.
 *
 * The card follows the shared vehicle selection (rivian-vehicle-bar.js) unless
 * its config pins `vin`/`vins`: sessions of every selected vehicle are listed
 * together (the Charging tab's vehicle bar card is the selector), each row carrying its vehicle's color dot and letter when several
 * vehicles are in view, and a delete passes that row's own `vin`.
 *
 * A number of pure helpers are exported purely so a Node smoke test
 * (tests/frontend/charging_sessions_card.test.mjs) can import and exercise
 * them without a DOM or customElements environment. The module guards every
 * top-level use of HTMLElement/customElements/window/document so it can be
 * imported under plain Node, mirroring rivian-places-card.js.
 */

/** Newest-first by start_time; sessions missing a start_time sort last. */
export function sortSessionsNewestFirst(sessions) {
  const list = Array.isArray(sessions) ? sessions.slice() : [];
  const ts = (s) => {
    const t = s && s.start_time ? Date.parse(s.start_time) : NaN;
    return Number.isNaN(t) ? -Infinity : t;
  };
  return list.sort((a, b) => ts(b) - ts(a));
}

/** The vehicle (from `rivian/vehicles/list`) a combined-series session belongs to, or null. */
export function sessionVehicle(session, vehicles) {
  const vin = session && session.vin;
  return (Array.isArray(vehicles) ? vehicles : []).find((v) => v.vin === vin) || null;
}

/** Load the shared vehicle bar/selection module with this module's own cache-buster. */
function _loadBarModule() {
  return import(new URL("./rivian-vehicle-bar.js" + new URL(import.meta.url).search, import.meta.url));
}

/** "Sep 20, 3:35 PM" in the given IANA time zone, or "–" when unparseable. */
export function formatSessionTime(isoTime, tz) {
  if (!isoTime) return "–";
  const d = new Date(isoTime);
  if (Number.isNaN(d.getTime())) return "–";
  try {
    return new Intl.DateTimeFormat(undefined, {
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
      timeZone: tz,
    }).format(d);
  } catch (_err) {
    return d.toLocaleString();
  }
}

/** "Sep 20" in the given IANA time zone, or "–" when unparseable -- used in the delete confirm. */
function _shortDate(isoTime, tz) {
  if (!isoTime) return "–";
  const d = new Date(isoTime);
  if (Number.isNaN(d.getTime())) return "–";
  try {
    return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", timeZone: tz }).format(d);
  } catch (_err) {
    return d.toDateString();
  }
}

/** "32 min" / "1:05" (h:mm) duration between two ISO timestamps, or "–" if either is missing/invalid. */
export function formatSessionDuration(startTime, endTime) {
  if (!startTime || !endTime) return "–";
  const start = Date.parse(startTime);
  const end = Date.parse(endTime);
  if (Number.isNaN(start) || Number.isNaN(end) || end < start) return "–";
  const totalMinutes = Math.round((end - start) / 60000);
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (hours > 0) return `${hours}:${String(minutes).padStart(2, "0")}`;
  return `${minutes} min`;
}

/**
 * The `window.confirm` text for deleting one DC fast-charge session, e.g.
 * "Delete the DC fast-charge session on Sep 26 (45% -> 81%, 51.5 kWh)? This
 * can't be undone."
 */
export function deleteSessionMessage(session, tz) {
  const dateLabel = _shortDate(session && session.start_time, tz);
  const startSoc = typeof (session && session.start_soc) === "number" ? Math.round(session.start_soc) : "?";
  const endSoc = typeof (session && session.end_soc) === "number" ? Math.round(session.end_soc) : "?";
  const kwh =
    typeof (session && session.energy_added_kwh) === "number" ? session.energy_added_kwh.toFixed(1) : "?";
  return `Delete the DC fast-charge session on ${dateLabel} (${startSoc}% → ${endSoc}%, ${kwh} kWh)? This can't be undone.`;
}

/** Hover/tap explanations for a session row's stat columns. */
export const SESSION_STAT_TITLES = {
  Added: "Energy added to the battery during the session, in kWh",
  Peak: "Highest charging power seen during the session, in kW",
  Duration: "Plug-in to unplug time (h:mm, or minutes when under an hour)",
};

/** Tooltip for a session row's SoC line, e.g. "Battery 45% at start, 81% at end". */
export function sessionSocTitle(session) {
  const f = (v) => (typeof v === "number" ? `${Math.round(v)}%` : "unknown");
  return `Battery ${f(session && session.start_soc)} at start, ${f(session && session.end_soc)} at end`;
}

// -- DOM-dependent card -------------------------------------------------------

function _escapeText(el, text) {
  el.textContent = text === null || text === undefined ? "" : String(text);
}

const _CARD_STYLE = `
  :host { display: block; }
  ha-card {
    padding: 0;
    background: var(--ha-card-background, var(--card-background-color, #fff));
    color: var(--primary-text-color, #212121);
  }
  .rcs-vdot {
    flex: none;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 22px;
    height: 22px;
    border-radius: 50%;
    font-size: 12px;
    font-weight: 700;
  }
  .rcs-header {
    padding: 12px 16px 4px;
    font-weight: 500;
  }
  .rcs-empty, .rcs-error {
    padding: 16px;
    color: var(--secondary-text-color);
  }
  .rcs-error {
    color: var(--error-color, #b00020);
  }
  .rcs-list {
    display: flex;
    flex-direction: column;
  }
  .rcs-row {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 8px 16px;
    border-top: 1px solid var(--divider-color, #e0e0e0);
    flex-wrap: wrap;
  }
  .rcs-row-main {
    display: flex;
    flex-direction: column;
    min-width: 120px;
    flex: 1 1 140px;
  }
  .rcs-row-time {
    font-weight: 500;
  }
  .rcs-row-soc {
    font-size: 0.85em;
    color: var(--secondary-text-color);
  }
  .rcs-row-stat {
    min-width: 70px;
    text-align: right;
    font-size: 0.9em;
  }
  .rcs-row-stat-label {
    display: block;
    font-size: 0.72em;
    color: var(--secondary-text-color);
  }
  .rcs-delete-btn {
    font: inherit;
    font-size: 0.8em;
    padding: 4px 10px;
    border-radius: 4px;
    border: 1px solid var(--error-color, #db4437);
    background: var(--card-background-color, #fff);
    color: var(--error-color, #db4437);
    cursor: pointer;
  }
  @media (max-width: 500px) {
    .rcs-row { gap: 6px; }
    .rcs-row-stat { min-width: 50px; }
  }
`;

const BaseElement = typeof HTMLElement === "undefined" ? class {} : HTMLElement;

class RivianChargingSessionsCard extends BaseElement {
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
    const fixed = RivianChargingSessionsCard._fixedVins;
    const vinChanged = !!this._config && JSON.stringify(fixed(this._config)) !== JSON.stringify(fixed(next));
    this._config = next;
    if (!this._built) {
      this._build();
    } else if (vinChanged) {
      this._unsubscribe();
      this._sessions = [];
      this._started = false;
      if (this._hass) this.hass = this._hass;
    }
  }

  getCardSize() {
    return 3;
  }

  get hass() {
    return this._hass;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._built) return;
    if (!this._started) {
      this._started = true;
      this._start().catch((err) => this._showError(err));
    }
  }

  get _isAdmin() {
    return !!(this._hass && this._hass.user && this._hass.user.is_admin);
  }

  _build() {
    this._built = true;
    this._started = false;
    this._sessions = [];
    this._vins = [];
    this._vehicleList = [];
    this._bar = null;
    this._unsubSelection = null;

    this.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = _CARD_STYLE;
    this.shadowRoot.appendChild(style);

    this._card = document.createElement("ha-card");
    this.shadowRoot.appendChild(this._card);

    const header = document.createElement("div");
    header.className = "rcs-header";
    _escapeText(header, "DC Fast-Charge Sessions");
    this._card.appendChild(header);

    this._listEl = document.createElement("div");
    this._listEl.className = "rcs-list";
    this._card.appendChild(this._listEl);
  }

  connectedCallback() {
    if (this._hass && !this._started) {
      this._started = true;
      this._start().catch((err) => this._showError(err));
    }
    if (this._hass) this._subscribe();
    if (this._bar && !this._unsubSelection && this._followStore) {
      this._unsubSelection = this._bar.onSelectionChange((vins) => this._onSelectionChanged(vins));
    }
  }

  disconnectedCallback() {
    this._unsubscribe();
    if (this._unsubSelection) this._unsubSelection();
    this._unsubSelection = null;
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
        console.warn("rivian-charging-sessions-card: live updates unavailable", err);
        return null;
      });
  }

  _unsubscribe() {
    if (!this._unsubPromise) return;
    this._unsubPromise.then((unsub) => unsub && unsub()).catch(() => {});
    this._unsubPromise = null;
  }

  async _start() {
    await this._initScope();
    if (!this._vins.length) {
      this._sessions = [];
      this._render();
      return;
    }
    await this._refresh();
    this._subscribe();
  }

  /** Which vehicles to list: the config's fixed set, else the shared selection (with the vehicle bar). */
  async _initScope() {
    const fixed = RivianChargingSessionsCard._fixedVins(this._config);
    try {
      this._bar = await _loadBarModule();
      this._vehicleList = await this._bar.getVehicles(this._hass);
    } catch (err) {
      console.warn("rivian-charging-sessions-card: vehicle list unavailable", err);
    }
    this._followStore = !fixed;
    if (fixed) {
      this._vins = fixed;
      return;
    }
    try {
      this._vins = this._bar ? await this._bar.getSelection(this._hass) : [];
    } catch (err) {
      console.warn("rivian-charging-sessions-card: vehicle selection unavailable", err);
      this._vins = [];
    }
    if (this._bar && !this._unsubSelection) {
      this._unsubSelection = this._bar.onSelectionChange((vins) => this._onSelectionChanged(vins));
    }
  }

  _onSelectionChanged(vins) {
    if (!this._followStore || !this._bar) return;
    const next = this._bar.normalizeSelection(vins, this._vehicleList);
    if (this._bar.sameSelection(next, this._vins)) return;
    this._unsubscribe();
    this._vins = next;
    this._subscribe();
    this._refresh().catch((err) => this._showError(err));
  }

  async _refresh() {
    const result = await this._hass.callWS({
      type: "rivian/analytics/series",
      vins: [...this._vins],
      series: ["dcfc"],
    });
    this._sessions = sortSessionsNewestFirst((result && result.dcfc) || []);
    this._render();
  }

  _showError(err) {
    console.error("rivian-charging-sessions-card", err);
    this._card.textContent = "";
    const el = document.createElement("div");
    el.className = "rcs-error";
    _escapeText(el, `rivian-charging-sessions-card: ${err && err.message ? err.message : err}`);
    this._card.appendChild(el);
  }

  _render() {
    const tz = this._hass && this._hass.config ? this._hass.config.time_zone : undefined;
    this._listEl.textContent = "";
    if (!this._sessions.length) {
      const empty = document.createElement("div");
      empty.className = "rcs-empty";
      _escapeText(empty, "No DC fast-charge sessions recorded yet.");
      this._listEl.appendChild(empty);
      return;
    }
    const dark = !!(this._hass && this._hass.themes && this._hass.themes.darkMode);
    const showVehicle = this._vins.length > 1;
    for (const session of this._sessions) {
      const vehicle = showVehicle ? sessionVehicle(session, this._vehicleList) : null;
      this._listEl.appendChild(this._buildRow(session, tz, vehicle, dark));
    }
  }

  _buildRow(session, tz, vehicle = null, dark = false) {
    const row = document.createElement("div");
    row.className = "rcs-row";

    if (vehicle) {
      // Several vehicles in view: a dot with the vehicle's letter, in its color.
      const color = (dark ? vehicle.color_dark || vehicle.color : vehicle.color) || "#888888";
      const dot = document.createElement("span");
      dot.className = "rcs-vdot";
      dot.style.background = color;
      dot.style.color = this._bar ? this._bar.inkOn(color) : "#ffffff";
      dot.title = vehicle.name || vehicle.model || "";
      dot.setAttribute("aria-label", dot.title);
      _escapeText(dot, vehicle.letter || "");
      row.appendChild(dot);
    }

    const main = document.createElement("div");
    main.className = "rcs-row-main";
    const time = document.createElement("div");
    time.className = "rcs-row-time";
    _escapeText(time, formatSessionTime(session.start_time, tz));
    const soc = document.createElement("div");
    soc.className = "rcs-row-soc";
    const startSoc = typeof session.start_soc === "number" ? Math.round(session.start_soc) : "?";
    const endSoc = typeof session.end_soc === "number" ? Math.round(session.end_soc) : "?";
    _escapeText(soc, `${startSoc}% → ${endSoc}%`);
    soc.title = sessionSocTitle(session);
    main.appendChild(time);
    main.appendChild(soc);
    row.appendChild(main);

    const stats = [
      ["Added", typeof session.energy_added_kwh === "number" ? `${session.energy_added_kwh.toFixed(1)} kWh` : "–"],
      ["Peak", typeof session.max_power_kw === "number" ? `${Math.round(session.max_power_kw)} kW` : "–"],
      ["Duration", formatSessionDuration(session.start_time, session.end_time)],
    ];
    for (const [label, value] of stats) {
      const stat = document.createElement("div");
      stat.className = "rcs-row-stat";
      stat.title = SESSION_STAT_TITLES[label] || label;
      const valEl = document.createElement("div");
      _escapeText(valEl, value);
      const labEl = document.createElement("div");
      labEl.className = "rcs-row-stat-label";
      _escapeText(labEl, label);
      stat.appendChild(valEl);
      stat.appendChild(labEl);
      row.appendChild(stat);
    }

    if (this._isAdmin) {
      const deleteBtn = document.createElement("button");
      deleteBtn.type = "button";
      deleteBtn.className = "rcs-delete-btn";
      _escapeText(deleteBtn, "Delete");
      const delLabel = `Delete the session on ${formatSessionTime(session.start_time, tz)}`;
      deleteBtn.title = delLabel;
      deleteBtn.setAttribute("aria-label", delLabel);
      deleteBtn.addEventListener("click", () => {
        if (!window.confirm(deleteSessionMessage(session, tz))) return;
        this._deleteSession(session.session_id, session.vin).catch((err) => this._showError(err));
      });
      row.appendChild(deleteBtn);
    }

    return row;
  }

  async _deleteSession(sessionId, vin) {
    await this._hass.callWS({
      type: "rivian/charging/delete_session",
      vin: vin || this._vins[0],
      session_id: sessionId,
    });
    await this._refresh();
  }
}

if (typeof customElements !== "undefined" && !customElements.get("rivian-charging-sessions-card")) {
  customElements.define("rivian-charging-sessions-card", RivianChargingSessionsCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "rivian-charging-sessions-card",
    name: "Rivian Charging Sessions Card",
    description: "Lists DC fast-charge sessions with per-row delete (admin only).",
  });
}
