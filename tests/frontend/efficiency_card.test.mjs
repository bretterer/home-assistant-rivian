// Node smoke test for rivian-efficiency-card.js's pure helpers.
//   node --test tests/frontend/
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  RANGES,
  X_OPTIONS,
  rangeDays,
  extractX,
  scatterPoints,
  pointsByVin,
  linearRegression,
  trendLineEnds,
  pointRadius,
  nearestPoint,
  groupBands,
  bandLabel,
  trendShape,
  scoreClass,
  formatScore,
  scoreGlyph,
  formatX,
  sortDrives,
  defaultDir,
  summaryStats,
  localDayKey,
  navigationSelection,
  drivesPath,
  scatterDomains,
  roundedTopBar,
  scaleLinear,
  niceDomain,
  esc,
} from "../../custom_components/rivian/frontend/rivian-efficiency-card.js";

const D = (over) => ({
  vin: "A",
  drive_id: "d1",
  date_ts: 1000,
  distance_mi: 10,
  duration_s: 1200,
  avg_speed_mph: 30,
  temp_f: 60,
  headwind_mph: 2,
  wind_speed_mph: 5,
  precip_mm: 0,
  air_density: 1.2,
  climb_ft_per_mi: 20,
  trip_length_mi: 10,
  efficiency_mi_kwh: 2.5,
  mpge: 84,
  expected_eff_mi_kwh: 2.4,
  score: 1.04,
  ...over,
});

test("range toggle maps to days", () => {
  assert.deepEqual(RANGES.map((r) => r[0]), ["30d", "90d", "1y", "all"]);
  assert.equal(rangeDays("30d"), 30);
  assert.equal(rangeDays("1y"), 365);
  assert.equal(rangeDays("all"), null);
  assert.equal(rangeDays("bogus"), 30);
});

test("extractX covers every axis choice and rejects missing values", () => {
  const d = D({});
  const expected = { temp: 60, headwind: 2, density: 1.2, rain: 0, speed: 30, length: 10, climb: 20 };
  assert.deepEqual(X_OPTIONS.map((o) => o.key), Object.keys(expected));
  for (const [k, v] of Object.entries(expected)) assert.equal(extractX(d, k), v);
  assert.equal(extractX(D({ temp_f: null }), "temp"), null);
  assert.equal(extractX(D({ headwind_mph: undefined }), "headwind"), null);
  assert.equal(extractX(D({ precip_mm: 0 }), "rain"), 0, "zero rain is data, not missing");
  assert.equal(extractX(null, "temp"), null);
});

test("scatterPoints omits drives without the X value and counts them", () => {
  const drives = [D({ drive_id: "a", date_ts: 2 }), D({ drive_id: "b", date_ts: 1, temp_f: null }), D({ drive_id: "c", efficiency_mi_kwh: null })];
  const { points, missing } = scatterPoints(drives, "temp");
  assert.equal(points.length, 1);
  assert.equal(points[0].drive.drive_id, "a");
  assert.equal(missing, 1);
  const byVin = pointsByVin(points, ["A", "B"]);
  assert.equal(byVin.A.length, 1);
  assert.deepEqual(byVin.B, []);
});

test("linearRegression: exact line, minimum points, no spread", () => {
  const pts = [0, 1, 2, 3, 4].map((x) => ({ x, y: 2 * x + 1 }));
  const reg = linearRegression(pts);
  assert.ok(Math.abs(reg.slope - 2) < 1e-9);
  assert.ok(Math.abs(reg.intercept - 1) < 1e-9);
  assert.ok(Math.abs(reg.r2 - 1) < 1e-9);
  assert.equal(reg.n, 5);
  assert.deepEqual(trendLineEnds(reg), [
    [0, 1],
    [4, 9],
  ]);
  assert.equal(linearRegression(pts.slice(0, 4)), null, "needs 5 points");
  assert.ok(linearRegression(pts.slice(0, 4), 4));
  assert.equal(linearRegression([1, 2, 3, 4, 5].map((y) => ({ x: 3, y }))), null);
  assert.equal(trendLineEnds(null), null);
  const flat = linearRegression([0, 1, 2, 3, 4].map((x) => ({ x, y: 3 })));
  assert.equal(flat.slope, 0);
  assert.equal(flat.r2, 0);
});

test("pointRadius scales with distance and is bounded", () => {
  assert.equal(pointRadius(0, 50), 3);
  assert.equal(pointRadius(50, 50), 9);
  assert.equal(pointRadius(500, 50), 9);
  assert.ok(pointRadius(12.5, 50) > 3 && pointRadius(12.5, 50) < 9);
});

