# Entities: sensors and controls

Besides the dashboard, the integration creates ordinary Home Assistant entities for each vehicle, usable in your own dashboards and automations. Disabled-by-default entities are marked with an asterisk.

## Sensors

Entities are available for all models (R1T, R1S and R2) unless noted otherwise.

| Name                                                | Domain         | Description                                                                                                                    |
| --------------------------------------------------- | -------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| 12V battery health                                  | Sensor         |                                                                                                                                |
| _Vehicle name_                                      | Device tracker | Vehicle location, named after the vehicle itself, with the GPS accuracy when known                                             |
| Active driver                                       | Sensor         | The name of the active driver.                                                                                                 |
| Altitude                                            | Sensor         | Its `accuracy` attribute is the estimated error, when known                                                                    |
| Amperage                                            | Sensor         | Wallbox current                                                                                                                |
| Amperage maximum                                    | Sensor         | Wallbox maximum current                                                                                                        |
| Battery capacity                                    | Sensor         |                                                                                                                                |
| Battery state of charge                             | Sensor         |                                                                                                                                |
| Battery state of charge limit                       | Sensor         |                                                                                                                                |
| Battery thermal runaway propagation                 | Sensor         |                                                                                                                                |
| Battery thermal status                              | Sensor         |                                                                                                                                |
| Bearing                                             | Sensor         | Its `accuracy` attribute is the estimated error, when known                                                                    |
| Bluetooth module failure status door front left\*   | Sensor         |                                                                                                                                |
| Bluetooth module failure status door front right\*  | Sensor         |                                                                                                                                |
| Bluetooth module failure status fascia front\*      | Sensor         |                                                                                                                                |
| Bluetooth module failure status fascia rear\*       | Sensor         |                                                                                                                                |
| Bluetooth module failure status instrument controls\* | Sensor         |                                                                                                                                |
| Bluetooth module failure status overhead console\*  | Sensor         |                                                                                                                                |
| Brake fluid level                                   | Sensor         |                                                                                                                                |
| Cabin auto ventilation\*                            | Binary sensor  | On/off status                                                                                                                  |
| Cabin climate preconditioning                       | Binary sensor  | Running/not running status                                                                                                     |
| Cabin climate preconditioning type                  | Sensor         |                                                                                                                                |
| Cabin temperature                                   | Sensor         |                                                                                                                                |
| Car wash mode                                       | Binary sensor  | On/off status                                                                                                                  |
| Cellular carrier\*                                  | Sensor         |                                                                                                                                |
| Cellular mode\*                                     | Sensor         |                                                                                                                                |
| Cellular signal bars\*                              | Sensor         |                                                                                                                                |
| Cellular signal strength\*                          | Sensor         |                                                                                                                                |
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
| Charging time remaining validity\*                  | Sensor         | Whether the charging time remaining is valid, or the battery is discharging                                                    |
| Closure state                                       | Binary sensor  | Open/closed status of all closures                                                                                             |
| Cold range notification                             | Sensor         | Whether cold weather is reducing range                                                                                         |
| Defrost/defog                                       | Binary sensor  | Running/not running status                                                                                                     |
| Departure schedule _name_                           | Switch         | One per schedule, to enable or disable it (see [Departure Schedules](#departure-schedules))                                    |
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
| Gear Guard streaming consent\*                      | Sensor         | Whether Gear Guard live streaming is consented to in the vehicle, or not set                                                   |
| Gear Guard streaming daily limit\*                  | Sensor         | Whether the daily Gear Guard live streaming limit is reached                                                                   |
| Gear Guard streaming limit reset\*                  | Sensor         | When the daily Gear Guard live streaming limit resets, while it's reached                                                      |
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
| Next stop distance                                  | Sensor         | Distance left to the next stop                                                                                                 |
| Next stop drive time                                | Sensor         | Driving time left to the next stop                                                                                             |
| Next stop ETA                                       | Sensor         | Estimated arrival at the next stop                                                                                             |
| No updates since                                    | Sensor         | When the vehicle last reported data; empty unless that was over 10 minutes ago                                                 |
| Odometer                                            | Sensor         |                                                                                                                                |
| Outside temperature                                 | Sensor         | Current temperature at the vehicle's location from Open-Meteo; only with **Outside temperature sensor and drive weather (from Open-Meteo)** on                |
| Parked energy last 24 hours                         | Sensor         | Energy used while parked in the last 24 hours                                                                                  |
| Parked energy last 8 hours\*                        | Sensor         | Energy used while parked in the last 8 hours                                                                                   |
| Parked energy latest session                        | Sensor         | Energy used while parked in the current or last park session                                                                   |
| Pet comfort                                         | Binary sensor  | Running/not running status                                                                                                     |
| Pet comfort temperature status                      | Sensor         |                                                                                                                                |
| Power                                               | Sensor         | Wallbox power                                                                                                                  |
| Power maximum                                       | Sensor         | Wallbox maximum power                                                                                                          |
| Power state                                         | Sensor         |                                                                                                                                |
| Range threshold                                     | Sensor         |                                                                                                                                |
| Rear hitch status                                   | Sensor         | Nothing, an accessory or a trailer on the hitch                                                                                |
| Remote charging available                           | Sensor         |                                                                                                                                |
| Saved location                                      | Sensor         | Home or work, as saved in the Rivian app, or elsewhere                                                                         |
| Service mode                                        | Sensor         |                                                                                                                                |
| Software                                            | Update         |                                                                                                                                |
| Software OTA - Download progress                    | Sensor         |                                                                                                                                |
| Software OTA - Install duration                     | Sensor         |                                                                                                                                |
| Software OTA - Install ready                        | Sensor         |                                                                                                                                |
| Software OTA - Install time                         | Sensor         |                                                                                                                                |
| Software OTA - Install type                         | Sensor         | R1T and R1S only                                                                                                               |
| Software OTA - Last install result                  | Sensor         |                                                                                                                                |
| Software OTA - Status                               | Sensor         |                                                                                                                                |
| Speed                                               | Sensor         | Its `accuracy` attribute is the estimated error, when known                                                                    |
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
| Wi-Fi connection status\*                           | Sensor         |                                                                                                                                |
| Wi-Fi frequency\*                                   | Sensor         |                                                                                                                                |
| Wi-Fi link speed\*                                  | Sensor         |                                                                                                                                |
| Wi-Fi network\*                                     | Sensor         | Network name (SSID)                                                                                                            |
| Wi-Fi security\*                                    | Sensor         |                                                                                                                                |
| Wi-Fi signal bars\*                                 | Sensor         |                                                                                                                                |
| Wi-Fi signal strength\*                             | Sensor         |                                                                                                                                |
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
| Climate hold               | Switch  | Start/end a climate hold, vehicles with climate hold only                                                                                                |
| Climate hold duration      | Number  | Set how long a climate hold lasts, 2 to 8 hours; doesn't need a phone key                                                                                |
| Closures                   | Lock    | Lock/unlock all closures                                                                                                                                 |
| Drop tailgate              | Button  | Drop tailgate, R1T only                                                                                                                                  |
| Front trunk                | Cover   | Open/close front trunk                                                                                                                                   |
| Gear Guard cameras         | Camera  | Live view of each Gear Guard camera, vehicles with Connect+ Gear Guard live cam only; one viewer at a time, about an hour a day                          |
| Gear Guard video           | Switch  | Enable/disable gear guard video                                                                                                                          |
| Liftgate                   | Cover   | Open/close liftgate, R1S and R2 only                                                                                                                     |
| Open gear tunnel left      | Button  | Open left gear tunnel, R1T only                                                                                                                          |
| Open gear tunnel right     | Button  | Open right gear tunnel, R1T only                                                                                                                         |
| Pair                       | Button  | Initiate Bluetooth pairing from Home Assistant. When pairing is identified as completed, this entity will no longer be created and can safely be deleted |
| Pet comfort                | Switch  | Turn pet comfort on/off, vehicles with pet comfort only                                                                                                  |
| Seat 3rd row left heat     | Select  | Set 3rd row left seat heat level, R1S only                                                                                                               |
| Seat 3rd row right heat    | Select  | Set 3rd row right seat heat level, R1S only                                                                                                              |
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

### Departure Schedules

Departure schedules precondition the cabin ahead of a departure time. They do not need vehicle control to be enabled or a paired phone.

Each schedule gets its own Departure schedule _name_ switch to enable or disable it. Its attributes show the schedule's settings, including the `schedule_id` the update and delete actions take. The remaining settings, including the departure time and days, are changed with the update action (Home Assistant has no multi-day selector entity, and a per-schedule time entity crowds the controls card). Schedules are created, changed and removed with actions:

| Action                             | Description                                                                |
| ---------------------------------- | -------------------------------------------------------------------------- |
| `rivian.create_departure_schedule` | Create a schedule                                                          |
| `rivian.update_departure_schedule` | Change a schedule, fields that are left out keep their current value       |
| `rivian.delete_departure_schedule` | Delete a schedule, which also stops preconditioning it has started         |

```yaml
action: rivian.create_departure_schedule
data:
  device_id: <your vehicle>
  name: Weekdays
  departure_time: "07:30:00"
  days: [Monday, Tuesday, Wednesday, Thursday, Friday]
  temperature: 21 # in your HA unit system; use e.g. 70 if that is °F
  front_defrost: Defrost
  front_left_seat: Heat2
  steering_wheel: Heat1
```

The departure time is local to the vehicle. The temperature is entered in your Home Assistant unit system — 16–29 °C, or about 61–84 °F — and converted to °C for Rivian. Seats take `Off`, `Heat1`, `Heat2` or `Heat3`, the front seats also take `Vent1`, `Vent2` or `Vent3`, and the steering wheel takes `Off` or `Heat1`. Front defrost takes `Off`, `Defrost` or `Defog`. Schedule names are limited to 24 characters.

### Precondition Cabin (keyless)

On vehicles without a paired phone, the **Precondition cabin** button starts cabin preconditioning now by creating a short-lived departure schedule, which the vehicle preconditions for right away. It is created only for those vehicles, since paired vehicles already have the full Cabin Climate entity.

The button departs in 20 minutes, at the driver's set temperature (within the 16–29 °C schedule limits). The vehicle may not precondition for a departure about 15 minutes or less away, which is why it's 20. For a different lead time or temperature, use the `rivian.precondition_cabin` action, which works for any vehicle:

```yaml
action: rivian.precondition_cabin
data:
  device_id: <your vehicle>
  lead_time: 30 # minutes, 20–60; optional, defaults to 20
  temperature: 72 # in your HA unit system; optional, defaults to the driver's set temperature
```

The temporary schedule is a departure schedule named `HA Precondition Now`, with its own Departure schedule switch showing its departure time and temperature. Pressing the button or running the action again before it departs changes that schedule rather than adding another. Home Assistant deletes it once it has departed, so it doesn't repeat weekly, and when the integration is removed. If it can't, such as when Rivian can't be reached, the log says to delete it in the Rivian app.

Preconditioning runs until the departure time. To stop it early, turn off the schedule's switch or delete the schedule.

The departure time is computed in Home Assistant's timezone and interpreted by Rivian in the vehicle's local timezone, which the API does not expose. This assumes the two match (the vehicle is near home); if they differ, the button may precondition at the wrong time. Home Assistant recognizes its temporary schedules by their name, which the departure schedule actions don't let you use. Since one always departs within the hour, any schedule named `HA Precondition Now` that doesn't, including one you move further out in the Rivian app, is treated as departed and deleted. Don't give your own schedules that name in the app.

### Navigation

These actions need only the signed-in account, not a paired phone key.

| Action                 | Description                                                                                          |
| ---------------------- | ---------------------------------------------------------------------------------------------------- |
| `rivian.send_location` | Send a `location` (`latitude`, `longitude`, by default Home Assistant's home) to a vehicle's navigation, which starts navigating there, or adds it as a stop if it's already navigating |
| `rivian.send_trip`     | Send `stops` (each a `name`, by default "Stop 1" and so on, and a `location`, by default Home Assistant's home) to a vehicle's navigation, which plans the route and charging stops, ending any active trip first (optional `arrival_range`) |
| `rivian.end_trip`      | End the trip a vehicle is navigating |

## Trip Route Card

The integration includes a dashboard card that shows a vehicle on a map. While it's navigating, the card shows the trip's route and the vehicle, with the stops along the way numbered in trip order and a flag on the destination. The next stop is larger, charging stops are in a different color, and hovering over or tapping a stop, or selecting it with the keyboard, shows its name and arrival time. Clicking a stop, tapping it again or pressing Enter on it opens the Next stop sensor for the next stop, and the Destination sensor for the others. Otherwise, it follows the vehicle's location.

The card is loaded automatically. Add it from the card picker ("Rivian trip") and pick the vehicle's Destination sensor in the visual editor, or use YAML:

```yaml
type: custom:rivian-trip-card
entity: sensor.<vehicle>_destination # the vehicle's Destination sensor
title: Trip # optional
vehicle: device_tracker.<vehicle>_location # optional, a different location to show
```

The vehicle's location and next stop are found automatically from the Destination sensor's vehicle. Set `vehicle` to show a different location instead.

