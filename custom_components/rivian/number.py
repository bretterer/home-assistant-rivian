"""Support for Rivian number entities."""

from __future__ import annotations

import logging
from typing import Any, Final

from rivian import VehicleCommand

from homeassistant.components.number import NumberDeviceClass, NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfElectricCurrent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ATTR_COORDINATOR,
    ATTR_VEHICLE,
    CHARGING_SCHEDULE_AMPERAGE_MAXIMUM,
    CHARGING_SCHEDULE_AMPERAGE_MINIMUM,
    CHARGING_SCHEDULE_AMPERAGE_STEP,
    DEFAULT_CHARGING_SCHEDULE_AMPERAGE,
    DOMAIN,
)
from .coordinator import VehicleCoordinator
from .data_classes import RivianNumberEntityDescription
from .entity import RivianVehicleControlEntity, RivianVehicleEntity

_LOGGER = logging.getLogger(__name__)


NUMBERS: Final[tuple[RivianNumberEntityDescription, ...]] = (
    RivianNumberEntityDescription(
        key="charge_limit",
        device_class=NumberDeviceClass.BATTERY,
        translation_key="charge_limit",
        native_min_value=50,
        native_unit_of_measurement=PERCENTAGE,
        field="batteryLimit",
        set_fn=lambda coordinator, value: coordinator.send_vehicle_command(
            command=VehicleCommand.CHARGING_LIMITS, params={"SOC_limit": int(value)}
        ),
    ),
)

CHARGING_SCHEDULE_AMPERAGE_NUMBER = RivianNumberEntityDescription(
    key="charging_schedule_amperage",
    translation_key="charging_schedule_amperage",
    entity_category=EntityCategory.CONFIG,
    device_class=NumberDeviceClass.CURRENT,
    native_min_value=CHARGING_SCHEDULE_AMPERAGE_MINIMUM,
    native_max_value=CHARGING_SCHEDULE_AMPERAGE_MAXIMUM,
    native_step=CHARGING_SCHEDULE_AMPERAGE_STEP,
    native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
    field="charging_schedule_amperage",
    set_fn=lambda c, v: c.update_charging_schedule_data({"amperage": int(v)}),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the number entities."""
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    vehicles: dict[str, dict[str, Any]] = data[ATTR_VEHICLE]
    coordinators: dict[str, VehicleCoordinator] = data[ATTR_COORDINATOR][ATTR_VEHICLE]

    entities = [
        RivianNumberEntity(coordinators[vehicle_id], entry, description, vehicle)
        for vehicle_id, vehicle in vehicles.items()
        if vehicle.get("phone_identity_id")
        for description in NUMBERS
    ]
    for vehicle_id, vehicle in vehicles.items():
        coord = coordinators[vehicle_id]
        entities.append(
            RivianChargingScheduleAmperageEntity(
                coord, entry, CHARGING_SCHEDULE_AMPERAGE_NUMBER, vehicle
            )
        )
    async_add_entities(entities)


class RivianChargingScheduleAmperageEntity(RivianVehicleEntity, NumberEntity):
    """Charging Schedule Amperage Entity."""

    entity_description: RivianNumberEntityDescription

    @property
    def available(self) -> bool:
        """Return availability."""
        return self._available

    @property
    def native_value(self) -> int | None:
        """Return native value."""
        sched = self.coordinator.charging_schedule
        val = sched.get("amperage", DEFAULT_CHARGING_SCHEDULE_AMPERAGE)
        return int(val) if val is not None else None

    async def async_set_native_value(self, value: float) -> None:
        """Set new value."""
        await self.entity_description.set_fn(self.coordinator, value)


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
