// Node smoke test for rivian-charging-card.js's pure helpers.
//   node --test tests/frontend/
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  batteryChartDirections,
  filterHistory,
  historyFrameStart,
  historyTypeMatches,
  sessionCountText,
  INFERRED_TITLE,
  socBand,
  chargeTypeLabel,
  formatRate,
  formatTempF,
  detectedSessions,
  detectedTooltipLines,
  rangeWindow,
  scaleLinear,
  invertLinear,
  niceTicks,
  niceDomain,
  chartLayout,
  timeTicks,
  monthTicks,
  valueAt,
  formatDuration,
  kindLabel,
  socRange,
  placeLabel,
  sessionKey,
  sessionTooltipLines,
  expectedText,
  matchesFilter,
  filterSessions,
  filterLabel,
  sameFilter,
  scorecardTiles,
  countText,
  defaultChecked,
  sortHistory,
  curveAt,
  referenceBand,
  bandPath,
  powerDomains,
  capacitySummary,
  projectedRangeText,
  linePath,
  sessionsAtPixel,
  deleteMessage,
  esc,
  BANDS, MIN_ZOOM_S, brushSpan, isLongPress, spanLabel, hourTicks, BRAND_ORDER, BRAND_COLORS, brandColor,
  sessionColor, brandsPresent, chargerTags, chargerTagsPresent, fastSpan, inSpan, filterFast,
  sessionLegendLabel, stationLine, countSessions, tempColor, tempGradientCss, pctToKwh, kwhToPct,
  dualAxisTicks, sharedOriginal, healthPoints, limitHealth, tempSourceNote, tempText,
} from "../../custom_components/rivian/frontend/rivian-charging-card.js";

const S = (over) => ({
  vin: "V1",
  session_id: "s",
  kind: "dc",
  start_ts: 1000,
  end_ts: 2000,
  start_soc: 30,
  end_soc: 80,
  energy_added_kwh: 50,
  max_power_kw: 180,
  avg_power_kw: 100,
  duration_s: 1800,
  place: null,
  samples: [],
  expected: null,
  ...over,
});

const id = (v) => v;

test("socBand classifies the five bands with strict backend-like edges", () => {
  assert.equal(socBand(5), "red_low");
  assert.equal(socBand(10), "orange_low");
  assert.equal(socBand(20), "green");
  assert.equal(socBand(80), "green");
  assert.equal(socBand(80.1), "orange_high");
  assert.equal(socBand(90), "orange_high");
  assert.equal(socBand(95), "red_high");
  assert.equal(socBand(null), null);
});

test("rangeWindow: fixed days, and 'all' bounded by earliest data", () => {
  const now = 1_000_000_000;
  assert.deepEqual(rangeWindow("7d", now), { start: now - 7 * 86400, end: now });
  assert.equal(rangeWindow("1y", now).start, now - 365 * 86400);
  assert.equal(rangeWindow("all", now, now - 40 * 86400).start, now - 40 * 86400);
  assert.equal(rangeWindow("all", now).start, now - 365 * 86400);
  assert.equal(rangeWindow("all", now, 1).start, now - 5 * 365 * 86400);
});

test("scales invert each other and handle a zero span", () => {
  const f = scaleLinear(0, 100, 200, 0);
  assert.equal(f(0), 200);
  assert.equal(f(100), 0);
  assert.equal(invertLinear(0, 100, 200, 0)(50), 75);
  assert.equal(scaleLinear(5, 5, 0, 10)(5), 5);
});

test("niceTicks / niceDomain / chartLayout", () => {
  assert.deepEqual(niceTicks(0, 250, 5), [0, 50, 100, 150, 200, 250]);
  assert.deepEqual(niceDomain(0, 212, 5), [0, 250]);
  assert.deepEqual(niceTicks(3, 3), [3]);
  const g = chartLayout(400, 200, { left: 30, right: 10, top: 5, bottom: 20 });
  assert.equal(g.x0, 30);
  assert.equal(g.x1, 390);
  assert.equal(g.h, 175);
});

