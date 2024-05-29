"""Support for Rivian switch entities."""

from __future__ import annotations

import logging
from typing import Any, Final

from rivian import VehicleCommand

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ATTR_COORDINATOR, ATTR_VEHICLE, DOMAIN
from .coordinator import VehicleCoordinator
from .data_classes import RivianSwitchEntityDescription
from .entity import RivianVehicleControlEntity

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
        available=lambda coor: coor.get("remoteChargingAvailable") == 1
        or coor.get("chargerState") == "charging_active",
        is_on=lambda coor: coor.get("chargerState")
        in ("charging_active", "charging_connecting"),
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


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the sensor entities"""
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    vehicles: dict[str, dict[str, Any]] = data[ATTR_VEHICLE]
    coordinators: dict[str, VehicleCoordinator] = data[ATTR_COORDINATOR][ATTR_VEHICLE]

    entities = [
        RivianSwitchEntity(coordinators[vehicle_id], entry, description, vehicle)
        for vehicle_id, vehicle in vehicles.items()
        if vehicle.get("phone_identity_id")
        for description in SWITCHES
    ]
    async_add_entities(entities)


class RivianSwitchEntity(RivianVehicleControlEntity, SwitchEntity):
    """Representation of a Rivian sensor entity."""

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
        return await self.entity_description.turn_off(self.coordinator)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the entity on."""
        await self.entity_description.turn_on(self.coordinator)
