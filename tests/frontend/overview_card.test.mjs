// Node smoke test for rivian-overview-card.js's pure helpers.
//
// The card module guards every top-level use of HTMLElement/customElements/
// window/document, so it imports cleanly here with no DOM. Run with:
//   node --test tests/frontend/
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  confirmMatches,
  deleteVehicleMessage,
  demoStatus,
} from "../../custom_components/rivian/frontend/rivian-overview-card.js";

test("confirmMatches: exact match", () => {
  assert.equal(confirmMatches("Demo R2", "Demo R2"), true);
});

test("confirmMatches: case-insensitive match", () => {
  assert.equal(confirmMatches("demo r2", "Demo R2"), true);
});

test("confirmMatches: extra surrounding whitespace is trimmed", () => {
  assert.equal(confirmMatches("  Demo R2  ", "Demo R2"), true);
});

test("confirmMatches: a cancelled prompt (null) never matches", () => {
  assert.equal(confirmMatches(null, "Demo R2"), false);
  assert.equal(confirmMatches(undefined, "Demo R2"), false);
});

test("confirmMatches: wrong text does not match", () => {
  assert.equal(confirmMatches("Demo R1T", "Demo R2"), false);
});

test("confirmMatches: empty string does not match a real name", () => {
  assert.equal(confirmMatches("", "Demo R2"), false);
});

test("deleteVehicleMessage: a demo vehicle is removed completely", () => {
  const msg = deleteVehicleMessage("Demo R2", true);
  assert.match(msg, /removes the demo vehicle/);
  assert.match(msg, /completely/);
  assert.doesNotMatch(msg, /still be recorded/);
});

test("deleteVehicleMessage: a real vehicle keeps recording", () => {
  const msg = deleteVehicleMessage("My R1S", false);
  assert.match(msg, /permanently deletes/);
  assert.match(msg, /still be recorded/);
});

test("demoStatus: reads the summary vehicle block", () => {
  const s = demoStatus({
    battery_pct: 61.4,
    range_mi: 187.2,
    odometer_mi: 1823.6,
    location: "Home",
  });
  assert.equal(s.socValue, 61.4);
  assert.equal(s.rangeText, "187 mi");
  assert.equal(s.odometerText, "1,824 mi");
  assert.equal(s.locationText, "Home");
});

test("demoStatus: missing values come back null", () => {
  const s = demoStatus(null);
  assert.equal(s.socValue, null);
  assert.equal(s.rangeText, null);
  assert.equal(s.odometerText, null);
  assert.equal(s.locationText, null);
});
