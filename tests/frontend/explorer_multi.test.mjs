// Node tests for the several-vehicles helpers of rivian-drive-explorer-card.js:
// per-vehicle drive numbering (1A, 2A, 1B...), the combined day's map markers,
// the per-vehicle summary table and the multi-vehicle tree rows.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  breadcrumbParts,
  inkOn,
  multiDayMarkerSpecs,
  multiSegmentLabels,
  projectDay,
  vehicleSummaryRows,
  vinCounts,
  visibleTreeRows,
} from "../../custom_components/rivian/frontend/rivian-drive-explorer-card.js";

const TZ = "America/Denver";
const VEHICLES = [
  { vin: "VA", name: "Rivi", letter: "A", color: "#1b6ac9", color_dark: "#5aa0f0" },
  { vin: "VB", name: "Test R2", letter: "B", color: "#c9561b", color_dark: "#f0925a" },
];

// A: two drives (home -> work -> home). B: one drive, starting next to A's start.
// Both vehicles have a drive with id 1: vin must disambiguate.
function makeDay() {
  const seg = (vin, drive_id, index, start_ts, extra = {}) => ({
    vin,
    drive_id,
    index,
    start_ts,
    end_ts: start_ts + 600,
    distance_miles: 5,
    duration_seconds: 600,
    efficiency_mi_kwh: 2.5,
    ...extra,
  });
  return {
    date: "2026-09-23",
    totals: { drives: 3, miles: 15, hours: 0.5, energy_kwh: 6, efficiency_mi_kwh: 2.5 },
    segments: [
      seg("VA", 1, 0, 1_000_000, { moving_seconds: 500 }),
      seg("VB", 1, 0, 1_000_100, { moving_seconds: 400 }),
      seg("VA", 2, 1, 1_000_900, { moving_seconds: 300 }),
    ],
    vehicles: {
      VA: {
        start: { lat: 40.1242, lon: -104.7880 },
        end: { lat: 40.1242, lon: -104.7880 },
        stops: [{ after_index: 0, lat: 40.1742, lon: -104.8380, duration_seconds: 600 }],
        gaps: [],
        prior_tail: null,
        totals: { drives: 2, miles: 10, hours: 0.3, energy_kwh: 4, efficiency_mi_kwh: 2.5 },
      },
      VB: {
        start: { lat: 40.1243, lon: -104.7881 },
        end: { lat: 40.3242, lon: -104.9880 },
        stops: [],
        gaps: [],
        prior_tail: null,
        totals: { drives: 1, miles: 5, hours: 0.2, energy_kwh: 2, efficiency_mi_kwh: 2.5 },
      },
    },
  };
}

test("multiSegmentLabels: numbers each vehicle's drives 1.. in time order with its letter", () => {
  const day = makeDay();
  const labels = multiSegmentLabels(day.segments, VEHICLES);
  assert.deepEqual(day.segments.map((s) => labels.get(s)), ["1A", "1B", "2A"]);
});

test("projectDay: one vehicle's slice of a combined day, others kept aside", () => {
  const day = makeDay();
  const p = projectDay(day, "VA");
  assert.equal(p.vin, "VA");
  assert.deepEqual(p.segments.map((s) => s.drive_id), [1, 2]);
  assert.equal(p.stops.length, 1);
  assert.deepEqual(p.start, { lat: 40.1242, lon: -104.7880 });
  assert.equal(p.totals.drives, 2);
  assert.deepEqual(p.others.map((s) => s.vin), ["VB"]);
});

test("multiDayMarkerSpecs: per-vehicle start/end markers and numbered badges for later drives", () => {
  const specs = multiDayMarkerSpecs(makeDay(), VEHICLES, null);
  assert.deepEqual(specs.starts.map((m) => m.label), ["1A", "1B"]);
  assert.deepEqual(specs.ends.map((m) => m.label), ["2A", "1B"]);
  assert.ok(specs.starts.every((m) => !m.selected));
  assert.equal(specs.starts[0].color, "#1b6ac9");
  // Only A's second drive starts somewhere new (its stop).
  assert.equal(specs.badges.length, 1);
  assert.deepEqual(specs.badges[0].labels, ["2A"]);
  assert.equal(specs.badges[0].lat, 40.1742);
  assert.equal(specs.badges[0].items[0].parked, 600);
});

