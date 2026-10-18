# Vehicle control

In addition to viewing data, you can send some commands from Home Assistant, as in the Rivian phone app (lock/unlock, climate, charging limits, seat heat, closures and more; see the controls table in [Entities](entities.md)).

**Gen2 (2025+) vehicles are not supported for remote control.** Their Bluetooth hardware changed and cannot take part in the secure pairing step yet. Data and analytics work normally.

## Requirements

- A Home Assistant host with a Bluetooth adapter (version 4.2 or newer), or an [ESP32 Bluetooth proxy](https://esphome.io/projects/?type=bluetooth). It must be within reach of the vehicle during the one-time pairing. Afterwards commands travel through the Rivian cloud and Bluetooth is no longer needed.
- Two-factor authentication enabled on your Rivian account.
- A free phone-key slot on the vehicle (the limit is 4).

## Pairing

1. In the integration's options, enable vehicle control for the vehicle (optionally restrict it to Home Assistant zones).
2. In the vehicle, open **Settings > Drivers and Keys** and choose **Set Up** for the phone key named after your Home Assistant home location (typically "Home").
3. In Home Assistant press the **Pair** button entity for that vehicle. On macOS a pop-up asks you to allow pairing; on Linux and Windows it happens automatically.

Once pairing completes the Pair button disappears and can be deleted. If pairing is unreliable, an ESP32 Bluetooth proxy usually fixes it.

Commands only run while the vehicle is in Park, and respect the optional zone restriction. A sleeping vehicle is woken first.