test("nearestPoint finds the closest point within range", () => {
  const xs = scaleLinear(0, 10, 0, 100);
  const ys = scaleLinear(0, 10, 100, 0);
  const pts = [
    { x: 2, y: 2 },
    { x: 5, y: 5 },
  ];
  assert.deepEqual(nearestPoint(pts, xs, ys, 52, 48), { x: 5, y: 5 });
  assert.equal(nearestPoint(pts, xs, ys, 80, 20), null);
});

test("scatterDomains pads and includes trend ends", () => {
  const dom = scatterDomains(
    [
      { x: 10, y: 2 },
      { x: 90, y: 3 },
    ],
    [
      [
        [10, 1.5],
        [90, 3.6],
      ],
    ]
  );
  assert.ok(dom.x[0] <= 10 && dom.x[1] >= 90);
  assert.ok(dom.y[0] <= 1.5 && dom.y[1] >= 3.6);
  assert.equal(scatterDomains([], []), null);
});

test("groupBands orders bands numerically and keeps per-vehicle values", () => {
  const bands = {
    A: [
      { band: "30-39", miles: 5, kwh: 1.5, efficiency: 3.3 },
      { band: "0-9", miles: 1, kwh: 0.5, efficiency: 2 },
      { band: "70+", miles: 9, kwh: 4, efficiency: 2.25 },
      { band: "10-19", miles: 3, kwh: 1, efficiency: null },
    ],
    B: [{ band: "30-39", miles: 2, kwh: 0.5, efficiency: 4 }],
  };
  const g = groupBands(bands, ["A", "B", "C"]);
  assert.deepEqual(g.bands, ["0-9", "30-39", "70+"]);
  assert.equal(g.series.A["30-39"].efficiency, 3.3);
  assert.equal(g.series.B["30-39"].efficiency, 4);
  assert.deepEqual(g.series.C, {});
  assert.equal(bandLabel("30-39"), "30–39");
  assert.equal(bandLabel("70+"), "70+");
});

test("trendShape keeps per-vehicle rows, spans, and overall bounds", () => {
  const trend = {
    A: {
      weekly: [
        [100, 2.5, 84, 30],
        [700000, 2.7, 91, 40],
      ],
      monthly: [[100, 2.6, 87, 70]],
    },
    B: { weekly: [[300000, 3, 101, 12]], monthly: [] },
  };
  const w = trendShape(trend, ["A", "B"], "weekly");
  assert.equal(w.span, 7 * 86400);
  assert.equal(w.series.A.length, 2);
  assert.equal(w.series.A[0].miles, 30);
  assert.equal(w.tMin, 100);
  assert.equal(w.tMax, 700000 + 7 * 86400);
  const m = trendShape(trend, ["A", "B"], "monthly");
  assert.ok(m.span > 29 * 86400);
  assert.equal(m.series.B.length, 0);
  const empty = trendShape({}, ["A"], "weekly");
  assert.equal(empty.tMin, null);
});

test("score class, format and glyph follow the 105 % / 95 % thresholds", () => {
  assert.equal(scoreClass(1.05), "good");
  assert.equal(scoreClass(1.2), "good");
  assert.equal(scoreClass(0.95), "bad");
  assert.equal(scoreClass(0.7), "bad");
  assert.equal(scoreClass(1.0), "ok");
  assert.equal(scoreClass(null), null);
  assert.equal(formatScore(1.043), "104 %");
  assert.equal(formatScore(null), "–");
  assert.equal(scoreGlyph(1.1), "▲");
  assert.equal(scoreGlyph(0.9), "▼");
  assert.equal(scoreGlyph(1), "");
});

test("formatX shows units and uses a true minus for tailwind", () => {
  assert.equal(formatX("temp", 41.6), "42 °F");
  assert.equal(formatX("headwind", -3.24), "−3.2 mph");
  assert.equal(formatX("density", 1.2346), "1.235 kg/m³");
  assert.equal(formatX("rain", null), "–");
});

