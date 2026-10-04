/**
 * rivian-series-card.js
 *
 * Thin wrapper around the vendored `plotly-graph` card (plotly-graph-card.js).
 *
 * Problem: bulk time-series analytics (drives, 3-minute chunks, vampire drain,
 * DCFC curves) used to live in HA entity state attributes so that plotly-graph's
 * synchronous `$ex` expressions could read them. That blew past the recorder's
 * attribute size limit. Now the data comes from a WebSocket command instead,
 * but `$ex` expressions are evaluated synchronously by plotly-graph (it does
 * `t[n]=i=i(this.fnParam)` with no await), so an `$ex` cannot itself await a
 * fetch. This card fetches the data first, stashes it on `window.__rivianAnalytics`
 * where the synchronous `$ex` expressions can read it, and only then creates the
 * real plotly-graph card (unmodified) so its `$ex` expressions see complete data
 * on their very first synchronous evaluation.
 */

// Module-level in-flight request map so N cards on one dashboard requesting the
// same vin/series/days share a single WebSocket round trip instead of firing
// one each.
const _inFlight = new Map();

function _cacheKey(vin, series, days) {
  return `${vin}|${[...series].sort().join(",")}|${days}`;
}

function _fetchSeries(hass, vin, series, days) {
  const key = _cacheKey(vin, series, days);
  let promise = _inFlight.get(key);
  if (!promise) {
    promise = hass
      .callWS({ type: "rivian/analytics/series", vin, series, days })
      .finally(() => {
        _inFlight.delete(key);
      });
    _inFlight.set(key, promise);
  }
  return promise;
}

function _bustInFlight(vin) {
  for (const key of _inFlight.keys()) {
    if (key.startsWith(`${vin}|`)) _inFlight.delete(key);
  }
}

class RivianSeriesCard extends HTMLElement {
  setConfig(config) {
    if (!config || !config.vin) {
      throw new Error("rivian-series-card: `vin` is required in config");
    }
    if (!config.card || typeof config.card !== "object") {
      throw new Error("rivian-series-card: `card` (the wrapped plotly-graph config) is required");
    }
    this._config = config;
    this._series = Array.isArray(config.series) ? config.series : ["drives", "chunks", "vampire", "dcfc"];
    this._days = config.days || 90;
    // Do not build DOM here; wait for hass.
  }

  set hass(hass) {
    this._hass = hass;
    if (this._childCard) {
      // Already rendering: keep the child card alive and updating.
      this._childCard.hass = hass;
      return;
    }
    if (this._starting) return; // guard re-entrancy; `set hass` fires very frequently
    this._starting = true;
    this._start(hass).catch((err) => {
      this._starting = false;
      console.error("rivian-series-card: failed to initialize", err);
      this._renderError();
    });
  }

  get hass() {
    return this._hass;
  }

  async _start(hass) {
    await this._refresh(hass);
    this._subscribe(hass);
    this._starting = false;
  }

  async _refresh(hass) {
    const vin = this._config.vin;
    const result = await _fetchSeries(hass, vin, this._series, this._days);

    // Merge, don't replace: another card on the same dashboard may have
    // requested a different subset of `series` for the same vin and already
    // populated part of this cache entry.
    window.__rivianAnalytics = window.__rivianAnalytics || {};
    window.__rivianAnalytics[vin] = Object.assign({}, window.__rivianAnalytics[vin], result);

    // Only after the cache is populated do we create (or reconfigure) the
    // Plotly card, so its synchronous `$ex` expressions see complete data the
    // first time they run.
    if (!this._childCard) {
      const card = document.createElement("plotly-graph");
      card.hass = hass;
      card.setConfig(this._config.card);
      this._teardownError();
      this.appendChild(card);
      this._childCard = card;
    } else {
      // Repeated setConfig on plotly-graph-card is safe: it just reassigns
      // its internal config and re-derives render state, no teardown needed.
      this._childCard.hass = hass;
      this._childCard.setConfig(this._config.card);
    }
  }

  _subscribe(hass) {
    if (this._unsubPromise) return;
    this._unsubPromise = hass.connection.subscribeEvents((event) => {
      const data = event && event.data;
      if (!data || data.vin !== this._config.vin) return;
      _bustInFlight(this._config.vin);
      this._refresh(this._hass).catch((err) => {
        console.error("rivian-series-card: failed to refresh after update event", err);
        this._renderError();
      });
    }, "rivian_analytics_updated");
  }

  _renderError() {
    this._teardownError();
    const box = document.createElement("div");
    box.style.padding = "16px";
    box.style.color = "var(--primary-text-color)";
    box.style.background = "var(--card-background-color)";
    box.textContent = "Rivian analytics data could not be loaded right now.";
    this.appendChild(box);
    this._errorBox = box;
  }

  _teardownError() {
    if (this._errorBox) {
      this._errorBox.remove();
      this._errorBox = null;
    }
  }

  getCardSize() {
    if (this._childCard && typeof this._childCard.getCardSize === "function") {
      return this._childCard.getCardSize();
    }
    return 6;
  }

  connectedCallback() {
    // HA moves cards through the DOM on view switches, which fires
    // disconnectedCallback; without re-subscribing here the card would stop
    // receiving refresh events for the rest of the session.
    if (this._hass && !this._unsubPromise) this._subscribe(this._hass);
  }

  disconnectedCallback() {
    if (this._unsubPromise) {
      this._unsubPromise.then((unsub) => unsub()).catch(() => {});
      this._unsubPromise = null;
    }
  }
}

customElements.define("rivian-series-card", RivianSeriesCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: "rivian-series-card",
  name: "Rivian Series Card",
  description: "Fetches Rivian trip/charge analytics via WebSocket and hands them to a wrapped plotly-graph card.",
});
