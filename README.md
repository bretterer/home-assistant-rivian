# Home Assistant integration for Rivian (Unofficial)

[![GitHub Release][releases-shield]][releases]
[![Add Integration][add-integration-badge]][add-integration]

An unofficial Rivian integration for Home Assistant. It exposes your vehicle's live state as entities, and, beyond that, records every drive and charge on your own Home Assistant host and turns them into a ready-made dashboard: a calendar and heat maps of where you drive, favorite routes ranked like Strava segments, named destinations, charging and battery-health analysis, and an efficiency page that explains how weather, wind and speed affect your mi/kWh. Everything is stored locally.

![Drives tab: a day with the two demo vehicles](docs/images/drives-day.png)

## Feature tour

The generated **Rivian** dashboard has six tabs, all with a [vehicle bar](docs/vehicle-bar.md) to view one car or compare several (selection remembered per user).

- **[Vehicles](docs/vehicles.md)**: one card per vehicle with battery, range, live chips, 7-day to lifetime totals and a household summary.
- **[Drives](docs/drives.md)**: an All time → year → month → day → drive calendar; road heat maps; speed-colored day maps with numbered drives; speed, elevation, efficiency and battery charts with hover readouts; Strava-style stats; delete a drive or a day.
- **[Fav Routes](docs/fav-routes.md)**: repeated trips (Home → Office) with path variants, fastest/average/slowest, a dot chart and a sortable table.
- **[Destinations](docs/destinations.md)**: Home Assistant zones plus auto-detected and custom places with 14 categories; rename, merge, hide, drag to move or resize.
- **[Charging](docs/charging.md)**: battery level with 20-80 % bands, a charging scorecard, fast-charge curves against an ideal curve per pack, battery capacity over time, full history.
- **[Efficiency](docs/efficiency.md)**: efficiency against temperature, headwind, air density, rain, speed, trip length and climb, by speed band, over time, and per drive against an expected value.

![Charging tab: battery level and scorecard for the two demo vehicles](docs/images/charging-top.png)

![Efficiency tab for the demo vehicles](docs/images/efficiency.png)

![Vehicles tab with the two demo vehicles](docs/images/vehicles.png)

On a phone the layouts stack, one finger scrolls the page and two fingers move the map ([Drives on a phone](docs/drives.md#on-a-phone)).

## Requirements

- Home Assistant 2025.1.0 or newer.
- A Rivian account with a driver profile for a delivered vehicle. Two-factor authentication is recommended, and required for remote controls.

## Installation

**HACS (recommended):** in HACS, open the menu > **Custom repositories**, add this repository with category *Integration*, download **Rivian (Unofficial)** and restart Home Assistant.

**Manual:** copy `custom_components/rivian` into your Home Assistant `custom_components` folder and restart.

## Quick start

1. **Settings > Devices & services > Add integration > Rivian (Unofficial)**; enter your Rivian username, password and, if asked, the verification code.
2. The **Rivian** dashboard is created automatically the first time the integration starts. Restart Home Assistant once if it is not in the sidebar yet. (The `rivian.create_efficiency_dashboard` action regenerates or repairs it later.)
3. Drive, charge, and the pages fill in. Want a look first? Run `rivian.create_demo_data` for two demo vehicles with sample data (remove with `rivian.delete_demo_data`).
4. Optional: rebuild earlier drives from Home Assistant's recorder with `rivian.backfill_drive_history` (start with `dry_run: true`).

Details in [Getting started](docs/getting-started.md). Upgrading from 1.5.x? Read [Upgrading](docs/upgrading.md).

## Security recommendations

- Enable 2FA on your Rivian account.
- Don't reuse your everyday account: invite a separate driver (for example `you+ha@example.com`) for Home Assistant. It must sign in once on the Rivian phone app and be linked to the vehicle before sensors appear.

## Remote vehicle control

You can lock/unlock, precondition, set charge limits, and more, as in the Rivian app. It needs 2FA, one-time Bluetooth pairing from the Home Assistant host (a Bluetooth 4.2+ adapter or an ESP32 Bluetooth proxy) and a free phone-key slot. **Gen2 (2025+) vehicles are not supported for control** because their Bluetooth hardware changed. Pairing steps are in [Vehicle control](docs/vehicle-control.md); the available [sensors and controls](docs/entities.md) are listed too.

## Privacy

Drive and charge data stay on your Home Assistant host. Optional public lookups (OpenStreetMap for road snapping, place names and charging stations; Open-Meteo for weather) send only coordinates and times, and can be limited in the options. Full table in [Privacy](docs/privacy.md).

## Documentation

[Documentation index](docs/README.md) · [Getting started](docs/getting-started.md) · [Vehicle bar](docs/vehicle-bar.md) · [Services](docs/services.md) · [Options](docs/options.md) · [Privacy](docs/privacy.md) · [Upgrading](docs/upgrading.md) · [FAQ](docs/faq.md)

## Disclaimer

**Unofficial: not affiliated with, endorsed by or supported by Rivian Automotive.** Rivian and related names are trademarks of their owner, used only to describe compatibility. Provided as is, without warranty of any kind; use at your own risk.

This integration is not affiliated with, associated with or sponsored by Rivian Automotive, Inc. Use is at the sole discretion and risk of the vehicle owner, who is responsible for protecting their own Home Assistant installation. Accounts may be locked by Rivian due to unofficial API use. Developers and maintainers take no responsibility for misconfiguration or misuse. The integration relies on Rivian's undocumented API, which can change without notice.

## Credits

- This project builds on [bretterer/home-assistant-rivian](https://github.com/bretterer/home-assistant-rivian), the original Rivian integration for Home Assistant.
- [rivian-python-client](https://github.com/bretterer/rivian-python-client) provides the API client.
- Thanks to [jrgutier](https://github.com/jrgutier) (Rivian API research), [tmack8001](https://github.com/tmack8001) (development and testing) and [natekspencer](https://github.com/natekspencer) (keeping the integration current with Home Assistant).
- Map data &copy; [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors (ODbL); weather by [Open-Meteo](https://open-meteo.com) (CC BY 4.0); basemaps by Esri; maps drawn with [Leaflet](https://leafletjs.com).
- Bundled front-end libraries (Leaflet, Mushroom, Plotly Graph Card and plotly.js) keep their own licenses: see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Attribution for data sources is in [Privacy](docs/privacy.md#attribution--data-licenses).

[releases-shield]: https://img.shields.io/github/release/bretterer/home-assistant-rivian.svg?style=flat-square
[releases]: https://github.com/bretterer/home-assistant-rivian/releases
[add-integration]: https://my.home-assistant.io/redirect/config_flow_start?domain=rivian
[add-integration-badge]: https://my.home-assistant.io/badges/config_flow_start.svg
