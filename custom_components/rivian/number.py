"""Support for Rivian number entities."""

from __future__ import annotations

import logging
from typing import Any, Final

from rivian import VehicleCommand

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfElectricCurrent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ATTR_COORDINATOR, ATTR_VEHICLE, DOMAIN
from .coordinator import VehicleCoordinator
from .data_classes import RivianNumberEntityDescription
from .entity import RivianVehicleControlEntity

_LOGGER = logging.getLogger(__name__)


NUMBERS: Final[tuple[RivianNumberEntityDescription, ...]] = (
    RivianNumberEntityDescription(
        key="charge_limit",
        device_class=NumberDeviceClass.BATTERY,
        icon="mdi:battery-charging-70",
        name="Charge Limit",
        native_min_value=50,
        native_unit_of_measurement=PERCENTAGE,
        field="batteryLimit",
        set_fn=lambda coordinator, value: coordinator.send_vehicle_command(
            command=VehicleCommand.CHARGING_LIMITS, params={"SOC_limit": int(value)}
        ),
    ),
)

CHARGE_CURRENT_LIMIT_DESCRIPTION = NumberEntityDescription(
    key="charge_current_limit",
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the number entities."""
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    vehicles: dict[str, dict[str, Any]] = data[ATTR_VEHICLE]
    coordinators: dict[str, VehicleCoordinator] = data[ATTR_COORDINATOR][ATTR_VEHICLE]

    entities: list[NumberEntity] = []
    for vehicle_id, vehicle in vehicles.items():
        if not vehicle.get("phone_identity_id"):
            continue
        for description in NUMBERS:
            entities.append(
                RivianNumberEntity(
                    coordinators[vehicle_id], entry, description, vehicle
                )
            )
        entities.append(
            RivianChargingScheduleNumberEntity(
                coordinators[vehicle_id], entry, vehicle
            )
        )
    async_add_entities(entities)


class RivianNumberEntity(RivianVehicleControlEntity, NumberEntity):
    """Representation of a Rivian number entity."""

    entity_description: RivianNumberEntityDescription

    @property
    def native_value(self) -> float | None:
        """Return the value reported by the number."""
        return self._get_value(self.entity_description.field)

    async def async_set_native_value(self, value: float) -> None:
        """Set new value."""
        await self.entity_description.set_fn(self.coordinator, value)


_ALL_WEEKDAYS = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]


class RivianChargingScheduleNumberEntity(RivianVehicleControlEntity, NumberEntity):
    """Rivian charge current limit number entity backed by charging schedules."""

    _attr_device_class = NumberDeviceClass.CURRENT
    _attr_native_max_value = 48
    _attr_native_min_value = 8
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE

    entity_description: NumberEntityDescription

    def __init__(
        self,
        coordinator: VehicleCoordinator,
        config_entry: ConfigEntry,
        vehicle: dict[str, Any],
    ) -> None:
        """Initialize the entity."""
        super().__init__(
            coordinator, config_entry, CHARGE_CURRENT_LIMIT_DESCRIPTION, vehicle
        )

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.charging_schedule_coordinator.async_add_listener(
                self._handle_coordinator_update
            )
        )

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        if not super().available:
            return False
        schedules = self.coordinator.charging_schedule_coordinator.data
        if schedules:
            return True
        return self._get_value("gnssLocation") is not None

    @property
    def native_value(self) -> int | None:
        """Return the current charge current limit."""
        schedules = self.coordinator.charging_schedule_coordinator.data
        if schedules:
            return schedules[0].get("amperage")
        return None

    async def async_set_native_value(self, value: float) -> None:
        """Set the charge current limit."""
        schedule_coordinator = self.coordinator.charging_schedule_coordinator
        schedules = schedule_coordinator.data or []

        if schedules:
            schedule = dict(schedules[0])
            schedule["amperage"] = int(value)
        else:
            location = self._get_value("gnssLocation")
            schedule = {
                "weekDays": _ALL_WEEKDAYS,
                "startTime": 0,
                "duration": 1440,
                "location": {
                    "latitude": location["latitude"],
                    "longitude": location["longitude"],
                },
                "amperage": int(value),
                "enabled": True,
            }

        await self.coordinator.api.set_charging_schedules(
            self.coordinator.vehicle_id, [schedule]
        )
        await schedule_coordinator.async_request_refresh()
