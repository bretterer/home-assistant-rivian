"""Rivian entities."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any, TypeVar

from homeassistant.components.zone import in_zone
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ZONE
from homeassistant.core import callback
from homeassistant.helpers.entity import DeviceInfo, EntityDescription
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import ATTR_COORDINATOR, ATTR_USER, DOMAIN
from .coordinator import (
    ChargingCoordinator,
    RivianDataUpdateCoordinator,
    UserCoordinator,
    VehicleCoordinator,
    WallboxCoordinator,
)

_LOGGER = logging.getLogger(__name__)
T = TypeVar("T", bound=RivianDataUpdateCoordinator)


class RivianEntity(CoordinatorEntity[T]):
    """Base class for Rivian entities."""

    _attr_has_entity_name = True


class RivianVehicleEntity(RivianEntity[VehicleCoordinator]):
    """Base class for Rivian vehicle entities."""

    def __init__(
        self,
        coordinator: VehicleCoordinator,
        config_entry: ConfigEntry,
        description: EntityDescription,
        vehicle: dict[str, Any],
    ) -> None:
        """Construct a Rivian vehicle entity."""
        super().__init__(coordinator)
        self._config_entry = config_entry
        self.entity_description = description
        self._vin = (vin := vehicle["vin"])
        self._attr_unique_id = f"{vin}-{description.key}"

        self._available = True

        name = vehicle["name"]
        model = vehicle["model"]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, vin), (DOMAIN, vehicle["id"])},
            name=name if name else model,
            manufacturer="Rivian",
            model=model,
            serial_number=vin,
            sw_version=self._get_value("otaCurrentVersion"),
        )

    @property
    def available(self) -> bool:
        """Return the availability of the entity."""
        field = getattr(self.entity_description, "field", None)
        if field and self._get_value(field) is None:
            return False
        return self._available

    def _get_value(self, key: str) -> Any | None:
        """Get a data value from the coordinator."""
        return self.coordinator.get(key)


class RivianVehicleFreshnessEntity(RivianVehicleEntity):
    """Base class for entities reporting whether vehicle updates are current.

    Their state changes with time rather than data, so they re-check every
    minute, and stay available so a stalled connection still shows.
    """

    async def async_added_to_hass(self) -> None:
        """Re-check periodically, since a stalled connection sends nothing."""
        await super().async_added_to_hass()

        @callback
        def _async_check(_now: datetime) -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            async_track_time_interval(self.hass, _async_check, timedelta(minutes=1))
        )

    @property
    def available(self) -> bool:
        """Stay available, so a stalled connection still shows."""
        return True


# How long an entity's own availability check is set aside after a command
COMMAND_SETTLE_TIME = timedelta(seconds=30)


class RivianVehicleControlEntity(RivianVehicleEntity):
    """Base class for Rivian vehicle control entities."""

    _settle_until: datetime | None = None
    _unsub_settle: Any = None

    @property
    def available(self) -> bool:
        """Return the availability of the entity."""
        if not (super().available and self._get_value("gearStatus") == "park"):
            return False
        _fn = getattr(self.entity_description, "available", None)
        if _fn and not _fn(self.coordinator) and not self._settling:
            return False
        if zone_entity_ids := self._config_entry.options.get(CONF_ZONE, []):
            location = self.coordinator.data.get("gnssLocation", {})
            for entity_id in zone_entity_ids:
                zone = self.hass.states.get(entity_id)
                if in_zone(zone, location.get("latitude"), location.get("longitude")):
                    return True
            return False
        return True

    @property
    def _settling(self) -> bool:
        """Return True shortly after a command, while its effects are reported."""
        return self._settle_until is not None and dt_util.utcnow() < self._settle_until

    @callback
    def _settle_after_command(self) -> None:
        """Keep the entity available while the vehicle reports a command's effects.

        The vehicle reports them in separate messages (e.g. that a remote start
        is no longer offered, then that charging is connecting), so an
        availability check based on them could otherwise briefly fail.
        """
        self._settle_until = dt_util.utcnow() + COMMAND_SETTLE_TIME
        if self._unsub_settle:
            self._unsub_settle()
        self._unsub_settle = async_call_later(
            self.hass, COMMAND_SETTLE_TIME, self._async_settled
        )

    @callback
    def _async_settled(self, _now: datetime) -> None:
        """Re-check availability once the settle time is over."""
        self._unsub_settle = None
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Cancel a pending settle check."""
        if self._unsub_settle:
            self._unsub_settle()
            self._unsub_settle = None
        await super().async_will_remove_from_hass()

    def _handle_driver_update(self) -> None:
        """Handle driver update."""
        entry_data = self.hass.data[DOMAIN][self._config_entry.entry_id]
        user: UserCoordinator = entry_data[ATTR_COORDINATOR][ATTR_USER]
        phone_info = user.get_enrolled_phone_data(
            self._config_entry.options.get("public_key")
        )
        device = self.coordinator.drivers_coordinator.get_device_details(
            phone_info[1].get(self.coordinator.vehicle_id)
        )
        self._available = device["isPaired"]

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        self._handle_driver_update()
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.drivers_coordinator.async_add_listener(
                self._handle_driver_update
            )
        )


class RivianChargingEntity(RivianEntity[ChargingCoordinator]):
    """Base class for Rivian charging entities."""

    def __init__(
        self,
        coordinator: ChargingCoordinator,
        description: EntityDescription,
        vin: str,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self.entity_description = description
        self.vin = vin
        self._attr_unique_id = f"{vin}-{description.key}"

        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, vin)})


class RivianWallboxEntity(RivianEntity[WallboxCoordinator]):
    """Base class for Rivian wallbox entities."""

    def __init__(
        self,
        coordinator: WallboxCoordinator,
        description: EntityDescription,
        wallbox: dict[str, Any],
    ) -> None:
        """Construct a Rivian wallbox entity."""
        super().__init__(coordinator)
        self.entity_description = description
        self.wallbox = wallbox

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, wallbox["serialNumber"])},
            name=wallbox["name"],
            manufacturer="Rivian",
            model=wallbox["model"],
            serial_number=wallbox["serialNumber"],
            sw_version=wallbox["softwareVersion"],
        )
        self._attr_unique_id = f"{wallbox['serialNumber']}-{description.key}"

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        wallbox = next(
            (
                wallbox
                for wallbox in self.coordinator.data
                if wallbox["wallboxId"] == self.wallbox["wallboxId"]
            ),
            self.wallbox,
        )
        if self.wallbox != wallbox:
            self.wallbox = wallbox
            self.async_write_ha_state()
