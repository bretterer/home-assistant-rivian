// Node tests for the tooltip / hover / keyboard helpers added in the hover
// audit, across every Rivian card module (all import without a DOM).
//   node --test tests/frontend/
import assert from "node:assert/strict";
import { test } from "node:test";

import * as bar from "../../custom_components/rivian/frontend/rivian-vehicle-bar.js";
import * as overview from "../../custom_components/rivian/frontend/rivian-overview-card.js";
import * as routes from "../../custom_components/rivian/frontend/rivian-routes-card.js";
import * as places from "../../custom_components/rivian/frontend/rivian-places-card.js";
import * as rde from "../../custom_components/rivian/frontend/rivian-drive-explorer-card.js";

// -- vehicle bar -------------------------------------------------------------

test("chipHint: identity plus what the two click targets do", () => {
  const hint = bar.chipHint({ letter: "A", name: "Rivi", model: "R1S" });
  assert.equal(hint, "A · Rivi (R1S) — tap to toggle, tap name to show only this vehicle");
  assert.equal(bar.chipHint(null), "");
});

// -- overview ----------------------------------------------------------------------

test("overview: every stats row and window header has a title", () => {
  for (const row of overview.summaryRows({})) assert.ok(overview.STAT_ROW_TITLES[row.label], row.label);
  for (const row of overview.householdRows({})) assert.ok(overview.STAT_ROW_TITLES[row.label], row.label);
  for (const header of ["7 days", "30 days", "Year", "Lifetime"]) assert.ok(overview.STAT_WINDOW_TITLES[header]);
});

test("overview: chip titles say when a chip opens details", () => {
  assert.equal(overview.overviewChipTitle({ text: "Parked", entityId: "sensor.x" }), "Parked — tap for details");
  assert.equal(overview.overviewChipTitle({ text: "Parked" }), "Parked");
  assert.equal(overview.overviewChipTitle(null), "");
});

// -- routes -------------------------------------------------------------------------

test("routes: driveReadout lists the drive's numbers", () => {
  const text = routes.driveReadout({
    start_ts: 1700000000,
    duration_seconds: 990,
    moving_seconds: 900,
    efficiency_mi_kwh: 3.456,
    temp_f: 52.4,
    vs_avg_pct: -8,
  });
  assert.ok(text.includes("16:30 elapsed"), text);
  assert.ok(text.includes("15:00 moving"), text);
  assert.ok(text.includes("3.46 mi/kWh"), text);
  assert.ok(text.includes("52°F"), text);
  assert.ok(text.includes("vs avg"), text);
  assert.equal(routes.driveReadout(null), "");
  assert.ok(routes.driveReadout({ start_ts: 1, duration_seconds: 60, outlier: true }).includes("outlier"));
});

test("routes: ariaSortFor and column titles", () => {
  assert.equal(routes.ariaSortFor("date", "desc", "date"), "descending");
  assert.equal(routes.ariaSortFor("date", "asc", "date"), "ascending");
  assert.equal(routes.ariaSortFor("date", "asc", "elapsed"), "none");
  for (const key of ["date", "elapsed", "moving", "efficiency", "temp", "car", "vs"]) {
    assert.ok(routes.COLUMN_TITLES[key], key);
  }
  for (const line of routes.buildOverlayLines({ stats: {}, drives: [] }, null)) {
    assert.ok(routes.OVERLAY_TITLES[line.label], line.label);
  }
});

// -- places ----------------------------------------------------------------------------

test("places: placeTooltip summarizes a place", () => {
  const text = places.placeTooltip({
    label: "Home",
    name: "Home",
    visits: 79,
    last_visit_ts: Date.now() / 1000,
    radius_m: 100.4,
    source: "zone",
  });
  assert.equal(text, "Home · 79 visits · last today · 100 m radius · Home Assistant zone");
  assert.ok(places.placeTooltip({ label: "Place #3", visits: 3, source: "auto" }).includes("suggestion"));
  assert.equal(places.placeTooltip(null), "");
});

// -- drive explorer ----------------------------------------------------------------------------

test("explorer: statTileTitle covers every tile label", () => {
  for (const label of Object.keys(rde.STAT_TILE_TITLES)) assert.ok(rde.statTileTitle(label), label);
  assert.ok(rde.statTileTitle("Busiest month · 80 mi").includes("most miles"));
  assert.equal(rde.statTileTitle("Nonsense"), "");
  for (const key of ["speed", "elevation", "efficiency"]) assert.ok(rde.ROUTE_COLOR_TITLES[key]);
  for (const key of ["chunks", "rolling", "model"]) assert.ok(rde.EFF_METHOD_TITLES[key]);
});

test("explorer: stepChartX steps and clamps", () => {
  assert.equal(rde.stepChartX(10, 1, 100), 18);
  assert.equal(rde.stepChartX(10, -1, 100), 2);
  assert.equal(rde.stepChartX(3, -1, 100), 0);
  assert.equal(rde.stepChartX(98, 1, 100), 100);
  assert.equal(rde.stepChartX(50, 10, 1000, 2), 70);
  assert.equal(rde.stepChartX(NaN, 1, 100), 8);
});

test("explorer: heatCountAt picks the busiest nearby cell", () => {
  const counts = new Map([
    ["5,5", 3],
    ["6,5", 12],
    ["9,9", 40],
  ]);
  assert.equal(rde.heatCountAt(counts, 5.5, 5.5), 12);
  assert.equal(rde.heatCountAt(counts, 5.2, 5.5), 3);
  assert.equal(rde.heatCountAt(counts, 9.5, 9.5), 40);
  assert.equal(rde.heatCountAt(counts, 20, 20), 0);
  assert.equal(rde.heatCountAt(null, 1, 1), 0);
  assert.equal(rde.heatCountAt(counts, NaN, 1), 0);
});

test("explorer: heatTipText pluralizes", () => {
  assert.equal(rde.heatTipText(1), "Driven 1 time");
  assert.equal(rde.heatTipText(12), "Driven 12 times");
  assert.equal(rde.heatTipText(0), "");
});
