// Node smoke test for rivian-places-card.js's pure helpers.
//
// The card module guards every top-level use of HTMLElement/customElements/
// window/document, so it imports cleanly here with no DOM. Run with:
//   node --test tests/frontend/
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  categoryIcon,
  categoryColor,
  formatVisits,
  formatLastVisit,
  groupPlaces,
  clampRadius,
  radiusHandleLatLng,
  homeRegionPlaces,
  sourceDescription,
  deletePlaceMessage,
  ALL_FILTER,
  CHARGING_FILTER,
  SPOT_RADIUS_M,
  FILTER_STORAGE_KEY,
  filterChips,
  chargingSessions,
  isHomeCharge,
  homePlaceId,
  groupChargeSpots,
  spotLabel,
  placeMatchesFilter,
  hourOfDay,
  circularMeanHour,
  formatClock,
  formatStay,
  hourBins,
  histogram,
  stayStats,
  chargingStats,
  stackBins,
  staysUnavailableReason,
  stayHistogramSpec,
} from "../../custom_components/rivian/frontend/rivian-places-card.js";

test("categoryIcon: known categories map to their mdi icon", () => {
  assert.equal(categoryIcon("home"), "mdi:home");
  assert.equal(categoryIcon("work"), "mdi:briefcase");
  assert.equal(categoryIcon("school"), "mdi:school");
  assert.equal(categoryIcon("shop"), "mdi:cart");
  assert.equal(categoryIcon("charging"), "mdi:ev-station");
  assert.equal(categoryIcon("other"), "mdi:map-marker");
});

test("categoryIcon: unknown/null category falls back to a plain pin", () => {
  assert.equal(categoryIcon(null), "mdi:map-marker");
  assert.equal(categoryIcon("something-else"), "mdi:map-marker");
});

test("categoryColor: every known category has a distinct color, unknowns fall back to 'other'", () => {
  const cats = ["home", "work", "school", "shop", "charging", "other"];
  const colors = cats.map(categoryColor);
  assert.equal(new Set(colors).size, colors.length);
  assert.equal(categoryColor(null), categoryColor("other"));
  assert.equal(categoryColor("unknown"), categoryColor("other"));
});

test("formatVisits: singular/plural/zero", () => {
  assert.equal(formatVisits(0), "No visits yet");
  assert.equal(formatVisits(1), "1 visit");
  assert.equal(formatVisits(2), "2 visits");
  assert.equal(formatVisits(undefined), "No visits yet");
});

test("formatLastVisit: null timestamp is 'never'", () => {
  assert.equal(formatLastVisit(null), "never");
  assert.equal(formatLastVisit(undefined), "never");
});

test("formatLastVisit: relative buckets", () => {
  const now = 1_700_000_000;
  const DAY = 86400;
  assert.equal(formatLastVisit(now, now), "today");
  assert.equal(formatLastVisit(now - DAY, now), "yesterday");
  assert.equal(formatLastVisit(now - 3 * DAY, now), "3 days ago");
  assert.equal(formatLastVisit(now - 14 * DAY, now), "2 weeks ago");
  assert.equal(formatLastVisit(now - 60 * DAY, now), "2 months ago");
  assert.equal(formatLastVisit(now - 400 * DAY, now), "1 year ago");
});

function _place(overrides) {
  return {
    id: 1,
    label: "Place #1",
    name: null,
    geocode_name: null,
    category: null,
    lat: 0,
    lon: 0,
    radius_m: 150,
    source: "auto",
    zone_entity_id: null,
    hidden: false,
    visits: 0,
    arrivals: 0,
    departures: 0,
    last_visit_ts: null,
    ...overrides,
  };
}

test("groupPlaces: unnamed auto places are suggestions, sorted by visits desc", () => {
  const places = [
    _place({ id: 1, visits: 5 }),
    _place({ id: 2, visits: 20 }),
    _place({ id: 3, visits: 8 }),
  ];
  const { suggestions, named, hidden } = groupPlaces(places);
  assert.deepEqual(
    suggestions.map((p) => p.id),
    [2, 3, 1]
  );
  assert.equal(named.length, 0);
  assert.equal(hidden.length, 0);
});

