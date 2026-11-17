"""Tests for the cabin climate entity."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from custom_components.rivian import climate


def entity(values: dict[str, object]) -> climate.RivianClimateEntity:
    """A climate entity reading the given vehicle values."""
    climate_entity = object.__new__(climate.RivianClimateEntity)
    climate_entity._get_value = MagicMock(side_effect=values.get)  # type: ignore[method-assign]
    return climate_entity


def test_native_unit_is_celsius() -> None:
    """The native unit is °C; Home Assistant converts it for display."""
    assert (
        climate.RivianClimateEntity._attr_native_temperature_unit
        == climate.UnitOfTemperature.CELSIUS
    )


def test_lo_hi_presets_read_celsius() -> None:
    """LO and HI are the vehicle's 0 °C and 63.5 °C set points."""
    for target, preset in ((0, "LO"), (63.5, "HI"), (21, None)):
        cabin = entity({"defrostDefogStatus": "off", "hvacTargetTemperature": target})
        assert cabin.preset_mode == preset


def test_on_during_pet_mode_and_climate_hold() -> None:
    """Pet mode and climate hold show as on, as the vehicle reports no precondition."""
    off = {"defrostDefogStatus": "off", "cabinPreconditioningType": "none"}
    assert entity(off).hvac_mode == climate.HVACMode.OFF
    assert entity(off | {"petModeStatus": "on"}).hvac_mode == climate.HVACMode.HEAT_COOL
    assert (
        entity(off | {"climateHoldStatus": "on"}).hvac_mode
        == climate.HVACMode.HEAT_COOL
    )


class TranslatedError(Exception):
    """Stands in for Home Assistant's errors, keeping their translation."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__()
        self.translation_key = kwargs.get("translation_key")


def commanded(values: dict[str, object]) -> climate.RivianClimateEntity:
    """A climate entity whose commands are recorded."""
    cabin = entity({"defrostDefogStatus": "off"} | values)
    cabin.coordinator = MagicMock(
        send_parallax_command=AsyncMock(), send_vehicle_command=AsyncMock()
    )
    return cabin


@pytest.fixture(autouse=True)
def encoders() -> Any:
    """Readable stand-ins for the Parallax encoders and translated errors."""
    with (
        patch.object(climate, "VehicleCommand"),
        patch.object(climate, "encode_pet_mode", lambda on: ("pet", on)),
        patch.object(climate, "encode_climate_hold", lambda on: ("hold", on)),
        patch.object(climate, "ServiceValidationError", TranslatedError),
        # Home Assistant's value, which conftest mocks
        patch.object(climate, "ATTR_TEMPERATURE", "temperature"),
    ):
        yield


async def test_off_keeps_pet_comfort() -> None:
    """Turning off leaves pet comfort on and says to use its switch."""
    cabin = commanded({"petModeStatus": "on", "climateHoldStatus": "on"})
    with pytest.raises(TranslatedError) as err:
        await cabin.async_set_hvac_mode(climate.HVACMode.OFF)
    assert err.value.translation_key == "pet_comfort_on"
    cabin.coordinator.send_parallax_command.assert_not_awaited()
    cabin.coordinator.send_vehicle_command.assert_not_awaited()


async def test_off_ends_climate_hold() -> None:
    """Turning off ends a climate hold, which preconditioning doesn't."""
    cabin = commanded({"climateHoldStatus": "on"})
    await cabin.async_set_hvac_mode(climate.HVACMode.OFF)
    assert cabin.coordinator.send_parallax_command.await_args_list == [
        call(climate.SET_CLIMATE_HOLD_RVM, ("hold", False))
    ]
    cabin.coordinator.send_vehicle_command.assert_awaited_once()


def test_preset_order() -> None:
    """Pet comfort shows first, then climate hold, defrost/defog, LO/HI."""
    values = {"defrostDefogStatus": "on", "hvacTargetTemperature": 0}
    assert entity(values).preset_mode == climate.DEFROST_DEFOG
    values |= {"climateHoldStatus": "on"}
    assert entity(values).preset_mode == climate.CLIMATE_HOLD
    values |= {"petModeStatus": "on"}
    assert entity(values).preset_mode == climate.PET_COMFORT
    # Defrost/defog still shows as heat under pet comfort
    assert entity(values).hvac_mode == climate.HVACMode.HEAT


def test_presets_follow_supported_features() -> None:
    """Pet comfort and climate hold are offered only when the vehicle has them."""

    def presets(features: list[str]) -> list[str]:
        vehicle = {
            "id": "id",
            "vin": "vin",
            "name": "name",
            "model": "R1S",
            "supported_features": features,
        }
        return climate.RivianClimateEntity(
            MagicMock(), MagicMock(), climate.CLIMATE, vehicle
        )._attr_preset_modes

    assert presets([]) == ["LO", "HI", climate.DEFROST_DEFOG]
    assert presets(["PET_COMFORT_CONTROL", "CLM_HOLD"]) == [
        "LO",
        "HI",
        climate.DEFROST_DEFOG,
        climate.PET_COMFORT,
        climate.CLIMATE_HOLD,
    ]


@pytest.mark.parametrize(
    ("preset", "rvm", "payload"),
    [
        (climate.PET_COMFORT, climate.SET_PET_MODE_RVM, ("pet", True)),
        (climate.CLIMATE_HOLD, climate.SET_CLIMATE_HOLD_RVM, ("hold", True)),
    ],
)
async def test_presets_turn_on_pet_comfort_and_climate_hold(
    preset: str, rvm: str, payload: tuple[str, bool]
) -> None:
    """Choosing pet comfort or climate hold sends the same command as its switch."""
    cabin = commanded({})
    await cabin.async_set_preset_mode(preset)
    assert cabin.coordinator.send_parallax_command.await_args_list == [
        call(rvm, payload)
    ]


async def test_other_presets_keep_pet_comfort() -> None:
    """While pet comfort is on, choosing another preset raises like turning off."""
    for preset in ("LO", climate.DEFROST_DEFOG, climate.CLIMATE_HOLD):
        cabin = commanded({"petModeStatus": "on"})
        with pytest.raises(TranslatedError) as err:
            await cabin.async_set_preset_mode(preset)
        assert err.value.translation_key == "pet_comfort_on"
        cabin.coordinator.send_parallax_command.assert_not_awaited()
        cabin.coordinator.send_vehicle_command.assert_not_awaited()


async def test_other_presets_end_climate_hold() -> None:
    """LO, HI and defrost/defog end a climate hold first; pet comfort leaves it."""
    cabin = commanded({"climateHoldStatus": "on", "cabinPreconditioningType": "x"})
    await cabin.async_set_preset_mode("HI")
    assert cabin.coordinator.send_parallax_command.await_args_list == [
        call(climate.SET_CLIMATE_HOLD_RVM, ("hold", False))
    ]
    cabin.coordinator.send_vehicle_command.assert_awaited()

    cabin = commanded({"climateHoldStatus": "on"})
    await cabin.async_set_preset_mode(climate.PET_COMFORT)
    assert cabin.coordinator.send_parallax_command.await_args_list == [
        call(climate.SET_PET_MODE_RVM, ("pet", True))
    ]
