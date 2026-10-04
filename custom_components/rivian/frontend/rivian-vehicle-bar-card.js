/**
 * rivian-vehicle-bar-card.js
 *
 * `custom:rivian-vehicle-bar-card`: a thin card wrapping `<rivian-vehicle-bar>`
 * for dashboard tabs that are not panel cards (Charging, Efficiency). The
 * panel cards (Drives, Places, Routes) draw the bar in their own headers.
 */

/** Load the shared bar module with this module's own cache-buster query. */
function _loadBarModule() {
  return import(new URL("./rivian-vehicle-bar.js" + new URL(import.meta.url).search, import.meta.url));
}

const BaseElement = typeof HTMLElement === "undefined" ? class {} : HTMLElement;

class RivianVehicleBarCard extends BaseElement {
  static getStubConfig() {
    return {};
  }

  setConfig(config) {
    this._config = config || {};
    if (!this._built) this._build();
  }

  getCardSize() {
    return 1;
  }

  get hass() {
    return this._hass;
  }

  set hass(hass) {
    this._hass = hass;
    if (this._bar) this._bar.hass = hass;
    else if (this._built) this._mountBar();
  }

  _build() {
    this._built = true;
    this.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = `
      :host { display: block; }
      .vbc { padding: 4px 4px 8px; }
    `;
    this.shadowRoot.appendChild(style);
    this._wrap = document.createElement("div");
    this._wrap.className = "vbc";
    this.shadowRoot.appendChild(this._wrap);
    if (this._hass) this._mountBar();
  }

  async _mountBar() {
    if (this._mounting || this._bar) return;
    this._mounting = true;
    try {
      await _loadBarModule();
      const bar = document.createElement("rivian-vehicle-bar");
      this._wrap.appendChild(bar);
      this._bar = bar;
      bar.hass = this._hass;
    } catch (err) {
      console.error("rivian-vehicle-bar-card:", err);
    } finally {
      this._mounting = false;
    }
  }
}

if (typeof customElements !== "undefined") {
  if (!customElements.get("rivian-vehicle-bar-card")) {
    customElements.define("rivian-vehicle-bar-card", RivianVehicleBarCard);
  }
  window.customCards = window.customCards || [];
  if (!window.customCards.some((c) => c.type === "rivian-vehicle-bar-card")) {
    window.customCards.push({
      type: "rivian-vehicle-bar-card",
      name: "Rivian Vehicle Bar",
      description: "Pick which Rivian vehicles the dashboard shows.",
    });
  }
}
