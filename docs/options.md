# Options

Open **Settings > Devices & services > Rivian (Unofficial) > Configure** to change these. Changing options reloads the integration; a drive in progress is checkpointed and resumed.

## Main page

| Option | Meaning |
|---|---|
| Vehicle image style | Style of the legacy vehicle image entities (the dashboard Vehicles tab uses its own picture). |
| Enable vehicle control | Experimental. Choose which vehicles can be controlled; requires 2FA and Bluetooth pairing. See [Vehicle control](vehicle-control.md). |
| Limit vehicle control to the following zones | Optional Home Assistant zones in which commands are allowed. |

| Option | Default | Range | Meaning |
|---|---|---|---|
| Drive analytics (`drive_analytics`) | Off | on/off | Records drives, GPS routes and charging sessions in `.storage/rivian_analytics.db` on your Home Assistant host, for the drive sensors, the statistics and the Rivian dashboard. When off, none of that runs: no recording, no dashboard cards, and the analytics actions report an error. Turning it off later keeps the history already recorded and removes the drive sensors and the vehicle picture; removing the integration deletes the history. |
| Outside temperature sensor and drive weather (from Open-Meteo) (`weather_lookups`) | Off | on/off | Gives each vehicle an *Outside temperature* sensor (the vehicle doesn't report one) and, with drive analytics on, each drive's and charging session's temperature, wind and rain, all from Open-Meteo. Only coordinates rounded to about 1 km and times are sent. The sensor is looked up every 15 minutes (a failed lookup is retried after 1, 2 and 5 minutes) and when the vehicle has moved more than about a kilometer, at most once every 5 minutes. When off, nothing is sent: there is no Outside temperature sensor, drives and sessions show no weather, and the Efficiency score uses calm, mild conditions. Turning it on with drive analytics fills in the past year once. See [Privacy](privacy.md). |

## Drive analytics page

After **Submit**, a second page with these settings appears while **Drive analytics** is on. With it off the page is skipped, and the settings keep their values for when it's turned on again.

| Option | Default | Range | Meaning |
|---|---|---|---|
| Let non-admin users view drive analytics (`analytics_all_users`) | Off | on/off | Drive analytics include your full route history, so by default only administrators can read them (other users cannot load the dashboard's data). Turn on to let every Home Assistant user view them. Changing or deleting data stays admin-only. |
| Analytics history retention (`analytics_retention_days`) | 365 | 0-3650 (0 = forever) | Drives and idle-drain events older than this are pruned (daily). Fast-charge session summaries are kept, but only the newest 50 keep their detailed curves. Home Assistant long-term statistics and battery capacity history are **never** pruned. |
| Store GPS routes for drives (`track_capture`) | On | on/off | When off, drives are still recorded but no GPS route is stored, so maps, heat maps, favorite routes and route-based efficiency are unavailable for them. |
| Keep GPS routes for (`track_retention_days`) | 365 | 0-3650 (0 = as long as the drive) | How long a drive's full route is kept. Typical daily driving takes roughly 12 MB per year at full detail. The road heat map is kept even after routes are removed. |
| Thin routes older than (`track_full_detail_days`) | 0 (never) | 0-3650 | Older routes are simplified to about a tenth of their size (same shape, about 10 m accuracy) instead of deleted. |
| Automatically name frequent places (`place_geocoding`) | On | on/off | Looks up a suggested name for unnamed places you visit often, using OpenStreetMap Nominatim. Only that place's coordinates are sent. When off, suggestions show as "Place #N" until you name them, and fast-charge sessions are not looked up in OpenStreetMap for a station name or brand. See [Privacy](privacy.md). |

Credentials and the username/password can be changed by reconfiguring the integration.
