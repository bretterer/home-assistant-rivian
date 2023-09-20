"""Support for Rivian button entities."""
from __future__ import annotations

import asyncio
import logging
import platform
import secrets
from typing import Any, Final

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from home_assistant_bluetooth import BluetoothServiceInfoBleak
from rivian import VehicleCommand
from rivian.utils import get_message_signature, get_secret_key

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothScanningMode
from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
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
        if vehicle.get("phone_identity_id")
    )
    async_add_entities(entities)


class RivianButtonEntity(RivianVehicleControlEntity, ButtonEntity):
    """Representation of a Rivian button entity."""

    entity_description: RivianButtonEntityDescription

    async def async_press(self) -> None:
        """Press the button."""
        await self.entity_description.press_fn(self.coordinator)


DEVICE_LOCAL_NAME = "Rivian Phone Key"
ACTIVE_ENTRY_CHARACTERISTIC_UUID = "5249565f-4d4f-424b-4559-5f5752495445"
PHONE_ID_VEHICLE_ID_UUID = "aa49565a-4d4f-424b-4559-5f5752495445"
PHONE_NONCE_VEHICLE_NONCE_UUID = "e020a15d-e730-4b2c-908b-51daf9d41e19"


def generate_ble_command_hmac(hmac_data: bytes, vehicle_key: str, private_key: str):
    """Generate ble command hmac."""
    secret_key = get_secret_key(private_key, vehicle_key)
    return bytes.fromhex(get_message_signature(secret_key, hmac_data))


class RivianPairPhoneButtonEntity(RivianVehicleControlEntity, ButtonEntity):
    """Representation of a Rivian pair phone button entity."""

    async def async_press(self) -> None:
        """Press the button."""
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
            _LOGGER.debug("Found %s (%s)", service_info.name, service_info.address)
            rivian_phone_keys.add(service_info.address)
            return True

        async def _find_phone_key() -> BLEDevice | None:
            _LOGGER.debug("Searching for %s", DEVICE_LOCAL_NAME)
            try:
                service_info = await bluetooth.async_process_advertisements(
                    self.hass,
                    _process_more_advertisements,
                    {"local_name": DEVICE_LOCAL_NAME, "connectable": True},
                    BluetoothScanningMode.ACTIVE,
                    30,
                )
                return service_info.device
            except Exception as ex:  # pylint: disable=broad-except
                _LOGGER.error(
                    "%s not found%s",
                    DEVICE_LOCAL_NAME,
                    ("" if isinstance(ex, asyncio.TimeoutError) else f": {ex}"),
                )
                return None

        while device := await _find_phone_key():
            if await pair_phone(
                device,
                phone_info[0],
                vehicle["vas_id"],
                vehicle["public_key"],
                self._config_entry.options.get("private_key"),
            ):
                return

        _LOGGER.debug("Unable to complete pairing")


async def pair_phone(
    device: BLEDevice, phone_id: str, vas_id: str, public_key: str, private_key: str
) -> bool:
    """Pair "phone" with Rivian."""
    notify_dict = {}
    notify_event = asyncio.Event()

    def _notify_handler(characteristic: BleakGATTCharacteristic, data: bytearray):
        """Notification handler."""
        if characteristic.uuid == PHONE_ID_VEHICLE_ID_UUID:
            field = "vas_vehicle_id"
        elif characteristic.uuid == PHONE_NONCE_VEHICLE_NONCE_UUID:
            field = "vehicle_nonce"
        else:
            field = characteristic.description
        notify_dict[field] = data.hex()
        notify_event.set()

    _LOGGER.debug("Attempting connection to %s (%s)", device.name, device.address)
    try:
        async with BleakClient(device, timeout=30) as client:
            _LOGGER.debug("Connected to %s (%s)", device.name, device.address)
            await client.start_notify(PHONE_ID_VEHICLE_ID_UUID, _notify_handler)
            await client.start_notify(PHONE_NONCE_VEHICLE_NONCE_UUID, _notify_handler)

            _LOGGER.debug("Requesting vas vehicle id")
            await client.write_gatt_char(
                PHONE_ID_VEHICLE_ID_UUID,
                bytes.fromhex(phone_id.replace("-", "")),
                response=True,
            )
            await asyncio.wait_for(notify_event.wait(), 5)
            notify_event.clear()

            vas_vehicle_id = notify_dict.get("vas_vehicle_id")
            if vas_vehicle_id != vas_id:
                _LOGGER.debug(
                    "Incorrect vas vehicle id: received %s, expected %s",
                    vas_vehicle_id,
                    vas_id,
                )
                return False

            _LOGGER.debug('Vas vehicle id "%s" received', vas_vehicle_id)

            _LOGGER.debug("Exchanging nonce")
            phone_nonce = secrets.token_bytes(16)
            hmac = generate_ble_command_hmac(phone_nonce, public_key, private_key)
            await client.write_gatt_char(
                PHONE_NONCE_VEHICLE_NONCE_UUID, phone_nonce + hmac, response=True
            )
            await asyncio.wait_for(notify_event.wait(), 5)
            notify_event.clear()

            # Vehicle is authenticated, trigger bonding
            if platform.system() == "Darwin":
                # Mac BLE API doesn't have an explicit way to trigger bonding
                # Instead, enable notification on ACTIVE_ENTRY_CHARACTERISTIC_UUID to trigger bonding manually
                await client.start_notify(
                    ACTIVE_ENTRY_CHARACTERISTIC_UUID, _notify_handler
                )
            else:
                await client.pair()

            _LOGGER.debug(
                "Successfully paired with %s (%s)", device.name, device.address
            )
            return True
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.debug(
            "Couldn't connect to %s (%s). "
            "If you have multiple vehicles nearby, you may see this message a few times as the correct device is scanned. "
            'Otherwise, make sure you are in the correct vehicle and have selected "Set Up" for the appropriate key and try again'
            "%s",
            device.name,
            device.address,
            ("" if isinstance(ex, asyncio.TimeoutError) else f": {ex}"),
        )
    return False
