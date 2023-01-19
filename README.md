# Home Assistant integration for Rivian (Unofficial)
[![GitHub Release][releases-shield]][releases]
![GitHub all releases][download-all]
![GitHub release (latest by SemVer)][download-latest]
[![GitHub Activity][commits-shield]][commits]


An unofficial Rivian integration for Home Assistant, installed through [HACS](https://hacs.xyz/docs/setup/download).

In order to use this extension, you'll need the following information
 - Rivian Username
 - Rivian Password
 - VIN

Your vehicle must be in delivered status and in your possession for this integration to function.

# Disclaimer
This [Home Assistant](https://www.home-assistant.io/) integration is not affiliated, associated, nor sponsored by Rivian Automotive, Inc.

Any use of this integration is at the sole discretion and risk of the Rivian vehicle owner integrating it into their Home Assistant installation. This owner takes full responsibility to protect their local Home Assistant installation.

This integration is provided out of love and passion towards the Rivian ownership community and with a focus on building insights and awareness into the state of one's own vehicle for use by the owner and the owner alone.

No user or vehicle data is or ever will be shared outside of the Home Assistant integration itself which is a single user environment hosted and running within an owner's own private network and hardware systems.

## Installation

1. Use [HACS](https://hacs.xyz/docs/setup/download). In HACS add a custom repository by going to `HACS > Integrations > 3 dots > custom repositories` and adding this github repo `https://github.com/bretterer/home-assistant-rivian`. Now skip to 7.
2. If no HACS, use the tool of choice to open the directory (folder) for your HA configuration (where you find `configuration.yaml`).
3. If you do not have a `custom_components` directory (folder) there, you need to create it.
4. In the `custom_components` directory (folder) create a new folder called `rivian`.
5. Download _all_ the files from the `custom_components/rivian/` directory (folder) in this repository.
6. Place the files you downloaded in the new directory (folder) you created.
7. Restart Home Assistant.
8. [![Add Integration][add-integration-badge]][add-integration] or in the HA UI go to "Configuration" -> "Integrations" click "+" and search for "Rivian (Unofficial)".

## Configuration

To connect your newly installed integration up to pull available sensor data (see below example) for your Rivian vehicle you should follow the following configuration recommendations. Not doing so could put your main Rivian account at risk of being locked and available to you.

* One Time Password (**recommended**) - It is best security practice to always enable One Time Password / Multi Factor Authentication this way your credentials (username + password) can not be used without your explicit consent through the personal device configured on your Rivian Account.
* No Account Reuse* (**recommended**) - Another best security practice is to not reuse account or credentials across different integrations (eg. sharing credentials between HA with your every day account). We highly recommend the owner to *Invite a new driver* for example, `user+<purpose>@domain.com`, and use only this profile for configuration within HA.

The configuration flow is as follows:

1. **Username and Password (required)**
   - For the integration to work we require a username and password to a Rivian account that has an active *driver profile* for the vehicle you wish to monitor remotely. 
2. **OTP/MFA (optional)**
   - If OTP is enabled on the account provided you will receive a txt from Rivian with a one time use passcode enter that at this step.
3. Vehicle VIN (required)
   - Type the interested vehicle's identification number (VIN) as found on the [Rivian Account website](https://rivian.com/account/home)
   - Currently we only poll for a single vehicle's sensor data, track https://github.com/bretterer/home-assistant-rivian/issues/20 for multiple VIN support to come later.


*Developer and Maintainers of this project take no personal responsibility for misconfiguration, or misuse of the code provided by this integration. Account security is fully the sole responsibility of the owner configuring this integration. Accounts may become locked/quarantined due to the use of this unofficial integration.

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
- [tmack8001](https://github.com/tmack8001) - Helping with Development and Testing

## Sponsors
- [74656b](https://github.com/74656b) Thank you for the sponsorship!
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
