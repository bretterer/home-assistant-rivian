/**
 * Rivian trip card: the route of a vehicle's active navigation trip on a map.
 *
 * Served and loaded by the Rivian integration. Configuration:
 *
 *   type: custom:rivian-trip-card
 *   entity: sensor.<vehicle>_destination   # required, the Destination sensor
 *   title: Trip                            # optional card header
 *   vehicle: device_tracker.<vehicle>      # optional, found automatically
 *
 * It uses Home Assistant's own map (tiles, theme and markers). While the vehicle
 * is navigating, it shows the route with the vehicle, its stops numbered in
 * trip order and the destination; otherwise, it follows the vehicle's location.
 * It can be set up with the visual editor.
 *
 * The route and numbered stops are drawn through the map's engine. That's
 * internal to Home Assistant's map, so when it isn't available, the route falls
 * back to the map's paths, which mark every point (thinned, with the estimated
 * time it's passed), and the next stop and destination are shown as entities.
 */

const CARD_TYPE = "rivian-trip-card";
const EDITOR_TYPE = "rivian-trip-card-editor";
// The map's paths mark every point, so for them a route is thinned to this many
const MAX_ROUTE_POINTS = 100;
const MAP_LOAD_TIMEOUT_MS = 10000;

/** Decode a Google-encoded polyline into [latitude, longitude] pairs. */
export function decodePolyline(encoded, precision = 5) {
  const factor = 10 ** precision;
  const points = [];
  let index = 0;
  let lat = 0;
  let lng = 0;
  while (index < encoded.length) {
    for (const axis of [0, 1]) {
      let result = 0;
      let shift = 0;
      let byte;
      do {
        byte = encoded.charCodeAt(index++) - 63;
        result |= (byte & 0x1f) << shift;
        shift += 5;
      } while (byte >= 0x20 && index < encoded.length);
      const delta = result & 1 ? ~(result >> 1) : result >> 1;
      if (axis === 0) lat += delta;
      else lng += delta;
    }
    points.push([lat / factor, lng / factor]);
  }
  return points;
}

/**
 * Decode a route polyline. Rivian's precision isn't documented, so use the
 * first precision whose coordinates are all valid (5 is the Google default).
 */
export function decodeRoute(encoded) {
  if (typeof encoded !== "string" || !encoded) return [];
  for (const precision of [5, 6]) {
    const points = decodePolyline(encoded, precision);
    if (
      points.length >= 2 &&
      points.every(([lat, lng]) => Math.abs(lat) <= 90 && Math.abs(lng) <= 180)
    ) {
      return points;
    }
  }
  return [];
}

/** Approximate distance between two [lat, lng] points, in kilometers. */
function distanceKm([lat1, lng1], [lat2, lng2]) {
  const rad = Math.PI / 180;
  const x = (lng2 - lng1) * rad * Math.cos(((lat1 + lat2) / 2) * rad);
  const y = (lat2 - lat1) * rad;
  return Math.sqrt(x * x + y * y) * 6371;
}

/**
 * Thin a route to at most `maxPoints` evenly spaced (by distance) points,
 * keeping the first and last, each with its distance along the route.
 */
export function thinRoute(points, maxPoints = MAX_ROUTE_POINTS) {
  if (points.length === 0) return [];
  const along = [0];
  for (let i = 1; i < points.length; i++) {
    along.push(along[i - 1] + distanceKm(points[i - 1], points[i]));
  }
  const total = along[along.length - 1];
  if (points.length <= maxPoints || total === 0) {
    return points.map((point, i) => ({ point, along: along[i], total }));
  }
  const spacing = total / (maxPoints - 1);
  const kept = [{ point: points[0], along: 0, total }];
  let next = spacing;
  for (let i = 1; i < points.length - 1; i++) {
    if (along[i] >= next) {
      kept.push({ point: points[i], along: along[i], total });
      next = along[i] + spacing;
    }
  }
  kept.push({ point: points[points.length - 1], along: total, total });
  return kept;
}

/**
 * Build the map path: the thinned route, each point with the estimated time
 * the vehicle passes it (spread between the trip start and arrival by
 * distance), which the map shows when hovering a point.
 */
