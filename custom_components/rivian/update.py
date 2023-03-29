"""Rivian (Unofficial)"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature as Feature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import RivianDataUpdateCoordinator, RivianEntity
from .const import ATTR_COORDINATOR, CONF_VIN, DOMAIN

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


class RivianUpdateEntity(RivianEntity, UpdateEntity):
    """Rivian Update Entity."""

    _attr_has_entity_name = True
    _attr_name = "Software"
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_supported_features = Feature.PROGRESS

    def __init__(
        self, coordinator: RivianDataUpdateCoordinator, vin: str, entry: ConfigEntry
    ) -> None:
        """Create a Rivian update entity."""
        super().__init__(coordinator, entry)
        self._vin = vin
        self._attr_unique_id = f"{vin}-software"

    @property
    def installed_version(self) -> str:
        """Version installed and in use."""
        return self._get_value("otaCurrentVersion")

    @property
    def latest_version(self) -> str:
        """Latest version available for install."""
        if (value := self._get_value("otaAvailableVersion")) == "0.0.0":
            value = self.installed_version
        return value

    @property
    def in_progress(self) -> int | None:
        """Update installation progress."""
        if self._get_value("otaStatus") in INSTALLING_STATUS:
            return self._get_value("otaInstallProgress")
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
                "year": self._get_value("otaCurrentVersionYear"),
                "week": self._get_value("otaCurrentVersionWeek"),
                "number": self._get_value("otaCurrentVersionNumber"),
                "git_hash": self._get_value("otaCurrentVersionGitHash"),
            },
            "available_version": {
                "year": self._get_value("otaAvailableVersionYear"),
                "week": self._get_value("otaAvailableVersionWeek"),
                "number": self._get_value("otaAvailableVersionNumber"),
                "git_hash": self._get_value("otaAvailableVersionGitHash"),
            },
        }
