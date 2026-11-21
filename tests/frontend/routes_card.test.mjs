// Node smoke test for rivian-routes-card.js's pure helpers.
//
// The card module guards every top-level use of HTMLElement/customElements/
// window/document, so it imports cleanly here with no DOM. Run with:
//   node --test tests/frontend/
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  formatDuration,
  formatDelta,
  formatVsAvgPct,
  colorForVsAvg,
  formatShortDate,
  sortRoutes,
  sortRouteDrives,
  defaultSelectedDrive,
  mapCoverageText,
  buildOverlayLines,
  dotChartLayout,
} from "../../custom_components/rivian/frontend/rivian-routes-card.js";

test("formatDuration: m:ss under an hour, h:mm:ss at/over an hour", () => {
  assert.equal(formatDuration(0), "0:00");
  assert.equal(formatDuration(62), "1:02");
  assert.equal(formatDuration(842), "14:02");
  assert.equal(formatDuration(3600), "1:00:00");
  assert.equal(formatDuration(3725), "1:02:05");
});

test("formatDuration: null/undefined/NaN is '--:--'", () => {
  assert.equal(formatDuration(null), "--:--");
  assert.equal(formatDuration(undefined), "--:--");
  assert.equal(formatDuration(NaN), "--:--");
});

test("formatDelta: signed, using formatDuration's magnitude", () => {
  assert.equal(formatDelta(72), "+1:12");
  assert.equal(formatDelta(-40), "-0:40");
  assert.equal(formatDelta(0), "+0:00");
  assert.equal(formatDelta(null), "--");
});

test("formatVsAvgPct: signed percentage, rounded", () => {
  assert.equal(formatVsAvgPct(8.4), "+8%");
  assert.equal(formatVsAvgPct(-5.2), "-5%");
  assert.equal(formatVsAvgPct(0), "0%");
  assert.equal(formatVsAvgPct(null), "--");
});

test("colorForVsAvg: faster (negative) green, slower (positive) red, near zero neutral", () => {
  assert.equal(colorForVsAvg(-10), "var(--success-color, #2e7d32)");
  assert.equal(colorForVsAvg(10), "var(--error-color, #b00020)");
  assert.equal(colorForVsAvg(0), "var(--secondary-text-color)");
  assert.equal(colorForVsAvg(null), "var(--secondary-text-color)");
});

test("formatShortDate: month/day, year only when it differs from the reference", () => {
  const now = new Date("2026-06-15T12:00:00Z").getTime() / 1000;
  const sameYear = new Date("2026-01-05T12:00:00Z").getTime() / 1000;
  const otherYear = new Date("2025-01-05T12:00:00Z").getTime() / 1000;
  assert.equal(formatShortDate(sameYear, now).includes("2026"), false);
  assert.equal(formatShortDate(otherYear, now).includes("2025"), true);
  assert.equal(formatShortDate(null), "--");
});

test("sortRoutes: by drive_count desc, ties broken by id", () => {
  const routes = [
    { id: 3, drive_count: 5 },
    { id: 1, drive_count: 15 },
    { id: 2, drive_count: 15 },
  ];
  const sorted = sortRoutes(routes);
  assert.deepEqual(sorted.map((r) => r.id), [1, 2, 3]);
});

test("sortRouteDrives: sorts by key/direction, nulls always last", () => {
  const drives = [
    { drive_id: "a", duration_seconds: 600 },
    { drive_id: "b", duration_seconds: null },
    { drive_id: "c", duration_seconds: 500 },
  ];
  const asc = sortRouteDrives(drives, "elapsed", "asc");
  assert.deepEqual(asc.map((d) => d.drive_id), ["c", "a", "b"]);
  const desc = sortRouteDrives(drives, "elapsed", "desc");
  assert.deepEqual(desc.map((d) => d.drive_id), ["a", "c", "b"]);
});