export function buildRoutePath(encoded, tripStart, arrival, now = new Date()) {
  const thinned = thinRoute(decodeRoute(encoded));
  if (thinned.length < 2) return null;
  const start = toDate(tripStart) ?? now;
  const end = toDate(arrival) ?? start;
  const span = Math.max(0, end - start);
  return {
    name: "Route",
    points: thinned.map(({ point, along, total }) => ({
      point,
      timestamp: new Date(start.getTime() + (total ? (span * along) / total : 0)),
    })),
  };
}

function toDate(value) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** The route as a map engine path: just the line, as the stops are numbered markers. */
export function buildRouteLayer(encoded, color) {
  const points = decodeRoute(encoded);
  if (points.length < 2) return null;
  return { color, segments: [{ points }], markers: [] };
}

const sameLocation = (a, b) =>
  Boolean(a && b) && a.latitude === b.latitude && a.longitude === b.longitude;

/**
 * The stops' markers, numbered in trip order, each with its name and arrival
 * time. The next stop (flagged by the Destination sensor) stands out; the stop
 * at `destination` is left out, as it's shown as the destination entity.
 */
export function stopMarkers(stops, { destination, formatTime }) {
  return (stops ?? [])
    .filter(
      (stop) =>
        typeof stop.latitude === "number" &&
        typeof stop.longitude === "number" &&
        !sameLocation(stop, destination)
    )
    .map((stop, index) => {
      const name = stop.name || (stop.charging ? "Charging stop" : "Stop");
      const arrival = toDate(stop.arrival);
      const time = arrival ? formatTime(arrival) : null;
      return {
        number: index + 1,
        location: [stop.latitude, stop.longitude],
        name,
        time,
        title: `${index + 1}. ${name}${time ? ` · ${time}` : ""}`,
        charging: Boolean(stop.charging),
        next: Boolean(stop.next),
      };
    });
}

/** Whether the map's engine can draw the route with numbered stops. */
function canNumberStops(engine) {
  return typeof engine?.addPath === "function" && typeof engine?.addMarker === "function";
}

// The stop tooltip opened by a tap, closed when another opens
let openTooltip;

/**
 * A numbered stop marker, with a tooltip of the stop's name and arrival time
 * shown on hover or keyboard focus, and toggled by a tap. Styled inline, as
 * it's placed inside the map.
 */
function markerElement({ number, name, time, charging, next }, { color, chargingColor }) {
  const element = document.createElement("div");
  element.textContent = String(number);
  Object.assign(element.style, {
    position: "relative",
    // Fills the size the map gives it (some maps size only a wrapper)
    width: "100%",
    height: "100%",
    boxSizing: "border-box",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    borderRadius: "50%",
    border: `${next ? 3 : 2}px solid #fff`,
    boxShadow: "0 1px 3px rgba(0, 0, 0, 0.4)",
    background: charging ? chargingColor : color,
    color: "#fff",
    font: `bold ${next ? 16 : 12}px sans-serif`,
    cursor: "pointer",
  });

  const tooltip = document.createElement("div");
  const title = document.createElement("div");
  title.textContent = name;
  title.style.fontWeight = "bold";
  tooltip.append(title);
  if (time) {
    const arrival = document.createElement("div");
    arrival.textContent = time;
    tooltip.append(arrival);
  }
  Object.assign(tooltip.style, {
    display: "none",
    position: "absolute",
    bottom: "calc(100% + 6px)",
    left: "50%",
    transform: "translateX(-50%)",
    padding: "4px 8px",
    borderRadius: "4px",
    background: "var(--card-background-color, #fff)",
    color: "var(--primary-text-color, #212121)",
    boxShadow: "0 1px 4px rgba(0, 0, 0, 0.3)",
    font: "12px sans-serif",
    textAlign: "center",
    whiteSpace: "nowrap",
    pointerEvents: "none",
  });
  element.append(tooltip);

  // Raised while shown, so it isn't covered by the markers around it
  let zIndex;
  const show = () => {
    if (tooltip.style.display !== "none") return;
    if (openTooltip && openTooltip !== hide) openTooltip();
    openTooltip = hide;
    zIndex = element.style.zIndex;
    element.style.zIndex = "1000";
    tooltip.style.display = "block";
  };
  function hide() {
    if (tooltip.style.display === "none") return;
    tooltip.style.display = "none";
    element.style.zIndex = zIndex;
    if (openTooltip === hide) openTooltip = undefined;
  }
  // A tap also emulates hovering, so only a mouse hovers, and only a tap toggles
  let pointer;
  element.addEventListener("pointerdown", (event) => {
    pointer = event.pointerType;
  });
  element.addEventListener("pointerenter", (event) => {
    if (event.pointerType === "mouse") show();
  });
  element.addEventListener("pointerleave", (event) => {
    if (event.pointerType === "mouse") hide();
  });
  element.addEventListener("click", (event) => {
    event.stopPropagation();
    if (pointer && pointer !== "mouse") {
      if (tooltip.style.display === "none") show();
      else hide();
    }
  });
  element.addEventListener("focus", () => {
    if (element.matches?.(":focus-visible")) show();
  });
  element.addEventListener("blur", hide);
  return element;
}

