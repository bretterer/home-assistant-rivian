// Node smoke test for rivian-charging-sessions-card.js's pure helpers.
//
// The card module guards every top-level use of HTMLElement/customElements/
// window/document, so it imports cleanly here with no DOM. Run with:
//   node --test tests/frontend/
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  sortSessionsNewestFirst,
  formatSessionTime,
  formatSessionDuration,
  deleteSessionMessage,
} from "../../custom_components/rivian/frontend/rivian-charging-sessions-card.js";

const TZ = "America/Denver";

test("sortSessionsNewestFirst: orders by start_time descending", () => {
  const sessions = [
    { session_id: "a", start_time: "2026-09-01T10:00:00+00:00" },
    { session_id: "b", start_time: "2026-09-26T10:00:00+00:00" },
    { session_id: "c", start_time: "2026-09-15T10:00:00+00:00" },
  ];
  assert.deepEqual(sortSessionsNewestFirst(sessions).map((s) => s.session_id), ["b", "c", "a"]);
});

test("sortSessionsNewestFirst: missing/invalid start_time sorts last, input not mutated", () => {
  const sessions = [
    { session_id: "a", start_time: "2026-09-01T10:00:00+00:00" },
    { session_id: "b", start_time: null },
  ];
  const sorted = sortSessionsNewestFirst(sessions);
  assert.deepEqual(sorted.map((s) => s.session_id), ["a", "b"]);
  assert.equal(sessions[0].session_id, "a"); // original order untouched
});

test("sortSessionsNewestFirst: empty/non-array input", () => {
  assert.deepEqual(sortSessionsNewestFirst(null), []);
  assert.deepEqual(sortSessionsNewestFirst([]), []);
});

test("formatSessionTime: formats in the given time zone", () => {
  assert.equal(formatSessionTime("2026-09-20T21:35:00+00:00", TZ), "Sep 20, 3:35 PM");
});

test("formatSessionTime: missing/invalid is a dash", () => {
  assert.equal(formatSessionTime(null, TZ), "–");
  assert.equal(formatSessionTime("not-a-date", TZ), "–");
});

test("formatSessionDuration: minutes-only and hour:minute forms", () => {
  assert.equal(
    formatSessionDuration("2026-09-20T21:00:00+00:00", "2026-09-20T21:32:00+00:00"),
    "32 min"
  );
  assert.equal(
    formatSessionDuration("2026-09-20T21:00:00+00:00", "2026-09-20T22:05:00+00:00"),
    "1:05"
  );
});

test("formatSessionDuration: missing or inverted timestamps are a dash", () => {
  assert.equal(formatSessionDuration(null, "2026-09-20T21:32:00+00:00"), "–");
  assert.equal(
    formatSessionDuration("2026-09-20T22:00:00+00:00", "2026-09-20T21:00:00+00:00"),
    "–"
  );
});

test("deleteSessionMessage: matches the plan's exact confirm text", () => {
  const session = {
    start_time: "2026-09-26T21:00:00+00:00",
    start_soc: 45,
    end_soc: 81,
    energy_added_kwh: 51.5,
  };
  assert.equal(
    deleteSessionMessage(session, TZ),
    "Delete the DC fast-charge session on Sep 26 (45% → 81%, 51.5 kWh)? This can't be undone."
  );
});

test("deleteSessionMessage: missing fields render as '?'", () => {
  const msg = deleteSessionMessage({}, TZ);
  assert.ok(msg.includes("?% → ?%, ? kWh"));
});