test("sortRouteDrives: date accessor prefers sort_ts over start_ts", () => {
  const drives = [
    { drive_id: "a", sort_ts: 200, start_ts: 100 },
    { drive_id: "b", sort_ts: 100, start_ts: 999 },
  ];
  const asc = sortRouteDrives(drives, "date", "asc");
  assert.deepEqual(asc.map((d) => d.drive_id), ["b", "a"]);
});

test("defaultSelectedDrive: the most recent drive by date", () => {
  const route = {
    drives: [
      { drive_id: "old", sort_ts: 100 },
      { drive_id: "new", sort_ts: 300 },
      { drive_id: "mid", sort_ts: 200 },
    ],
  };
  assert.equal(defaultSelectedDrive(route).drive_id, "new");
});

test("defaultSelectedDrive: null for an empty/missing route", () => {
  assert.equal(defaultSelectedDrive({ drives: [] }), null);
  assert.equal(defaultSelectedDrive(null), null);
});

test("mapCoverageText: null when every drive (or none) has a map line", () => {
  assert.equal(
    mapCoverageText({ drives: [{ preview: { lat: [1, 2] } }, { preview: { lat: [1] } }] }),
    null
  );
  assert.equal(mapCoverageText({ drives: [] }), null);
});

test("mapCoverageText: counts drives missing a preview", () => {
  const route = {
    drives: [
      { preview: { lat: [1, 2] } },
      { preview: null },
      { preview: { lat: [] } },
    ],
  };
  assert.equal(mapCoverageText(route), "1 of 3 drives have a route on the map");
});

test("buildOverlayLines: fastest/average/slowest/drives/efficiency, plus selected drive info", () => {
  const route = {
    stats: {
      fastest_seconds: 500,
      fastest_drive_id: "fast",
      avg_seconds: 600,
      slowest_seconds: 700,
      slowest_drive_id: "slow",
      count: 3,
      avg_efficiency_mi_kwh: 2.5,
    },
  };
  const selected = { drive_id: "mid", duration_seconds: 650, rank: 2 };
  const lines = buildOverlayLines(route, selected);
  const byLabel = Object.fromEntries(lines.map((l) => [l.label, l.value]));
  assert.equal(byLabel["Fastest"], "8:20");
  assert.equal(byLabel["Average"], "10:00");
  assert.equal(byLabel["Slowest"], "11:40");
  assert.equal(byLabel["Drives"], "3");
  assert.equal(byLabel["Avg mi/kWh"], "2.50");
  assert.equal(byLabel["This drive"], "10:50");
  assert.equal(byLabel["Rank"], "#2 of 3");
  assert.ok(byLabel["vs best / avg"].includes("vs best"));
  assert.ok(byLabel["vs best / avg"].includes("vs avg"));
});

test("buildOverlayLines: unranked/outlier selected drive shows 'unranked (outlier)'", () => {
  const route = { stats: { count: 3 } };
  const selected = { drive_id: "x", duration_seconds: 9999, rank: null };
  const lines = buildOverlayLines(route, selected);
  const rank = lines.find((l) => l.label === "Rank");
  assert.equal(rank.value, "unranked (outlier)");
});

test("dotChartLayout: lays out points within the given width/height and omits incomplete drives", () => {
  const drives = [
    { drive_id: "a", sort_ts: 100, duration_seconds: 500 },
    { drive_id: "b", sort_ts: 200, duration_seconds: 700 },
    { drive_id: "c", sort_ts: 300, duration_seconds: null },
  ];
  const layout = dotChartLayout(drives, { width: 400, height: 100 });
  assert.equal(layout.points.length, 2);
  for (const p of layout.points) {
    assert.ok(p.x >= 0 && p.x <= 400);
    assert.ok(p.y >= 0 && p.y <= 100);
  }
  // Faster drive ("a") should plot higher (smaller y) than slower ("b").
  const a = layout.points.find((p) => p.driveId === "a");
  const b = layout.points.find((p) => p.driveId === "b");
  assert.ok(a.y < b.y);
});