/** The entity IDs of every Rivian Destination sensor. */
export function destinationSensors(entities) {
  return Object.values(entities ?? {})
    .filter(
      (e) =>
        e.platform === "rivian" &&
        (e.translation_key === "destination" ||
          /^sensor\..+_destination$/.test(e.entity_id))
    )
    .map((e) => e.entity_id);
}

/**
 * The entities to show: with a route, the vehicle, the next stop (unless it's
 * the destination) and the destination; without one, just the vehicle.
 */
export function mapEntities({ route, vehicle, nextStop, destination, nextStopIsDestination }) {
  if (!route) return vehicle ? [vehicle] : [];
  return [vehicle, nextStopIsDestination ? null : nextStop, destination].filter(Boolean);
}

let mapLoaded;

/** Make sure Home Assistant's map element is defined, by loading the map card. */
function loadMap() {
  if (customElements.get("ha-map")) return Promise.resolve(true);
  mapLoaded ??= (async () => {
    const helpers = await window.loadCardHelpers?.();
    helpers?.createCardElement({ type: "map", entities: ["zone.home"] });
    const timeout = new Promise((resolve) =>
      setTimeout(() => resolve(false), MAP_LOAD_TIMEOUT_MS)
    );
    return Promise.race([
      customElements.whenDefined("ha-map").then(() => true),
      timeout,
    ]);
  })();
  return mapLoaded;
}

