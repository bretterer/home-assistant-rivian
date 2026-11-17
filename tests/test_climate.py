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

    def presets(features: list[str], pet_mode: str = "off") -> list[str]:
        vehicle = {
            "id": "id",
            "vin": "vin",
            "name": "name",
            "model": "R1S",
            "supported_features": features,
        }
        cabin = climate.RivianClimateEntity(
            MagicMock(), MagicMock(), climate.CLIMATE, vehicle
        )
        cabin._get_value = {"petModeStatus": pet_mode}.get  # type: ignore[method-assign]
        return cabin.preset_modes

    assert presets([]) == ["LO", "HI", climate.DEFROST_DEFOG]
    assert presets([], "on") == ["LO", "HI", climate.DEFROST_DEFOG]
    assert presets(["PET_COMFORT_CONTROL", "CLM_HOLD"]) == [
        "LO",
        "HI",
        climate.DEFROST_DEFOG,
        climate.CLIMATE_HOLD,
    ]


def test_pet_comfort_listed_only_while_on() -> None:
    """Pet comfort is turned on from its switch, so it's listed only while on."""
    vehicle = {
        "id": "id",
        "vin": "vin",
        "name": "name",
        "model": "R1S",
        "supported_features": ["PET_COMFORT_CONTROL", "CLM_HOLD"],
    }
    cabin = climate.RivianClimateEntity(
        MagicMock(), MagicMock(), climate.CLIMATE, vehicle
    )
    values = {"petModeStatus": "off"}
    cabin._get_value = values.get  # type: ignore[method-assign]
    assert climate.PET_COMFORT not in cabin.preset_modes
    values["petModeStatus"] = "on"
    assert cabin.preset_modes == [
        "LO",
        "HI",
        climate.DEFROST_DEFOG,
        climate.PET_COMFORT,
        climate.CLIMATE_HOLD,
    ]
    values["petModeStatus"] = "off"
    assert climate.PET_COMFORT not in cabin.preset_modes


async def test_climate_hold_preset_turns_it_on() -> None:
    """Choosing climate hold sends the same command as its switch."""
    cabin = commanded({})
    await cabin.async_set_preset_mode(climate.CLIMATE_HOLD)
    assert cabin.coordinator.send_parallax_command.await_args_list == [
        call(climate.SET_CLIMATE_HOLD_RVM, ("hold", True))
    ]


async def test_pet_comfort_preset_while_on_does_nothing() -> None:
    """Choosing pet comfort, listed only while on, sends nothing and keeps a hold."""
    cabin = commanded({"petModeStatus": "on", "climateHoldStatus": "on"})
    await cabin.async_set_preset_mode(climate.PET_COMFORT)
    cabin.coordinator.send_parallax_command.assert_not_awaited()
    cabin.coordinator.send_vehicle_command.assert_not_awaited()


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
    """LO and HI end a hold and turn the cabin on."""
    # A hold keeps the cabin conditioned without reporting preconditioning
    hold = {"climateHoldStatus": "on", "cabinPreconditioningType": "none"}
    cabin = commanded(hold)
    await cabin.async_set_preset_mode("HI")
    assert cabin.coordinator.send_parallax_command.await_args_list == [
        call(climate.SET_CLIMATE_HOLD_RVM, ("hold", False))
    ]
    command = climate.VehicleCommand
    assert cabin.coordinator.send_vehicle_command.await_args_list == [
        call(command=command.VEHICLE_CABIN_PRECONDITION_ENABLE),
        call(
            command=command.CABIN_PRECONDITIONING_SET_TEMP,
            params={"HVAC_set_temp": 63.5},
        ),
    ]


async def test_heat_is_the_defrost_preset() -> None:
    """Heat (defrost/defog) is refused with pet comfort on and ends a hold."""
    cabin = commanded({"petModeStatus": "on"})
    with pytest.raises(TranslatedError):
        await cabin.async_set_hvac_mode(climate.HVACMode.HEAT)
    cabin.coordinator.send_vehicle_command.assert_not_awaited()

    cabin = commanded({"climateHoldStatus": "on"})
    await cabin.async_set_hvac_mode(climate.HVACMode.HEAT)
    assert cabin.coordinator.send_parallax_command.await_args_list == [
        call(climate.SET_CLIMATE_HOLD_RVM, ("hold", False))
    ]
    assert cabin.coordinator.send_vehicle_command.await_args_list == [
        call(
            command=climate.VehicleCommand.CABIN_HVAC_DEFROST_DEFOG,
            params={"level": 1},
        )
    ]
