# Privacy

Your drives, locations, charging sessions and statistics are stored **only on your Home Assistant host** (`.storage/rivian_analytics.db` and Home Assistant's own long-term statistics). Nothing is uploaded to a service run by this project.

The integration necessarily talks to Rivian's cloud to read your vehicle, as the official app does. Beyond that, a few optional lookups go to public services. This is everything that leaves Home Assistant:

| Service | What is sent | When | How to stop it |
|---|---|---|---|
| **Rivian cloud** (GraphQL and websocket, undocumented API) | Your login, vehicle state requests, remote commands you send; for pictures, your vehicle's order and configuration; for charging history, session records. Payment and account-address fields are never requested. | Always for live data; pictures once per vehicle (retried weekly on failure); charging history daily | Live data is the integration's purpose. Skip history with no action; pictures can be replaced with `rivian.set_vehicle_picture` |
| **OpenStreetMap Overpass** | The bounding box of a GPS dropout (to find roads for gap snapping); a **rounded** location of a fast-charge session (to find the station) | After drives with gaps; after fast charges | Gap snapping needs stored routes: turn off **Store GPS routes** in [Options](options.md). Turning off **Automatically name frequent places** in [Options](options.md) also stops the charging-station lookup |
| **OpenStreetMap Nominatim** | One place coordinate per suggested place (to propose a name). Never your name, VIN or drive times | Once per frequently visited unnamed place, retried after 7 days if nothing found | Turn off **Automatically name frequent places** in [Options](options.md) |
| **Open-Meteo** | Latitude, longitude and times of sample points along your drives (to get weather for the drive), and the location and time of each charging session (for the outside temperature while it charged) | During drives (about every 15 miles or 20 minutes), after each charge and nightly for sessions still missing a temperature, and for the one-off weather backfill | Not optional today; skipped for demo vehicles |
| **rivian.com/compimg** | Normal image request from Home Assistant for your own vehicle's picture (real vehicles only) | When a picture is first looked up | Use `rivian.set_vehicle_picture` with your own image |
| **Map tiles (Esri)** | Your browser requests map tiles for the area you are viewing, like any web map | When you view a map | The Drives, Fav Routes and Destinations tabs need a basemap |

All public lookups use Home Assistant's shared web session with a clear User-Agent, are rate-limited, and are cached (roads for 90 days).

Demo vehicles never send anything about real locations: they use synthetic data and vehicle illustrations that ship with the integration (no photos or logos), so they contact no Rivian server.

## Attribution & data licenses

- **OpenStreetMap.** Map data &copy; [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors, available under the [Open Database License (ODbL)](https://opendatacommons.org/licenses/odbl/). The integration reads it through the public Overpass API (road snapping, charging-station lookup) and Nominatim (place names). It follows the [Nominatim usage policy](https://operations.osmfoundation.org/policies/nominatim/): at most one request per second, an identifying User-Agent (`home-assistant-rivian/<version>`), and results cached so a location is not asked about repeatedly. The maps show the OpenStreetMap credit.
- **Open-Meteo.** Weather data by [Open-Meteo.com](https://open-meteo.com), licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Open-Meteo's free API is for non-commercial use; this integration is a personal, non-commercial tool for your own vehicle. Requests carry the same identifying User-Agent.
- **Esri.** Basemap tiles (Map, Streets, Satellite) come from Esri's public basemap services and are requested by your browser. Esri's terms of use apply; the map shows an Esri credit.
- **Rivian.** This is an unofficial integration. It uses Rivian's undocumented APIs, which can change or stop working at any time, and it is not affiliated with, endorsed by or supported by Rivian Automotive. Rivian and related names are trademarks of their owner, used only to describe compatibility.
- **Bundled software.** Leaflet, Mushroom and the Plotly Graph Card (with plotly.js) ship with the integration under their own licenses: see [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).

## Deleting your data

- A single drive, a day, a place, a charging session: use the admin delete buttons on the tabs.
- A whole vehicle's history: **Delete vehicle history...** on the [Vehicles tab](vehicles.md) (also clears its long-term statistics).
- Everything: remove the integration and delete `.storage/rivian_analytics.db`.