test("multiDayMarkerSpecs: badges within 100 m merge across vehicles ('2A, 2B')", () => {
  const day = makeDay();
  day.segments.push({
    vin: "VB",
    drive_id: 2,
    index: 1,
    start_ts: 1_001_000,
    end_ts: 1_001_600,
  });
  day.vehicles.VB.stops = [{ after_index: 0, lat: 40.1744, lon: -104.8380, duration_seconds: 300 }];
  const specs = multiDayMarkerSpecs(day, VEHICLES, null);
  assert.equal(specs.badges.length, 1);
  assert.deepEqual(specs.badges[0].labels, ["2A", "2B"]);
  assert.deepEqual(specs.badges[0].items.map((i) => i.vin), ["VA", "VB"]);
});

test("multiDayMarkerSpecs: a selected drive gets its own selected start/end; everything else is a badge", () => {
  const specs = multiDayMarkerSpecs(makeDay(), VEHICLES, { vin: "VB", driveId: 1 });
  assert.equal(specs.starts.length, 1);
  assert.equal(specs.ends.length, 1);
  assert.equal(specs.starts[0].vin, "VB");
  assert.equal(specs.starts[0].selected, true);
  assert.equal(specs.ends[0].lat, 40.3242);
  // A's 1A (start, 11 m from B's start) and 2A (the stop) become badges.
  const labels = specs.badges.flatMap((b) => b.labels).sort();
  assert.deepEqual(labels, ["1A", "2A"]);
  const near = specs.badges.find((b) => b.labels.includes("1A"));
  assert.equal(near.beside, true);
});

test("multiDayMarkerSpecs: the same drive id on two vehicles selects only the matching vehicle", () => {
  const specs = multiDayMarkerSpecs(makeDay(), VEHICLES, { vin: "VA", driveId: 1 });
  assert.equal(specs.starts[0].vin, "VA");
  assert.equal(specs.starts[0].number, 1);
  assert.ok(specs.badges.flatMap((b) => b.labels).includes("1B"));
});

test("multiDayMarkerSpecs: empty day", () => {
  const specs = multiDayMarkerSpecs({ segments: [], vehicles: {} }, VEHICLES, null);
  assert.deepEqual(specs, { starts: [], ends: [], badges: [] });
});

test("vehicleSummaryRows: one row per vehicle that drove, plus a combined total", () => {
  const { rows, total } = vehicleSummaryRows(makeDay(), VEHICLES);
  assert.deepEqual(rows.map((r) => [r.letter, r.drives, r.miles]), [
    ["A", 2, 10],
    ["B", 1, 5],
  ]);
  assert.equal(rows[0].movingSeconds, 800);
  assert.equal(rows[1].energyKwh, 2);
  assert.equal(total.drives, 3);
  assert.equal(total.movingSeconds, 1200);
  assert.equal(total.efficiency, 2.5);
});

test("vehicleSummaryRows: a vehicle with no drives that day is omitted", () => {
  const day = makeDay();
  delete day.vehicles.VB;
  const { rows } = vehicleSummaryRows(day, VEHICLES);
  assert.deepEqual(rows.map((r) => r.vin), ["VA"]);
});

test("vinCounts: colored per-vehicle counts in vehicle order, zeros skipped", () => {
  const counts = vinCounts({ VB: { drives: 4, miles: 40 }, VA: { drives: 0, miles: 0 } }, VEHICLES);
  assert.equal(counts.length, 1);
  assert.equal(counts[0].letter, "B");
  assert.equal(counts[0].color, "#c9561b");
  assert.equal(counts[0].drives, 4);
  assert.deepEqual(vinCounts(undefined, VEHICLES), []);
});

