"""Support for Rivian time entities."""

from __future__ import annotations

import datetime
import logging
from typing import Any

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo

from .const import ATTR_COORDINATOR, ATTR_VEHICLE, DOMAIN
from .coordinator import VehicleCoordinator, ChargingScheduleCoordinator
from .entity import RivianEntity

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the time entities."""
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    vehicles: dict[str, dict[str, Any]] = data[ATTR_VEHICLE]
    coordinators: dict[str, VehicleCoordinator] = data[ATTR_COORDINATOR][ATTR_VEHICLE]

    entities = []
    for vehicle_id, vehicle in vehicles.items():
        coor = coordinators[vehicle_id]
        if hasattr(coor, "charging_schedule_coordinator"):
            entities.append(RivianChargingScheduleStartTime(coor.charging_schedule_coordinator, vehicle))
            entities.append(RivianChargingScheduleEndTime(coor.charging_schedule_coordinator, vehicle))

    async_add_entities(entities)


class RivianChargingScheduleEntity(RivianEntity[ChargingScheduleCoordinator]):
    """Base class for Rivian Charging Schedule entities."""

    def __init__(
        self,
        coordinator: ChargingScheduleCoordinator,
        vehicle: dict[str, Any],
    ) -> None:
        """Construct a Rivian vehicle entity."""
        super().__init__(coordinator)
        self._vin = (vin := vehicle["vin"])
        
        name = vehicle["name"]
        model = vehicle["model"]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, vin), (DOMAIN, vehicle["id"])},
            name=name if name else model,
            manufacturer="Rivian",
            model=model,
            serial_number=vin,
        )

    def _get_schedule(self) -> dict[str, Any]:
        """Get the current schedule or default."""
        schedules = self.coordinator.data.get("chargingSchedules", [])
        if not schedules:
            return {
                "startTime": 0,
                "duration": 1440,
                "location": {},
                "amperage": 48,
                "enabled": False,
                "weekDays": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
            }
        return schedules[0].copy()

class RivianChargingScheduleStartTime(RivianChargingScheduleEntity, TimeEntity):
    """Representation of a Rivian charging schedule start time."""

    def __init__(self, coordinator, vehicle):
        super().__init__(coordinator, vehicle)
        self._attr_name = "Charging Schedule Start Time"
        self._attr_unique_id = f"{self._vin}-charging_schedule_start"
        self._attr_icon = "mdi:clock-start"

    @property
    def native_value(self) -> datetime.time | None:
        """Return the value of the entity."""
        schedule = self._get_schedule()
        start_mins = schedule.get("startTime", 0)
        return datetime.time(start_mins // 60, start_mins % 60)

    async def async_set_value(self, value: datetime.time) -> None:
        """Set the time."""
        schedule = self._get_schedule()
        
        old_start_mins = schedule.get("startTime", 0)
        old_duration = schedule.get("duration", 1440)
        end_mins = (old_start_mins + old_duration) % 1440
        
        new_start_mins = value.hour * 60 + value.minute
        
        duration = end_mins - new_start_mins
        if duration <= 0:
            duration += 1440
            
        schedule["startTime"] = new_start_mins
        schedule["duration"] = duration
        
        await self.coordinator.set_charging_schedule([schedule])


class RivianChargingScheduleEndTime(RivianChargingScheduleEntity, TimeEntity):
    """Representation of a Rivian charging schedule end time."""

    def __init__(self, coordinator, vehicle):
        super().__init__(coordinator, vehicle)
        self._attr_name = "Charging Schedule End Time"
        self._attr_unique_id = f"{self._vin}-charging_schedule_end"
        self._attr_icon = "mdi:clock-end"

    @property
    def native_value(self) -> datetime.time | None:
        """Return the value of the entity."""
        schedule = self._get_schedule()
        start_mins = schedule.get("startTime", 0)
        duration = schedule.get("duration", 1440)
        end_mins = (start_mins + duration) % 1440
        return datetime.time(end_mins // 60, end_mins % 60)

    async def async_set_value(self, value: datetime.time) -> None:
        """Set the time."""
        schedule = self._get_schedule()
        
        start_mins = schedule.get("startTime", 0)
        new_end_mins = value.hour * 60 + value.minute
        
        duration = new_end_mins - start_mins
        if duration <= 0:
            duration += 1440
            
        schedule["duration"] = duration
        
        await self.coordinator.set_charging_schedule([schedule])