test("dotChartLayout: empty input returns no points without throwing", () => {
  const layout = dotChartLayout([]);
  assert.deepEqual(layout.points, []);
});

// -- shared routes (schema v10): per-vehicle stats, favorites, colors ------------------

import {
  datasetGroups,
  driveKey,
  fastestSlowestKeys,
  formatVehicleCounts,
  routeDriveStyle,
  routeVehicleCounts,
  selectedRouteStats,
  vehicleStatRows,
} from "../../custom_components/rivian/frontend/rivian-routes-card.js";

const CARS = [
  { vin: "VA", letter: "A", color: "#1b6ac9" },
  { vin: "VB", letter: "B", color: "#c9561b" },
];

const SHARED_ROUTE = {
  id: 1,
  stats: {
    count: 8,
    fastest_seconds: 700,
    fastest_drive_id: "VB|b1",
    slowest_seconds: 1100,
    slowest_drive_id: "VA|a3",
    avg_seconds: 850,
    avg_efficiency_mi_kwh: 3,
    by_vin: {
      VA: { count: 5, fastest_seconds: 760, avg_seconds: 900, slowest_seconds: 1100, avg_efficiency_mi_kwh: 2.5 },
      VB: { count: 3, fastest_seconds: 700, avg_seconds: 775, slowest_seconds: 820, avg_efficiency_mi_kwh: 3.5 },
    },
  },
};

test("sortRoutes: by the selected vehicles' own counts (favorites), then total", () => {
  const routes = [
    { id: 1, drive_count: 9, selected_count: 1 },
    { id: 2, drive_count: 5, selected_count: 5 },
    { id: 3, drive_count: 7, selected_count: 5 },
  ];
  assert.deepEqual(sortRoutes(routes).map((r) => r.id), [3, 2, 1]);
  // Without selected_count it is the plain drive count (older backends).
  assert.deepEqual(sortRoutes([{ id: 1, drive_count: 2 }, { id: 2, drive_count: 4 }]).map((r) => r.id), [2, 1]);
});

test("routeVehicleCounts / formatVehicleCounts: per-car drives on a route", () => {
  const parts = routeVehicleCounts(SHARED_ROUTE, CARS, ["VA", "VB"]);
  assert.deepEqual(parts.map((p) => [p.letter, p.count]), [["A", 5], ["B", 3]]);
  assert.equal(formatVehicleCounts(parts), "5 A · 3 B");
  assert.equal(formatVehicleCounts(routeVehicleCounts(SHARED_ROUTE, CARS, ["VB"])), "3 B");
  assert.deepEqual(routeVehicleCounts({ stats: {} }, CARS, ["VA"]), []);
});

test("selectedRouteStats: overall when everyone is selected, combined for a subset", () => {
  assert.equal(selectedRouteStats(SHARED_ROUTE, ["VA", "VB"]), SHARED_ROUTE.stats);
  assert.equal(selectedRouteStats(SHARED_ROUTE, null), SHARED_ROUTE.stats);
  const a = selectedRouteStats(SHARED_ROUTE, ["VA"]);
  assert.equal(a.count, 5);
  assert.equal(a.fastest_seconds, 760);
  assert.equal(a.avg_seconds, 900);
  assert.equal(a.avg_efficiency_mi_kwh, 2.5);
});

test("vehicleStatRows: one formatted best/avg/slowest row per selected car", () => {
  const rows = vehicleStatRows(SHARED_ROUTE, CARS, ["VA", "VB"]);
  assert.deepEqual(
    rows.map((r) => [r.letter, r.count, r.fastest, r.avg, r.slowest]),
    [["A", 5, "12:40", "15:00", "18:20"], ["B", 3, "11:40", "12:55", "13:40"]]
  );
  assert.equal(rows[0].color, "#1b6ac9");
  assert.deepEqual(vehicleStatRows(SHARED_ROUTE, CARS, ["VA"]).map((r) => r.letter), ["A"]);
});