test("groupPlaces: named or zone places are 'named', sorted by visits desc", () => {
  const places = [
    _place({ id: 1, name: "Home", source: "user", visits: 100 }),
    _place({ id: 2, source: "zone", name: "Work", visits: 50 }),
    _place({ id: 3, visits: 999 }), // unnamed auto: still a suggestion, not named
  ];
  const { suggestions, named } = groupPlaces(places);
  assert.deepEqual(
    named.map((p) => p.id),
    [1, 2]
  );
  assert.deepEqual(
    suggestions.map((p) => p.id),
    [3]
  );
});

test("groupPlaces: hidden places are excluded from suggestions/named and listed separately", () => {
  const places = [
    _place({ id: 1, name: "Old spot", hidden: true, visits: 10 }),
    _place({ id: 2, name: "Home", visits: 5 }),
  ];
  const { suggestions, named, hidden } = groupPlaces(places);
  assert.equal(suggestions.length, 0);
  assert.deepEqual(
    named.map((p) => p.id),
    [2]
  );
  assert.deepEqual(
    hidden.map((p) => p.id),
    [1]
  );
});

test("sourceDescription: each source kind gets a human sentence", () => {
  assert.equal(sourceDescription("zone"), "From an HA zone");
  assert.equal(sourceDescription("user"), "Added by you");
  assert.equal(sourceDescription("auto"), "Detected from your stops");
});

test("homeRegionPlaces: frames the most-visited place's area, not a far road-trip stop", () => {
  const places = [
    { id: 1, lat: 39.7242, lon: -104.9880, visits: 79, hidden: false },
    { id: 2, lat: 39.7442, lon: -105.0380, visits: 24, hidden: false },
    { id: 3, lat: 41.0242, lon: -104.8880, visits: 3, hidden: false }, // ~145 km north
    { id: 4, lat: 39.7342, lon: -104.9980, visits: 50, hidden: true },
  ];
  assert.deepEqual(homeRegionPlaces(places).map((p) => p.id), [1, 2]);
  assert.deepEqual(homeRegionPlaces(places, 500).map((p) => p.id), [1, 2, 3]);
  assert.deepEqual(homeRegionPlaces([]), []);
});

test("groupPlaces: an unnamed place pinned by a resize/move (source user) stays a suggestion", () => {
  const { suggestions, named } = groupPlaces([_place({ id: 7, source: "user", name: null, visits: 4 })]);
  assert.deepEqual(suggestions.map((p) => p.id), [7]);
  assert.equal(named.length, 0);
});

test("clampRadius: rounds to 5 m and clamps to 25-500 m", () => {
  assert.equal(clampRadius(203.4), 205);
  assert.equal(clampRadius(3), 25);
  assert.equal(clampRadius(9000), 500);
  assert.equal(clampRadius(NaN), 25);
});

test("radiusHandleLatLng: due east of the centre by the radius", () => {
  const [lat, lon] = radiusHandleLatLng(39.7242, -104.9880, 200);
  assert.equal(lat, 39.7242);
  // 200 m east at 39.7242 deg N is ~0.002336 deg of longitude.
  assert.ok(Math.abs(lon - -104.9880 - 0.002336) < 0.00002, String(lon));
});

test("deletePlaceMessage: a user place names it and warns drives are unlabeled", () => {
  const msg = deletePlaceMessage({ source: "user", name: "Home", label: "Home" });
  assert.equal(
    msg,
    "Delete “Home”? Drives that started or ended here will no longer be labeled with it. This can't be undone."
  );
});

test("deletePlaceMessage: falls back to the computed label when unnamed", () => {
  const msg = deletePlaceMessage({ source: "user", name: null, label: "Place #7" });
  assert.ok(msg.includes("“Place #7”"));
});

test("deletePlaceMessage: an auto suggestion is hidden, not deleted", () => {
  assert.equal(
    deletePlaceMessage({ source: "auto", label: "Place #3" }),
    "Remove this suggestion? It won't be suggested again; you can restore it from Hidden."
  );
});

// -- shared places (schema v10): categories, datasets, per-vehicle visits ----------

import {
  FALLBACK_CATEGORIES,
  categoryList,
  datasetGroups,
  formatVisitsByVehicle,
  sameDataset,
  visitsByVehicle,
} from "../../custom_components/rivian/frontend/rivian-places-card.js";

const VEHICLES = [
  { vin: "VA", letter: "A", color: "#1b6ac9", is_demo: false },
  { vin: "VB", letter: "B", color: "#c9561b", is_demo: false },
  { vin: "DD", letter: "C", color: "#2a9d5c", is_demo: true },
];