function makeCache() {
  const byVin = { VA: { drives: 2, miles: 10 }, VB: { drives: 1, miles: 5 } };
  return {
    root: {
      totals: { drives: 3, miles: 15, by_vin: byVin },
      years: [{ key: "2026", drives: 3, miles: 15, by_vin: byVin }],
    },
    years: { 2026: { months: [{ key: "2026-09", drives: 3, miles: 15, by_vin: byVin }] } },
    months: { "2026-09": { days: [{ key: "2026-09-23", drives: 3, miles: 15, by_vin: byVin }] } },
    days: { "2026-09-23": makeDay() },
  };
}

test("visibleTreeRows (multi): nodes carry colored per-vehicle counts", () => {
  const rows = visibleTreeRows(makeCache(), { level: "day", key: "2026-09-23" }, TZ, {
    multi: true,
    vehicles: VEHICLES,
  });
  const nodes = rows.filter((r) => r.level !== "segment");
  assert.equal(nodes.length, 4);
  for (const node of nodes) {
    assert.deepEqual(node.vinCounts.map((c) => [c.letter, c.drives]), [
      ["A", 2],
      ["B", 1],
    ]);
  }
});

test("visibleTreeRows (multi): a day's drives interleave by time as '1A', '1B', '2A' with vehicle colors", () => {
  const rows = visibleTreeRows(makeCache(), { level: "day", key: "2026-09-23" }, TZ, {
    multi: true,
    vehicles: VEHICLES,
  });
  const segs = rows.filter((r) => r.level === "segment");
  assert.deepEqual(segs.map((r) => r.number), ["1A", "1B", "2A"]);
  assert.deepEqual(segs.map((r) => r.vin), ["VA", "VB", "VA"]);
  assert.equal(segs[1].color, "#c9561b");
  assert.equal(segs[1].colorDark, "#f0925a");
  assert.ok(segs.every((r) => !r.selected));
});

test("visibleTreeRows (multi): selection matches drive id AND vehicle", () => {
  const rows = visibleTreeRows(
    makeCache(),
    { level: "segment", key: "2026-09-23", driveId: 1, vin: "VB" },
    TZ,
    { multi: true, vehicles: VEHICLES }
  );
  const selected = rows.filter((r) => r.selected);
  assert.equal(selected.length, 1);
  assert.equal(selected[0].number, "1B");
});

test("visibleTreeRows: single-vehicle rows are unchanged (numeric numbers, no vehicle fields)", () => {
  const cache = makeCache();
  cache.days["2026-09-23"].segments = cache.days["2026-09-23"].segments
    .filter((s) => s.vin === "VA")
    .map(({ vin: _vin, ...s }) => s);
  const rows = visibleTreeRows(cache, { level: "day", key: "2026-09-23" }, TZ);
  const segs = rows.filter((r) => r.level === "segment");
  assert.deepEqual(segs.map((r) => r.number), [1, 2]);
  assert.equal("vin" in segs[0], false);
  assert.equal("vinCounts" in rows[0], false);
});

test("breadcrumbParts (multi): the drive crumb carries its number and vehicle", () => {
  const parts = breadcrumbParts(
    makeCache(),
    { level: "segment", key: "2026-09-23", driveId: 1, vin: "VB" },
    TZ,
    { multi: true, vehicles: VEHICLES }
  );
  const last = parts[parts.length - 1];
  assert.equal(last.level, "segment");
  assert.equal(last.vin, "VB");
  assert.ok(last.label.startsWith("1B · "));
});

test("inkOn: white on dark fills, near-black on light fills", () => {
  assert.equal(inkOn("#1b3a8a"), "#ffffff");
  assert.equal(inkOn("#f2d03b"), "#111111");
  assert.equal(inkOn("nonsense"), "#ffffff");
});
