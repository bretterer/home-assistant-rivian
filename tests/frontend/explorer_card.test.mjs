// Node smoke test for rivian-drive-explorer-card.js's pure helpers.
//
// The card module guards every top-level use of HTMLElement/customElements/
// window/document, so it imports cleanly here with no DOM. Run with:
//   node --test tests/frontend/
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  heatColor,
  dayMarkerSpecs,
  heatLineWidth,
  heatStrokes,
  speedColor,
  speedScaleMax,
  dayScaleMax,
  speedPieces,
  speedGradientCss,
  heatGradientCss,
  formatDuration,
  formatParkedDuration,
  formatGapDistance,
  formatBytes,
  formatMonthLabel,
  formatDayLabel,
  visibleTreeRows,
  drillDownRows,
  breadcrumbParts,
  dayTimeline,
  rollingEfficiency,
  rollingEfficiencySeries,
  continuousDriveSeries,
  continuousChunkSteps,
  PRIOR_TAIL_KEY,
  chunkSteps,
  nearestPointIndex,
  elevationColor,
  efficiencyColor,
  ROLLING_EFF_MIN_SOC_DROP_PCT,
  trackFilledRuns,
  splitFilledRuns,
  segmentPlaceLabel,
  deleteDriveMessage,
  deleteDayMessage,
} from "../../custom_components/rivian/frontend/rivian-drive-explorer-card.js";

const TZ = "America/Denver";

test("heatColor: zero/negative counts are transparent (null)", () => {
  assert.equal(heatColor(0, 10), null);
  assert.equal(heatColor(-1, 10), null);
  assert.equal(heatColor(null, 10), null);
});

test("heatColor: endpoints are the documented blue and red", () => {
  assert.equal(heatColor(1, 10), "rgb(37, 99, 235)");
  assert.equal(heatColor(10, 10), "rgb(220, 38, 38)");
});

test("heatColor: passes through blue -> green -> yellow -> red in order (log scale)", () => {
  const scaleMax = 1_000_000;
  const toRgb = (s) => s.match(/\d+/g).map(Number);
  // Pick counts that land (via the log scale) almost exactly on each of the
  // documented stops, and check we get close to that stop's color.
  const atFraction = (t) => Math.max(1, Math.round(scaleMax ** t));
  assert.deepEqual(toRgb(heatColor(atFraction(0), scaleMax)), [37, 99, 235]); // blue
  const green = toRgb(heatColor(atFraction(0.4), scaleMax));
  assert.ok(Math.abs(green[0] - 22) <= 2 && Math.abs(green[1] - 163) <= 2 && Math.abs(green[2] - 74) <= 2);
  const yellow = toRgb(heatColor(atFraction(0.7), scaleMax));
  assert.ok(
    Math.abs(yellow[0] - 250) <= 2 && Math.abs(yellow[1] - 204) <= 2 && Math.abs(yellow[2] - 21) <= 2
  );
  assert.deepEqual(toRgb(heatColor(atFraction(1), scaleMax)), [220, 38, 38]); // red
});

test("heatColor: clamps counts above scaleMax to the top color", () => {
  assert.equal(heatColor(10, 10), heatColor(1000, 10));
});

test("speedColor: gray for unknown speed, red at 0, green at/above max", () => {
  assert.equal(speedColor(null, 60), "#888888");
  assert.equal(speedColor(undefined, 60), "#888888");
  assert.equal(speedColor(0, 60), "#d32f2f");
  assert.equal(speedColor(60, 60), "#2ea043");
});

test("speedScaleMax: floors at the minimum and ignores a single spike", () => {
  const n = 100;
  const lat = Array.from({ length: n }, (_, i) => i);
  const speed_mps = Array.from({ length: n }, () => 1);
  speed_mps[n - 1] = 100; // one GPS spike among many steady points
  const max = speedScaleMax({ lat, speed_mps });
  assert.ok(max >= 5);
  assert.ok(max < 100, "a single spike should not dominate the scale");
});

test("dayScaleMax: combines every segment's points onto one scale", () => {
  const segments = [
    { track: { lat: [0, 1], speed_mps: [5, 5] } },
    { track: { lat: [0, 1], speed_mps: [50, 50] } },
  ];
  const combined = dayScaleMax(segments);
  const single = speedScaleMax(segments[1].track);
  assert.ok(combined >= single - 1, "the day scale should reflect the fastest segment");
});

test("dayScaleMax: empty/missing tracks fall back to the minimum", () => {
  assert.equal(dayScaleMax([]), 5);
  assert.equal(dayScaleMax([{ track: null }, {}]), 5);
});

test("speedPieces: merges consecutive same-color pieces and keeps continuity", () => {
  const track = { lat: [0, 0.001, 0.002], lon: [0, 0, 0], speed_mps: [0, 0, 0] };
  const pieces = speedPieces(track, 60);
  assert.equal(pieces.length, 1);
  assert.equal(pieces[0].points.length, 3);
});