test("timeTicks are day-aligned, ordered and bounded", () => {
  const start = Date.UTC(2026, 8, 3, 12) / 1000;
  const end = start + 30 * 86400;
  const ticks = timeTicks(start, end, "UTC", 7);
  assert.ok(ticks.length >= 3 && ticks.length <= 8);
  for (const t of ticks) {
    assert.equal(t.ts % 86400, 0);
    assert.ok(t.ts >= start && t.ts <= end);
  }
  assert.deepEqual(timeTicks(5, 5, "UTC"), []);
});

test("valueAt interpolates inside the series and is null outside", () => {
  const pts = [
    [0, 10],
    [10, 20],
    [20, 0],
  ];
  assert.equal(valueAt(pts, 5), 15);
  assert.equal(valueAt(pts, 15), 10);
  assert.equal(valueAt(pts, -1), null);
  assert.equal(valueAt(pts, 21), null);
  assert.equal(valueAt([], 1), null);
});

test("formatting helpers", () => {
  assert.equal(formatDuration(45), "45 s");
  assert.equal(formatDuration(1620), "27 min");
  assert.equal(formatDuration(4320), "1 h 12 min");
  assert.equal(formatDuration(3600), "1 h");
  assert.equal(formatDuration(null), "–");
  assert.equal(kindLabel("dc"), "Fast charge");
  assert.equal(kindLabel("ac"), "AC charge");
  assert.equal(socRange(S({ start_soc: 36.8, end_soc: 76.8 })), "37 → 77 %");
  assert.equal(placeLabel(S({ place: { id: 1, label: "Home" } })), "Home");
  assert.equal(placeLabel(S()), "–");
  assert.equal(sessionKey(S()), "V1|s");
  assert.equal(esc('<a "b">&'), "&lt;a &quot;b&quot;&gt;&amp;");
});

test("sessionTooltipLines covers vehicle, place, soc, power, duration", () => {
  const lines = sessionTooltipLines(S({ place: { id: 1, label: "Home" } }), "Rivi", "UTC");
  assert.equal(lines[0], "Rivi · DC fast charge");
  assert.ok(lines[1].startsWith("Home · "));
  assert.equal(lines[2], "30 → 80 % · 50.0 kWh");
  assert.equal(lines[3], "Peak 180 kW · avg 100 kW");
  assert.equal(lines[4], "30 min");
  const warm = sessionTooltipLines(S({ battery_temp_f: 91.4, outside_temp_f: 58 }), "Rivi", "UTC");
  assert.ok(warm.includes("Battery 91 °F · outside 58 °F"));
});

test("expectedText", () => {
  assert.equal(expectedText(S()), "");
  const text = expectedText(S({ expected: { minutes: 27.2, pct_of_expected: 90.8, approximate: false } }));
  assert.equal(text, "30 min vs 27 min expected · 91 % of expected");
  assert.ok(expectedText(S({ expected: { minutes: 17, approximate: true } })).includes("~17"));
});

test("filters match the backend's strict above/below semantics", () => {
  const sessions = [
    S({ session_id: "a", start_soc: 19.9, end_soc: 80 }),
    S({ session_id: "b", kind: "ac", start_soc: 10, end_soc: 80.1 }),
    S({ session_id: "c", kind: "ac", start_soc: 9.9, end_soc: 90.1 }),
    S({ session_id: "d", vin: "V2", start_soc: 5, end_soc: 95 }),
  ];
  const ids = (f) => filterSessions(sessions, f).map((s) => s.session_id);
  assert.deepEqual(ids({ vin: "V1", metric: "end80" }), ["b", "c"]);
  assert.deepEqual(ids({ vin: "V1", metric: "end80", fastOnly: true }), []);
  assert.deepEqual(ids({ vin: "V1", metric: "end90" }), ["c"]);
  assert.deepEqual(ids({ vin: "V1", metric: "start20" }), ["a", "b", "c"]);
  assert.deepEqual(ids({ vin: "V1", metric: "start10" }), ["c"]);
  assert.deepEqual(ids({ vin: "V1", metric: "ac" }), ["b", "c"]);
  assert.deepEqual(ids({ vin: "V2", metric: "dc" }), ["d"]);
  assert.equal(filterSessions(sessions, null).length, 4);
  assert.equal(matchesFilter(sessions[0], null), true);
});

