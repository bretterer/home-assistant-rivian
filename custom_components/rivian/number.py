"""Support for Rivian number entities."""

from __future__ import annotations

import logging
from typing import Any, Final

from rivian import VehicleCommand

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
    RestoreNumber,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfElectricCurrent, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ATTR_COORDINATOR,
    ATTR_VEHICLE,
    CHARGING_SCHEDULE_AMPERAGE_MAXIMUM,
    CHARGING_SCHEDULE_AMPERAGE_MINIMUM,
    CHARGING_SCHEDULE_AMPERAGE_STEP,
    DEFAULT_CHARGING_SCHEDULE_AMPERAGE,
    DEFAULT_PRECONDITION_LEAD_MINUTES,
    DEFAULT_PRECONDITION_TEMPERATURE,
    DEPARTURE_SCHEDULE_TEMPERATURE_MAXIMUM,
    DEPARTURE_SCHEDULE_TEMPERATURE_MINIMUM,
    DOMAIN,
    PRECONDITION_LEAD_MINUTES_MAXIMUM,
    PRECONDITION_LEAD_MINUTES_MINIMUM,
    PRECONDITION_LEAD_MINUTES_STEP,
)
from .coordinator import VehicleCoordinator
from .data_classes import RivianNumberEntityDescription
from .entity import RivianVehicleControlEntity, RivianVehicleEntity

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

CHARGING_SCHEDULE_AMPERAGE_NUMBER = RivianNumberEntityDescription(
    key="charging_schedule_amperage",
    translation_key="charging_schedule_amperage",
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
        if not vehicle.get("phone_identity_id"):
            entities.append(RivianPreconditionLeadTimeEntity(coord, entry, vehicle))
            entities.append(RivianPreconditionTemperatureEntity(coord, entry, vehicle))
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


class RivianPreconditionNumberEntity(RivianVehicleEntity, RestoreNumber):
    """Base for keyless preconditioning settings stored on the coordinator.

    The value lives on the coordinator so the button can read it, and is restored
    across restarts from the entity's own last state.
    """

    _attr_mode = NumberMode.BOX
    _coordinator_attr: str

    @property
    def available(self) -> bool:
        """Return availability."""
        return self._available

    @property
    def native_value(self) -> float:
        """Return the stored value."""
        return getattr(self.coordinator, self._coordinator_attr)

    async def async_added_to_hass(self) -> None:
        """Restore the last value into the coordinator."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_number_data()) and (
            last.native_value is not None
        ):
            setattr(self.coordinator, self._coordinator_attr, last.native_value)
            self.async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        """Store a new value on the coordinator."""
        setattr(self.coordinator, self._coordinator_attr, value)
        self.async_write_ha_state()


class RivianPreconditionLeadTimeEntity(RivianPreconditionNumberEntity):
    """How many minutes ahead keyless preconditioning departs (and roughly runs)."""

    _coordinator_attr = "precondition_lead_minutes"
    _attr_native_min_value = PRECONDITION_LEAD_MINUTES_MINIMUM
    _attr_native_max_value = PRECONDITION_LEAD_MINUTES_MAXIMUM
    _attr_native_step = PRECONDITION_LEAD_MINUTES_STEP
    _attr_native_unit_of_measurement = "min"

    def __init__(self, coordinator, config_entry, vehicle) -> None:
        """Initialize the entity."""
        description = NumberEntityDescription(
            key="precondition_lead_time", translation_key="precondition_lead_time"
        )
        super().__init__(coordinator, config_entry, description, vehicle)
        self.coordinator.precondition_lead_minutes = DEFAULT_PRECONDITION_LEAD_MINUTES


class RivianPreconditionTemperatureEntity(RivianPreconditionNumberEntity):
    """Target cabin temperature for keyless preconditioning.

    The value is stored and sent to Rivian in Celsius. ``device_class`` lets Home
    Assistant show and accept it in the user's unit system; the number's native value
    stays Celsius, which is what the coordinator reads.
    """

    _coordinator_attr = "precondition_temperature"
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_min_value = DEPARTURE_SCHEDULE_TEMPERATURE_MINIMUM
    _attr_native_max_value = DEPARTURE_SCHEDULE_TEMPERATURE_MAXIMUM
    _attr_native_step = 0.5
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator, config_entry, vehicle) -> None:
        """Initialize the entity."""
        description = NumberEntityDescription(
            key="precondition_temperature", translation_key="precondition_temperature"
        )
        super().__init__(coordinator, config_entry, description, vehicle)
        self.coordinator.precondition_temperature = DEFAULT_PRECONDITION_TEMPERATURE
