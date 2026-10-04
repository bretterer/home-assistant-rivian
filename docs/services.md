# Services (actions)

Run any of these from **Developer tools > Actions** or from automations and scripts. All `vin` fields are optional: leave them out to process every configured vehicle. Most services are one-offs for maintenance; the integration already does the routine work automatically.

## Setup

### `rivian.create_efficiency_dashboard`

Creates (or regenerates) the tabbed Rivian dashboard in the sidebar. Run it again after adding a vehicle or upgrading.

| Field | Default | Meaning |
|---|---|---|
| `title` | `Rivian` | Sidebar and header title |
| `icon` | `mdi:car-electric` | Sidebar icon |
| `url_path` | `rivian-dashboard` | URL slug |

```yaml
action: rivian.create_efficiency_dashboard
data:
  title: Rivian
  url_path: rivian-dashboard
```

### `rivian.create_demo_data`

Installs two synthetic demo vehicles (Demo R2 and Demo R1T) with about ten days of drives, routes, places and fast-charge sessions, then regenerates the dashboard. Re-running replaces them. No fields. Administrators only.

```yaml
action: rivian.create_demo_data
```

### `rivian.delete_demo_data`

Removes the demo vehicles completely (drives, routes, places, heat map and statistics) and regenerates the dashboard. Never touches a real vehicle. Administrators only.

| Field | Meaning |
|---|---|
| `vin` | One demo VIN to remove (default: all demo vehicles) |

```yaml
action: rivian.delete_demo_data
```

### `rivian.set_vehicle_picture`

Downloads an image once and keeps it as the vehicle's picture on the Vehicles tab. Use it when the automatic picture does not match your car, for example by copying the image address of your build on rivian.com. Admin only. Only real JPEG, PNG, GIF or WebP images are kept (checked by their content, not the file name); anything else, including SVG, is refused.

| Field | Required | Meaning |
|---|---|---|
| `url` | yes | Address of a JPEG, PNG, GIF or WebP image (up to 10 MB) |
| `vin` | only with several vehicles | Vehicle to set it for |

```yaml
action: rivian.set_vehicle_picture
data:
  vin: "7PDSGABA8NN000000"
  url: "https://example.com/my-rivian.webp"
```

## Importing and backfilling

### `rivian.backfill_drive_history`

Reconstructs past drives from Home Assistant's recorder history (read-only; the recorder is never modified).

| Field | Default | Meaning |
|---|---|---|
| `vin` | all | Vehicle to backfill |
| `days` | 365 | How many past days (1-365) |
| `dry_run` | **true** | Only report what would be added, write nothing |
| `tracks` | true | Also rebuild GPS routes (only as far back as the recorder keeps location history, about 10 days by default) |

A reconstructed drive that overlaps a stored drive by more than half is skipped; live routes always win. After a real run, places and routes are rebuilt.

```yaml
action: rivian.backfill_drive_history
data:
  days: 30
  dry_run: true
```

### `rivian.import_charging_history`

Imports completed charging sessions (home and public) from Rivian's cloud history and merges them with those recorded locally: an overlapping session gains the vendor, home or public flag and measured energy; a missing one is added. Then looks up the station (brand, network, name, maximum kW) of fast charges. Also runs once a day. Real vehicles only. It never requests payment or account details.

| Field | Meaning |
|---|---|
| `vin` | Vehicle to process |

```yaml
action: rivian.import_charging_history
```

### `rivian.backfill_weather`

Fills each stored drive's wind, headwind, rain, pressure, humidity and air density (and the energy model's expected energy behind the Efficiency score) from the Open-Meteo archive. Only empty values are filled, requests are rate-limited and demo vehicles are skipped. It runs once automatically after the upgrade.

| Field | Default | Meaning |
|---|---|---|
| `vin` | all | Vehicle to process |
| `days` | 365 | How many past days to fill |

```yaml
action: rivian.backfill_weather
data:
  days: 90
```

## Rebuilding derived data

All take just an optional `vin`:

| Action | What it does | When you need it |
|---|---|---|
| `rivian.rebuild_places` | Re-detects frequent start/stop spots (seeded from HA zones) and relabels every drive | After many drives changed at once, e.g. a backfill |
| `rivian.rebuild_routes` | Regroups repeated start-to-end pairs into routes and variants and recomputes their stats | After a backfill or place merge |
| `rivian.rebuild_heat_map` | Recounts the road heat map from stored routes | Only after changing Home Assistant's time zone |
| `rivian.recompute_drive_stats` | Recomputes moving/stopped time, stops, climb, descent, max speed and highway share from stored GPS routes (live-only fields such as range and driver are untouched) | After upgrades that change stat definitions; older, simplified routes give slightly less accurate numbers |
| `rivian.fit_energy_model` | Refits the efficiency model from the last 90 days of routed drives (needs at least 15, otherwise the previous fit is kept) | To refresh the Model view and Efficiency scores now instead of at the nightly refit |
| `rivian.snap_route_gaps` | Fills GPS dropouts by snapping the gap onto OpenStreetMap roads; gaps that cannot be matched within about 50 m stay unfilled | Runs automatically after drives; use to retry |

```yaml
action: rivian.rebuild_places
data:
  vin: "7PDSGABA8NN000000"
```
