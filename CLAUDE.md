# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is an unofficial Home Assistant custom integration for Rivian vehicles. It uses the `rivian-python-client` library to communicate with Rivian's cloud API via GraphQL subscriptions and REST endpoints.

## Development Commands

### Setup (in devcontainer or local)
```bash
pip install -r requirements.txt
pre-commit install
```

### Linting
```bash
ruff check custom_components/
ruff format custom_components/
```

### Running Home Assistant for Testing
Use VS Code devcontainer (recommended) - opens at port 8123. The devcontainer automatically:
- Installs dependencies via `.devcontainer/setup`
- Creates `config/` directory for HA configuration
- Configures debug logging for `custom_components.rivian`

## Architecture

### Core Components

**Entry Point (`__init__.py`):**
- Sets up coordinators and forwards platform setup
- Creates `UserCoordinator` first to get vehicle list
- Creates a `VehicleCoordinator` per vehicle
- Creates a single `WallboxCoordinator` for all wallboxes
- Stores everything in `hass.data[DOMAIN][entry_id]`

**Data Coordinators (`coordinator.py`):**
- `RivianDataUpdateCoordinator` - Abstract base with error handling and exponential backoff
- `UserCoordinator` - Fetches user info and vehicle list
- `VehicleCoordinator` - Subscribes to real-time vehicle state updates via WebSocket
- `ChargingCoordinator` - Polls charging session data (adjusts interval based on plugged-in status)
- `DriverKeyCoordinator` - Fetches driver/key enrollment data
- `WallboxCoordinator` - Polls wallbox data
- `VehicleImageCoordinator` - Fetches vehicle images on-demand

**Entity Base Classes (`entity.py`):**
- `RivianEntity` - Generic base
- `RivianVehicleEntity` - For vehicle sensors/binary sensors
- `RivianVehicleControlEntity` - For commands (checks gear=park, zone restrictions, pairing status)
- `RivianChargingEntity` - For charging session data
- `RivianWallboxEntity` - For wallbox data

**Entity Descriptions (`data_classes.py`):**
Custom dataclasses extending HA entity descriptions with Rivian-specific fields like `field` (API field name), `value_lambda`, `on_value`, etc.

**Sensor Definitions (`const.py`):**
Dictionaries `SENSORS` and `BINARY_SENSORS` keyed by vehicle type ("R1", "R1T", "R1S") containing entity descriptions.

### Data Flow

1. Vehicle state comes via WebSocket subscription (not polling)
2. `VehicleCoordinator._process_new_data()` receives updates and merges with existing data
3. Invalid sensor values are filtered out using `INVALID_SENSOR_STATES`
4. Entities read values via `coordinator.get(field_name)`

### Vehicle Control

Remote commands require:
- 2FA enabled on Rivian account
- Bluetooth pairing (one-time, in-vehicle)
- Vehicle in "Park" gear
- Optionally restricted by Home Assistant zones

Commands flow through `VehicleCoordinator.send_vehicle_command()` which:
- Wakes vehicle if sleeping
- Uses enrolled phone credentials from config entry options

### Platform Files

Each platform (sensor, binary_sensor, button, climate, cover, lock, number, select, switch, update, device_tracker, image) has its own module that:
1. Defines entity descriptions specific to that platform
2. Implements `async_setup_entry()` to create entities
3. Extends the appropriate base entity class

## Key Dependencies

- `rivian-python-client[ble]==2.0.0` - Core API client (includes BLE support for pairing)
- `homeassistant>=2025.1.0` - Home Assistant core
