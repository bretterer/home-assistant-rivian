# Entities: sensors and controls

Besides the dashboard, the integration creates ordinary Home Assistant entities for each vehicle, usable in your own dashboards and automations. Disabled-by-default entities are marked with an asterisk.

## Sensors

Entities are available for all models (R1T, R1S and R2) unless noted otherwise.

| Name                                                | Domain         | Description                                                                                                                    |
| --------------------------------------------------- | -------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| 12V battery health                                  | Sensor         |                                                                                                                                |
| _Vehicle name_                                      | Device tracker | Vehicle location, named after the vehicle itself                                                                               |
| Active driver                                       | Sensor         | The name of the active driver.                                                                                                 |
| Altitude                                            | Sensor         |                                                                                                                                |
| Amperage                                            | Sensor         | Wallbox current                                                                                                                |
| Amperage maximum                                    | Sensor         | Wallbox maximum current                                                                                                        |
| Battery capacity                                    | Sensor         |                                                                                                                                |
| Battery state of charge                             | Sensor         |                                                                                                                                |
| Battery state of charge limit                       | Sensor         |                                                                                                                                |
| Battery thermal runaway propagation                 | Sensor         |                                                                                                                                |
| Battery thermal status                              | Sensor         |                                                                                                                                |
| Bearing                                             | Sensor         |                                                                                                                                |
| Bluetooth module failure status door front left     | Sensor         |                                                                                                                                |
| Bluetooth module failure status door front right    | Sensor         |                                                                                                                                |
| Bluetooth module failure status fascia front        | Sensor         |                                                                                                                                |
| Bluetooth module failure status fascia rear         | Sensor         |                                                                                                                                |
| Bluetooth module failure status instrument controls | Sensor         |                                                                                                                                |
| Brake fluid level                                   | Sensor         |                                                                                                                                |
| Cabin climate preconditioning                       | Binary sensor  | Running/not running status                                                                                                     |
| Cabin climate preconditioning type                  | Sensor         |                                                                                                                                |
| Cabin temperature                                   | Sensor         |                                                                                                                                |
| Car wash mode                                       | Binary sensor  | On/off status                                                                                                                  |
| Charge port                                         | Binary sensor  | Open/closed status, R1T and R1S only                                                                                           |
| Charge port door next action                        | Sensor         | R1T and R1S only                                                                                                               |
| Charger connection                                  | Binary sensor  | Plugged in/unplugged status                                                                                                    |
| Charger derate status                               | Sensor         |                                                                                                                                |
| Charger state                                       | Sensor         | Detailed charging state: charging, scheduled, complete, waiting on charger, errors, etc.                                       |
| Charging cost                                       | Sensor         | Cost of the current or last charging session                                                                                   |
| Charging energy delivered                           | Sensor         | Energy delivered in the current or last charging session                                                                       |
| Charging fault                                      | Sensor         | Why charging is disabled or reduced, if it is                                                                                  |
| Charging range added                                | Sensor         | Range added in the current or last charging session                                                                            |
| Charging rate                                       | Sensor         | Range added per hour while charging                                                                                            |
| Charging schedule days                              | Sensor         |                                                                                                                                |
| Charging speed                                      | Sensor         | Charging power                                                                                                                 |
| Charging start time                                 | Sensor         | Start of the current or last charging session                                                                                  |
| Charging status                                     | Binary sensor  | Charging/not charging status                                                                                                   |
| Charging status                                     | Sensor         | Wallbox status: available, plugged in, charging, etc.                                                                          |
| Charging time elapsed                               | Sensor         | Time spent actively charging in the current or last session                                                                    |
| Charging time remaining                             | Sensor         |                                                                                                                                |
| Closure state                                       | Binary sensor  | Open/closed status of all closures                                                                                             |
| Cold range notification                             | Sensor         | Whether cold weather is reducing range                                                                                         |
| Defrost/defog                                       | Binary sensor  | Running/not running status                                                                                                     |
| Destination                                         | Sensor         | Final stop of the active navigation trip                                                                                       |
| Destination ETA                                     | Sensor         | Estimated arrival at the destination                                                                                           |
| Door front left                                     | Binary sensor  | Open/closed status                                                                                                             |
| Door front left lock                                | Binary sensor  | Locked/unlocked status                                                                                                         |
| Door front right                                    | Binary sensor  | Open/closed status                                                                                                             |
| Door front right lock                               | Binary sensor  | Locked/unlocked status                                                                                                         |
| Door rear left                                      | Binary sensor  | Open/closed status                                                                                                             |
| Door rear left lock                                 | Binary sensor  | Locked/unlocked status                                                                                                         |
| Door rear right                                     | Binary sensor  | Open/closed status                                                                                                             |
| Door rear right lock                                | Binary sensor  | Locked/unlocked status                                                                                                         |
| Door state                                          | Binary sensor  | Open/closed status of all doors                                                                                                |
| Drive mode                                          | Sensor         |                                                                                                                                |
| Drive status                                        | Sensor         | Whether the vehicle is parked or driving                                                                                       |
| Driver temperature                                  | Sensor         |                                                                                                                                |
| Drivers                                             | Sensor         | Number of drivers with access to the vehicle                                                                                   |
| Efficiency (30 days)                                | Sensor         | Rolling 30-day drive efficiency                                                                                                |
| Efficiency (all-time)                               | Sensor         | Lifetime drive efficiency                                                                                                      |
| Estimated vehicle range                             | Sensor         |                                                                                                                                |
| Front trunk                                         | Binary sensor  | Open/closed status                                                                                                             |
| Front trunk lock                                    | Binary sensor  | Locked/unlocked status                                                                                                         |
| Front trunk next action                             | Sensor         |                                                                                                                                |
| Gear Guard                                          | Binary sensor  | Locked/unlocked status, R1T and R1S only                                                                                       |
| Gear Guard alarm                                    | Binary sensor  | Tampered/clear status                                                                                                          |
| Gear Guard video mode                               | Sensor         |                                                                                                                                |
| Gear Guard video status                             | Sensor         |                                                                                                                                |
| Gear Guard video terms accepted\*                   | Sensor         |                                                                                                                                |
| Gear selector                                       | Sensor         |                                                                                                                                |
| Gear tunnel left                                    | Binary sensor  | Open/closed status, R1T only                                                                                                   |
| Gear tunnel left lock                               | Binary sensor  | Locked/unlocked status, R1T only                                                                                               |
| Gear tunnel left next action                        | Sensor         | R1T only                                                                                                                       |
| Gear tunnel right                                   | Binary sensor  | Open/closed status, R1T only                                                                                                   |
| Gear tunnel right lock                              | Binary sensor  | Locked/unlocked status, R1T only                                                                                               |
| Gear tunnel right next action                       | Sensor         | R1T only                                                                                                                       |
| Heated seat 3rd row left                            | Binary sensor  | Running/not running status, R1S only                                                                                           |
| Heated seat 3rd row right                           | Binary sensor  | Running/not running status, R1S only                                                                                           |
| Heated seat front left                              | Binary sensor  | Running/not running status                                                                                                     |
| Heated seat front right                             | Binary sensor  | Running/not running status                                                                                                     |
| Heated seat rear left                               | Binary sensor  | Running/not running status                                                                                                     |
| Heated seat rear right                              | Binary sensor  | Running/not running status                                                                                                     |
| Heated steering wheel                               | Binary sensor  | Running/not running status                                                                                                     |
| In use state                                        | Binary sensor  | Moving/stopped status                                                                                                          |
| Keys                                                | Sensor         | Number of phone keys across all drivers                                                                                        |
| Last drive distance                                 | Sensor         | Distance of the last drive                                                                                                     |
| Last drive efficiency                               | Sensor         | Energy efficiency of the last drive                                                                                            |
| Last drive MPGe                                     | Sensor         | MPGe equivalent of the last drive                                                                                              |
| Liftgate                                            | Binary sensor  | Open/close status, R1S and R2 only                                                                                             |
| Liftgate lock                                       | Binary sensor  | Locked/unlocked status, R1S and R2 only                                                                                        |
| Liftgate next action                                | Sensor         | R1S and R2 only                                                                                                                |
| Limited acceleration (cold)                         | Sensor         |                                                                                                                                |
| Limited regenerative braking (cold)                 | Sensor         |                                                                                                                                |
| Locked state                                        | Binary sensor  | Locked/unlocked status of all closures                                                                                         |
| MPGe (30 days)                                      | Sensor         | Rolling 30-day MPGe equivalent                                                                                                 |
| MPGe (all-time)                                     | Sensor         | Lifetime MPGe equivalent                                                                                                       |
| Next stop                                           | Sensor         | Next stop of the active navigation trip, including charging stops, past the one the vehicle is at (its `arrived_at` attribute) |
| Next stop ETA                                       | Sensor         | Estimated arrival at the next stop                                                                                             |
| No updates since                                    | Sensor         | When the vehicle last reported data; empty unless that was over 10 minutes ago                                                 |
| Odometer                                            | Sensor         |                                                                                                                                |
| Pet mode                                            | Binary sensor  | Running/not running status                                                                                                     |
| Pet mode temperature status                         | Sensor         |                                                                                                                                |
| Power                                               | Sensor         | Wallbox power                                                                                                                  |
| Power maximum                                       | Sensor         | Wallbox maximum power                                                                                                          |
| Power state                                         | Sensor         |                                                                                                                                |
| Range threshold                                     | Sensor         |                                                                                                                                |
| Rear hitch status                                   | Sensor         | Nothing, an accessory or a trailer on the hitch                                                                                |
| Remote charging available                           | Sensor         |                                                                                                                                |
| Service mode                                        | Sensor         |                                                                                                                                |
| Software                                            | Update         |                                                                                                                                |
| Software OTA - Download progress                    | Sensor         |                                                                                                                                |
| Software OTA - Install duration                     | Sensor         |                                                                                                                                |
| Software OTA - Install ready                        | Sensor         |                                                                                                                                |
| Software OTA - Install time                         | Sensor         |                                                                                                                                |
| Software OTA - Install type                         | Sensor         | R1T and R1S only                                                                                                               |
| Software OTA - Last install result                  | Sensor         |                                                                                                                                |
| Software OTA - Status                               | Sensor         |                                                                                                                                |
| Speed                                               | Sensor         |                                                                                                                                |
| Tailgate                                            | Binary sensor  | Open/close status, R1T only                                                                                                    |
| Tailgate lock                                       | Binary sensor  | Locked/unlocked status, R1T and R1S only                                                                                       |
| Tailgate next action                                | Sensor         | R1T only                                                                                                                       |
| Tire pressure front left                            | Sensor         |                                                                                                                                |
| Tire pressure front left status                     | Sensor         | OK/warning status                                                                                                              |
| Tire pressure front left validity                   | Binary sensor  | OK/problem status                                                                                                              |
| Tire pressure front right                           | Sensor         |                                                                                                                                |
| Tire pressure front right status                    | Sensor         | OK/warning status                                                                                                              |
| Tire pressure front right validity                  | Binary sensor  | OK/problem status                                                                                                              |
| Tire pressure rear left                             | Sensor         |                                                                                                                                |
| Tire pressure rear left status                      | Sensor         | OK/warning status                                                                                                              |
| Tire pressure rear left validity                    | Binary sensor  | OK/problem status                                                                                                              |
| Tire pressure rear right                            | Sensor         |                                                                                                                                |
| Tire pressure rear right status                     | Sensor         | OK/warning status                                                                                                              |
| Tire pressure rear right validity                   | Binary sensor  | OK/problem status                                                                                                              |
| Tonneau                                             | Binary sensor  | Open/close status, R1T with a powered tonneau only                                                                             |
| Tonneau lock                                        | Binary sensor  | Locked/unlocked status, R1T with a powered tonneau only                                                                        |
| Trailer status                                      | Sensor         |                                                                                                                                |
| Vehicle updates                                     | Binary sensor  | OK/problem status: a problem when nothing has arrived from Rivian for 10 minutes                                               |
| Vented seat front left                              | Binary sensor  | Running/not running status                                                                                                     |
| Vented seat front right                             | Binary sensor  | Running/not running status                                                                                                     |
| Voltage                                             | Sensor         | Wallbox voltage                                                                                                                |
| Voltage maximum                                     | Sensor         | Wallbox maximum voltage                                                                                                        |
| Window calibration front left state                 | Sensor         |                                                                                                                                |
| Window calibration front right state                | Sensor         |                                                                                                                                |
| Window calibration liftgate state                   | Sensor         | R2 only                                                                                                                        |
| Window calibration rear left state                  | Sensor         |                                                                                                                                |
| Window calibration rear right state                 | Sensor         |                                                                                                                                |
| Window front left                                   | Binary sensor  | Open/close status                                                                                                              |
| Window front right                                  | Binary sensor  | Open/close status                                                                                                              |
| Window liftgate                                     | Binary sensor  | Open/closed status, R2 only                                                                                                    |
| Window rear left                                    | Binary sensor  | Open/close status                                                                                                              |
| Window rear right                                   | Binary sensor  | Open/close status                                                                                                              |
| Windows next action                                 | Sensor         |                                                                                                                                |
| Wiper fluid level                                   | Binary sensor  | OK/problem status                                                                                                              |