test("speedGradientCss / heatGradientCss: produce a CSS linear-gradient", () => {
  assert.match(speedGradientCss(), /^linear-gradient\(to right, .+\)$/);
  assert.match(heatGradientCss(), /^linear-gradient\(to right, .+\)$/);
});

test("formatDuration / formatParkedDuration / formatBytes", () => {
  assert.equal(formatDuration(90 * 60), "1:30");
  assert.equal(formatDuration(45 * 60), "45 min");
  assert.equal(formatDuration(null), "–");
  assert.equal(formatParkedDuration(72 * 60), "Parked 1 h 12 min");
  assert.equal(formatParkedDuration(5 * 60), "Parked 5 min");
  assert.equal(formatBytes(3.4 * 1024 * 1024), "3.4 MB");
  assert.equal(formatBytes(500), "500 B");
});

test("formatMonthLabel / formatDayLabel: no time-zone shifting", () => {
  assert.equal(formatMonthLabel("2026-09"), "September");
  assert.equal(formatMonthLabel("2026-01"), "January");
  assert.equal(formatDayLabel("2026-09-23"), "Wed, Sep 23");
  assert.equal(formatDayLabel("2026-09-24"), "Thu, Sep 24");
  // Known reference date: 2026-01-01 is a Thursday.
  assert.equal(formatDayLabel("2026-01-01"), "Thu, Jan 1");
});

// -- visibleTreeRows / breadcrumbParts fixtures ------------------------------

function buildCache() {
  return {
    root: {
      totals: { drives: 153, miles: 1428, hours: 200, energy_kwh: 600, efficiency_mi_kwh: 2.28 },
      years: [
        { key: "2026", drives: 153, miles: 1428, hours: 200, energy_kwh: 600, efficiency_mi_kwh: 2.28 },
        { key: "2025", drives: 10, miles: 100, hours: 5, energy_kwh: 40, efficiency_mi_kwh: 2.5 },
      ],
    },
    years: {
      "2026": {
        totals: { drives: 153, miles: 1428, hours: 200, energy_kwh: 600, efficiency_mi_kwh: 2.28 },
        years: [],
        months: [
          { key: "2026-09", drives: 100, miles: 900, hours: 120, energy_kwh: 400, efficiency_mi_kwh: 2.25 },
          { key: "2026-08", drives: 53, miles: 528, hours: 80, energy_kwh: 200, efficiency_mi_kwh: 2.35 },
        ],
      },
    },
    months: {
      "2026-09": {
        totals: {},
        years: [],
        months: [],
        days: [
          { key: "2026-09-23", drives: 6, miles: 42.1, hours: 1.2, energy_kwh: 14, efficiency_mi_kwh: 3.0 },
          { key: "2026-09-22", drives: 2, miles: 10, hours: 0.3, energy_kwh: 3, efficiency_mi_kwh: 3.3 },
        ],
      },
    },
    days: {
      "2026-09-23": {
        date: "2026-09-23",
        totals: { drives: 6, miles: 42.1 },
        segments: [
          {
            drive_id: "d1",
            start_ts: 1758636720, // an arbitrary epoch second
            distance_miles: 11.4,
            duration_seconds: 24 * 60,
            efficiency_mi_kwh: 2.91,
            has_track: true,
            track_source: "live",
          },
          {
            drive_id: "d2",
            start_ts: 1758650000,
            distance_miles: 3.2,
            duration_seconds: 10 * 60,
            efficiency_mi_kwh: 2.5,
            has_track: false,
            track_source: null,
          },
        ],
      },
    },
  };
}

test("segmentPlaceLabel: both ends known join with an arrow", () => {
  assert.equal(
    segmentPlaceLabel({ start_place: { id: 1, label: "Home" }, end_place: { id: 2, label: "Work" } }),
    "Home → Work"
  );
});

test("segmentPlaceLabel: only one end known shows just that side", () => {
  assert.equal(segmentPlaceLabel({ start_place: { id: 1, label: "Home" }, end_place: null }), "Home →");
  assert.equal(segmentPlaceLabel({ start_place: null, end_place: { id: 2, label: "Work" } }), "→ Work");
});

test("segmentPlaceLabel: neither end known is empty", () => {
  assert.equal(segmentPlaceLabel({ start_place: null, end_place: null }), "");
  assert.equal(segmentPlaceLabel({}), "");
});

test("visibleTreeRows: segment rows append the place label to the time label", () => {
  const cache = buildCache();
  cache.days["2026-09-23"].segments[0].start_place = { id: 1, label: "Home", category: "home" };
  cache.days["2026-09-23"].segments[0].end_place = { id: 2, label: "Work", category: "work" };
  const rows = visibleTreeRows(cache, { level: "day", key: "2026-09-23" }, TZ);
  const seg1 = rows.find((r) => r.level === "segment" && r.driveId === "d1");
  assert.ok(seg1.label.includes("Home → Work"));
  const seg2 = rows.find((r) => r.level === "segment" && r.driveId === "d2");
  assert.equal(seg2.label.includes("→"), false);
});

