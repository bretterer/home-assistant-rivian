"""Support for Rivian button entities."""
from __future__ import annotations

import asyncio
import logging
import platform
from typing import Any, Final
from uuid import UUID

from bleak import BLEDevice
from home_assistant_bluetooth import BluetoothServiceInfoBleak
from rivian import VehicleCommand
import rivian.ble as rivian_ble

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothScanningMode
from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ATTR_COORDINATOR, ATTR_USER, ATTR_VEHICLE, DOMAIN
from .coordinator import UserCoordinator, VehicleCoordinator
from .data_classes import RivianButtonEntityDescription
from .entity import RivianVehicleControlEntity

_LOGGER = logging.getLogger(__name__)


BUTTONS: Final[dict[str | None, tuple[RivianButtonEntityDescription, ...]]] = {
    None: (
        RivianButtonEntityDescription(
            key="wake",
            icon="mdi:weather-night",
            name="Wake",
            available=lambda coordinator: coordinator.get("powerState") == "sleep",
            press_fn=lambda coordinator: coordinator.send_vehicle_command(
                command=VehicleCommand.WAKE_VEHICLE
            ),
        ),
    ),
    "SIDE_BIN_NXT_ACT": (
        RivianButtonEntityDescription(
            key="open_gear_tunnel_left",
            name="Open Gear Tunnel Left",
            press_fn=lambda coordinator: coordinator.send_vehicle_command(
                command=VehicleCommand.RELEASE_LEFT_SIDE_BIN
            ),
        ),
        RivianButtonEntityDescription(
            key="open_gear_tunnel_right",
            name="Open Gear Tunnel Right",
            press_fn=lambda coordinator: coordinator.send_vehicle_command(
                command=VehicleCommand.RELEASE_RIGHT_SIDE_BIN
            ),
        ),
    ),
    "TAILGATE_CMD": (
        RivianButtonEntityDescription(
            key="drop_tailgate",
            name="Drop Tailgate",
            available=lambda coordinator: coordinator.get("closureTailgateClosed")
            != "open",
            press_fn=lambda coordinator: coordinator.send_vehicle_command(
                command=VehicleCommand.OPEN_LIFTGATE_UNLATCH_TAILGATE
            ),
        ),
    ),
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the button entities"""
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    vehicles: dict[str, dict[str, Any]] = data[ATTR_VEHICLE]
    coordinators: dict[str, VehicleCoordinator] = data[ATTR_COORDINATOR][ATTR_VEHICLE]

    entities = [
        RivianButtonEntity(coordinators[vehicle_id], entry, description, vehicle)
        for vehicle_id, vehicle in vehicles.items()
        if vehicle.get("phone_identity_id")
        for feature, descriptions in BUTTONS.items()
        if feature is None or feature in (vehicle.get("supported_features", []))
        for description in descriptions
    ]
    entities.extend(
        RivianPairPhoneButtonEntity(
            coordinators[vehicle_id],
            entry,
            ButtonEntityDescription(key="pair", name="Pair"),
            vehicle,
        )
        for vehicle_id, vehicle in vehicles.items()
        if (
            device := coordinators[vehicle_id].drivers_coordinator.get_device_details(
                vehicle.get("phone_identity_id")
            )
        )
        and not device["isPaired"]
    )
    async_add_entities(entities)


class RivianButtonEntity(RivianVehicleControlEntity, ButtonEntity):
    """Representation of a Rivian button entity."""

    entity_description: RivianButtonEntityDescription

    async def async_press(self) -> None:
        """Press the button."""
        await self.entity_description.press_fn(self.coordinator)


class RivianPairPhoneButtonEntity(RivianVehicleControlEntity, ButtonEntity):
    """Representation of a Rivian pair phone button entity."""

    _pairing: bool = False

    async def async_press(self) -> None:
        """Press the button."""
        if self._pairing:
            raise HomeAssistantError(
                "Either a pairing process is currently under way or pairing is already complete. Please try again later"
            )

        self._pairing = True

        entry_data = self.hass.data[DOMAIN][self._config_entry.entry_id]
        vehicle = entry_data[ATTR_VEHICLE][self.coordinator.vehicle_id]
        user: UserCoordinator = entry_data[ATTR_COORDINATOR][ATTR_USER]
        phone_info = user.get_enrolled_phone_data(
            self._config_entry.options.get("public_key")
        )

        rivian_phone_keys = set()

        def _process_more_advertisements(
            service_info: BluetoothServiceInfoBleak,
        ) -> bool:
            if service_info.address in rivian_phone_keys:
                return False
            _LOGGER.debug("Found %s (RSSI: %s)", service_info.device, service_info.rssi)
            rivian_phone_keys.add(service_info.address)
            return True

        async def _find_phone_key() -> tuple[BLEDevice, bool] | None:
            _LOGGER.debug("Searching for %s", rivian_ble.DEVICE_LOCAL_NAME)
            try:
                service_info = await bluetooth.async_process_advertisements(
                    self.hass,
                    _process_more_advertisements,
                    {"local_name": rivian_ble.DEVICE_LOCAL_NAME, "connectable": True},
                    BluetoothScanningMode.ACTIVE,
                    30,
                )
                return (
                    service_info.device,
                    str(UUID(vehicle["vas_id"])) in service_info.service_uuids,
                )
            except Exception as ex:  # pylint: disable=broad-except
                _LOGGER.error(
                    "%s not found%s",
                    rivian_ble.DEVICE_LOCAL_NAME,
                    ("" if isinstance(ex, asyncio.TimeoutError) else f": {ex}"),
                )
                return None

        while search_result := await _find_phone_key():
            if platform.system() == "Linux":
                _LOGGER.debug("Making sure BT controller can be paired")
                # TODO: find out how BT proxy presents itself to avoid invalid warnings
                if not await rivian_ble.set_bluez_pairable(search_result[0]):
                    _LOGGER.warning(
                        "Couldn't set BT controller to pairable, phone pairing may fail"
                    )
            if await rivian_ble.pair_phone(
                search_result[0],
                phone_info[0],
                vehicle["vas_id"],
                vehicle["public_key"],
                self._config_entry.options.get("private_key"),
            ):
                _LOGGER.debug("Querying API to validate vehicle pairing was successful")
                await asyncio.sleep(10)
                await (coor := self.coordinator.drivers_coordinator).async_refresh()
                if (
                    device := coor.get_device_details(phone_info[1].get(vehicle["id"]))
                ) and device["isPaired"]:
                    _LOGGER.debug("Success, pairing is now complete")
                    self._available = False
                    self.async_write_ha_state()
                    return
                _LOGGER.debug(
                    "Unable to validate pairing via API, please manually test vehicle controls"
                )
                self._pairing = False
                return
            if search_result[1]:
                # we found the appropriate key but pairing didn't work, so no need to continue
                break

        _LOGGER.debug("Unable to complete pairing")
        self._pairing = False

    def _handle_driver_update(self) -> None:
        """Handle driver update."""
