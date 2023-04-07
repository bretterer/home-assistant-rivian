"""Rivian (Unofficial)"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import RivianDataUpdateCoordinator, RivianEntity, get_device_identifier
from .const import ATTR_COORDINATOR, CONF_VIN, DOMAIN
from .helpers import get_model_and_year

_LOGGER: logging.Logger = logging.getLogger(__name__)

INSTALLING_STATUS = ("Install_Countdown", "Awaiting_Install", "Installing")


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the sensor entities"""
    coordinator = hass.data[DOMAIN][entry.entry_id][ATTR_COORDINATOR]
    vin = entry.data.get(CONF_VIN)
    async_add_entities(
        [RivianUpdateEntity(coordinator=coordinator, vin=vin, entry=entry)], True
    )


class RivianUpdateEntity(RivianEntity, CoordinatorEntity, UpdateEntity):
    """Rivian Update Entity."""

    _attr_has_entity_name = True
    _attr_name = "Software OTA"
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_supported_features = UpdateEntityFeature.PROGRESS

    def __init__(
        self, coordinator: RivianDataUpdateCoordinator, vin: str, entry: ConfigEntry
    ) -> None:
        """Create a Rivian update entity."""
        CoordinatorEntity.__init__(self, coordinator)
        super().__init__(coordinator)
        self.coordinator = coordinator
        self._vin = vin

        manufacturer = "Rivian"
        model, year = get_model_and_year(vin)

        self._attr_unique_id = f"{vin}-software_ota"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, vin), get_device_identifier(entry)},
            name=f"{manufacturer} {model}" if model else manufacturer,
            manufacturer=manufacturer,
            model=f"{year} {manufacturer} {model}" if model and year else None,
        )

    @property
    def installed_version(self) -> str:
        """Version installed and in use."""
        return self.coordinator.data["otaCurrentVersion"]["value"]

    @property
    def latest_version(self) -> str:
        """Latest version available for install."""
        if (value := self.coordinator.data["otaAvailableVersion"]["value"]) == "0.0.0":
            value = self.installed_version
        return value

    @property
    def in_progress(self) -> int | None:
        """Update installation progress."""
        if self.coordinator.data["otaStatus"]["value"] in INSTALLING_STATUS:
            return self.coordinator.data["otaInstallProgress"]["value"]
        return None

    @property
    def release_url(self) -> str:
        """URL to the full release notes of the latest version available."""
        return f"https://rivian.software/{self.latest_version.replace('.','-')}/"

    @property
    def extra_state_attributes(self) -> Mapping[str, Any]:
        """Return entity specific state attributes."""
        return {
            "current_version": {
                "year": self.coordinator.data["otaCurrentVersionYear"]["value"],
                "week": self.coordinator.data["otaCurrentVersionWeek"]["value"],
                "number": self.coordinator.data["otaCurrentVersionNumber"]["value"],
                "git_hash": self.coordinator.data["otaCurrentVersionGitHash"]["value"],
            },
            "available_version": {
                "year": self.coordinator.data["otaAvailableVersionYear"]["value"],
                "week": self.coordinator.data["otaAvailableVersionWeek"]["value"],
                "number": self.coordinator.data["otaAvailableVersionNumber"]["value"],
                "git_hash": self.coordinator.data["otaAvailableVersionGitHash"][
                    "value"
                ],
            },
        }
