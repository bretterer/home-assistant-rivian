"""Support for Rivian time entities."""

from __future__ import annotations

from datetime import time
import logging
from typing import Any, Final

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ATTR_COORDINATOR,
    ATTR_VEHICLE,
    DEFAULT_CHARGING_SCHEDULE_DURATION,
    DEFAULT_CHARGING_SCHEDULE_START,
    DOMAIN,
    MINUTES_PER_DAY,
    MINUTES_PER_HOUR,
)
from .coordinator import VehicleCoordinator
from .data_classes import RivianTimeEntityDescription
from .entity import RivianVehicleEntity

_LOGGER = logging.getLogger(__name__)


def _get_schedule_time(
    coordinator: VehicleCoordinator, is_end_time: bool = False
) -> time:
    """Get start or end time from schedule coordinator."""
    sched = coordinator.charging_schedule
    start_mins = sched.get("startTime", DEFAULT_CHARGING_SCHEDULE_START)
    if is_end_time:
        duration = sched.get("duration", DEFAULT_CHARGING_SCHEDULE_DURATION)
        mins = (start_mins + duration) % MINUTES_PER_DAY
    else:
        mins = start_mins

    return time(
        hour=(mins // MINUTES_PER_HOUR) % 24,
        minute=mins % MINUTES_PER_HOUR,
    )


async def _async_set_schedule_time(
    coordinator: VehicleCoordinator, value: time, is_end_time: bool = False
) -> None:
    """Set start or end time for schedule."""
    sched = await coordinator.get_charging_schedule_data()
    start_mins = sched.get("startTime", DEFAULT_CHARGING_SCHEDULE_START)
    target_mins = value.hour * MINUTES_PER_HOUR + value.minute

    if is_end_time:
        duration = target_mins - start_mins
        if duration <= 0:
            duration += MINUTES_PER_DAY
        await coordinator.update_charging_schedule_data({"duration": duration})
    else:
        old_dur = sched.get("duration", DEFAULT_CHARGING_SCHEDULE_DURATION)
        if old_dur >= MINUTES_PER_DAY:
            # An all-day schedule stays all day from its new start
            new_dur = MINUTES_PER_DAY
        else:
            old_end = (start_mins + old_dur) % MINUTES_PER_DAY
            new_dur = old_end - target_mins
            if new_dur <= 0:
                new_dur += MINUTES_PER_DAY
        await coordinator.update_charging_schedule_data(
            {"startTime": target_mins, "duration": new_dur}
        )


TIME_ENTITIES: Final[tuple[RivianTimeEntityDescription, ...]] = (
    RivianTimeEntityDescription(
        key="charging_schedule_start",
        translation_key="charging_schedule_start",
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda c: _get_schedule_time(c, is_end_time=False),
        set_fn=lambda c, v: _async_set_schedule_time(c, v, is_end_time=False),
    ),
    RivianTimeEntityDescription(
        key="charging_schedule_end",
        translation_key="charging_schedule_end",
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda c: _get_schedule_time(c, is_end_time=True),
        set_fn=lambda c, v: _async_set_schedule_time(c, v, is_end_time=True),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the time entities."""
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    vehicles: dict[str, dict[str, Any]] = data[ATTR_VEHICLE]
    coordinators: dict[str, VehicleCoordinator] = data[ATTR_COORDINATOR][ATTR_VEHICLE]

    entities = []
    for vehicle_id, vehicle in vehicles.items():
        coord = coordinators[vehicle_id]
        for description in TIME_ENTITIES:
            entities.append(
                RivianChargingScheduleTimeEntity(coord, entry, description, vehicle)
            )

    async_add_entities(entities)


class RivianChargingScheduleTimeEntity(RivianVehicleEntity, TimeEntity):
    """Charging Schedule Time Entity."""

    entity_description: RivianTimeEntityDescription

    @property
    def available(self) -> bool:
        """Return availability."""
        return self._available

    @property
    def native_value(self) -> time | None:
        """Return native time value."""
        return self.entity_description.value_fn(self.coordinator)

    async def async_set_value(self, value: time) -> None:
        """Set time value."""
        await self.entity_description.set_fn(self.coordinator, value)
