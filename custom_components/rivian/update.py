"""Rivian (Unofficial)"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityDescription,
    UpdateEntityFeature as Feature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ATTR_COORDINATOR, ATTR_VEHICLE, DOMAIN
from .coordinator import RivianDataUpdateCoordinator
from .entity import RivianEntity

INSTALLING_STATUS = ("Install_Countdown", "Awaiting_Install", "Installing")

UPDATE_DESCRIPTION = UpdateEntityDescription(
    key="software_ota",
    name="Software",
    device_class=UpdateDeviceClass.FIRMWARE,
    entity_category=EntityCategory.DIAGNOSTIC,
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the sensor entities"""
    coordinator: RivianDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][
        ATTR_COORDINATOR
    ][ATTR_VEHICLE]
    entities = [
        RivianUpdateEntity(coordinator, entry, UPDATE_DESCRIPTION, vin)
        for vin in coordinator.vehicles
    ]
    async_add_entities(entities)


class RivianUpdateEntity(RivianEntity, UpdateEntity):
    """Rivian Update Entity."""

    _attr_supported_features = Feature.PROGRESS

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