test("visibleTreeRows: root selection shows years collapsed, no deeper levels", () => {
  const cache = buildCache();
  const rows = visibleTreeRows(cache, { level: "all" }, TZ);
  assert.equal(rows[0].level, "all");
  assert.equal(rows[0].selected, true);
  assert.equal(rows[0].expanded, true);
  const years = rows.filter((r) => r.level === "year");
  assert.equal(years.length, 2);
  for (const y of years) assert.equal(y.expanded, false);
  assert.equal(
    rows.some((r) => r.level === "month"),
    false
  );
});

test("visibleTreeRows: year selection expands that year only", () => {
  const cache = buildCache();
  const rows = visibleTreeRows(cache, { level: "year", key: "2026" }, TZ);
  const year2026 = rows.find((r) => r.level === "year" && r.key === "2026");
  const year2025 = rows.find((r) => r.level === "year" && r.key === "2025");
  assert.equal(year2026.expanded, true);
  assert.equal(year2026.selected, true);
  assert.equal(year2025.expanded, false);
  const months = rows.filter((r) => r.level === "month");
  assert.equal(months.length, 2);
  for (const m of months) assert.equal(m.expanded, false);
});

test("visibleTreeRows: month selection expands year+month, siblings collapsed", () => {
  const cache = buildCache();
  const rows = visibleTreeRows(cache, { level: "month", key: "2026-09" }, TZ);
  const sep = rows.find((r) => r.level === "month" && r.key === "2026-09");
  const aug = rows.find((r) => r.level === "month" && r.key === "2026-08");
  assert.equal(sep.expanded, true);
  assert.equal(sep.label, "September");
  assert.equal(aug.expanded, false);
  const days = rows.filter((r) => r.level === "day");
  assert.equal(days.length, 2);
  for (const d of days) assert.equal(d.expanded, false);
});

test("visibleTreeRows: day selection expands full path down to segments", () => {
  const cache = buildCache();
  const rows = visibleTreeRows(cache, { level: "day", key: "2026-09-23" }, TZ);
  const day23 = rows.find((r) => r.level === "day" && r.key === "2026-09-23");
  const day22 = rows.find((r) => r.level === "day" && r.key === "2026-09-22");
  assert.equal(day23.expanded, true);
  assert.equal(day23.selected, true);
  assert.equal(day22.expanded, false);
  const segments = rows.filter((r) => r.level === "segment");
  assert.equal(segments.length, 2);
  assert.equal(segments[1].badges.includes("no route"), true);
  assert.match(segments[0].meta, /11\.4 mi.*24 min.*2\.91 mi\/kWh/);
});

test("visibleTreeRows: segment selection marks exactly that drive selected", () => {
  const cache = buildCache();
  const rows = visibleTreeRows(cache, { level: "segment", key: "2026-09-23", driveId: "d2" }, TZ);
  const segments = rows.filter((r) => r.level === "segment");
  assert.equal(segments.find((s) => s.driveId === "d1").selected, false);
  assert.equal(segments.find((s) => s.driveId === "d2").selected, true);
});

test("breadcrumbParts: builds the full path with clickable labels", () => {
  const cache = buildCache();
  const parts = breadcrumbParts(cache, { level: "segment", key: "2026-09-23", driveId: "d1" }, TZ);
  assert.deepEqual(
    parts.map((p) => p.level),
    ["all", "year", "month", "day", "segment"]
  );
  assert.equal(parts[0].label, "All time");
  assert.equal(parts[1].label, "2026");
  assert.equal(parts[2].label, "September");
  assert.equal(parts[3].label, "Wed, Sep 23");
});

test("breadcrumbParts: root-only selection is just All time", () => {
  const parts = breadcrumbParts(buildCache(), { level: "all" }, TZ);
  assert.equal(parts.length, 1);
  assert.equal(parts[0].label, "All time");
});

test("heatStrokes: a dot per cell, a line per neighbour pair at the cooler count, coolest first", () => {
  const strokes = heatStrokes([
    [0, 0, 3],
    [1, 0, 5],
    [1, 1, 9],
    [5, 5, 1],
  ]);
  const dots = strokes.filter((s) => s.x0 === s.x1 && s.y0 === s.y1);
  const lines = strokes.filter((s) => s.x0 !== s.x1 || s.y0 !== s.y1);
  assert.equal(dots.length, 4);
  // (0,0)-(1,0) at 3, (0,0)-(1,1) diagonal at 3, (1,0)-(1,1) at 5; (5,5) is isolated.
  assert.deepEqual(
    lines.map((s) => s.value).sort((a, b) => a - b),
    [3, 3, 5]
  );
  const values = strokes.map((s) => s.value);
  assert.deepEqual(values, [...values].sort((a, b) => a - b));
  assert.deepEqual(heatStrokes([]), []);
});

