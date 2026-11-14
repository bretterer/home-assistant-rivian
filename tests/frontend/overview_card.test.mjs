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
} from "../../custom_components/rivian/frontend/rivian-overview-card.js";

test("confirmMatches: exact match", () => {
  assert.equal(confirmMatches("My R2", "My R2"), true);
});

test("confirmMatches: case-insensitive match", () => {
  assert.equal(confirmMatches("my r2", "My R2"), true);
});

test("confirmMatches: extra surrounding whitespace is trimmed", () => {
  assert.equal(confirmMatches("  My R2  ", "My R2"), true);
});

test("confirmMatches: a cancelled prompt (null) never matches", () => {
  assert.equal(confirmMatches(null, "My R2"), false);
  assert.equal(confirmMatches(undefined, "My R2"), false);
});

test("confirmMatches: wrong text does not match", () => {
  assert.equal(confirmMatches("My R1T", "My R2"), false);
});

test("confirmMatches: empty string does not match a real name", () => {
  assert.equal(confirmMatches("", "My R2"), false);
});

test("deleteVehicleMessage: the vehicle keeps recording", () => {
  const msg = deleteVehicleMessage("My R1S");
  assert.match(msg, /permanently deletes/);
  assert.match(msg, /still be recorded/);
});
