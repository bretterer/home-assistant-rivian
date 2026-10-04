# Rivian Trip Efficiency Dashboard

The hand-maintained YAML templates that used to live in this folder
(`efficiency_dashboard.yaml`, and `../lovelace_efficiency_dashboard.yaml` at
the repo root) are **deprecated and no longer functional**. They referenced
sensor attributes (`recent_drives`, `recent_segments`, `recent_vampire_events`,
`recent_dcfc_sessions`) that have been removed from the integration for
performance reasons — those bulk payloads were rebuilt on every telemetry
tick and exceeded Home Assistant's 16 KiB recorder attribute limit. That data
is now delivered to the frontend via a WebSocket command consumed by the
`custom:rivian-series-card` wrapper card, which also needs your vehicle's
real VIN rather than a find/replace entity prefix.

## Supported install path: `rivian.create_efficiency_dashboard`

Generate (or refresh) the dashboard by calling the
`rivian.create_efficiency_dashboard` service. This builds the whole dashboard
configuration for you and registers it in your Home Assistant sidebar — no
manual YAML editing or entity-prefix substitution required.

### Via Developer Tools

1. Go to **Developer Tools → Actions**.
2. Search for and select **Rivian: Create efficiency dashboard**.
3. Optionally fill in the fields below.
4. Click **Perform action**.

### Via YAML

```yaml
action: rivian.create_efficiency_dashboard
data:
  title: Rivian Efficiency
  icon: mdi:gauge
  url_path: rivian-efficiency
```

| Field      | Optional | Description                                                     |
| ---------- | -------- | ----------------------------------------------------------------|
| `title`    | yes      | Title displayed in the sidebar and header (default: "Rivian Efficiency"). |
| `icon`     | yes      | Material Design icon for the sidebar navigation (default: "mdi:gauge"). |
| `url_path` | yes      | URL slug for the dashboard (default: "rivian-efficiency").      |

Re-running the service after upgrading the integration refreshes a stale
dashboard in place — it's safe to call again at any time.

## What the dashboard shows

For each discovered vehicle, the generated dashboard includes:

- **Hero Efficiency & KPI Badge**: Color-coded efficiency badge (`mi/kWh`), Last Drive distance, MPGe, and live vehicle status (`Parked` vs `Driving`).
- **Interactive Temperature vs. Efficiency Scatterplot**: Plots each completed drive against ambient road temperature, color-coded by elevation change:
  - Downhill Drives (Δh < -100 ft)
  - Flat Drives (±100 ft)
  - Uphill Drives (Δh > +100 ft)
  - Marker size scales dynamically based on trip distance.
- **Drive Distance vs. Efficiency Scatterplot**: Same elevation color-coding, plotted against trip distance.
- **10 mph Speed Bin Bar Chart**: Shows miles driven across each 10 mph speed bracket (`0–9`, `10–19`, `20–29`, `30–39`, `40–49`, ... `80+` mph).
- **Efficiency & MPGe Distribution by Speed Range**: Box plots comparing 3-minute chunks.
- **Vampire Drain Analysis**: Parked phantom drain vs. idle time and vs. ambient temperature.
- **DC Fast Charging Curves**: Power vs. battery state-of-charge for recent DCFC sessions, with reference curves for each Rivian battery pack size.
- **Native Core Fallback View**: Built entirely with standard Home Assistant Core cards (`tile`, `entities`, `statistics-graph`, `history-graph`) — zero custom frontend plugins required for this view.

### Requirements

The advanced analytics view uses the bundled `custom:rivian-series-card`,
Plotly Graph Card, and Mushroom Cards — all installed and registered
automatically by the integration, with no HACS install step required. The
Core fallback view has no external dependencies at all.