test("heatStrokes: margin cells outside the tile still join to cells inside", () => {
  const strokes = heatStrokes([
    [-1, 0, 2],
    [0, 0, 2],
  ]);
  assert.ok(strokes.some((s) => s.x0 === -0.5 && s.x1 === 0.5));
});

test("heatLineWidth: a little wider than a cell, clamped", () => {
  assert.equal(heatLineWidth(4), 5);
  assert.equal(heatLineWidth(1), 2.5);
  assert.equal(heatLineWidth(64), 9);
});

test("formatGapDistance: miles for longer stretches, feet for short ones", () => {
  assert.equal(formatGapDistance(1216), "0.8 mi");
  assert.equal(formatGapDistance(150), "492 ft");
  assert.equal(formatGapDistance(undefined), "distance unknown");
});

// A day of four drives: home -> A -> B -> home -> C (C is the day's end).
const HOME = { lat: 39.6785, lon: -104.9085 };
const markerDay = {
  date: "2026-09-16",
  segments: [
    { index: 0, drive_id: "d1" },
    { index: 1, drive_id: "d2" },
    { index: 2, drive_id: "d3" },
    { index: 3, drive_id: "d4" },
  ],
  start: { ...HOME, ts: 1 },
  end: { lat: 39.7442, lon: -105.0380, ts: 9 },
  stops: [
    { after_index: 0, lat: 39.7242, lon: -104.9880, duration_seconds: 600 },
    { after_index: 1, lat: 39.7342, lon: -104.9980, duration_seconds: 120 },
    { after_index: 2, lat: HOME.lat + 0.0001, lon: HOME.lon, duration_seconds: 3600 }, // ~11 m from home
  ],
};

test("dayMarkerSpecs: day view has green drive 1, red drive x, numbered badges for 2..x", () => {
  const specs = dayMarkerSpecs(markerDay);
  assert.deepEqual([specs.start.lat, specs.start.number], [HOME.lat, 1]);
  assert.deepEqual([specs.end.lat, specs.end.number], [39.7442, 4]);
  assert.deepEqual(
    specs.badges.map((b) => b.numbers),
    [[2], [3], [4]]
  );
  assert.deepEqual(specs.badges[0].parked, [600]);
  // Drive 4 starts at home, right next to the green start: drawn beside it.
  assert.deepEqual(
    specs.badges.map((b) => b.beside),
    [false, false, true]
  );
});

test("dayMarkerSpecs: segment view marks the selected drive's own start and end", () => {
  const specs = dayMarkerSpecs(markerDay, "d2");
  assert.equal(specs.start.number, 2);
  assert.equal(specs.start.lat, 39.7242); // where it left from: the stop after drive 1
  assert.equal(specs.end.number, 2);
  assert.equal(specs.end.lat, 39.7342); // where it parked: the stop after drive 2
  // Every other drive's start is badged, the one at drive 2's end beside the red square.
  assert.deepEqual(
    specs.badges.map((b) => [b.numbers, b.beside]),
    [
      [[1, 4], false],
      [[3], true],
    ]
  );
});

test("dayMarkerSpecs: empty day and missing stop positions", () => {
  assert.deepEqual(dayMarkerSpecs({ segments: [] }), { start: null, end: null, badges: [] });
  const noStops = dayMarkerSpecs({ ...markerDay, stops: [] });
  assert.deepEqual(noStops.badges, []);
});

// -- charts helpers -----------------------------------------------------

test("dayTimeline: single drive fills the whole width with no breaks", () => {
  const tl = dayTimeline([{ start_ts: 1000, end_ts: 1600, drive_id: "d1" }], 18, 400);
  assert.equal(tl.pieces.length, 1);
  assert.equal(tl.breaks.length, 0);
  assert.equal(tl.totalPx, 400);
  assert.equal(tl.tToX(1000), 0);
  assert.equal(tl.tToX(1600), 400);
  assert.equal(tl.tToX(1300), 200);
});

test("dayTimeline: drives are laid back to back, widths proportional to duration, with fixed-width breaks", () => {
  const segments = [
    { start_ts: 0, end_ts: 100, drive_id: "d1" }, // 100s
    { start_ts: 500, end_ts: 600, drive_id: "d2" }, // 100s, same duration -> equal width
  ];
  const tl = dayTimeline(segments, 18, 218); // 218 - 18 gap = 200 plot px, 100 each
  assert.equal(tl.pieces.length, 2);
  assert.equal(tl.breaks.length, 1);
  assert.equal(tl.pieces[0].x0, 0);
  assert.equal(tl.pieces[0].x1, 100);
  assert.equal(tl.breaks[0].x0, 100);
  assert.equal(tl.breaks[0].x1, 118);
  assert.equal(tl.pieces[1].x0, 118);
  assert.equal(tl.pieces[1].x1, 218);
  assert.equal(tl.totalPx, 218);
});