test("categoryList: the server's list wins, else the fallback", () => {
  const server = [{ key: "swim", label: "Swimming", icon: "mdi:swim" }];
  assert.deepEqual(categoryList({ categories: server }), server);
  assert.equal(categoryList({}), FALLBACK_CATEGORIES);
  assert.equal(categoryList({ categories: [] }), FALLBACK_CATEGORIES);
  assert.equal(categoryList(null), FALLBACK_CATEGORIES);
});

test("categoryIcon: uses the server list for new categories", () => {
  const server = [
    { key: "mountain_biking", label: "Mountain biking", icon: "mdi:bike" },
    { key: "swim", label: "Swimming", icon: "mdi:swim" },
  ];
  assert.equal(categoryIcon("mountain_biking", server), "mdi:bike");
  assert.equal(categoryIcon("swim", server), "mdi:swim");
  assert.equal(categoryIcon("bogus", server), "mdi:map-marker");
  assert.equal(categoryIcon("home"), "mdi:home"); // fallback list
});

test("categoryColor: every server category has a color, unknown ones are neutral", () => {
  for (const key of ["dining", "friends", "family", "gym", "swim", "mountain_biking", "park", "medical"]) {
    assert.notEqual(categoryColor(key), categoryColor("nope"), key);
  }
});

test("visitsByVehicle: per-car counts for the selected vehicles, most first", () => {
  const place = { visits_by_vin: { VA: 79, VB: 12, DD: 3 } };
  const parts = visitsByVehicle(place, VEHICLES, ["VA", "VB"]);
  assert.deepEqual(
    parts.map((p) => [p.vin, p.letter, p.count]),
    [["VA", "A", 79], ["VB", "B", 12]]
  );
  assert.equal(parts[0].color, "#1b6ac9");
  assert.equal(formatVisitsByVehicle(parts), "79 A · 12 B");
});

test("visitsByVehicle: unselected, zero and missing visits are dropped", () => {
  assert.deepEqual(visitsByVehicle({ visits_by_vin: { VA: 0, VB: 4 } }, VEHICLES, ["VA"]), []);
  assert.deepEqual(visitsByVehicle({}, VEHICLES, ["VA"]), []);
  assert.equal(formatVisitsByVehicle([]), "");
  const unknown = visitsByVehicle({ visits_by_vin: { ZZ: 2 } }, VEHICLES, ["ZZ"]);
  assert.equal(formatVisitsByVehicle(unknown), "2");
});

test("datasetGroups: real and demo selections are queried separately", () => {
  assert.deepEqual(datasetGroups(VEHICLES, ["VA", "VB"]), [{ dataset: "real", vins: ["VA", "VB"] }]);
  assert.deepEqual(datasetGroups(VEHICLES, ["DD"]), [{ dataset: "demo", vins: ["DD"] }]);
  assert.deepEqual(datasetGroups(VEHICLES, ["DD", "VA"]), [
    { dataset: "real", vins: ["VA"] },
    { dataset: "demo", vins: ["DD"] },
  ]);
  assert.deepEqual(datasetGroups(VEHICLES, ["??"]), []);
});

test("sameDataset: merging stays within one dataset", () => {
  assert.equal(sameDataset({ dataset: "real" }, { dataset: "real" }), true);
  assert.equal(sameDataset({ dataset: "real" }, { dataset: "demo" }), false);
  assert.equal(sameDataset({}, { dataset: "real" }), true);
});

test("schemeForColor picks the dropdown color scheme from the card background", async () => {
  const { schemeForColor } = await import("../../custom_components/rivian/frontend/rivian-places-card.js");
  const explorer = await import("../../custom_components/rivian/frontend/rivian-drive-explorer-card.js");
  const efficiency = await import("../../custom_components/rivian/frontend/rivian-efficiency-card.js");
  for (const fn of [schemeForColor, explorer.schemeForColor, efficiency.schemeForColor]) {
    assert.equal(fn("rgb(28, 28, 28)"), "dark");
    assert.equal(fn("rgb(255, 255, 255)"), "light");
    assert.equal(fn("rgba(17, 17, 17, 0.9)"), "dark");
    assert.equal(fn("rgba(0, 0, 0, 0)"), null);
    assert.equal(fn(""), null);
  }
});

