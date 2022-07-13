# Home Assistant integration for Rivian (Unofficial)
[![GitHub Release][releases-shield]][releases]
![GitHub all releases][download-all]
![GitHub release (latest by SemVer)][download-latest]
[![GitHub Activity][commits-shield]][commits]


This integration is an unofficial Rivian integration for Home Assistant, installed through [HACS](https://hacs.xyz/docs/setup/download).

In order to use this extension, you will need the following information
 - Rivian Username
 - Rivian Password
 - Client ID
 - Client Secret
 - VIN

Your vehicle must be in delivered status and in your posession for this integration to function. The client id and client secret must be obtained outside of this integration and there is currently no official or unofficial way to do this. You can search forums or the [Rivian Discord][rivian-discord]

## Installation

1. Use [HACS](https://hacs.xyz/docs/setup/download), in `HACS > Integrations > Explore & Add Repositories` search for "Rivian". After adding this `https://github.com/bretterer/home-assistant-rivian` as a custom repository. Skip to 7.
2. If no HACS, use the tool of choice to open the directory (folder) for your HA configuration (where you find `configuration.yaml`).
3. If you do not have a `custom_components` directory (folder) there, you need to create it.
4. In the `custom_components` directory (folder) create a new folder called `rivian`.
5. Download _all_ the files from the `custom_components/rivian/` directory (folder) in this repository.
6. Place the files you downloaded in the new directory (folder) you created.
7. Restart Home Assistant.
8. [![Add Integration][add-integration-badge]][add-integration] or in the HA UI go to "Configuration" -> "Integrations" click "+" and search for "Rivian (Unofficial)".

<!---->

## Available Sensors
| Sensor                      | ID                                                            | Description                                                      |
|-----------------------------|---------------------------------------------------------------|------------------------------------------------------------------|
| Global Closure Locked State | sensor.rivian_body_closure_global_closure_locked_state        | Doors and windows locked status (Locked\|Unlocked\|              |
| Global Closure State        | sensor.rivian_body_closure_global_closure_state               | Doors and windows closure state (Closed\|Open)                   |
| Power State                 | sensor.rivian_core_power_modes_power_state                    | Determines the Power State (sleep\|go)                           |
| Odometer                    | sensor.rivian_dynamics_odometer_value                         | Odometer Reading (in miles)                                      |
| Gear Selector               | sensor.rivian_dynamics_propulsion_status_prndl                | Current Gear Selection                                           |
| Front Left Tire Pressure    | sensor.rivian_dynamics_tires_tire_fl_pressure                 | Front left tire pressure in PSI (will display `--` when parked)  |
| Front Right Tire Pressure   | sensor.rivian_dynamics_tires_tire_fr_pressure                 | Front right tire pressure in PSI (will display `--` when parked) |
| Rear Left Tire Pressure     | sensor.rivian_dynamics_tires_tire_rl_pressure                 | Rear left tire pressure in PSI (will display `--` when parked)   |
| Rear Right Tire Pressure    | sensor.rivian_dynamics_tires_tire_rr_pressure                 | Rear Right tire pressure in PSI (will display `--` when parked)  |
| Current Charge              | sensor.rivian_energy_storage_charger_adjusted_soc             | Percentage of charge                                             |
| Max Charge Setting          | sensor.rivian_energy_storage_charger_stored_user_range_select | Current charge setting (Daily, Extended, Full)                   |
| Charging Status             | sensor.rivian_energy_storage_charger_vehicle_charger_state    | Current charging status                                          |
| Estimated Vehicle Range     | sensor.rivian_energy_storage_vehicle_energy_vehicle_range     | Estimated range based on current drive mode                      |
| Cabin Temperature           | sensor.rivian_thermal_hvac_cabin_control_cabin_temperature    | Current temperature of cabin in `TEMP_FAHRENHEIT`                |


## Location tracking
This integration uses the Rivian's GNSS sensors for device tracking. View the location on the map in Home Assistant.

## Special Thanks
- [jrgutier](https://github.com/jrgutier) - Helped with getting information on the Rivian API

---

[commits-shield]: https://img.shields.io/github/commit-activity/w/bretterer/home-assistant-rivian?style=flat-square
[commits]: https://github.com/bretterer/home-assistant-rivian/commits/main
[releases-shield]: https://img.shields.io/github/release/bretterer/home-assistant-rivian.svg?style=flat-square
[releases]: https://github.com/bretterer/home-assistant-rivian/releases
[download-all]: https://img.shields.io/github/downloads/bretterer/home-assistant-rivian/total?style=flat-square
[download-latest]: https://img.shields.io/github/downloads/bretterer/home-assistant-rivian/latest/total?style=flat-square
[add-integration]: https://my.home-assistant.io/redirect/config_flow_start?domain=rivian
[add-integration-badge]: https://my.home-assistant.io/badges/config_flow_start.svg
[rivian-discord]: https://discord.gg/jEc5RUPd