test("dayTimeline: tToX/xToT are monotonic and compress a gap's real time into its fixed pixel span", () => {
  const segments = [
    { start_ts: 0, end_ts: 100, drive_id: "d1" },
    { start_ts: 4000, end_ts: 4100, drive_id: "d2" }, // huge gap in real time
  ];
  const tl = dayTimeline(segments, 20, 220);
  // The whole 3900s gap maps onto the 20px break.
  assert.equal(tl.tToX(100), 100);
  assert.equal(tl.tToX(4000), 120);
  assert.equal(tl.tToX(2050), 110); // gap midpoint -> break midpoint
  // Monotonic across a sampled sweep, including before/after the data range.
  const samples = [-100, 0, 50, 100, 2000, 4000, 4050, 4100, 5000];
  const xs = samples.map((t) => tl.tToX(t));
  for (let i = 1; i < xs.length; i++) assert.ok(xs[i] >= xs[i - 1]);
  assert.equal(tl.tToX(-100), 0);
  assert.equal(tl.tToX(5000), 220);
  // xToT inverts tToX at the knots.
  assert.equal(tl.xToT(0), 0);
  assert.equal(tl.xToT(220), 4100);
  assert.equal(tl.xToT(110), 2050);
});

test("dayTimeline: empty input", () => {
  const tl = dayTimeline([], 18, 300);
  assert.deepEqual(tl.pieces, []);
  assert.deepEqual(tl.breaks, []);
  assert.equal(tl.tToX(123), 0);
  assert.equal(tl.xToT(50), null);
});

test("rollingEfficiency: flat SoC never finds a drop -> all null", () => {
  const track = {
    t: [0, 10, 20, 30],
    lat: [39.7242, 39.7252, 39.7262, 39.7272],
    lon: [-104.9880, -104.9880, -104.9880, -104.9880],
    soc: [80, 80, 80, 80],
    odo_m: [0, 100, 200, 300],
  };
  const out = rollingEfficiency(track, 100, ROLLING_EFF_MIN_SOC_DROP_PCT);
  assert.deepEqual(out, [null, null, null, null]);
});

test("rollingEfficiency: a clean 0.5% drop yields the expected mi/kWh", () => {
  // capacity 100 kWh; a 0.5% SoC drop = 0.5 kWh. 2 miles traveled (via odo_m) -> 4 mi/kWh.
  const track = {
    t: [0, 60, 120],
    lat: [0, 0, 0],
    lon: [0, 0, 0],
    soc: [80, 79.6, 79.5],
    odo_m: [0, 1609.344 * 1, 1609.344 * 2],
  };
  const out = rollingEfficiency(track, 100, 0.5);
  assert.equal(out[0], null);
  assert.equal(out[1], null); // only a 0.4-point drop so far
  assert.ok(Math.abs(out[2] - 4) < 1e-9);
});

test("rollingEfficiency: missing capacity or SoC -> null throughout", () => {
  const track = { t: [0, 60], lat: [0, 0], lon: [0, 0], soc: [80, 79], odo_m: [0, 1000] };
  assert.deepEqual(rollingEfficiency(track, null, 0.5), [null, null]);
  assert.deepEqual(rollingEfficiency(track, 0, 0.5), [null, null]);
  const noSoc = { t: [0, 60], lat: [0, 0], lon: [0, 0], soc: [null, null], odo_m: [0, 1000] };
  assert.deepEqual(rollingEfficiency(noSoc, 100, 0.5), [null, null]);
});

test("rollingEfficiency: falls back to haversine distance when odo_m is missing", () => {
  const track = {
    t: [0, 60],
    lat: [39.7242, 39.7342],
    lon: [-104.9880, -104.9880],
    soc: [80, 79],
    odo_m: [null, null],
  };
  const out = rollingEfficiency(track, 100, 0.5);
  assert.equal(out[0], null);
  assert.ok(out[1] > 0);
});

test("chunkSteps: builds explicit [t0, t1] boundaries per chunk", () => {
  const steps = chunkSteps([
    { start_ts: 1000, duration_seconds: 180, efficiency_mi_kwh: 2.5 },
    { start_ts: 1180, duration_seconds: 180, efficiency_mi_kwh: null },
  ]);
  assert.deepEqual(steps, [
    { t0: 1000, t1: 1180, value: 2.5 },
    { t0: 1180, t1: 1360, value: null },
  ]);
  assert.deepEqual(chunkSteps([]), []);
  assert.deepEqual(chunkSteps(null), []);
});

