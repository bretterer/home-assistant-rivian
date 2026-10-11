"""Support for Rivian switch entities."""

from __future__ import annotations

import logging
from typing import Any, Final

from rivian import VehicleCommand

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ATTR_COORDINATOR, ATTR_VEHICLE, DOMAIN
from .coordinator import VehicleCoordinator
from .data_classes import RivianSwitchEntityDescription
from .entity import RivianVehicleControlEntity, RivianVehicleEntity
from .helpers import departure_schedule_summary, track_departure_schedule_entities

_LOGGER = logging.getLogger(__name__)


SWITCHES: Final[tuple[RivianSwitchEntityDescription, ...]] = (
    RivianSwitchEntityDescription(
        key="alarm",
        icon="mdi:alarm-light",
        name="Alarm",
        is_on=lambda coor: coor.get("alarmSoundStatus") == "true",
        turn_off=lambda coor: coor.send_vehicle_command(
            command=VehicleCommand.PANIC_OFF
        ),
        turn_on=lambda coor: coor.send_vehicle_command(command=VehicleCommand.PANIC_ON),
    ),
    RivianSwitchEntityDescription(
        key="charging_enabled",
        icon="mdi:lightning-bolt",
        name="Charging Enabled",
        available=lambda coor: (
            coor.get("remoteChargingAvailable") == 1
            or coor.get("chargerState") == "charging_active"
        ),
        is_on=lambda coor: (
            coor.get("chargerState") in ("charging_active", "charging_connecting")
        ),
        turn_off=lambda coor: coor.send_vehicle_command(
            command=VehicleCommand.STOP_CHARGING
        ),
        turn_on=lambda coor: coor.send_vehicle_command(
            command=VehicleCommand.START_CHARGING
        ),
    ),
    RivianSwitchEntityDescription(
        key="gear_guard_video",
        icon="mdi:cctv",
        name="Gear Guard Video",
        is_on=lambda coor: coor.get("gearGuardVideoStatus") != "Disabled",
        turn_off=lambda coor: coor.send_vehicle_command(
            command=VehicleCommand.DISABLE_GEAR_GUARD_VIDEO
        ),
        turn_on=lambda coor: coor.send_vehicle_command(
            command=VehicleCommand.ENABLE_GEAR_GUARD_VIDEO
        ),
    ),
    RivianSwitchEntityDescription(
        key="steering_wheel_heat",
        icon="mdi:steering",
        name="Steering Wheel Heat",
        is_on=lambda coor: coor.get("steeringWheelHeat") != "Off",
        turn_off=lambda coor: coor.send_vehicle_command(
            command=VehicleCommand.CABIN_HVAC_STEERING_HEAT, params={"level": 0}
        ),
        turn_on=lambda coor: coor.send_vehicle_command(
            command=VehicleCommand.CABIN_HVAC_STEERING_HEAT, params={"level": 1}
        ),
    ),
)

CHARGING_SCHEDULE_ENABLED_SWITCH = RivianSwitchEntityDescription(
    key="charging_schedule_enabled",
    translation_key="charging_schedule_enabled",
    is_on=lambda c: c.charging_schedule.get("enabled", True),
    turn_off=lambda c: c.update_charging_schedule_data({"enabled": False}),
    turn_on=lambda c: c.update_charging_schedule_data({"enabled": True}),
)

DEPARTURE_SCHEDULE_KEY: Final = "departure_schedule"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the switch entities."""
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    vehicles: dict[str, dict[str, Any]] = data[ATTR_VEHICLE]
    coordinators: dict[str, VehicleCoordinator] = data[ATTR_COORDINATOR][ATTR_VEHICLE]

    entities = [
        RivianSwitchEntity(coordinators[vehicle_id], entry, description, vehicle)
        for vehicle_id, vehicle in vehicles.items()
        if vehicle.get("phone_identity_id")
        for description in SWITCHES
    ]
    for vehicle_id, vehicle in vehicles.items():
        coord = coordinators[vehicle_id]
        entities.append(
            RivianChargingScheduleEnabledEntity(
                coord, entry, CHARGING_SCHEDULE_ENABLED_SWITCH, vehicle
            )
        )
    async_add_entities(entities)

    for vehicle_id, vehicle in vehicles.items():
        coordinator = coordinators[vehicle_id]
        track_departure_schedule_entities(
            hass,
            entry,
            coordinator,
            vehicle["vin"],
            Platform.SWITCH,
            DEPARTURE_SCHEDULE_KEY,
            lambda sid, c=coordinator, v=vehicle: RivianDepartureScheduleSwitchEntity(
                c, entry, v, sid
            ),
            async_add_entities,
        )


class RivianDepartureScheduleSwitchEntity(RivianVehicleEntity, SwitchEntity):
    """Departure Schedule Enabled Entity."""

    def __init__(
        self,
        coordinator: VehicleCoordinator,
        config_entry: ConfigEntry,
        vehicle: dict[str, Any],
        schedule_id: str,
    ) -> None:
        """Construct a departure schedule switch entity."""
        description = SwitchEntityDescription(
            key=f"{DEPARTURE_SCHEDULE_KEY}_{schedule_id}",
            translation_key=DEPARTURE_SCHEDULE_KEY,
        )
        super().__init__(coordinator, config_entry, description, vehicle)
        self._schedule_id = schedule_id
        self._attr_name = self._get_name()

    @property
    def _schedule(self) -> dict[str, Any]:
        """Return the departure schedule or empty dict."""
        return self.coordinator.get_departure_schedule(self._schedule_id) or {}

    def _get_name(self) -> str:
        """Return the name of the entity."""
        return f"Departure schedule {self._schedule.get('name', '')}".strip()

    @property
    def available(self) -> bool:
        """Return availability."""
        return self._available and bool(self._schedule)

    @property
    def is_on(self) -> bool:
        """Return True if entity is on."""
        return bool(self._schedule.get("isEnabled"))

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the departure schedule."""
        if not (schedule := self._schedule):
            return None
        return departure_schedule_summary(schedule)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self._schedule:
            # the name is cached, setting it again picks up a renamed schedule
            self._attr_name = self._get_name()
        super()._handle_coordinator_update()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the entity on."""
        await self.coordinator.update_departure_schedule(
            self._schedule_id, {"isEnabled": True}
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the entity off."""
        await self.coordinator.update_departure_schedule(
            self._schedule_id, {"isEnabled": False}
        )


class RivianChargingScheduleEnabledEntity(RivianVehicleEntity, SwitchEntity):
    """Charging Schedule Enabled Entity."""

    entity_description: RivianSwitchEntityDescription

    @property
    def available(self) -> bool:
        """Return availability."""
        return self._available

    @property
    def is_on(self) -> bool:
        """Return True if entity is on."""
        return self.entity_description.is_on(self.coordinator)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the entity on."""
        await self.entity_description.turn_on(self.coordinator)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the entity off."""
        await self.entity_description.turn_off(self.coordinator)


class RivianSwitchEntity(RivianVehicleControlEntity, SwitchEntity):
    """Representation of a Rivian switch entity."""

    entity_description: RivianSwitchEntityDescription

    @property
    def is_on(self) -> bool:
        """Return True if entity is on."""
        return self.entity_description.is_on(self.coordinator)

    @property
    def available(self) -> bool:
        """Return the availability of the entity."""
        return super().available and (
            _fn(self.coordinator)
            if (_fn := self.entity_description.available)
            else True
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the entity off."""
        await self.entity_description.turn_off(self.coordinator)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the entity on."""
        await self.entity_description.turn_on(self.coordinator)
