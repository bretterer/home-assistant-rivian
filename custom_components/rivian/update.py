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
from .coordinator import VehicleCoordinator
from .entity import RivianVehicleEntity

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
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    vehicles: dict[str, Any] = data[ATTR_VEHICLE]
    coordinators: dict[str, VehicleCoordinator] = data[ATTR_COORDINATOR][ATTR_VEHICLE]

    entities = [
        RivianUpdateEntity(coordinators[vehicle_id], entry, UPDATE_DESCRIPTION, vehicle)
        for vehicle_id, vehicle in vehicles.items()
    ]
    async_add_entities(entities)


class RivianUpdateEntity(RivianVehicleEntity, UpdateEntity):
    """Rivian Update Entity."""

    _attr_supported_features = Feature.PROGRESS | Feature.RELEASE_NOTES

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

    async def async_release_notes(self) -> str | None:
        """Return Rivian release notes."""
        vehicle_id = self.coordinator.vehicle_id
        try:
            resp = await self.coordinator.api.get_vehicle_ota_update_details(vehicle_id)
            resp_data = await resp.json()
            data = resp_data["data"]["getVehicle"]
            if details := data["availableOTAUpdateDetails"]:
                url = details["url"]
            else:
                url = data["currentOTAUpdateDetails"]["url"]
        except:  # pylint: disable=bare-except
            url = f"https://rivian.software/{self.latest_version.replace('.','-')}/"
        return f"[Read release announcement]({url})"