test("continuousDriveSeries: rebases SoC across a parked gap so charging in between never counts as driving energy", () => {
  const partA = {
    t: [0, 60],
    lat: [40, 40],
    lon: [-105, -105],
    soc: [60, 55],
    odo_m: [100, 200],
  };
  // Raw SoC jumps up to 90 here -- the car charged during the parked gap.
  const partB = {
    t: [1000, 1060],
    lat: [41, 41],
    lon: [-106, -106],
    soc: [90, 85],
    odo_m: [5000, 5100],
  };
  const series = continuousDriveSeries([
    { key: 0, track: partA },
    { key: 1, track: partB },
  ]);
  assert.deepEqual(series.partOf, [0, 0, 1, 1]);
  assert.deepEqual(series.ranges, { 0: [0, 2], 1: [2, 4] });
  // Rebased SoC continues smoothly from part A's last value -- no upward jump.
  assert.deepEqual(series.soc, [60, 55, 55, 50]);
  for (let i = 1; i < series.soc.length; i++) {
    assert.ok(series.soc[i] <= series.soc[i - 1], "rebased SoC never rises across the gap");
  }
  // The parked gap's (huge) geographic jump never gets added as driven distance.
  assert.deepEqual(series.distance_m, [0, 100, 100, 200]);
});

test("continuousDriveSeries + rollingEfficiencySeries: lookback crosses a drive boundary and fills the next drive's start", () => {
  const priorTail = {
    t: [-180, -120, -60],
    lat: [40, 40, 40],
    lon: [-105, -105, -105],
    soc: [80.6, 80.3, 80.0],
    odo_m: [8800, 8900, 9000],
  };
  const drive0 = {
    t: [0, 60, 120],
    lat: [40, 40.001, 40.002],
    lon: [-105, -105, -105],
    soc: [79.4, 79.2, 79.0],
    odo_m: [9000, 9300, 9600],
  };
  const series = continuousDriveSeries([
    { key: PRIOR_TAIL_KEY, track: priorTail },
    { key: 0, track: drive0 },
  ]);
  const out = rollingEfficiencySeries(series, 100, 0.3);
  assert.equal(out.length, 6);
  assert.equal(out[0], null); // nothing earlier than the very first point
  // drive0's own first point (index 3) is filled by looking back into the
  // prior tail -- the whole point of carrying it forward.
  assert.ok(out[3] !== null, "the first drive of the day is no longer blank at its start");
  assert.ok(Math.abs(out[3] - 0.20712373) < 1e-5);
  assert.ok(Math.abs(out[4] - 0.4970970) < 1e-5);
  assert.ok(Math.abs(out[5] - 0.9320568) < 1e-5);

  // A lone rollingEfficiency() call on drive0 alone (no prior context) can't
  // do this -- its own first minutes stay blank, which is exactly the gap
  // this fix removes.
  const lonely = rollingEfficiency(drive0, 100, 0.3);
  assert.equal(lonely[0], null);
});

test("continuousChunkSteps: a steady SoC drop tiles the whole span with no gaps and no null values", () => {
  const n = 11;
  const track = {
    t: Array.from({ length: n }, (_, i) => i * 60),
    lat: Array.from({ length: n }, () => 40),
    lon: Array.from({ length: n }, () => -105),
    soc: Array.from({ length: n }, (_, i) => 80 - i * 0.5),
    odo_m: Array.from({ length: n }, (_, i) => i * 500),
  };
  const series = continuousDriveSeries([{ key: 0, track }]);
  const steps = continuousChunkSteps(series, () => 100);
  assert.ok(steps.length > 0);
  for (const s of steps) {
    assert.notEqual(s.value, null);
    assert.equal(s.seg, 0);
  }
  // Contiguous: each step picks up exactly where the previous left off.
  for (let i = 1; i < steps.length; i++) {
    assert.equal(steps[i].t0, steps[i - 1].t1);
  }
  assert.equal(steps[0].t0, track.t[0]);
  assert.equal(steps[steps.length - 1].t1, track.t[n - 1]);
});

test("continuousChunkSteps: a chunk window straddling a drive boundary splits into per-drive pieces sharing one value", () => {
  const driveA = {
    t: [0, 60, 120],
    lat: [40, 40, 40],
    lon: [-105, -105, -105],
    soc: [80, 79.8, 79.6],
    odo_m: [0, 300, 600],
  };
  const driveB = {
    t: [130, 190, 250],
    lat: [40, 40, 40],
    lon: [-105, -105, -105],
    soc: [79.6, 79.3, 79.0],
    odo_m: [600, 900, 1200],
  };
  const series = continuousDriveSeries([
    { key: 0, track: driveA },
    { key: 1, track: driveB },
  ]);
  const steps = continuousChunkSteps(series, () => 100);
  // At least one window's [t0, t1] crosses from drive 0 into drive 1.
  const crossing = [];
  for (let i = 0; i + 1 < steps.length; i++) {
    if (steps[i].seg !== steps[i + 1].seg && steps[i].t1 === steps[i + 1].t0 && steps[i].value === steps[i + 1].value) {
      crossing.push([steps[i], steps[i + 1]]);
    }
  }
  assert.ok(crossing.length > 0, "expected a merged chunk split across the drive boundary");
});

