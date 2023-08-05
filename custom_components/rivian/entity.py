"""Rivian entities."""
from __future__ import annotations

from collections.abc import Iterable
import logging
from typing import Any, TypeVar

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo, EntityDescription
import homeassistant.helpers.entity_registry as er
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import (
    ChargingCoordinator,
    RivianDataUpdateCoordinator,
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
            sw_version=self._get_value("otaCurrentVersion"),
        )

    @property
    def available(self) -> bool:
        """Return the availability of the entity."""
        return self._available

    def _get_value(self, key: str) -> Any | None:
        """Get a data value from the coordinator."""
        return self.coordinator.get(key)


class RivianVehicleControlEntity(RivianVehicleEntity):
    """Base class for Rivian vehicle control entities."""

    @property
    def available(self) -> bool:
        """Return the availability of the entity."""
        return super().available and self._get_value("gearStatus") == "park"


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


def async_update_unique_id(
    hass: HomeAssistant, domain: str, entities: Iterable[RivianVehicleEntity]
) -> None:
    """Update unique ID to be based on VIN and entity description key instead of name."""
    ent_reg = er.async_get(hass)
    for entity in entities:
        if not (old_key := getattr(entity.entity_description, "old_key")):
            continue
        if entity._config_entry.data.get("vin") != entity._vin:
            continue
        old_unique_id = f"{DOMAIN}_{old_key}_{entity._config_entry.entry_id}"  # pylint: disable=protected-access
        if entity_id := ent_reg.async_get_entity_id(domain, DOMAIN, old_unique_id):
            new_unique_id = f"{entity._vin}-{entity.entity_description.key}"  # pylint: disable=protected-access
            ent_reg.async_update_entity(entity_id, new_unique_id=new_unique_id)