if (typeof customElements !== "undefined" && !customElements.get(CARD_TYPE)) {
  class RivianTripCard extends HTMLElement {
    static getStubConfig(hass) {
      return { entity: destinationSensors(hass?.entities)[0] ?? "" };
    }

    static getConfigElement() {
      return document.createElement(EDITOR_TYPE);
    }

    setConfig(config) {
      if (!config?.entity) {
        throw new Error("Set entity to a Rivian Destination sensor");
      }
      this._config = config;
      this._routeKey = undefined;
      this._entitiesKey = undefined;
      this._timingKey = undefined;
      this._render();
    }

    set hass(hass) {
      this._hass = hass;
      this._update();
    }

    getCardSize() {
      return 6;
    }

    getGridOptions() {
      return { columns: 12, rows: 6, min_columns: 6, min_rows: 4 };
    }

    connectedCallback() {
      loadMap().then((loaded) => {
        this._mapLoaded = loaded;
        this._update();
      });
    }

    disconnectedCallback() {
      clearTimeout(this._engineRetry);
      this._engineRetry = undefined;
      this._removeRouteLayer();
    }

    _render() {
      if (!this.shadowRoot) this.attachShadow({ mode: "open" });
      this.shadowRoot.innerHTML = `
        <style>
          ha-card { height: 100%; display: flex; flex-direction: column; overflow: hidden; }
          .map { flex: 1; min-height: 240px; }
          ha-map { display: block; height: 100%; }
          .message { padding: 16px; color: var(--secondary-text-color); }
        </style>
        <ha-card>
          <div class="message" hidden></div>
          <div class="map"></div>
        </ha-card>`;
      if (this._config.title) {
        this.shadowRoot.querySelector("ha-card").header = this._config.title;
      }
      this._map = undefined;
    }

    /** The vehicle and next stop on the same device as the destination sensor. */
    _relatedEntities() {
      const registry = this._hass.entities ?? {};
      const device = registry[this._config.entity]?.device_id;
      const onDevice = Object.values(registry).filter(
        (e) => device && e.device_id === device && e.platform === "rivian"
      );
      const vehicle =
        this._config.vehicle ??
        onDevice.find((e) => e.entity_id.startsWith("device_tracker."))?.entity_id;
      const nextStop = onDevice.find((e) => e.translation_key === "next_stop")?.entity_id;
      return { vehicle, nextStop };
    }

    _update() {
      if (!this._hass || !this._config || !this.shadowRoot) return;
      const message = this.shadowRoot.querySelector(".message");
      const container = this.shadowRoot.querySelector(".map");
      const destination = this._hass.states[this._config.entity];
      const show = (text) => {
        message.textContent = text;
        message.hidden = false;
        container.hidden = true;
      };

      if (!destination) return show(`Entity not found: ${this._config.entity}`);
      const { route, trip_start: tripStart, arrival } = destination.attributes;
      const { vehicle, nextStop } = this._relatedEntities();
      if (!route && !vehicle) return show("Not navigating");
      if (this._mapLoaded === false) return show("The map couldn't be loaded");
      if (!this._mapLoaded) return show("Loading map…");

      message.hidden = true;
      container.hidden = false;
      if (!this._map) {
        this._map = document.createElement("ha-map");
        container.append(this._map);
      }
      this._map.hass = this._hass;

      const next = this._hass.states[nextStop]?.attributes;
      // With numbered stops, the vehicle and destination are the entities shown
      const numbered = Boolean(route) && canNumberStops(this._map._engine);
      const entities = numbered
        ? [vehicle, this._config.entity].filter(Boolean)
        : mapEntities({
            route,
            vehicle,
            nextStop,
            destination: this._config.entity,
            nextStopIsDestination:
              next?.latitude === destination.attributes.latitude &&
              next?.longitude === destination.attributes.longitude,
          });
      // The live ETA changes often while driving: only refresh the stops (their
      // times, and which is next) then, and refit the map just when what's shown
      // changes (a trip starting, ending or being rerouted). Redraw too once the
      // map's engine is ready.
      const stops = destination.attributes.stops ?? [];
      const entitiesKey = entities.join(",");
      const timingKey = JSON.stringify([tripStart, arrival, stops]);
      const viewChanged = route !== this._routeKey || entitiesKey !== this._entitiesKey;
      const engineChanged = numbered && this._map._engine !== this._routeEngine;
      if (!viewChanged && !engineChanged && timingKey === this._timingKey) return;
      this._routeKey = route;
      this._entitiesKey = entitiesKey;
      this._timingKey = timingKey;
      this._drawRoute({ route, tripStart, arrival, stops, destination: destination.attributes });
      if (!viewChanged) return;

      // The vehicle keeps the map's usual marker; the destination (and, without
      // numbered stops, the next stop) shows its icon rather than a name label,
      // which is large and splits emoji in names
      this._map.entities = entities.map((entity_id) =>
        entity_id === vehicle ? { entity_id } : { entity_id, label_mode: "icon" },
      );
      // Without a trip, follow the vehicle as it moves
      this._map.autoFit = !route;
      if (numbered && typeof this._map.fitBounds === "function") {
        // Fit the whole route, which isn't one of the map's own entities or paths
        const location = this._hass.states[vehicle]?.attributes;
        const points = [...decodeRoute(route), ...stops.map((s) => [s.latitude, s.longitude])];
        if (typeof location?.latitude === "number") {
          points.push([location.latitude, location.longitude]);
        }
        this._map.fitBounds(points.filter(([lat, lng]) => typeof lat === "number" && typeof lng === "number"));
      } else {
        this._map.fitMap?.({ unpause_autofit: true });
      }
    }

    _removeRouteLayer() {
      for (const handle of this._routeHandles ?? []) handle.remove();
      this._routeHandles = undefined;
      this._routeEngine = undefined;
    }

    _drawRoute({ route, tripStart, arrival, stops, destination }) {
      this._removeRouteLayer();
      this._map.paths = [];
      if (!route) return;

      const engine = this._map._engine;
      if (canNumberStops(engine)) {
        const style = typeof getComputedStyle === "function" ? getComputedStyle(this._map) : null;
        const color =
          style?.getPropertyValue("--dark-primary-color").trim() || "#03a9f4";
        const chargingColor =
          style?.getPropertyValue("--success-color").trim() || "#43a047";
        const language = this._hass.locale?.language;
        const layer = buildRouteLayer(route, color);
        if (!layer) return;
        this._routeHandles = [engine.addPath(layer)];
        const markers = stopMarkers(stops, {
          destination,
          formatTime: (date) =>
            date.toLocaleTimeString(language, { hour: "numeric", minute: "2-digit" }),
        });
        // The next stop is placed last, on top of any stops it overlaps
        const ordered = [...markers.filter((m) => !m.next), ...markers.filter((m) => m.next)];
        for (const marker of ordered) {
          const size = marker.next ? 34 : 22;
          const element = markerElement(marker, { color, chargingColor });
          this._routeHandles.push(
            engine.addMarker(element, marker.location, {
              size: [size, size],
              title: marker.title,
              // Its own tooltip replaces the browser's
              nativeTitle: false,
              interactive: true,
              raised: marker.next,
            })
          );
          // Above the other raised markers too, such as the vehicle when selected
          if (marker.next) element.style.zIndex = "2";
        }
        this._routeEngine = engine;
        return;
      }

      // The engine isn't ready yet (or can't number stops): use the map's
      // paths, and check again shortly if it's just not ready
      const path = buildRoutePath(route, tripStart, arrival);
      this._map.paths = path ? [path] : [];
      if (engine) return;
      this._engineRetry ??= setTimeout(() => {
        this._engineRetry = undefined;
        this._update();
      }, 500);
    }
  }

  const LABELS = {
    entity: "Destination sensor",
    title: "Title",
    vehicle: "Vehicle location",
  };
  const HELPERS = {
    vehicle: "Optional; found automatically from the destination sensor's vehicle",
  };

  class RivianTripCardEditor extends HTMLElement {
    setConfig(config) {
      this._config = config;
      this._render();
    }

    set hass(hass) {
      this._hass = hass;
      this._render();
    }

    _schema() {
      const destinations = destinationSensors(this._hass.entities);
      return [
        {
          name: "entity",
          required: true,
          selector: {
            entity: destinations.length
              ? { include_entities: destinations }
              : { filter: { integration: "rivian", domain: "sensor" } },
          },
        },
        { name: "title", selector: { text: {} } },
        {
          name: "vehicle",
          selector: {
            entity: { filter: { integration: "rivian", domain: "device_tracker" } },
          },
        },
      ];
    }

    _render() {
      if (!this._hass || !this._config) return;
      if (!this._form) {
        this._form = document.createElement("ha-form");
        this._form.computeLabel = (schema) => LABELS[schema.name];
        this._form.computeHelper = (schema) => HELPERS[schema.name];
        this._form.addEventListener("value-changed", (ev) =>
          this._changed(ev.detail.value)
        );
        this.append(this._form);
      }
      this._form.hass = this._hass;
      this._form.data = this._config;
      this._form.schema = this._schema();
    }

    _changed(value) {
      const config = { ...value };
      for (const key of ["title", "vehicle"]) {
        if (!config[key]) delete config[key];
      }
      this._config = config;
      this.dispatchEvent(
        new CustomEvent("config-changed", {
          detail: { config },
          bubbles: true,
          composed: true,
        })
      );
    }
  }

  customElements.define(CARD_TYPE, RivianTripCard);
  customElements.define(EDITOR_TYPE, RivianTripCardEditor);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: CARD_TYPE,
    name: "Rivian trip",
    description:
      "A Rivian vehicle's location, and the route of its active navigation trip, on a map.",
    preview: true,
    documentationURL: "https://github.com/bretterer/home-assistant-rivian",
  });
}
