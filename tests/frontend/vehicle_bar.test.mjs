// Node tests for rivian-vehicle-bar.js: pure helpers and the selection store
// (per-user data save/load mocked, localStorage fallback, change event,
// never-empty rule). The module imports with no DOM.
import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";

import {
  CHANGE_EVENT,
  SELECTION_KEY,
  _resetStore,
  chipLabel,
  chipTitle,
  getSelection,
  getVehicles,
  inkOn,
  normalizeSelection,
  onSelectionChange,
  onlyVin,
  sameSelection,
  selectedVehicles,
  selectionKey,
  setSelection,
  toggleVin,
  vehicleByVin,
  vehicleColor,
  vehicleLetter,
  vehiclePictureUrl,
} from "../../custom_components/rivian/frontend/rivian-vehicle-bar.js";

const VEHICLES = [
  { vin: "VA", name: "Rivi", model: "R1S", letter: "A", color: "#1b6ac9", color_dark: "#5aa0f0", picture_entity: "image.rivi_picture" },
  { vin: "VB", name: "Test R2", model: "R2", letter: "B", color: "#c9561b", color_dark: "#f0925a", picture_url: "https://x/y.png" },
  { vin: "VC", name: "Test R1T", model: "R1T", letter: "C", color: "#2a9d5c" },
];

// -- fakes ------------------------------------------------------------------

class FakeStorage {
  constructor() {
    this.map = new Map();
  }
  getItem(k) {
    return this.map.has(k) ? this.map.get(k) : null;
  }
  setItem(k, v) {
    this.map.set(k, String(v));
  }
}

/** A hass whose callWS serves the vehicle list and an in-memory user-data store. */
function makeHass({ userData = {}, userDataBroken = false } = {}) {
  const calls = [];
  return {
    calls,
    userData,
    async callWS(msg) {
      calls.push(msg);
      if (msg.type === "rivian/vehicles/list") return VEHICLES;
      if (userDataBroken && msg.type.startsWith("frontend/")) throw new Error("no user data");
      if (msg.type === "frontend/get_user_data") {
        return { value: Object.hasOwn(userData, msg.key) ? userData[msg.key] : null };
      }
      if (msg.type === "frontend/set_user_data") {
        userData[msg.key] = msg.value;
        return null;
      }
      throw new Error(`unexpected ${msg.type}`);
    },
  };
}

beforeEach(() => {
  _resetStore();
  globalThis.window = new EventTarget();
  Object.defineProperty(globalThis, "localStorage", {
    value: new FakeStorage(),
    configurable: true,
    writable: true,
  });
});

// -- pure helpers -------------------------------------------------------------

test("normalizeSelection: unknown vins dropped, vehicle order kept, empty -> all", () => {
  assert.deepEqual(normalizeSelection(["VC", "VA", "ZZ"], VEHICLES), ["VA", "VC"]);
  assert.deepEqual(normalizeSelection([], VEHICLES), ["VA", "VB", "VC"]);
  assert.deepEqual(normalizeSelection(["ZZ"], VEHICLES), ["VA", "VB", "VC"]);
  assert.deepEqual(normalizeSelection(null, VEHICLES), ["VA", "VB", "VC"]);
  assert.deepEqual(normalizeSelection(["VA", "VA"], VEHICLES), ["VA"]);
});

test("toggleVin: toggles, and never deselects the last vehicle", () => {
  assert.deepEqual(toggleVin(["VA", "VB", "VC"], "VB", VEHICLES), ["VA", "VC"]);
  assert.deepEqual(toggleVin(["VA", "VC"], "VB", VEHICLES), ["VA", "VB", "VC"]);
  assert.deepEqual(toggleVin(["VA"], "VA", VEHICLES), ["VA"]);
  assert.deepEqual(toggleVin(["VA"], "ZZ", VEHICLES), ["VA"]);
});

test("onlyVin / selectionKey / sameSelection", () => {
  assert.deepEqual(onlyVin("VB", VEHICLES), ["VB"]);
  assert.deepEqual(onlyVin("ZZ", VEHICLES), ["VA", "VB", "VC"]);
  assert.equal(selectionKey(["VB", "VA"]), "VA,VB");
  assert.equal(sameSelection(["VA", "VB"], ["VB", "VA"]), true);
  assert.equal(sameSelection(["VA"], ["VA", "VB"]), false);
  assert.equal(sameSelection(null, ["VA"]), false);
});

test("vehicleColor: light/dark with fallbacks", () => {
  assert.equal(vehicleColor(VEHICLES[0], false), "#1b6ac9");
  assert.equal(vehicleColor(VEHICLES[0], true), "#5aa0f0");
  assert.equal(vehicleColor(VEHICLES[2], true), "#2a9d5c"); // no dark variant
  assert.equal(vehicleColor(null, false), "#8a8a8a");
});

test("vehicleLetter / chipLabel / chipTitle / vehicleByVin", () => {
  assert.equal(vehicleLetter(VEHICLES[1]), "B");
  assert.equal(vehicleLetter(null), "");
  assert.equal(chipLabel(VEHICLES[0]), "Rivi");
  assert.equal(chipLabel({ model: "R1T" }), "R1T");
  assert.equal(chipTitle(VEHICLES[0]), "A · Rivi (R1S)");
  assert.equal(chipTitle({ letter: "D", name: "R2", model: "R2" }), "D · R2");
  assert.equal(vehicleByVin(VEHICLES, "VB").name, "Test R2");
  assert.equal(vehicleByVin(VEHICLES, "ZZ"), null);
});

