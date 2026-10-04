# Options

Open **Settings > Devices & services > Rivian (Unofficial) > Configure** to change these. Changing options reloads the integration; a drive in progress is checkpointed and resumed.

## Analytics and storage

| Option | Default | Range | Meaning |
|---|---|---|---|
| Analytics history retention (`analytics_retention_days`) | 365 | 0-3650 (0 = forever) | Drives and idle-drain events older than this are pruned (daily). Fast-charge session summaries are kept, but only the newest 50 keep their detailed curves. Home Assistant long-term statistics and battery capacity history are **never** pruned. |
| Store GPS routes for drives (`track_capture`) | On | on/off | When off, drives are still recorded but no GPS route is stored, so maps, heat maps, favorite routes and route-based efficiency are unavailable for them. |
| Keep GPS routes for (`track_retention_days`) | 365 | 0-3650 (0 = as long as the drive) | How long a drive's full route is kept. Typical daily driving takes roughly 12 MB per year at full detail. The road heat map is kept even after routes are removed. |
| Thin routes older than (`track_full_detail_days`) | 0 (never) | 0-3650 | Older routes are simplified to about a tenth of their size (same shape, about 10 m accuracy) instead of deleted. |
| Chart history (`chart_window_days`) | 365 | 7-3650 | How far back the dashboard charts reach. Does not change what is stored. |
| Automatically name frequent places (`place_geocoding`) | On | on/off | Looks up a suggested name for unnamed places you visit often, using OpenStreetMap Nominatim. Only that place's coordinates are sent. When off, suggestions show as "Place #N" until you name them, and fast-charge sessions are not looked up in OpenStreetMap for a station name or brand. See [Privacy](privacy.md). |

## Vehicle

| Option | Meaning |
|---|---|
| Vehicle image style | Style of the legacy vehicle image entities (the dashboard Vehicles tab uses its own picture). |
| Enable vehicle control | Experimental. Choose which vehicles can be controlled; requires 2FA and Bluetooth pairing. See [Vehicle control](vehicle-control.md). |
| Limit vehicle control to the following zones | Optional Home Assistant zones in which commands are allowed. |

Credentials and the username/password can be changed by reconfiguring the integration.
