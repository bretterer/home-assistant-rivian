# Home Assistant integration for Rivian (Unofficial)

[![GitHub Release][releases-shield]][releases]
![GitHub all releases][download-all]
![GitHub release (latest by SemVer)][download-latest]
[![GitHub Activity][commits-shield]][commits]

An unofficial Rivian integration for Home Assistant, installed through [HACS](https://hacs.xyz/docs/setup/download).

To use this extension, you'll need the following information

- Rivian Username
- Rivian Password
- Rivian Account's MFA/OTP Device (optional, depending on your Rivian account settings)

Your vehicle must be in delivered status and possession for this integration to function.

# Disclaimer

This [Home Assistant](https://www.home-assistant.io/) integration is not affiliated, associated, nor sponsored by Rivian Automotive, Inc.

Any use of this integration is at the sole discretion and risk of the Rivian vehicle owner integrating it into their Home Assistant installation. This owner takes full responsibility for protecting their local Home Assistant installation.

This integration is provided out of love and passion towards the Rivian ownership community. It focuses on building insights and awareness into the state of one's vehicle for use by the owner and the owner alone.

This integration will not share user or vehicle data outside Home Assistant, a single-user environment hosted and running within an owner's private network and hardware systems.

![Home Assistant Rivian Card](.github/images/home_assistant_rivian_entity_card.png)

## Installation

1. Use [HACS](https://hacs.xyz/docs/setup/download). In HACS add a custom repository by going to `HACS > Integrations > 3 dots > custom repositories` and adding this github repo `https://github.com/bretterer/home-assistant-rivian`. Now skip to 7.
2. If no HACS, use the tool of choice to open the directory (folder) for your Home Assistant configuration (where you find `configuration.yaml`).
3. If you do not have a `custom_components` directory (folder) there, you need to create it.
4. In the `custom_components` directory (folder) create a new folder called `rivian`.
5. Download _all_ the files from the `custom_components/rivian/` directory (folder) in this repository.
6. Place the files you downloaded in the new directory (folder) you created.
7. Restart Home Assistant.
8. [![Add Integration][add-integration-badge]][add-integration] or in the Home Assistant UI go to "Configuration" -> "Integrations" click "+" and search for "Rivian (Unofficial)".

## Configuration

To connect your newly installed integration to pull available sensor data (see the below example) for your Rivian vehicle, you should follow the following configuration recommendations. Not doing so could put your main Rivian account at risk of being locked and available.

- One Time Password (**recommended**) - It is best security practice always to enable One Time Password / Multi-Factor Authentication. This way, your credentials (username + password) can not be used without your explicit consent through the personal device configured on your Rivian account.
- No Account Reuse* (**recommended**) - Another best security practice is not to reuse accounts or credentials across different integrations (e.g., sharing credentials between Home Assistant and your everyday account). We highly recommend the owner *invite a new driver\*, for example, `user+<purpose>@domain.com`, and use only this profile for configuration within Home Assistant. **Note:** Sensors will not be displayed for this new driver until the driver is signed in to a Rivian phone app and linked to the vehicle like an actual new driver. You may temporarily sign out of your Rivian phone app to set up this driver profile: Profile icon (bottom nav bar) > "Manage account" > Sign out (top right corner).

The configuration flow is as follows:

1. **Username and Password (required)**
   - For the integration to work, we require a username and password to a Rivian account with an active _driver profile_ for the vehicle you wish to monitor remotely.
2. **OTP/MFA (optional)**
   - If OTP is enabled on the account provided, you will receive a text from Rivian with a one-time use passcode entered at this step.

\*Developers and Maintainers of this project take no personal responsibility for misconfiguration or misuse of the code provided by this integration. Account security is entirely the sole responsibility of the owner configuring this integration. Accounts may become locked/quarantined due to this unofficial integration.

## Remote Vehicle Control

In addition to viewing vehicle data, it is also possible to issue certain vehicle commands remotely via Home Assistant as you would in the Rivian mobile app.

**Note:** Gen2 (2025) models are not curently supported for Remote Vehicle Control due to changes in their bluetooth hardware that have not been implemented yet. They are not able to particpate in the secure pairing process required.

To enable this feature, you need to have:

- Home Assistant instance with a Bluetooth adapter (or [ESP32 Bluetooth proxy](https://esphome.io/projects/?type=bluetooth)) with a minimum Bluetooth version of 4.2
  - The Bluetooth adapter/proxy needs to be within reach of your vehicle's Bluetooth radio to complete the one-time, in-vehicle pairing process
  - After pairing, Bluetooth is no longer required as commands are sent remotely via the Rivian cloud
- Two-factor authentication (2FA) enabled on your Rivian account
- An available slot for a phone key (currently limited to 4 per vehicle as of vehicle software 2023.26.00)

To complete the pairing setup, configure the integration and select the vehicle(s) for which you want to enable vehicle controls in Home Assistant and then:

1. From within your vehicle, navigate to "Drivers and Keys" under settings
2. Select "Set Up" for the relevant phone key (this should be the name of your Home Assistant instance's home location, typically "Home")
3. Click on the "Pair" button entity for the associated vehicle in Home Assistant
   - Home Assistant will then attempt to connect to your vehicle and complete the pairing process. On Darwin (MacOS) based systems, a pop-up will appear which you will have to manually click to allow pairing. On Linux or Windows systems, this should happen automatically.

Note: If you are having issues with pairing your vehicle, we recommend investing in an ESP32 with Bluetooth proxy installed as it offers flexibility in placement and has been proven to work where other adapters have not.

## Available Sensors

| Name                                                | Domain         | Description                            |
| --------------------------------------------------- | -------------- | -------------------------------------- |
| 12V Battery Health                                  | Sensor         |                                        |
| Altitude                                            | Sensor         |                                        |
| Battery Capactiy                                    | Sensor         |                                        |
| Battery State of Charge                             | Sensor         |                                        |
| Battery State of Charge Limit                       | Sensor         |                                        |
| Battery Thermal Runaway Propagation                 | Sensor         |                                        |
| Battery Thermal Status                              | Sensor         |                                        |
| Bearing                                             | Sensor         |                                        |
| Bluetooth Module Failure Status Door Front Left     | Sensor         |                                        |
| Bluetooth Module Failure Status Door Front Right    | Sensor         |                                        |
| Bluetooth Module Failure Status Fascia Front        | Sensor         |                                        |
| Bluetooth Module Failure Status Fascia Rear         | Sensor         |                                        |
| Bluetooth Module Failure Status Instrument Controls | Sensor         |                                        |
| Brake Fluid Level Low                               | Sensor         |                                        |
| Cabin Climate Preconditioning                       | Binary Sensor  | Running/not running status             |
| Cabin Climate Preconditioning Type                  | Sensor         |                                        |
| Cabin Temperature                                   | Sensor         |                                        |
| Car Wash Mode                                       | Binary Sensor  | On/off status                          |
| Charger Connection                                  | Binary Sensor  | Plugged in/unplugged status            |
| Charger Derate Status                               | Sensor         |                                        |
| Charging Status                                     | Binary Sensor  | Charging/not charging status           |
| Charging Time Remaining                             | Sensor         |                                        |
| Closure State                                       | Binary Sensor  | Open/closed status of all closures     |
| Defrost/Defog                                       | Binary Sensor  | Running/not running status             |
| Door Front Left                                     | Binary Sensor  | Open/closed status                     |
| Door Front Left Lock                                | Binary Sensor  | Locked/unlocked status                 |
| Door Front Right                                    | Binary Sensor  | Open/closed status                     |
| Door Front Right Lock                               | Binary Sensor  | Locked/unlocked status                 |
| Door Rear Left                                      | Binary Sensor  | Open/closed status                     |
| Door Rear Left Lock                                 | Binary Sensor  | Locked/unlocked status                 |
| Door Rear Right                                     | Binary Sensor  | Open/closed status                     |
| Door Rear Right Lock                                | Binary Sensor  | Locked/unlocked status                 |
| Door State                                          | Binary Sensor  | Open/closed status of all doors        |
| Drive Mode                                          | Sensor         |                                        |
| Driver Temperature                                  | Sensor         |                                        |
| Estimated Vehicle Range                             | Sensor         |                                        |
| Front Trunk                                         | Binary Sensor  | Open/closed status                     |
| Front Trunk Lock                                    | Binary Sensor  | Locked/unlocked status                 |
| Gear Guard                                          | Binary Sensor  | Locked/unlocked status                 |
| Gear Guard Alarm                                    | Binary Sensor  | Tampered/clear status                  |
| Gear Guard Video Mode                               | Sensor         |                                        |
| Gear Guard Video Status                             | Sensor         |                                        |
| Gear Guard Video Terms Accepted\*                   | Sensor         |                                        |
| Gear Selector                                       | Sensor         |                                        |
| Gear Tunnel Left                                    | Binary Sensor  | Open/closed status, R1T only           |
| Gear Tunnel Left Lock                               | Binary Sensor  | Locked/unlocked status, R1T only       |
| Gear Tunnel Right                                   | Binary Sensor  | Open/closed status, R1T only           |
| Gear Tunnel Right Lock                              | Binary Sensor  | Locked/unlocked status, R1T only       |
| Heated Seat 3rd Row Left                            | Binary Sensor  | Running/not running status, R1S only   |
| Heated Seat 3rd Row Right                           | Binary Sensor  | Running/not running status, R1S only   |
| Heated Seat Front Left                              | Binary Sensor  | Running/not running status             |
| Heated Seat Front Right                             | Binary Sensor  | Running/not running status             |
| Heated Seat Rear Left                               | Binary Sensor  | Running/not running status             |
| Heated Seat Rear Right                              | Binary Sensor  | Running/not running status             |
| Heated Steering Wheel                               | Binary Sensor  | Running/not running status             |
| In Use State                                        | Binary Sensor  | Moving/stopped status                  |
| Liftgate                                            | Binary Sensor  | Open/close status                      |
| Liftgate Lock                                       | Binary Sensor  | Locked/unlocked status                 |
| Liftgate Next Action                                | Sensor         |                                        |
| Limited Acceleration (Cold)                         | Sensor         |                                        |
| Limited Regenerative Braking (Cold)                 | Sensor         |                                        |
| Location                                            | Device Tracker |                                        |
| Locked State                                        | Binary Sensor  | Locked/unlocked status of all closures |
| Odometer                                            | Sensor         |                                        |
| Pet Mode                                            | Binary Sensor  | Running/not running status             |
| Pet Mode Temperature Status                         | Sensor         |                                        |
| Power State                                         | Sensor         |                                        |
| Range Threshold                                     | Sensor         |                                        |
| Remote Charging Available                           | Sensor         |                                        |
| Service Mode                                        | Sensor         |                                        |
| Software                                            | Update         |                                        |
| Software OTA - Available Version\*                  | Sensor         |                                        |
| Software OTA - Available Version Git Hash\*         | Sensor         |                                        |
| Software OTA - Available Version Number\*           | Sensor         |                                        |
| Software OTA - Available Version Week\*             | Sensor         |                                        |
| Software OTA - Available Version Year\*             | Sensor         |                                        |
| Software OTA - Current Version\*                    | Sensor         |                                        |
| Software OTA - Current Version Git Hash\*           | Sensor         |                                        |
| Software OTA - Current Version Number\*             | Sensor         |                                        |
| Software OTA - Current Version Week\*               | Sensor         |                                        |
| Software OTA - Current Version Year\*               | Sensor         |                                        |
| Software OTA - Download Progress                    | Sensor         |                                        |
| Software OTA - Install Duration                     | Sensor         |                                        |
| Software OTA - Install Progress\*                   | Sensor         |                                        |
| Software OTA - Install Ready                        | Sensor         |                                        |
| Software OTA - Install Time                         | Sensor         |                                        |
| Software OTA - Install Type                         | Sensor         |                                        |
| Software OTA - Status                               | Sensor         |                                        |
| Software OTA - Status Current                       | Sensor         |                                        |
| Speed                                               | Sensor         |                                        |
| Tailgate                                            | Binary Sensor  | Open/close status                      |
| Tailgate Lock                                       | Binary Sensor  | Locked/unlocked status                 |
| Tire Pressure Front Left                            | Sensor         |                                        |
| Tire Pressure Front Left Validity                   | Binary Sensor  | OK/problem status                      |
| Tire Pressure Front Right                           | Sensor         |                                        |
| Tire Pressure Front Right Validity                  | Binary Sensor  | OK/problem status                      |
| Tire Pressure Rear Left                             | Sensor         |                                        |
| Tire Pressure Rear Left Validity                    | Binary Sensor  | OK/problem status                      |
| Tire Pressure Rear Right                            | Sensor         |                                        |
| Tire Pressure Rear Right Validity                   | Binary Sensor  | OK/problem status                      |
| Tonneau                                             | Binary Sensor  | Open/close status, R1T only            |
| Tonneau Lock                                        | Binary Sensor  | Locked/unlocked status, R1T only       |
| Trailer Status                                      | Sensor         |                                        |
| Vented Seat Front Left                              | Binary Sensor  | Running/not running status             |
| Vented Seat Front Right                             | Binary Sensor  | Running/not running status             |
| Window Calibration Front Left State                 | Sensor         |                                        |
| Window Calibration Front Right State                | Sensor         |                                        |
| Window Calibration Rear Left State                  | Sensor         |                                        |
| Window Calibration Rear Right State                 | Sensor         |                                        |
| Window Front Left                                   | Binary Sensor  | Open/close status                      |
| Window Front Right                                  | Binary Sensor  | Open/close status                      |
| Window Rear Left                                    | Binary Sensor  | Open/close status                      |
| Window Rear Right                                   | Binary Sensor  | Open/close status                      |
| Windows Next Action                                 | Sensor         |                                        |
| Wiper Fluid Level                                   | Binary Sensor  | OK/problem status                      |

\* Disabled by default

### Remote Vehicle Controls

| Name                   | Domain  | Description                                                                                                                                               |
| ---------------------- | ------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Alarm                  | Switch  | Sound/mute alarm                                                                                                                                          |
| Cabin Climate          | Climate | Adjust cabin temperature/preset                                                                                                                           |
| Charge Limit           | Number  | Set battery charge limit percent                                                                                                                          |
| Charging Enabled       | Switch  | Enable/disable charging while vehicle plugged in and below battery state of charge limit                                                                  |
| Closures               | Lock    | Lock/unlock all closures                                                                                                                                  |
| Drop Tailgate          | Button  | Drop tailgate                                                                                                                                             |
| Front Trunk            | Cover   | Open/close front trunk                                                                                                                                    |
| Gear Guard Video       | Switch  | Enable/disable gear guard video                                                                                                                           |
| Liftgate               | Cover   | Open/close liftgate, R1S only                                                                                                                             |
| Open Gear Tunnel Left  | Button  | Open left gear tunnel                                                                                                                                     |
| Open Gear Tunnel Right | Button  | Open right gear tunnel                                                                                                                                    |
| Pair                   | Button  | Inititate Bluetooth pairing from Home Assistant. When pairing is identified as completed, this entity will no longer be created and can safely be deleted |
| Seat Front Left Heat   | Select  | Set front left seat heat level                                                                                                                            |
| Seat Front Left Vent   | Select  | Set front right seat vent level                                                                                                                           |
| Seat Front Right Heat  | Select  | Set front right seat heat level                                                                                                                           |
| Seat Front Right Vent  | Select  | Set front right seat vent level                                                                                                                           |
| Seat Rear Left Heat    | Select  | Set rear left seat heat level                                                                                                                             |
| Seat Rear Right Heat   | Select  | Set rear right seat heat level                                                                                                                            |
| Software               | Update  | Trigger OTA update, if available                                                                                                                          |
| Steering Wheel Heat    | Switch  | Turn on/off steering wheel heat                                                                                                                           |
| Tonneau                | Cover   | Open/close powered tonneau, R1T only                                                                                                                      |
| Wake                   | Button  | Wake vehicle                                                                                                                                              |
| Windows                | Cover   | Vent/close all windows                                                                                                                                    |

## Special Thanks

- [jrgutier](https://github.com/jrgutier) - Helped with getting information on the Rivian API
- [tmack8001](https://github.com/tmack8001) - Helping with Development and Testing
- [natekspencer](https://github.com/natekspencer) - Keeping the integration up-to-date with changes from Home Assistant

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
