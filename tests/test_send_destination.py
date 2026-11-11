"""Tests for the send destination notify entity."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from aiohttp import ContentTypeError
import pytest
from rivian.exceptions import RivianApiException

from custom_components.rivian import notify
from custom_components.rivian.const import ATTR_COORDINATOR, ATTR_VEHICLE, DOMAIN
from homeassistant.exceptions import HomeAssistantError

VEHICLE = {"id": "vehicle-1", "vin": "VIN1", "name": "Truck", "model": "R1T"}
DESTINATION = "1 Ferry Building, San Francisco, CA"


def _response(result: int | None) -> SimpleNamespace:
    payload: dict[str, Any] = {
        "data": {
            "parseAndShareLocationToVehicle": {"publishResponse": {"result": result}}
        }
    }
    return SimpleNamespace(json=AsyncMock(return_value=payload))


def make_entity(**api: Any) -> notify.RivianSendDestinationEntity:
    coor = MagicMock()
    coor.vehicle_id = "vehicle-1"
    coor.api = SimpleNamespace(send_location_to_vehicle=AsyncMock(**api))
    return notify.RivianSendDestinationEntity(
        coor, MagicMock(), notify.SEND_DESTINATION, VEHICLE
    )


async def test_setup_adds_one_entity_per_vehicle() -> None:
    vehicles = {"vehicle-1": VEHICLE, "vehicle-2": VEHICLE | {"vin": "VIN2"}}
    entry = MagicMock()
    hass = MagicMock()
    hass.data = {
        DOMAIN: {
            entry.entry_id: {
                ATTR_VEHICLE: vehicles,
                ATTR_COORDINATOR: {ATTR_VEHICLE: {v: MagicMock() for v in vehicles}},
            }
        }
    }
    added: list[notify.RivianSendDestinationEntity] = []

    await notify.async_setup_entry(hass, entry, added.extend)

    assert [entity.unique_id for entity in added] == [
        "VIN1-navigation",
        "VIN2-navigation",
    ]


async def test_message_is_sent_as_the_destination() -> None:
    entity = make_entity(return_value=_response(0))

    await entity.async_send_message(DESTINATION)

    entity.coordinator.api.send_location_to_vehicle.assert_awaited_once_with(
        DESTINATION, "vehicle-1"
    )


@pytest.mark.parametrize("result", [1, None])
async def test_a_rejected_destination_raises(result: int | None) -> None:
    entity = make_entity(return_value=_response(result))

    with pytest.raises(HomeAssistantError, match="Unable to send the destination"):
        await entity.async_send_message(DESTINATION)


async def test_an_api_error_raises() -> None:
    error = RivianApiException("Timeout occurred while connecting to Rivian API.")
    entity = make_entity(side_effect=error)

    with pytest.raises(
        HomeAssistantError, match="Unable to send the destination"
    ) as err:
        await entity.async_send_message(DESTINATION)
    assert err.value.__cause__ is error


async def test_a_non_json_response_raises() -> None:
    error = ContentTypeError(MagicMock(), (), message="Attempt to decode JSON")
    entity = make_entity(
        return_value=SimpleNamespace(json=AsyncMock(side_effect=error))
    )

    with pytest.raises(
        HomeAssistantError, match="Unable to send the destination"
    ) as err:
        await entity.async_send_message(DESTINATION)
    assert err.value.__cause__ is error