test("filter labels and identity", () => {
  assert.equal(
    filterLabel({ vin: "V1", metric: "end80", fastOnly: true }, "Rivi"),
    "Rivi · Ended above 80 % (fast only)"
  );
  assert.equal(filterLabel({ vin: "V1", metric: "dc" }, ""), "Fast charges");
  assert.equal(filterLabel(null, "x"), "");
  assert.ok(sameFilter({ vin: "a", metric: "dc" }, { vin: "a", metric: "dc", fastOnly: false }));
  assert.ok(!sameFilter({ vin: "a", metric: "dc" }, { vin: "b", metric: "dc" }));
});

test("scorecardTiles maps counts_by_vin and time in band", () => {
  const counts = {
    total: 8,
    dc: 3,
    ac: 5,
    ended_above_80: 6,
    ended_above_90: 2,
    started_below_20: 4,
    started_below_10: 1,
    dc_ended_above_80: 3,
    dc_ended_above_90: 1,
    dc_started_below_20: 2,
    dc_started_below_10: 0,
  };
  const { tiles, timeInIdeal } = scorecardTiles(counts, { b20_80: 0.624 });
  assert.equal(timeInIdeal, "62 %");
  assert.deepEqual(
    tiles.map((t) => t.id),
    ["dc", "ac", "end80", "start20"]
  );
  assert.equal(tiles[2].all, 6);
  assert.equal(tiles[2].fast, 3);
  assert.equal(tiles[2].sub.all, 2);
  assert.equal(tiles[3].sub.fast, 0);
  assert.equal(scorecardTiles({}, null).timeInIdeal, "—");
  assert.equal(countText(12, 3), "12 · 3 fast");
  assert.equal(countText(4, null), "4");
});

test("defaultChecked picks the newest three DC sessions only", () => {
  const sessions = [
    S({ session_id: "1", start_ts: 1 }),
    S({ session_id: "2", start_ts: 2 }),
    S({ session_id: "3", start_ts: 3 }),
    S({ session_id: "4", start_ts: 4 }),
    S({ session_id: "ac", kind: "ac", start_ts: 5 }),
  ];
  assert.deepEqual([...defaultChecked(sessions, 3)].sort(), ["V1|2", "V1|3", "V1|4"]);
});

test("sortHistory by column and direction", () => {
  const rows = [
    S({ session_id: "a", start_ts: 1, energy_added_kwh: 9, place: { id: 1, label: "Work" } }),
    S({ session_id: "b", start_ts: 3, energy_added_kwh: 3, place: { id: 2, label: "Home" } }),
    S({ session_id: "c", start_ts: 2, energy_added_kwh: 6 }),
  ];
  const order = (k, d) => sortHistory(rows, k, d).map((s) => s.session_id).join("");
  assert.equal(order("when", "desc"), "bca");
  assert.equal(order("when", "asc"), "acb");
  assert.equal(order("kwh", "desc"), "acb");
  assert.equal(order("place", "asc"), "bac");
});

test("reference band and curve helpers", () => {
  const curve = { soc: [10, 50, 90], kw: [200, 100, 20] };
  assert.equal(curveAt(curve, 30), 150);
  assert.equal(curveAt(curve, 95), null);
  const band = referenceBand(curve, 0.1);
  assert.ok(Math.abs(band.hi[0] - 220) < 1e-9);
  assert.ok(Math.abs(band.lo[1] - 90) < 1e-9);
  const d = bandPath(band, id, id);
  assert.ok(d.startsWith("M10.0,220.0") && d.endsWith("Z"));
  assert.equal(bandPath({ soc: [1], lo: [1], hi: [1] }, id, id), "");
});

test("powerDomains covers samples and references", () => {
  const sessions = [
    S({
      samples: [
        { soc: 36, power_kw: 150 },
        { soc: 77, power_kw: 30 },
      ],
    }),
  ];
  const dom = powerDomains(sessions, [{ curve: { soc: [10, 90], kw: [215, 28] } }]);
  assert.deepEqual(dom.x, [0, 100]);
  assert.ok(dom.y[1] >= 215 * 1.1);
  assert.deepEqual(powerDomains([], []).x, [0, 100]);
});