test("continuousChunkSteps: prior_tail context never appears as its own drawn step", () => {
  const priorTail = {
    t: [-120, -60],
    lat: [40, 40],
    lon: [-105, -105],
    soc: [81, 80.5],
    odo_m: [8800, 9000],
  };
  const drive0 = {
    t: [0, 60, 120],
    lat: [40, 40, 40],
    lon: [-105, -105, -105],
    soc: [80.5, 80.0, 79.5],
    odo_m: [9000, 9300, 9600],
  };
  const series = continuousDriveSeries([
    { key: PRIOR_TAIL_KEY, track: priorTail },
    { key: 0, track: drive0 },
  ]);
  const steps = continuousChunkSteps(series, () => 100);
  assert.ok(steps.every((s) => s.seg !== PRIOR_TAIL_KEY));
  assert.ok(steps.length > 0);
});

test("trackFilledRuns: no filled array -> one unfilled run covering every point", () => {
  const track = { lat: [1, 2, 3, 4], lon: [1, 2, 3, 4] };
  assert.deepEqual(trackFilledRuns(track), [{ filled: false, from: 0, to: 3 }]);
});

test("trackFilledRuns: all-false filled array behaves the same as no array", () => {
  const track = { lat: [1, 2, 3], lon: [1, 2, 3], filled: [false, false, false] };
  assert.deepEqual(trackFilledRuns(track), [{ filled: false, from: 0, to: 2 }]);
});

test("trackFilledRuns: splits on filled transitions, runs sharing their boundary point", () => {
  const track = {
    lat: [1, 2, 3, 4, 5, 6],
    lon: [1, 2, 3, 4, 5, 6],
    filled: [false, false, true, true, false, false],
  };
  assert.deepEqual(trackFilledRuns(track), [
    { filled: false, from: 0, to: 2 },
    { filled: true, from: 2, to: 4 },
    { filled: false, from: 4, to: 5 },
  ]);
});

test("trackFilledRuns: a filled run at the very start or end", () => {
  const track = { lat: [1, 2, 3], lon: [1, 2, 3], filled: [true, true, false] };
  assert.deepEqual(trackFilledRuns(track), [
    { filled: true, from: 0, to: 2 },
    { filled: false, from: 2, to: 2 },
  ]);
});

test("trackFilledRuns: fewer than 2 points -> no runs", () => {
  assert.deepEqual(trackFilledRuns({ lat: [1], lon: [1] }), []);
  assert.deepEqual(trackFilledRuns({ lat: [], lon: [] }), []);
  assert.deepEqual(trackFilledRuns(null), []);
});

test("splitFilledRuns: one unfilled run when nothing is filled or gapped", () => {
  const points = [
    { t: 0, value: 1, seg: 0, filled: false },
    { t: 1, value: 2, seg: 0, filled: false },
    { t: 2, value: 3, seg: 0, filled: false },
  ];
  const runs = splitFilledRuns(points);
  assert.equal(runs.length, 1);
  assert.equal(runs[0].filled, false);
  assert.equal(runs[0].points.length, 3);
});

test("splitFilledRuns: a null value breaks a run (no bridging)", () => {
  const points = [
    { t: 0, value: 1, seg: 0, filled: false },
    { t: 1, value: null, seg: 0, filled: false },
    { t: 2, value: 3, seg: 0, filled: false },
  ];
  const runs = splitFilledRuns(points);
  assert.equal(runs.length, 2);
  assert.deepEqual(runs.map((r) => r.points.length), [1, 1]);
});

test("splitFilledRuns: a filled stretch splits off as its own run sharing its boundary points", () => {
  const points = [
    { t: 0, value: 1, seg: 0, filled: false },
    { t: 1, value: 2, seg: 0, filled: false },
    { t: 2, value: 3, seg: 0, filled: true },
    { t: 3, value: 4, seg: 0, filled: true },
    { t: 4, value: 5, seg: 0, filled: false },
  ];
  const runs = splitFilledRuns(points);
  assert.deepEqual(
    runs.map((r) => ({ filled: r.filled, ts: r.points.map((p) => p.t) })),
    [
      { filled: false, ts: [0, 1] },
      { filled: true, ts: [1, 2, 3] },
      { filled: false, ts: [3, 4] },
    ]
  );
});

test("splitFilledRuns: a seg change breaks a run even with matching filled flags", () => {
  const points = [
    { t: 0, value: 1, seg: 0, filled: false },
    { t: 1, value: 2, seg: 1, filled: false },
  ];
  const runs = splitFilledRuns(points);
  assert.equal(runs.length, 2);
  assert.deepEqual(runs.map((r) => r.points.length), [1, 1]);
});