test("vehiclePictureUrl: live entity picture, else the public render URL", () => {
  const hass = { states: { "image.rivi_picture": { attributes: { entity_picture: "/api/image_proxy/x?token=1" } } } };
  assert.equal(vehiclePictureUrl(VEHICLES[0], hass), "/api/image_proxy/x?token=1");
  assert.equal(vehiclePictureUrl(VEHICLES[0], { states: {} }), null);
  assert.equal(vehiclePictureUrl(VEHICLES[1], hass), "https://x/y.png");
});

test("selectedVehicles: selection order, unknown dropped", () => {
  assert.deepEqual(selectedVehicles(VEHICLES, ["VC", "VA"]).map((v) => v.letter), ["A", "C"]);
});

test("inkOn: contrast ink", () => {
  assert.equal(inkOn("#000000"), "#ffffff");
  assert.equal(inkOn("#ffffff"), "#111111");
});

// -- store ------------------------------------------------------------------

test("getVehicles: caches the list", async () => {
  const hass = makeHass();
  assert.equal((await getVehicles(hass)).length, 3);
  await getVehicles(hass);
  assert.equal(hass.calls.filter((c) => c.type === "rivian/vehicles/list").length, 1);
});

test("getSelection: defaults to every vehicle when nothing is stored", async () => {
  assert.deepEqual(await getSelection(makeHass()), ["VA", "VB", "VC"]);
});

test("getSelection: loads the saved per-user selection and drops unknown vins", async () => {
  const hass = makeHass({ userData: { [SELECTION_KEY]: { vins: ["VB", "GONE"] } } });
  assert.deepEqual(await getSelection(hass), ["VB"]);
  const get = hass.calls.find((c) => c.type === "frontend/get_user_data");
  assert.equal(get.key, "rivian_vehicle_selection");
});

test("getSelection: a saved selection of only unknown vins becomes all vehicles", async () => {
  const hass = makeHass({ userData: { [SELECTION_KEY]: { vins: ["GONE"] } } });
  assert.deepEqual(await getSelection(hass), ["VA", "VB", "VC"]);
});

test("getSelection: falls back to localStorage when user data fails", async () => {
  globalThis.localStorage.setItem(SELECTION_KEY, JSON.stringify({ vins: ["VC"] }));
  assert.deepEqual(await getSelection(makeHass({ userDataBroken: true })), ["VC"]);
});

test("getSelection: concurrent callers share one load", async () => {
  const hass = makeHass();
  await Promise.all([getSelection(hass), getSelection(hass), getSelection(hass)]);
  assert.equal(hass.calls.filter((c) => c.type === "frontend/get_user_data").length, 1);
});

test("setSelection: persists to user data and broadcasts the change event", async () => {
  const hass = makeHass();
  await getSelection(hass);
  const seen = [];
  const off = onSelectionChange((vins) => seen.push(vins));
  const result = await setSelection(hass, ["VC", "VA"]);
  assert.deepEqual(result, ["VA", "VC"]);
  assert.deepEqual(hass.userData[SELECTION_KEY], { vins: ["VA", "VC"] });
  assert.deepEqual(seen, [["VA", "VC"]]);
  assert.deepEqual(await getSelection(hass), ["VA", "VC"]);
  off();
  await setSelection(hass, ["VB"]);
  assert.equal(seen.length, 1); // unsubscribed
});

test("setSelection: the change event is a window CustomEvent with detail.vins", async () => {
  const hass = makeHass();
  await getSelection(hass);
  let event = null;
  globalThis.window.addEventListener(CHANGE_EVENT, (ev) => {
    event = ev;
  });
  await setSelection(hass, ["VB"]);
  assert.equal(event.type, "rivian-vehicles-changed");
  assert.deepEqual(event.detail.vins, ["VB"]);
});

test("setSelection: no event when nothing changed", async () => {
  const hass = makeHass();
  await getSelection(hass);
  const seen = [];
  onSelectionChange((vins) => seen.push(vins));
  await setSelection(hass, ["VA", "VB", "VC"]);
  assert.deepEqual(seen, []);
});

test("setSelection: never empty (an empty request selects every vehicle)", async () => {
  const hass = makeHass();
  await getSelection(hass);
  await setSelection(hass, ["VA"]);
  assert.deepEqual(await setSelection(hass, []), ["VA", "VB", "VC"]);
});

test("setSelection: falls back to localStorage when saving user data fails", async () => {
  const hass = makeHass({ userDataBroken: true });
  await getSelection(hass);
  await setSelection(hass, ["VB"]);
  assert.deepEqual(JSON.parse(globalThis.localStorage.getItem(SELECTION_KEY)), { vins: ["VB"] });
  // And a fresh page load (new store) reads it back through the same fallback.
  _resetStore();
  assert.deepEqual(await getSelection(makeHass({ userDataBroken: true })), ["VB"]);
});