test("capacitySummary and projected range text", () => {
  const entry = {
    points: [
      [1788000000, 135.0],
      [1790000000, 134.4],
    ],
    pct_points: [
      [1788000000, 100.0],
      [1790000000, 99.56],
    ],
    projected_range: [
      [1788000000, 378],
      [1790000000, 376.3],
    ],
  };
  const text = capacitySummary(entry, "UTC");
  assert.ok(text.startsWith("−0.4 % since "));
  assert.ok(text.endsWith("135.0 → 134.4 kWh"));
  assert.equal(projectedRangeText(entry), "~376 mi projected full range");
  assert.equal(projectedRangeText({}), "");
  assert.equal(capacitySummary({}, "UTC"), "No capacity readings yet");
  assert.ok(capacitySummary({ points: [[1, 130]], pct_points: [[1, 100]] }, "UTC").includes("only one reading"));
  const flat = {
    points: [
      [1, 1],
      [2, 1],
    ],
    pct_points: [
      [1, 100],
      [2, 100],
    ],
  };
  assert.ok(capacitySummary(flat, "UTC").startsWith("No change since"));
  const up = {
    points: [
      [1, 1],
      [86401, 2],
    ],
    pct_points: [
      [1, 99],
      [86401, 100],
    ],
  };
  assert.ok(capacitySummary(up, "UTC").startsWith("+1.0 %"));
});

test("linePath breaks on gaps; sessionsAtPixel hit-tests spans", () => {
  assert.equal(
    linePath(
      [
        [0, 0],
        [1, 1],
      ],
      id,
      id
    ),
    "M0.0,0.0L1.0,1.0"
  );
  assert.equal(
    linePath(
      [
        [0, 0],
        [10, 1],
      ],
      id,
      id,
      (a, b) => b[0] - a[0] > 5
    ),
    "M0.0,0.0M10.0,1.0"
  );
  const xs = scaleLinear(0, 1000, 0, 100);
  const sessions = [
    S({ session_id: "a", start_ts: 100, end_ts: 200 }),
    S({ session_id: "b", start_ts: 800, end_ts: 900 }),
  ];
  assert.deepEqual(
    sessionsAtPixel(sessions, xs, 15).map((s) => s.session_id),
    ["a"]
  );
  assert.deepEqual(sessionsAtPixel(sessions, xs, 50), []);
  const tiny = [S({ session_id: "t", start_ts: 500, end_ts: 500.1 })];
  assert.equal(sessionsAtPixel(tiny, xs, 50).length, 1);
});

test("deleteMessage names the vehicle, time and values", () => {
  const msg = deleteMessage(S({ place: { id: 1, label: "Home" } }), "Rivi", "UTC");
  assert.ok(msg.includes("Rivi"));
  assert.ok(msg.includes("Home"));
  assert.ok(msg.includes("30 → 80 %"));
  assert.ok(msg.includes("cannot be undone"));
});

// -- Phase 3C ------------------------------------------------------------------

test("bands: green is 20-80, orange 10-20 / 80-90, red outside", () => {
  assert.deepEqual(
    BANDS.map((b) => [b.tone, b.from, b.to]),
    [["red", 0, 10], ["orange", 10, 20], ["green", 20, 80], ["orange", 80, 90], ["red", 90, 100]]
  );
  assert.equal(socBand(79.9), "green");
  assert.equal(socBand(85), "orange_high");
  assert.equal(socBand(90.1), "red_high");
});

test("brushSpan maps a drag to a time range, clamped, ordered and widened", () => {
  const g = { x0: 30, x1: 530 };
  // 500 px over 1000 s => 2 s/px
  assert.deepEqual(brushSpan(130, 330, g, 0, 1000, 8, 0), { start: 200, end: 600 });
  assert.deepEqual(brushSpan(330, 130, g, 0, 1000, 8, 0), { start: 200, end: 600 });
  assert.equal(brushSpan(100, 104, g, 0, 1000), null);
  assert.deepEqual(brushSpan(-50, 9999, g, 0, 1000, 8, 0), { start: 0, end: 1000 });
  const wide = brushSpan(200, 208, g, 0, 100000);
  assert.equal(wide.end - wide.start, MIN_ZOOM_S);
  const edge = brushSpan(30, 38, g, 0, 100000);
  assert.equal(edge.start, 0);
  assert.equal(edge.end, MIN_ZOOM_S);
});