// -- destination stats & charging spots ------------------------------------------

const CATS = [
  { key: "home", label: "Home", icon: "mdi:home" },
  { key: "shop", label: "Shopping", icon: "mdi:cart" },
  { key: "charging", label: "Charging", icon: "mdi:ev-station" },
  { key: "family", label: "Family", icon: "mdi:account-group" },
];
const PL = (over) => ({ id: 1, label: "Home", category: "home", lat: 43.7, lon: -116.35, visits: 10, hidden: false, ...over });
const SES = (over) => ({ vin: "V1", session_id: "s", kind: "ac", charge_type: "ac_l2", lat: 43.7, lon: -116.35, place: null, energy_added_kwh: 20, avg_power_kw: 9.6, start_soc: 40, end_soc: 80, ...over });
// About 0.00000899 degrees of latitude per metre.
const north = (m) => 43.7 + m * 0.00000899;

test("constants", () => {
  assert.equal(ALL_FILTER, "all");
  assert.equal(CHARGING_FILTER, "charging");
  assert.equal(SPOT_RADIUS_M, 100);
  assert.equal(FILTER_STORAGE_KEY, "rivian-places-filter");
});

test("chargingSessions drops inferred sessions", () => {
  const out = chargingSessions([SES({ session_id: "a" }), SES({ session_id: "b", inferred: true }), SES({ session_id: "c", source: "inferred" })]);
  assert.deepEqual(out.map((s) => s.session_id), ["a"]);
  assert.deepEqual(chargingSessions(null), []);
});

test("isHomeCharge / homePlaceId", () => {
  assert.equal(isHomeCharge({ is_home_charge: true }), true);
  assert.equal(isHomeCharge({ is_home: true }), true);
  assert.equal(isHomeCharge({ is_home: null }), false);
  assert.equal(homePlaceId([PL({ id: 1, visits: 3 }), PL({ id: 2, visits: 9 }), PL({ id: 3, visits: 50, hidden: true })]), 2);
  assert.equal(homePlaceId([PL({ id: 4, category: "shop", zone_entity_id: "zone.home" })]), 4);
  assert.equal(homePlaceId([PL({ category: "shop" })]), null);
});

test("groupChargeSpots: places, 100 m spots, home charges without a location", () => {
  const places = [PL({ id: 1 }), PL({ id: 7, category: "shop", label: "Store", lat: 44, lon: -116 })];
  const sessions = [
    SES({ session_id: "p", place: { id: 7, label: "Store" }, lat: 50, lon: 0 }),
    SES({ session_id: "a", lat: north(1000), lon: -116.35, station_name: "EA Eagle" }),
    SES({ session_id: "b", lat: north(1060), lon: -116.35 }),
    SES({ session_id: "c", lat: north(1250), lon: -116.35 }),
    SES({ session_id: "h", lat: null, lon: null, is_home_charge: true }),
    SES({ session_id: "x", lat: null, lon: null }),
  ];
  const g = groupChargeSpots(sessions, places);
  assert.deepEqual(g.byPlace.get(7).map((s) => s.session_id), ["p"]);
  assert.deepEqual(g.byPlace.get(1).map((s) => s.session_id), ["h"]);
  assert.equal(g.spots.length, 2);
  assert.deepEqual(g.spots[0].sessions.map((s) => s.session_id), ["a", "b"]);
  assert.equal(g.spots[0].label, "EA Eagle");
  assert.match(g.spots[0].key, /^spot:/);
  assert.notEqual(g.spots[0].key, g.spots[1].key);
  assert.equal(g.unlocated, 1);
});

test("groupChargeSpots: a session at a hidden place becomes a spot", () => {
  const g = groupChargeSpots([SES({ place: { id: 3 } })], [PL({ id: 3, hidden: true })]);
  assert.equal(g.byPlace.size, 0);
  assert.equal(g.spots.length, 1);
});

test("spotLabel prefers the station name, then the network", () => {
  assert.equal(spotLabel([{ station_name: "A" }, { station_name: "B" }, { station_name: "B" }]), "B");
  assert.equal(spotLabel([{ brand_label: "Tesla" }]), "Tesla");
  assert.equal(spotLabel([{}]), "Charging spot");
});