test("fastestSlowestKeys: from the drives on screen, outliers excluded", () => {
  const drives = [
    { vin: "VA", drive_id: "a1", key: "VA|a1", duration_seconds: 800 },
    { vin: "VB", drive_id: "b1", key: "VB|b1", duration_seconds: 700 },
    { vin: "VA", drive_id: "a2", key: "VA|a2", duration_seconds: 5000, outlier: true },
    { vin: "VA", drive_id: "a3", key: "VA|a3", duration_seconds: 1000 },
  ];
  assert.deepEqual(fastestSlowestKeys(drives), { fastest: "VB|b1", slowest: "VA|a3" });
  assert.deepEqual(fastestSlowestKeys([]), { fastest: null, slowest: null });
  assert.equal(driveKey({ drive_id: "x" }), "x");
  assert.equal(driveKey({ drive_id: "x", key: "V|x" }), "V|x");
});

test("routeDriveStyle: vehicle colors with outlined fastest/slowest, selected on top", () => {
  const base = { selectedKey: "VA|a1", fastestKey: "VB|b1", slowestKey: "VA|a3", vehicles: CARS };
  const plain = routeDriveStyle({ vin: "VA", key: "VA|a2" }, { ...base, multi: true });
  assert.equal(plain.color, "#1b6ac9");
  assert.equal(plain.casing, null);
  assert.equal(plain.z, 0);
  const fast = routeDriveStyle({ vin: "VB", key: "VB|b1" }, { ...base, multi: true });
  assert.equal(fast.color, "#c9561b");
  assert.equal(fast.casing.color, "#2e7d32");
  const slow = routeDriveStyle({ vin: "VA", key: "VA|a3" }, { ...base, multi: true });
  assert.equal(slow.casing.color, "#c62828");
  const sel = routeDriveStyle({ vin: "VA", key: "VA|a1" }, { ...base, multi: true });
  assert.ok(sel.z > fast.z && sel.weight > fast.weight);
});

test("routeDriveStyle: one vehicle keeps the neutral/green/red/primary scheme", () => {
  const base = { selectedKey: "VA|a1", fastestKey: "VA|a2", slowestKey: "VA|a3", vehicles: CARS, multi: false };
  assert.match(routeDriveStyle({ vin: "VA", key: "VA|a9" }, base).color, /secondary-text/);
  assert.equal(routeDriveStyle({ vin: "VA", key: "VA|a2" }, base).color, "#2e7d32");
  assert.equal(routeDriveStyle({ vin: "VA", key: "VA|a3" }, base).color, "#c62828");
  assert.match(routeDriveStyle({ vin: "VA", key: "VA|a1" }, base).color, /primary-color/);
});

test("buildOverlayLines: a drive is ranked and compared against its own car", () => {
  const selected = {
    vin: "VA", drive_id: "a1", key: "VA|a1", duration_seconds: 840,
    rank: 4, vin_rank: 2, vs_avg_pct: -1, vin_vs_avg_pct: -6,
  };
  const lines = buildOverlayLines(SHARED_ROUTE, selected, ["VA", "VB"]);
  const byLabel = Object.fromEntries(lines.map((l) => [l.label, l.value]));
  assert.equal(byLabel.Rank, "#2 of 5");
  assert.equal(byLabel["vs best / avg"], "+1:20 vs best · -1:00 vs avg");
  assert.equal(byLabel.Drives, "8");
  // Only car A selected: the panel shows A's own numbers.
  const onlyA = Object.fromEntries(buildOverlayLines(SHARED_ROUTE, selected, ["VA"]).map((l) => [l.label, l.value]));
  assert.equal(onlyA.Drives, "5");
  assert.equal(onlyA.Fastest, "12:40");
});

test("datasetGroups (routes): the selection is queried as the real dataset", () => {
  assert.deepEqual(datasetGroups(CARS, ["VA", "ZZ"]), [{ dataset: "real", vins: ["VA"] }]);
});
