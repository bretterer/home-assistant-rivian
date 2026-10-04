# Entities: sensors and controls

Besides the dashboard, the integration creates ordinary Home Assistant entities for each vehicle, usable in your own dashboards and automations. Disabled-by-default entities are marked with an asterisk.

## Sensors

| Name                                                | Domain         | Description                            |
| --------------------------------------------------- | -------------- | -------------------------------------- |
| 12V Battery Health                                  | Sensor         |                                        |
| Active driver                                       | Sensor         | The name of the active driver.         |
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
| Charging Schedule Days                              | Sensor         |                                        |
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
| Drive Status                                        | Sensor         | Active driving status (parked, driving) |
| Driver Temperature                                  | Sensor         |                                        |
| Efficiency (30 days)                                | Sensor         | Rolling 30-day average drive efficiency |
| Efficiency (all-time)                               | Sensor         | Lifetime average drive efficiency      |
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
| Last Drive Distance                                 | Sensor         | Distance traveled during last drive    |
| Last Drive Efficiency                               | Sensor         | Energy efficiency of last drive        |
| Last Drive MPGe                                     | Sensor         | MPGe equivalent of last drive          |
| Liftgate                                            | Binary Sensor  | Open/close status                      |
| Liftgate Lock                                       | Binary Sensor  | Locked/unlocked status                 |
| Liftgate Next Action                                | Sensor         |                                        |
| Limited Acceleration (Cold)                         | Sensor         |                                        |
| Limited Regenerative Braking (Cold)                 | Sensor         |                                        |
| Location                                            | Device Tracker |                                        |
| Locked State                                        | Binary Sensor  | Locked/unlocked status of all closures |
| MPGe (30 days)                                      | Sensor         | Rolling 30-day MPGe equivalent         |
| MPGe (all-time)                                     | Sensor         | Lifetime MPGe equivalent               |
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

## Remote vehicle controls

| Name                           | Domain  | Description                                                                                                                                               |
| ------------------------------ | ------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Alarm                          | Switch  | Sound/mute alarm                                                                                                                                          |
| Cabin Climate                  | Climate | Adjust cabin temperature/preset                                                                                                                           |
| Charge Limit                   | Number  | Set battery charge limit percent                                                                                                                          |
| Charging Enabled               | Switch  | Enable/disable charging while vehicle plugged in and below battery state of charge limit                                                                  |
| Charging Schedule Amperage     | Number  | Set charging schedule amperage limit                                                                                                                      |
| Charging Schedule Enabled      | Switch  | Enable/disable charging schedule                                                                                                                          |
| Charging Schedule End          | Time    | Set charging schedule end time                                                                                                                            |
| Charging Schedule Start        | Time    | Set charging schedule start time                                                                                                                          |
| Closures                       | Lock    | Lock/unlock all closures                                                                                                                                  |
| Drop Tailgate                  | Button  | Drop tailgate                                                                                                                                             |
| Front Trunk                    | Cover   | Open/close front trunk                                                                                                                                    |
| Gear Guard Video               | Switch  | Enable/disable gear guard video                                                                                                                           |
| Liftgate                       | Cover   | Open/close liftgate, R1S only                                                                                                                             |
| Open Gear Tunnel Left          | Button  | Open left gear tunnel                                                                                                                                     |
| Open Gear Tunnel Right         | Button  | Open right gear tunnel                                                                                                                                    |
| Pair                           | Button  | Inititate Bluetooth pairing from Home Assistant. When pairing is identified as completed, this entity will no longer be created and can safely be deleted |
| Seat Front Left Heat           | Select  | Set front left seat heat level                                                                                                                            |
| Seat Front Left Vent           | Select  | Set front right seat vent level                                                                                                                           |
| Seat Front Right Heat          | Select  | Set front right seat heat level                                                                                                                           |
| Seat Front Right Vent          | Select  | Set front right seat vent level                                                                                                                           |
| Seat Rear Left Heat            | Select  | Set rear left seat heat level                                                                                                                             |
| Seat Rear Right Heat           | Select  | Set rear right seat heat level                                                                                                                            |
| Software                       | Update  | Trigger OTA update, if available                                                                                                                          |
| Steering Wheel Heat            | Switch  | Turn on/off steering wheel heat                                                                                                                           |
| Tonneau                        | Cover   | Open/close powered tonneau, R1T only                                                                                                                      |
| Wake                           | Button  | Wake vehicle                                                                                                                                              |
| Windows                        | Cover   | Vent/close all windows                                                                                                                                    |

Controls need the setup described in [Vehicle control](vehicle-control.md).