test("sortDrives sorts either way with missing values last", () => {
  const drives = [
    D({ drive_id: "a", date_ts: 1, score: 1.1, temp_f: 50 }),
    D({ drive_id: "b", date_ts: 3, score: null, temp_f: 70 }),
    D({ drive_id: "c", date_ts: 2, score: 0.9, temp_f: null }),
  ];
  const ids = (key, dir) => sortDrives(drives, key, dir).map((d) => d.drive_id);
  assert.deepEqual(ids("date", "desc"), ["b", "c", "a"]);
  assert.deepEqual(ids("date", "asc"), ["a", "c", "b"]);
  assert.deepEqual(ids("score", "desc"), ["a", "c", "b"]);
  assert.deepEqual(ids("score", "asc"), ["c", "a", "b"]);
  assert.deepEqual(ids("temp", "asc"), ["a", "b", "c"]);
  const named = [D({ drive_id: "x", vin: "A" }), D({ drive_id: "y", vin: "B" })];
  const byName = sortDrives(named, "vehicle", "asc", (v) => (v === "A" ? "Zed" : "Amy"));
  assert.deepEqual(byName.map((d) => d.drive_id), ["y", "x"]);
  assert.equal(defaultDir("vehicle"), "asc");
  assert.equal(defaultDir("date"), "desc");
});

test("summaryStats weights efficiency by energy and finds best/worst conditions", () => {
  const drives = [
    D({ drive_id: "a", distance_mi: 10, efficiency_mi_kwh: 2, expected_eff_mi_kwh: 2.1, score: 1.0 }),
    D({ drive_id: "b", distance_mi: 30, efficiency_mi_kwh: 3, expected_eff_mi_kwh: 3.4, score: 1.1 }),
    D({ drive_id: "c", distance_mi: 0.5, efficiency_mi_kwh: 1, expected_eff_mi_kwh: 9, score: 0.8 }),
    D({ drive_id: "z", vin: "B", distance_mi: 20, efficiency_mi_kwh: 4, expected_eff_mi_kwh: 1.5, score: 1.2 }),
  ];
  const s = summaryStats(drives, ["A", "B"]);
  // A: 40.5 mi over 10/2 + 30/3 + 0.5/1 = 15.5 kWh
  assert.ok(Math.abs(s.perVin.A.eff - 40.5 / 15.5) < 1e-9);
  assert.equal(s.perVin.A.drives, 3);
  assert.equal(s.perVin.B.eff, 4);
  assert.ok(Math.abs(s.avgScore - (1.0 + 1.1 + 0.8 + 1.2) / 4) < 1e-9);
  assert.equal(s.best.drive_id, "b", "the 0.5 mi drive is ignored");
  assert.equal(s.worst.drive_id, "z");
  assert.equal(summaryStats([], ["A"]).best, null);
  assert.equal(summaryStats([D({})], ["A"]).worst, null, "one drive is not both best and worst");
});

test("navigationSelection matches the explorer's stored selection", () => {
  // 22:30 UTC on Sep 20 is still Sep 20 in Denver (UTC-6 in September).
  const ts = Date.UTC(2026, 8, 20, 22, 30) / 1000;
  const nav = navigationSelection(D({ drive_id: "drv9", vin: "VIN2", date_ts: ts }), ["VIN2", "VIN1"], "America/Denver");
  assert.equal(nav.storageKey, "rivian-drive-explorer-selection:VIN1,VIN2");
  assert.deepEqual(nav.selection, { level: "segment", key: "2026-09-20", driveId: "drv9", vin: "VIN2" });
  // 02:00 UTC on Sep 21 is still Sep 20 evening in Denver.
  assert.equal(localDayKey(Date.UTC(2026, 8, 21, 2, 0) / 1000, "America/Denver"), "2026-09-20");
  assert.equal(localDayKey(null, "UTC"), null);
  const single = navigationSelection(D({ vin: "V" }), [], "UTC");
  assert.equal(single.storageKey, "rivian-drive-explorer-selection:V");
  // One vehicle: the explorer's segments carry no vin, so the selection must not either.
  assert.equal("vin" in single.selection, false);
  assert.equal("vin" in navigationSelection(D({ vin: "V" }), ["V"], "UTC").selection, false);
  assert.equal(navigationSelection(D({ date_ts: null }), ["V"], "UTC"), null);
});

test("drivesPath swaps the last URL segment unless configured", () => {
  assert.equal(drivesPath("/rivian-dashboard/efficiency", null), "/rivian-dashboard/drives");
  assert.equal(drivesPath("/rivian-dashboard/efficiency/", null), "/rivian-dashboard/drives");
  assert.equal(drivesPath("/x/y", "/custom/drives"), "/custom/drives");
  assert.equal(drivesPath("", null), "/drives");
});

test("chart helpers: rounded bars and nice domains", () => {
  assert.equal(roundedTopBar(0, 10, 10, 0), "");
  assert.match(roundedTopBar(0, 10, 10, 20), /^M0\.0,30\.0V/);
  const [lo, hi] = niceDomain(0.3, 4.7, 5);
  assert.ok(lo <= 0.3 && hi >= 4.7);
  assert.equal(esc('<a "b">'), "&lt;a &quot;b&quot;&gt;");
});
