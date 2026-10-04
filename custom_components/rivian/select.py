"""Support for Rivian select entities."""

from __future__ import annotations

import logging
from typing import Any, Final

from rivian import VehicleCommand

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import ATTR_COORDINATOR, ATTR_VEHICLE, DOMAIN, SIGNAL_DEMO_VEHICLES_UPDATED
from .coordinator import VehicleCoordinator
from .data_classes import RivianSelectEntityDescription
from .demo import demo_vehicle_names
from .entity import RivianVehicleControlEntity

_LOGGER = logging.getLogger(__name__)

LEVEL_MAP = {"Off": "0", "On": "1", "Level_1": "2", "Level_2": "3", "Level_3": "4"}
LEVELS = ["Off", "Level_1", "Level_2", "Level_3"]


SELECTS: Final[tuple[RivianSelectEntityDescription, ...]] = (
    RivianSelectEntityDescription(
        key="seat_front_left_heat",
        icon="mdi:car-seat-heater",
        name="Seat Front Left Heat",
        options=LEVELS,
        field="seatFrontLeftHeat",
        select=lambda coordinator, option: coordinator.send_vehicle_command(
            command=VehicleCommand.CABIN_HVAC_LEFT_SEAT_HEAT,
            params={"level": int(option)},
        ),
    ),
    RivianSelectEntityDescription(
        key="seat_front_left_vent",
        icon="mdi:car-seat-cooler",
        name="Seat Front Left Vent",
        options=LEVELS,
        field="seatFrontLeftVent",
        select=lambda coordinator, option: coordinator.send_vehicle_command(
            command=VehicleCommand.CABIN_HVAC_LEFT_SEAT_VENT,
            params={"level": int(option)},
        ),
    ),
    RivianSelectEntityDescription(
        key="seat_front_right_heat",
        icon="mdi:car-seat-heater",
        name="Seat Front Right Heat",
        options=LEVELS,
        field="seatFrontRightHeat",
        select=lambda coordinator, option: coordinator.send_vehicle_command(
            command=VehicleCommand.CABIN_HVAC_RIGHT_SEAT_HEAT,
            params={"level": int(option)},
        ),
    ),
    RivianSelectEntityDescription(
        key="seat_front_right_vent",
        icon="mdi:car-seat-cooler",
        name="Seat Front Right Vent",
        options=LEVELS,
        field="seatFrontRightVent",
        select=lambda coordinator, option: coordinator.send_vehicle_command(
            command=VehicleCommand.CABIN_HVAC_RIGHT_SEAT_VENT,
            params={"level": int(option)},
        ),
    ),
    RivianSelectEntityDescription(
        key="seat_rear_left_heat",
        icon="mdi:car-seat-heater",
        name="Seat Rear Left Heat",
        options=LEVELS,
        field="seatRearLeftHeat",
        select=lambda coordinator, option: coordinator.send_vehicle_command(
            command=VehicleCommand.CABIN_HVAC_REAR_LEFT_SEAT_HEAT,
            params={"level": int(option)},
        ),
    ),
    RivianSelectEntityDescription(
        key="seat_rear_right_heat",
        icon="mdi:car-seat-heater",
        name="Seat Rear Right Heat",
        options=LEVELS,
        field="seatRearRightHeat",
        select=lambda coordinator, option: coordinator.send_vehicle_command(
            command=VehicleCommand.CABIN_HVAC_REAR_RIGHT_SEAT_HEAT,
            params={"level": int(option)},
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the select entities."""
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    vehicles: dict[str, dict[str, Any]] = data[ATTR_VEHICLE]
    coordinators: dict[str, VehicleCoordinator] = data[ATTR_COORDINATOR][ATTR_VEHICLE]

    entities: list[Any] = [
        RivianSelectEntity(coordinators[vehicle_id], entry, description, vehicle)
        for vehicle_id, vehicle in vehicles.items()
        if vehicle.get("phone_identity_id")
        for description in SELECTS
    ]

    # The dashboard vehicle picker only makes sense (and is only wired up by
    # the dashboard generator's vehicle-picker conditionals) when this entry
    # actually has more than one vehicle to choose between. Unlike the seat
    # selects above, it's not a vehicle-control entity, so it's never gated
    # on phone_identity_id/BLE pairing. Demo vehicles (see demo.py) count
    # toward the two-vehicle minimum and appear as options after the real ones.
    real_names = [
        str(vehicle.get("name") or vehicle.get("model") or vehicle_id)
        for vehicle_id, vehicle in vehicles.items()
    ]
    if len(real_names) + len(demo_vehicle_names(hass)) >= 2:
        entities.append(RivianDashboardVehicleSelect(hass, entry, real_names))

    async_add_entities(entities)


class RivianSelectEntity(RivianVehicleControlEntity, SelectEntity):
    """Representation of a Rivian select entity."""

    entity_description: RivianSelectEntityDescription

    @property
    def current_option(self) -> str | None:
        """Return the selected entity option to represent the entity state."""
        return self._get_value(self.entity_description.field)

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        await self.entity_description.select(self.coordinator, LEVEL_MAP[option])


class RivianDashboardVehicleSelect(SelectEntity, RestoreEntity):
    """Picks which vehicle the generated Rivian dashboard's tabs display.

    Not tied to a vehicle device (there's no single vehicle it belongs to)
    and not a vehicle-control entity, so it isn't gated on
    ``phone_identity_id``/BLE pairing the way the seat selects above are.
    Its unique_id (``f"{entry.entry_id}-dashboard_vehicle"``) is how
    ``dashboard_generator.py`` resolves this entity's id through the entity
    registry to wire up its per-vehicle conditional cards.
    """

    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:car-select"
    _attr_should_poll = False

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, vehicle_names: list[str]
    ) -> None:
        """Construct the dashboard vehicle picker select entity.

        ``vehicle_names`` are the entry's real vehicles; demo vehicles are
        merged in (and kept current) from the demo registry.
        """
        self.hass = hass
        self._real_names = list(vehicle_names)
        self._attr_unique_id = f"{entry.entry_id}-dashboard_vehicle"
        self._attr_name = "Dashboard vehicle"
        self._attr_options = self._merged_options()
        self._attr_current_option = (
            self._attr_options[0] if self._attr_options else None
        )

    def _merged_options(self) -> list[str]:
        """Real vehicle names followed by the registered demo vehicles' names."""
        options = list(self._real_names)
        options.extend(n for n in demo_vehicle_names(self.hass) if n not in options)
        return options

    @callback
    def _async_demo_vehicles_changed(self) -> None:
        """Refresh the options after demo vehicles were installed or removed.

        If the selected vehicle is gone, fall back to the first option.
        """
        self._attr_options = self._merged_options()
        if self._attr_current_option not in self._attr_options:
            self._attr_current_option = (
                self._attr_options[0] if self._attr_options else None
            )
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Restore the last-selected vehicle, if it's still a valid option."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_DEMO_VEHICLES_UPDATED,
                self._async_demo_vehicles_changed,
            )
        )
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state in self._attr_options:
            self._attr_current_option = last_state.state

    async def async_select_option(self, option: str) -> None:
        """Change the selected vehicle."""
        self._attr_current_option = option
        self.async_write_ha_state()