test("filterChips: no separate charging category chip; Charging chip when relevant", () => {
  const places = [PL({ id: 1 }), PL({ id: 2, category: "shop" }), PL({ id: 3, category: "charging" })];
  const keys = filterChips(places, CATS, [], new Map()).map((c) => c.key);
  assert.deepEqual(keys, ["all", "home", "shop", "charging"]);
  assert.equal(keys.filter((k) => k === "charging").length, 1);
  // No charging anywhere: no Charging chip.
  assert.deepEqual(filterChips([PL({ id: 1 })], CATS, [], new Map()).map((c) => c.key), ["all", "home"]);
  // Spots alone are enough, and so is a place with charges.
  assert.ok(filterChips([PL({ id: 1 })], CATS, [{ key: "spot:0" }], new Map()).some((c) => c.key === "charging"));
  assert.ok(filterChips([PL({ id: 1 })], CATS, [], new Map([[1, [SES()]]])).some((c) => c.key === "charging"));
});

test("placeMatchesFilter", () => {
  const byPlace = new Map([[1, [SES()]]]);
  assert.equal(placeMatchesFilter(PL({ id: 1 }), "all", byPlace), true);
  assert.equal(placeMatchesFilter(PL({ id: 1 }), "charging", byPlace), true);
  assert.equal(placeMatchesFilter(PL({ id: 2, category: "charging" }), "charging", byPlace), true);
  assert.equal(placeMatchesFilter(PL({ id: 2, category: "shop" }), "charging", byPlace), false);
  assert.equal(placeMatchesFilter(PL({ id: 2, category: "shop" }), "shop", byPlace), true);
  assert.equal(placeMatchesFilter(PL({ id: 2, category: "shop", hidden: true }), "shop", byPlace), false);
  assert.equal(placeMatchesFilter(PL({ id: 2, hidden: true }), "all", byPlace), true);
});

test("hourOfDay in the HA time zone; circularMeanHour wraps midnight", () => {
  assert.equal(hourOfDay(Date.UTC(2026, 0, 1, 13, 30) / 1000, "America/Boise"), 6.5);
  const m = circularMeanHour([23.5, 0.5]);
  assert.ok(m < 0.01 || m > 23.99, String(m));
  assert.ok(Math.abs(circularMeanHour([8, 10]) - 9) < 1e-9);
  assert.equal(circularMeanHour([]), null);
});

test("formatClock / formatStay", () => {
  assert.equal(formatClock(17 + 40 / 60), "5:40 pm");
  assert.equal(formatClock(0), "12:00 am");
  assert.equal(formatClock(12.5), "12:30 pm");
  assert.equal(formatStay(2040), "34 min");
  assert.equal(formatStay(11400), "3 h 10 min");
  assert.equal(formatStay(2 * 86400 + 4 * 3600), "2 d 4 h");
});

test("hourBins and histogram", () => {
  const bins = hourBins([0.2, 0.9, 23.99, 13]);
  assert.equal(bins.length, 24);
  assert.equal(bins[0], 2);
  assert.equal(bins[23], 1);
  assert.equal(bins[13], 1);
  const h = histogram([0.5, 1.2, 1.9, 4, 9.3], 10);
  assert.equal(h.step, 1);
  assert.equal(h.bins.length, 10);
  assert.equal(histogram([]), null);
});

test("stayStats: open stays count as visits without a length", () => {
  const tz = "UTC";
  const day = Date.UTC(2026, 0, 5) / 1000;
  const st = stayStats([{ vin: "V1", arrive_ts: day + 9 * 3600, depart_ts: null }], tz);
  assert.equal(st.visits, 1);
  assert.equal(st.avgStayS, null);
  assert.equal(st.typicalDepart, null);
  assert.ok(Math.abs(st.typicalArrive - 9) < 1e-9);
  const st2 = stayStats(
    [
      { vin: "V1", arrive_ts: day + 9 * 3600, depart_ts: day + 10 * 3600 },
      { vin: "V1", arrive_ts: day + 11 * 3600, depart_ts: day + 14 * 3600 },
      { vin: "V1", arrive_ts: null, depart_ts: day + 16 * 3600 },
    ],
    tz
  );
  assert.equal(st2.visits, 2);
  assert.equal(st2.avgStayS, 2 * 3600);
  assert.deepEqual(st2.lengths, [3600, 3 * 3600]);
  assert.equal(st2.departHours.length, 3);
  assert.equal(stayStats([], tz).visits, 0);
});