\* Disabled by default

## Remote vehicle controls

These controls need a phone key paired over Bluetooth (see [Vehicle control](vehicle-control.md)), which isn't possible yet for Gen2 R1T and R1S vehicles or the R2. Otherwise, they're available for all models unless noted, and some also depend on the features your vehicle reports.

| Name                       | Domain  | Description                                                                                                                                              |
| -------------------------- | ------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Alarm                      | Switch  | Sound/mute alarm                                                                                                                                         |
| Cabin climate              | Climate | Adjust cabin temperature/preset                                                                                                                          |
| Charge limit               | Number  | Set battery charge limit percent                                                                                                                         |
| Charge port door           | Cover   | Open/close the powered charge port door, R1T and R1S only                                                                                                |
| Charging enabled           | Switch  | Enable/disable charging while vehicle plugged in and below battery state of charge limit                                                                 |
| Charging schedule amperage | Number  | Set charging schedule amperage limit                                                                                                                     |
| Charging schedule enabled  | Switch  | Enable/disable charging schedule                                                                                                                         |
| Charging schedule end      | Time    | Set charging schedule end time                                                                                                                           |
| Charging schedule start    | Time    | Set charging schedule start time                                                                                                                         |
| Closures                   | Lock    | Lock/unlock all closures                                                                                                                                 |
| Drop tailgate              | Button  | Drop tailgate, R1T only                                                                                                                                  |
| Front trunk                | Cover   | Open/close front trunk                                                                                                                                   |
| Gear Guard video           | Switch  | Enable/disable gear guard video                                                                                                                          |
| Liftgate                   | Cover   | Open/close liftgate, R1S and R2 only                                                                                                                     |
| Open gear tunnel left      | Button  | Open left gear tunnel, R1T only                                                                                                                          |
| Open gear tunnel right     | Button  | Open right gear tunnel, R1T only                                                                                                                         |
| Pair                       | Button  | Initiate Bluetooth pairing from Home Assistant. When pairing is identified as completed, this entity will no longer be created and can safely be deleted |
| Seat front left heat       | Select  | Set front left seat heat level                                                                                                                           |
| Seat front left vent       | Select  | Set front left seat vent level                                                                                                                           |
| Seat front right heat      | Select  | Set front right seat heat level                                                                                                                          |
| Seat front right vent      | Select  | Set front right seat vent level                                                                                                                          |
| Seat rear left heat        | Select  | Set rear left seat heat level                                                                                                                            |
| Seat rear right heat       | Select  | Set rear right seat heat level                                                                                                                           |
| Software                   | Update  | Trigger OTA update, if available                                                                                                                         |
| Steering wheel heat        | Switch  | Turn on/off steering wheel heat                                                                                                                          |
| Tonneau                    | Cover   | Open/close tonneau, R1T with a powered tonneau only                                                                                                      |
| Wake                       | Button  | Wake vehicle                                                                                                                                             |
| Windows                    | Cover   | Vent/close all windows                                                                                                                                   |
