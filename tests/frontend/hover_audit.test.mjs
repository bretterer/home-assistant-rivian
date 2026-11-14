// Node tests for the tooltip / hover / keyboard helpers added in the hover
// audit, across every Rivian card module (all import without a DOM).
//   node --test tests/frontend/
import assert from "node:assert/strict";
import { test } from "node:test";

import * as bar from "../../custom_components/rivian/frontend/rivian-vehicle-bar.js";
import * as overview from "../../custom_components/rivian/frontend/rivian-overview-card.js";

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