test("chargingStats: counts, kWh, SoC added, types and rates", () => {
  const st = chargingStats([
    SES({ energy_added_kwh: 20, start_soc: 40, end_soc: 80 }),
    SES({ energy_added_kwh: null, avg_power_kw: null, start_soc: 50, end_soc: 70 }),
    SES({ kind: "dc", charge_type: "dc", energy_added_kwh: 40, avg_power_kw: 120, start_soc: 10, end_soc: 70 }),
  ]);
  assert.equal(st.count, 3);
  assert.equal(st.totalKwh, 60);
  assert.equal(st.avgKwh, 30);
  assert.equal(st.avgSocAdded, 40);
  assert.deepEqual(st.byType, { dc: 1, ac_l2: 2, ac_l1: 0, ac: 0 });
  assert.deepEqual(st.rates, [9.6, 120]);
  assert.equal(st.noRate, 1);
  assert.equal(chargingStats([]).avgKwh, null);
});

test("stackBins splits each bin's count by vehicle", () => {
  const bins = stackBins([0.5, 1.5, 1.7, 30], ["A", "A", "B", "B"], (v) => Math.floor(v), 3);
  assert.equal(bins.length, 3);
  assert.deepEqual(bins[0], { count: 1, byVin: { A: 1 } });
  assert.deepEqual(bins[1], { count: 2, byVin: { A: 1, B: 1 } });
  // Out-of-range indexes clamp into the last bin.
  assert.deepEqual(bins[2], { count: 1, byVin: { B: 1 } });
});

// -- review fixes ----------------------------------------------------------------

test("groupChargeSpots keeps real and demo data apart", () => {
  const places = [
    // A real Home is usually the zone.home place, which has no category.
    PL({ id: 1, category: null, zone_entity_id: "zone.home", dataset: "real" }),
    PL({ id: 50, category: "home", visits: 99, dataset: "demo" }),
  ];
  const datasetOf = (vin) => (vin === "DEMO" ? "demo" : "real");
  const sessions = [
    SES({ session_id: "home", vin: "V1", lat: null, lon: null, is_home: true }),
    SES({ session_id: "r", vin: "V1", lat: north(5000), lon: -116.35 }),
    SES({ session_id: "d", vin: "DEMO", lat: north(5015), lon: -116.35 }),
    // A real session can't land in a demo place even if the id matches.
    SES({ session_id: "x", vin: "V1", place: { id: 50 }, lat: north(9000), lon: -116.35 }),
  ];
  const g = groupChargeSpots(sessions, places, datasetOf);
  assert.deepEqual(g.byPlace.get(1).map((s) => s.session_id), ["home"]);
  assert.equal(g.byPlace.has(50), false);
  assert.equal(g.spots.length, 3);
  assert.equal(new Set(g.spots.map((sp) => sp.key)).size, 3);
  assert.ok(g.spots.every((sp) => new Set(sp.sessions.map((s) => datasetOf(s.vin))).size === 1));
});

test("staysUnavailableReason: hidden places have no visit stats", () => {
  assert.match(staysUnavailableReason(PL({ hidden: true })), /Hidden/);
  assert.equal(staysUnavailableReason(PL({ hidden: false })), null);
});

test("stayHistogramSpec: a week away doesn't flatten the everyday stays", () => {
  const h = 3600;
  const spec = stayHistogramSpec([1, 2, 3, 10, 11, 12, 13, 14, 168].map((x) => x * h));
  assert.equal(spec.unit, h);
  assert.equal(spec.step, 5);
  assert.equal(spec.overflowFrom, 30);
  assert.equal(spec.n, 7);
  assert.equal(spec.index(168), 6);
  assert.equal(spec.index(12), 2);
  assert.equal(spec.index(1), 0);
  const counts = stackBins(spec.values, spec.values.map(() => "A"), spec.index, spec.n).map((b) => b.count);
  assert.deepEqual(counts, [3, 0, 5, 0, 0, 0, 1]);
  // No outlier: no overflow bin, minutes for short stays.
  const short = stayHistogramSpec([20, 30, 40, 45].map((m) => m * 60));
  assert.equal(short.unit, 60);
  assert.equal(short.overflowFrom, null);
  assert.equal(short.index(45), short.n - 1);
  assert.equal(stayHistogramSpec([]), null);
});