test("isLongPress needs both the hold time and a still finger", () => {
  assert.equal(isLongPress(0, 500, 3), true);
  assert.equal(isLongPress(0, 300, 3), false);
  assert.equal(isLongPress(0, 600, 30), false);
});

test("spanLabel and hourTicks for a zoomed window", () => {
  const start = Date.UTC(2026, 8, 20, 15) / 1000;
  assert.equal(spanLabel(null, "UTC"), "");
  assert.ok(spanLabel({ start, end: start + 3600 }, "UTC").includes(" – "));
  assert.equal(spanLabel({ start, end: start + 5 * 86400 }, "UTC").split(" – ").length, 2);
  const ticks = hourTicks(start, start + 12 * 3600, "UTC", 7);
  assert.ok(ticks.length >= 3 && ticks.length <= 8);
  for (const t of ticks) assert.equal(t.ts % 3600, 0);
  assert.equal(timeTicks(start, start + 12 * 3600, "UTC", 7)[0].ts % 3600, 0);
  assert.deepEqual(hourTicks(5, 5, "UTC"), []);
});

test("brand colors are fixed, distinct, and fall back to Other", () => {
  assert.deepEqual(BRAND_ORDER.slice(0, 3), ["tesla", "rivian", "electrify_america"]);
  for (const b of BRAND_ORDER) assert.match(brandColor(b), /^#[0-9a-f]{6}$/);
  const light = BRAND_ORDER.filter((b) => b !== "other" && b !== "home").map((b) => brandColor(b));
  assert.equal(new Set(light).size, light.length);
  assert.equal(brandColor("tesla", true), BRAND_COLORS.tesla[1]);
  assert.notEqual(brandColor("tesla"), brandColor("tesla", true));
  assert.equal(brandColor("nope"), brandColor("other"));
  assert.equal(sessionColor({ brand: "evgo" }, "brand", "#123456"), brandColor("evgo"));
  assert.equal(sessionColor({ brand: "evgo" }, "vehicle", "#123456"), "#123456");
});

test("temperature scale: cold blue, mild green, hot red, clamped, gray when unknown", () => {
  const rgb = (hex) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
  const cold = rgb(tempColor(20));
  const mild = rgb(tempColor(60));
  const hot = rgb(tempColor(100));
  assert.ok(cold[2] > cold[0] && cold[2] > cold[1]);
  assert.ok(mild[1] > mild[0] && mild[1] > mild[2]);
  assert.ok(hot[0] > hot[1] && hot[0] > hot[2]);
  assert.equal(tempColor(-40), tempColor(20));
  assert.equal(tempColor(150), tempColor(100));
  assert.equal(tempColor(null), "#8a8a8a");
  assert.notEqual(tempColor(40), tempColor(80));
  assert.notEqual(tempColor(60, true), tempColor(60, false));
  assert.match(tempGradientCss(), /^linear-gradient\(to right, #[0-9a-f]{6} 0\.0%/);
});

test("dual axis: kWh and % are two scales of one position", () => {
  assert.equal(pctToKwh(50, 135), 67.5);
  assert.equal(kwhToPct(67.5, 135), 50);
  const t = dualAxisTicks(99, 101, 135, 4);
  for (const k of t.kwh) assert.ok(Math.abs(pctToKwh(k.pct, 135) - k.v) < 1e-9);
  for (const p of t.pct) assert.equal(p.pct, p.v);
  assert.ok(t.kwh.every((k) => k.pct >= 99 - 1e-9 && k.pct <= 101 + 1e-9));
  assert.equal(sharedOriginal([{ original_kwh: 135 }, { original_kwh: 134.2 }]), 135);
  assert.equal(sharedOriginal([{ original_kwh: 135 }, { original_kwh: 87.9 }]), null);
  assert.equal(sharedOriginal([]), null);
});

test("healthPoints merges kWh and % by day; windows and temperature text", () => {
  const entry = {
    vin: "V",
    points: [[100, 135, 60.8, "battery"], [200, 134, null, null], [300, 133, 40, "outside"]],
    pct_points: [[100, 100], [200, 99.3], [300, 98.5]],
  };
  const pts = healthPoints(entry);
  assert.equal(pts.length, 3);
  assert.deepEqual(pts[0], { t: 100, kwh: 135, pct: 100, temp: 60.8, source: "battery" });
  assert.equal(pts[1].temp, null);
  assert.equal(limitHealth(pts, "all", 300).length, 3);
  assert.equal(limitHealth(pts, "90d", 300 + 200 * 86400).length, 0);
  assert.equal(tempText(pts[0]), "61 °F battery");
  assert.equal(tempText(pts[2]), "40 °F outside");
  assert.equal(tempText(pts[1]), "");
  assert.equal(tempSourceNote([entry], id, false), "Dot color: battery temperature, else outside");
  assert.equal(
    tempSourceNote([{ vin: "A", points: [[1, 1, 50, "outside"]], pct_points: [[1, 100]] }], (v) => `car ${v}`, true),
    "Dot color: car A: outside temperature"
  );
  assert.equal(tempSourceNote([{ vin: "A", points: [], pct_points: [] }], id, false), "No temperature recorded");
});

const BR = (over) => S({ brand: "electrify_america", brand_label: "Electrify America", place: { id: 1, label: "Denver" }, ...over });

test("legend labels: day, brand, place; brand dropped when the place names it", () => {
  const ts = Date.UTC(2026, 8, 26, 12) / 1000;
  assert.equal(sessionLegendLabel(BR({ start_ts: ts }), "UTC"), "Sep 26 · Electrify America · Denver");
  assert.equal(sessionLegendLabel(BR({ start_ts: ts }), "UTC", "Rivi"), "Rivi · Sep 26 · Electrify America · Denver");
  assert.equal(
    sessionLegendLabel(BR({ start_ts: ts, place: { id: 2, label: "Electrify America Meridian" } }), "UTC"),
    "Sep 26 · Electrify America Meridian"
  );
  assert.equal(
    sessionLegendLabel(S({ start_ts: ts, brand: "other", brand_label: "Other", station_name: "Joes Chargers" }), "UTC"),
    "Sep 26 · Joes Chargers"
  );
  assert.equal(stationLine(BR({ station_name: "Electrify America - Denver", charger_max_kw: 350 })), "Electrify America - Denver · 350 kW");
  assert.equal(
    stationLine(S({ brand: "tesla", brand_label: "Tesla Supercharger", station_version: "V3", charger_max_kw: 250 })),
    "Tesla Supercharger · V3 · 250 kW"
  );
  assert.equal(stationLine(S()), "");
  const lines = sessionTooltipLines(BR({ station_name: "Electrify America - Denver" }), "Rivi", "UTC");
  assert.ok(lines.includes("Electrify America - Denver"));
});

test("fast filters: timeframe/zoom span, brand chips, charger chips", () => {
  const now = 300 * 86400;
  const sessions = [
    BR({ session_id: "a", start_ts: now - 5 * 86400, end_ts: now - 5 * 86400 + 1800, charger_max_kw: 350 }),
    BR({ session_id: "b", start_ts: now - 50 * 86400, end_ts: now - 50 * 86400 + 1800, brand: "tesla", brand_label: "Tesla Supercharger", station_version: "V3", charger_max_kw: 250 }),
    S({ session_id: "c", kind: "ac", start_ts: now - 2 * 86400, end_ts: now - 2 * 86400 + 100, brand: "home", brand_label: "Home" }),
    BR({ session_id: "d", start_ts: now - 200 * 86400, end_ts: now - 200 * 86400 + 1800, brand: "rivian", brand_label: "Rivian Adventure Network" }),
  ];
  const ids = (o) => filterFast(sessions, o).map((s) => s.session_id);
  assert.deepEqual(ids({}), ["a", "b", "d"]);
  assert.deepEqual(ids({ span: fastSpan("30d", null, now) }), ["a"]);
  assert.deepEqual(ids({ span: fastSpan("90d", null, now) }), ["a", "b"]);
  assert.deepEqual(ids({ span: fastSpan("1y", null, now) }), ["a", "b", "d"]);
  assert.equal(fastSpan("all", null, now), null);
  const zoom = { start: now - 60 * 86400, end: now - 40 * 86400 };
  assert.deepEqual(fastSpan("30d", zoom, now), zoom);
  assert.deepEqual(ids({ span: zoom }), ["b"]);
  assert.deepEqual(ids({ brands: new Set(["tesla", "rivian"]) }), ["b", "d"]);
  assert.deepEqual(ids({ tags: new Set(["V3"]) }), ["b"]);
  assert.deepEqual(ids({ tags: new Set(["350 kW"]) }), ["a"]);
  assert.deepEqual(ids({ brands: new Set(), tags: new Set() }), ["a", "b", "d"]);
  assert.equal(inSpan(sessions[0], null), true);
  assert.equal(inSpan(sessions[0], { start: now, end: now + 1 }), false);
  assert.deepEqual(brandsPresent(sessions).map((b) => b.brand), ["tesla", "rivian", "electrify_america", "home"]);
  assert.equal(brandsPresent(sessions)[2].count, 1);
  assert.deepEqual(chargerTags(sessions[1]), ["V3", "250 kW"]);
  assert.deepEqual(chargerTagsPresent(sessions), ["V3", "250 kW", "350 kW"]);
  assert.deepEqual(chargerTagsPresent([]), []);
});

test("countSessions mirrors the backend keys for a zoomed span", () => {
  const c = countSessions([
    S({ start_soc: 8, end_soc: 91 }),
    S({ kind: "ac", start_soc: 15, end_soc: 80 }),
    S({ kind: "ac", start_soc: 50, end_soc: 85 }),
  ]);
  assert.deepEqual(c, {
    total: 3,
    dc: 1,
    ac: 2,
    ended_above_80: 2,
    ended_above_90: 1,
    started_below_20: 2,
    started_below_10: 1,
    dc_ended_above_80: 1,
    dc_ended_above_90: 1,
    dc_started_below_20: 1,
    dc_started_below_10: 1,
  });
  assert.equal(countSessions(null).total, 0);
});

test("detectedSessions flattens the selected vehicles' detected charges, flagged and sorted", () => {
  const detected = {
    A: [{ start_ts: 500, end_ts: 900, start_soc: 40, end_soc: 70, kind: "ac" }],
    B: [{ start_ts: 100, end_ts: 400, start_soc: 20, end_soc: 60, kind: "dc" }, { start_ts: null, end_ts: 1 }],
    C: [{ start_ts: 0, end_ts: 10, start_soc: 1, end_soc: 5, kind: "ac" }],
  };
  const out = detectedSessions(detected, ["A", "B"]);
  assert.deepEqual(out.map((s) => [s.vin, s.kind, s.detected, s.duration_s]), [
    ["B", "dc", true, 300],
    ["A", "ac", true, 400],
  ]);
  assert.deepEqual(detectedSessions(undefined, ["A"]), []);
});

test("detectedTooltipLines says it was seen in the battery level, not recorded", () => {
  const lines = detectedTooltipLines(
    { start_ts: Date.UTC(2026, 8, 10, 3) / 1000, end_ts: Date.UTC(2026, 8, 10, 11) / 1000, duration_s: 8 * 3600, start_soc: 40, end_soc: 75, kind: "ac", avg_power_kw: 5.9, energy_added_kwh: 47.3 },
    "Rivi",
    "UTC"
  );
  assert.equal(lines[0], "Rivi · Slow charge (AC) · detected");
  assert.ok(lines.some((l) => l.includes("~47.3 kWh")));
  assert.ok(lines.some((l) => l.includes("avg ~5.9 kW")));
  assert.match(lines[lines.length - 1], /Not in the charging history/);
});

test("charge type, rate and temperature formatting", () => {
  assert.equal(chargeTypeLabel({ charge_type: "ac_l1" }), "AC L1");
  assert.equal(chargeTypeLabel({ kind: "dc" }), "DC Fast");
  assert.equal(chargeTypeLabel({ kind: "ac", avg_power_kw: 1.4 }), "AC L1");
  assert.equal(chargeTypeLabel({ kind: "ac", avg_power_kw: 7.2 }), "AC L2");
  assert.equal(formatRate(7.24), "7.2 kW");
  assert.equal(formatRate(152.6), "153 kW");
  assert.equal(formatRate(null), "\u2013");
  assert.equal(formatTempF(61.6), "62 \u00b0F");
  assert.equal(formatTempF(undefined), "\u2013");
});

test("sortHistory by type, rate and temperature", () => {
  const rows = [
    S({ session_id: "a", kind: "ac", avg_power_kw: 1.4, max_power_kw: 1.5, outside_temp_f: 40 }),
    S({ session_id: "b", kind: "dc", avg_power_kw: 120, max_power_kw: 190, outside_temp_f: null }),
    S({ session_id: "c", kind: "ac", avg_power_kw: 7, max_power_kw: 9, outside_temp_f: 70 }),
  ];
  const order = (k, d) => sortHistory(rows, k, d).map((s) => s.session_id).join("");
  assert.equal(order("type", "asc"), "bca");
  assert.equal(order("peak", "desc"), "bca");
  assert.equal(order("avg", "asc"), "acb");
  assert.equal(order("outside_temp", "desc"), "cab");
});

test("batteryChartDirections: always explains double-click returns to the full range", () => {
  for (const zoomed of [false, true]) {
    const d = batteryChartDirections(zoomed);
    assert.ok(d.some((s) => /double-click/i.test(s) && /full time range/.test(s)));
    assert.ok(d.some((s) => /drag/i.test(s)));
  }
  assert.match(batteryChartDirections(true)[0], /double-click/i);
  assert.match(batteryChartDirections(false)[0], /drag/i);
});

test("historyTypeMatches: All / Home / L1-L2 / Fast", () => {
  const home = { kind: "ac", is_home_charge: true };
  const l2 = { kind: "ac", is_home_charge: false };
  const dc = { kind: "dc", is_home_charge: false };
  assert.ok([home, l2, dc].every((s) => historyTypeMatches(s, "all")));
  assert.deepEqual([home, l2, dc].filter((s) => historyTypeMatches(s, "home")), [home]);
  assert.deepEqual([home, l2, dc].filter((s) => historyTypeMatches(s, "ac")), [home, l2]);
  assert.deepEqual([home, l2, dc].filter((s) => historyTypeMatches(s, "fast")), [dc]);
  assert.ok(historyTypeMatches({ kind: "ac", is_home: true }, "home"));
});

test("historyFrameStart: week / month / year / all", () => {
  const now = 1_000_000_000;
  assert.equal(historyFrameStart("week", now), now - 7 * 86400);
  assert.equal(historyFrameStart("month", now), now - 30 * 86400);
  assert.equal(historyFrameStart("year", now), now - 365 * 86400);
  assert.equal(historyFrameStart("all", now), null);
});

test("filterHistory combines type and timeframe; sessionCountText pluralizes", () => {
  const now = 100 * 86400;
  const rows = [
    { session_id: "old", kind: "dc", start_ts: 10 * 86400, end_ts: 10 * 86400 + 600 },
    { session_id: "new", kind: "dc", start_ts: 99 * 86400, end_ts: 99 * 86400 + 600 },
    { session_id: "ac", kind: "ac", start_ts: 99 * 86400, end_ts: 99 * 86400 + 600 },
  ];
  assert.equal(filterHistory(rows, "fast", "week", now).map((s) => s.session_id).join(), "new");
  assert.equal(filterHistory(rows, "fast", "all", now).length, 2);
  assert.equal(sessionCountText(1), "1 session");
  assert.equal(sessionCountText(37), "37 sessions");
});

test("sessionTooltipLines marks an inferred session", () => {
  const lines = sessionTooltipLines({ kind: "ac", vin: "V", inferred: true, start_soc: 20, end_soc: 80, energy_added_kwh: null }, "R1T", "UTC");
  assert.ok(lines[0].includes("inferred"));
  assert.ok(lines.includes(INFERRED_TITLE));
});

test("timeTicks on a year-long axis tick month starts labeled with the year", () => {
  const start = Date.UTC(2025, 8, 23) / 1000; // Sep 23 2025
  const end = Date.UTC(2026, 8, 24) / 1000; // Sep 24 2026
  const ticks = timeTicks(start, end, "UTC", 7);
  assert.ok(ticks.length >= 3 && ticks.length <= 7, String(ticks.length));
  for (const t of ticks) {
    assert.equal(new Date(t.ts * 1000).getUTCDate(), 1);
    assert.match(t.label, /20(25|26)/);
  }
  assert.deepEqual(monthTicks(5, 5, "UTC"), []);
});
