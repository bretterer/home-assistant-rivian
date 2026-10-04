// Node tests for the Overview card's multi-vehicle layout helpers.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  ROW_BREAKPOINT_PX,
  householdRows,
  isDimmed,
  overviewLayout,
} from "../../custom_components/rivian/frontend/rivian-overview-card.js";
import { sessionVehicle } from "../../custom_components/rivian/frontend/rivian-charging-sessions-card.js";

test("overviewLayout: a wide root is a horizontal row, a narrow one a column", () => {
  assert.equal(ROW_BREAKPOINT_PX, 900);
  assert.equal(overviewLayout(900), "row");
  assert.equal(overviewLayout(1400), "row");
  assert.equal(overviewLayout(899), "column");
  assert.equal(overviewLayout(390), "column");
  assert.equal(overviewLayout(0), "column");
  assert.equal(overviewLayout(undefined), "column");
});

test("isDimmed: cards outside the selection dim; no selection yet dims nothing", () => {
  assert.equal(isDimmed("VA", ["VA", "VB"]), false);
  assert.equal(isDimmed("VC", ["VA", "VB"]), true);
  assert.equal(isDimmed("VA", null), false);
});

test("householdRows: miles, drives, kWh and mi/kWh across the four windows", () => {
  const w = (miles, drives, kwh, eff) => ({ miles, drives, kwh, efficiency_mi_kwh: eff, mpge: 90, hours: 1 });
  const rows = householdRows({
    "7d": w(120, 5, 40, 3.0),
    "30d": w(500, 20, 170, 2.94),
    "365d": w(6000, 240, 2000, 3.0),
    all: w(12000, 480, 4000, 3.0),
  });
  assert.deepEqual(rows.map((r) => r.label), ["Miles", "Drives", "kWh", "mi/kWh"]);
  assert.deepEqual(rows[0].values, ["120", "500", "6,000", "12,000"]);
  assert.deepEqual(rows[3].values, ["3.00", "2.94", "3.00", "3.00"]);
});

test("householdRows: missing windows are dashes", () => {
  const rows = householdRows({});
  assert.deepEqual(rows[0].values, ["–", "–", "–", "–"]);
});

test("sessionVehicle: matches a combined-series session to its vehicle", () => {
  const vehicles = [{ vin: "VA", letter: "A" }, { vin: "VB", letter: "B" }];
  assert.equal(sessionVehicle({ vin: "VB" }, vehicles).letter, "B");
  assert.equal(sessionVehicle({ vin: "ZZ" }, vehicles), null);
  assert.equal(sessionVehicle({}, vehicles), null);
});