test("nearestPointIndex: binary search including edges", () => {
  const track = { t: [0, 10, 20, 30, 40] };
  assert.equal(nearestPointIndex(track, -5), 0);
  assert.equal(nearestPointIndex(track, 0), 0);
  assert.equal(nearestPointIndex(track, 4), 0);
  assert.equal(nearestPointIndex(track, 6), 1);
  assert.equal(nearestPointIndex(track, 20), 2);
  assert.equal(nearestPointIndex(track, 100), 4);
  assert.equal(nearestPointIndex({ t: [] }, 5), -1);
  assert.equal(nearestPointIndex(null, 5), -1);
});

test("elevationColor / efficiencyColor: sequential/status endpoints and gray for unknown", () => {
  assert.equal(elevationColor(null, 0, 100), "#888888");
  assert.equal(elevationColor(0, 0, 100), "#cde2fb");
  assert.equal(elevationColor(100, 0, 100), "#0d366b");
  assert.equal(efficiencyColor(undefined, 0, 5), "#888888");
  assert.equal(efficiencyColor(0, 0, 5), "#d03b3b");
  assert.equal(efficiencyColor(5, 0, 5), "#0ca30c");
});

test("visibleTreeRows: a day's segments are numbered 1..x", () => {
  const data = {
    root: { totals: {}, years: [{ key: "2026", drives: 2 }] },
    years: { 2026: { months: [{ key: "2026-09", drives: 2 }] } },
    months: { "2026-09": { days: [{ key: "2026-09-16", drives: 2 }] } },
    days: { "2026-09-16": { segments: [{ index: 0, drive_id: "a", start_ts: 1 }, { index: 1, drive_id: "b", start_ts: 2 }] } },
  };
  const rows = visibleTreeRows(data, { level: "day", key: "2026-09-16" }, "America/Denver");
  assert.deepEqual(
    rows.filter((r) => r.level === "segment").map((r) => r.number),
    [1, 2]
  );
});

// -- drillDownRows (phone-width list) ---------------------------------------

test("drillDownRows: root shows only the years", () => {
  const rows = drillDownRows(visibleTreeRows(buildCache(), { level: "all" }, TZ));
  assert.deepEqual(rows.map((r) => r.key), ["2026", "2025"]);
  assert.ok(rows.every((r) => r.level === "year"));
});

test("drillDownRows: a month shows its days, not the other months", () => {
  const rows = drillDownRows(visibleTreeRows(buildCache(), { level: "month", key: "2026-09" }, TZ));
  assert.deepEqual(rows.map((r) => r.key), ["2026-09-23", "2026-09-22"]);
});

test("drillDownRows: a day shows its drives; a drive shows its day's drives", () => {
  const cache = buildCache();
  const day = drillDownRows(visibleTreeRows(cache, { level: "day", key: "2026-09-23" }, TZ));
  assert.deepEqual(day.map((r) => r.driveId), ["d1", "d2"]);
  const seg = drillDownRows(
    visibleTreeRows(cache, { level: "segment", key: "2026-09-23", driveId: "d2" }, TZ)
  );
  assert.deepEqual(seg.map((r) => r.driveId), ["d1", "d2"]);
  assert.equal(seg[1].selected, true);
});

test("drillDownRows: a node with no loaded children shows just itself", () => {
  const rows = drillDownRows(visibleTreeRows(buildCache(), { level: "day", key: "2026-09-22" }, TZ));
  assert.equal(rows.length, 1);
  assert.equal(rows[0].key, "2026-09-22");
  assert.equal(rows[0].selected, true);
});

test("deleteDriveMessage: matches the plan's exact confirm text", () => {
  const seg = { drive_id: "d1", start_ts: Date.UTC(2026, 8, 16, 21, 35, 0) / 1000, distance_miles: 6.12 };
  assert.equal(
    deleteDriveMessage(seg, TZ),
    "Delete the 3:35 PM drive on Wed, Sep 16 (6.1 mi)? Its route, stats and efficiency data are removed. This can't be undone."
  );
});

test("deleteDriveMessage: missing distance renders as '?'", () => {
  const seg = { drive_id: "d1", start_ts: Date.UTC(2026, 8, 16, 21, 35, 0) / 1000 };
  assert.ok(deleteDriveMessage(seg, TZ).includes("(? mi)"));
});

test("deleteDayMessage: matches the plan's exact confirm text", () => {
  const dayData = { date: "2026-09-16", segments: new Array(7).fill({}) };
  assert.equal(deleteDayMessage(dayData, TZ), "Delete all 7 drives on Wed, Sep 16?");
});

test("deleteDayMessage: zero segments still renders (defensive)", () => {
  const dayData = { date: "2026-09-16", segments: [] };
  assert.equal(deleteDayMessage(dayData, TZ), "Delete all 0 drives on Wed, Sep 16?");
});
