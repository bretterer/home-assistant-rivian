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
  { vin: "VA", letter: "A", color: "#1b6ac9" },
  { vin: "VB", letter: "B", color: "#c9561b" },
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

test("datasetGroups: the selection is queried as the real dataset", () => {
  assert.deepEqual(datasetGroups(VEHICLES, ["VA", "VB"]), [{ dataset: "real", vins: ["VA", "VB"] }]);
  assert.deepEqual(datasetGroups(VEHICLES, ["??"]), []);
});

test("sameDataset: merging stays within one dataset", () => {
  assert.equal(sameDataset({ dataset: "real" }, { dataset: "real" }), true);
  assert.equal(sameDataset({ dataset: "real" }, { dataset: "other" }), false);
  assert.equal(sameDataset({}, { dataset: "real" }), true);
});

test("schemeForColor picks the dropdown color scheme from the card background", async () => {
  const { schemeForColor } = await import("../../custom_components/rivian/frontend/rivian-places-card.js");
  const explorer = await import("../../custom_components/rivian/frontend/rivian-drive-explorer-card.js");
  for (const fn of [schemeForColor, explorer.schemeForColor]) {
    assert.equal(fn("rgb(28, 28, 28)"), "dark");
    assert.equal(fn("rgb(255, 255, 255)"), "light");
    assert.equal(fn("rgba(17, 17, 17, 0.9)"), "dark");
    assert.equal(fn("rgba(0, 0, 0, 0)"), null);
    assert.equal(fn(""), null);
  }
});
