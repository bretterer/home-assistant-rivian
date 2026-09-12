# Rivian Trip Efficiency Dashboard Package

This folder contains turnkey Lovelace dashboard configurations for the **Rivian Trip Efficiency, MPGe & Drive Analytics** subsystem in Home Assistant.

---

## 1. Overview of Views

The provided dashboard configuration ([`lovelace_efficiency_dashboard.yaml`](../lovelace_efficiency_dashboard.yaml)) contains two complete, independent dashboard views:

### View 1: Advanced Analytics (Plotly & Mushroom)
- **Hero Efficiency & KPI Badge**: Color-coded efficiency badge (`mi/kWh`), Last Drive distance, MPGe, and live vehicle status (`Parked` vs `Driving`).
- **Interactive Temperature vs. Efficiency Scatterplot**: Uses `plotly-graph-card` to plot each completed drive against ambient road temperature:
  - 🔵 **Downhill Drives** ($\Delta h < -100$ ft)
  - 🟢 **Flat Drives** ($\pm 100$ ft)
  - 🟠 **Uphill Drives** ($\Delta h > +100$ ft)
  - Marker size scales dynamically based on trip distance.
- **10 mph Speed Bin Bar Chart**: Shows miles driven across each 10 mph speed bracket (`0–9`, `10–19`, `20–29`, `30–39`, `40–49`, ... `80+` mph).
- **Drive Statistics Grid**: Breakdown of trip metrics and aggregations.

> **Requirements for View 1**:
> - [Plotly Graph Card](https://github.com/dbuezas/lovelace-plotly-graph-card) (available in HACS)
> - [Mushroom Cards](https://github.com/piitaya/lovelace-mushroom) (available in HACS)

---

### View 2: Native Core Home Assistant (Zero Dependencies)
- Built **entirely with standard Home Assistant Core cards** (`tile`, `entities`, `gauge`, `statistics-graph`, `history-graph`).
- **100% Zero-Dependency**: Works immediately on any vanilla Home Assistant installation without installing any HACS frontend plugins.
- Includes long-term rolling statistics line charts and multi-metric trip history graphs.

---

## 2. Installation Instructions

1. Open [`lovelace_efficiency_dashboard.yaml`](../lovelace_efficiency_dashboard.yaml).
2. Replace `{vin}` with your vehicle entity ID prefix.
   - For example, if your vehicle sensor is `sensor.rivian_r1s_my_rivian_last_drive_efficiency`, replace `sensor.{vin}_` with `sensor.rivian_r1s_my_rivian_`.
3. In Home Assistant:
   - Go to any dashboard → click **Three Dots (⋮)** → **Edit Dashboard** → **Raw configuration editor**.
   - Paste the desired view into your `views:` list.
   - Click **Save**.
