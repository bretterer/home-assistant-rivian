# Changelog

## 2.0.0

A large release. Alongside the existing vehicle entities and controls, the integration now keeps its own drive, charging and efficiency history and builds a full Rivian dashboard from it. See [Upgrading from 1.5.x](docs/upgrading.md) before updating.

### Attribution and neutral demo content

- Maps now credit OpenStreetMap contributors beside Esri, and [Privacy](docs/privacy.md) lists every data source's license (OpenStreetMap ODbL, Open-Meteo CC BY 4.0, Esri). Nominatim, Overpass and Open-Meteo requests all send an identifying `home-assistant-rivian/<version>` User-Agent.
- Bundled libraries (Leaflet, Mushroom, Plotly Graph Card, plotly.js) are listed with their licenses in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md); the README disclaimer is stronger.
- The demo vehicles now use two neutral, original illustrations that ship with the integration instead of Rivian images, so they contact no Rivian server. All documentation screenshots show only the demo vehicles.

### New: the Rivian dashboard

The dashboard is created automatically the first time the integration is set up (once per Home Assistant instance; not in YAML Lovelace mode, where a notification explains how to add it). Run `rivian.create_efficiency_dashboard` to regenerate or repair it. It has six tabs and needs no extra frontend installs, because every card it uses ships with the integration.

- **Vehicles:** a card per vehicle with its picture, live chips and 7-day, 30-day, 365-day and lifetime totals, plus household totals when several vehicles are selected.
- **Drives:**
  - A calendar tree, from all time down to year, month, day and drive.
  - Road heat maps.
  - Day and drive maps colored by speed, elevation or efficiency.
  - Speed, elevation, efficiency and battery charts, with hover readouts.
  - Strava-style stats: moving time, stops, climb, max speed, range used, drive mode, driver and trailer.
  - A phone layout.
- **Fav Routes:**
  - Repeated drives between the same two destinations, split by path.
  - Fastest, average and slowest times, overall and per vehicle.
  - A map of every drive, a time-over-date chart and a sortable table.
- **Destinations:**
  - Frequent stops become places automatically, seeded from Home Assistant zones.
  - Unnamed places get a name from OpenStreetMap.
  - Admins can rename, recategorize (14 categories), move or resize by dragging, merge, hide, delete, or turn a place into a Home Assistant zone.
- **Charging:**
  - Battery % over time, with 20–80 % bands and brush-to-zoom.
  - A charging-habits scorecard.
  - DC fast-charge curves against each pack's expected curve, with station brand and name.
  - Battery capacity over time, with temperature coloring.
  - A charging history table, AC sessions included: type (DC Fast, AC L2, AC L1), peak and average rate, battery temperature (when the vehicle reports one) and outside temperature.
  - Charges the battery level shows but no session recorded are drawn on the battery chart too, marked as detected.
- **Efficiency:**
  - mi/kWh against temperature, headwind, air density, rain, speed, trip length or climb.
  - Efficiency by speed band.
  - Weekly and monthly trends.
  - A drive table scoring each drive against a fitted energy model.
- **Several vehicles:** a vehicle bar on every tab lets you view one, some or all vehicles. The choice is remembered per Home Assistant user. Each vehicle gets a stable color and letter.

### New: analytics storage and services

- **Storage:**
  - Drive, route, charging and efficiency history lives in a SQLite database at `.storage/rivian_analytics.db`. It uses the standard library, so it adds no dependency, and migrates automatically.
  - Hourly totals also go to Home Assistant long-term statistics (`rivian:*`), which are never purged.
  - Retention is configurable: `analytics_retention_days`, `track_capture`, `track_retention_days`, `track_full_detail_days`, `chart_window_days` and `place_geocoding`.
- **Drive tracking:**
  - Records each drive's GPS route.
  - Survives a restart mid-drive.
  - Samples weather along the way.
  - Records DC fast-charge and home/AC charging sessions.
- **History import:**
  - `rivian.backfill_drive_history` rebuilds past drives, routes and charging sessions from the recorder database, which it opens read-only.
  - `rivian.import_charging_history` imports Rivian's completed-session list.
- **Maintenance services:**
  - `rivian.rebuild_heat_map`, `rivian.recompute_drive_stats` and `rivian.fit_energy_model`.
  - `rivian.snap_route_gaps` fills GPS dropouts along OpenStreetMap roads.
  - `rivian.rebuild_places`, `rivian.rebuild_routes` and `rivian.backfill_weather`.
- **Vehicle pictures:** each vehicle's picture comes from a rivian.com configurator render. `rivian.set_vehicle_picture` replaces it with any image.
- **Demo vehicles:** `rivian.create_demo_data` and `rivian.delete_demo_data` add or remove two made-up vehicles with fictional drives, so you can explore the dashboard. Demo places never mix with real ones.
- **Deleting data:** admins can delete a drive, a day, a place, a charging session or a vehicle's whole history, each after confirming. Long-term statistics are rewritten to match.

### Privacy

Only coordinates and times are sent to public services: OpenStreetMap Overpass and Nominatim, and Open-Meteo. No account data is sent. Turning off **Automatically name frequent places** stops both the place naming and the charging-station lookup. See [Privacy](docs/privacy.md).

### Changed

- **The dashboard:** the Plotly efficiency and charging tabs are replaced by custom cards. Dashboards from earlier builds are repaired the next time `rivian.create_efficiency_dashboard` runs.
- **The "Dashboard vehicle" select entity is deprecated.** The vehicle bar replaces it.
- **Home Assistant 2025.1.0 or later is required.**
- **Fewer dependencies:** unused dependencies are dropped, and `manifest.json` pins only `rivian-python-client`.

### Fixed

- Bulk analytics attributes are kept out of the recorder, so state attributes stay under its 16 KiB limit.
- Services are removed cleanly on unload, and service translations are in sync with `services.yaml`.
