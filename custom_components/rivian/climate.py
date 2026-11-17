"""Support for Rivian climate entities."""

from __future__ import annotations

import logging
from typing import Any, Final

from rivian import VehicleCommand
from rivian.parallax import (
    SET_CLIMATE_HOLD_RVM,
    SET_PET_MODE_RVM,
    encode_climate_hold,
    encode_pet_mode,
)

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityDescription,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, PRECISION_WHOLE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ATTR_COORDINATOR, ATTR_VEHICLE, DOMAIN
from .coordinator import VehicleCoordinator
from .entity import RivianVehicleControlEntity

_LOGGER = logging.getLogger(__name__)

CLIMATE: Final[ClimateEntityDescription] = ClimateEntityDescription(
    key="cabin_climate", translation_key="cabin_climate"
)

DEFROST_DEFOG = "Defrost/Defog"
PET_COMFORT = "pet_comfort"
CLIMATE_HOLD = "climate_hold"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the climate entity."""
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    vehicles: dict[str, dict[str, Any]] = data[ATTR_VEHICLE]
    coordinators: dict[str, VehicleCoordinator] = data[ATTR_COORDINATOR][ATTR_VEHICLE]

    entities = [
        RivianClimateEntity(coordinators[vehicle_id], entry, CLIMATE, vehicle)
        for vehicle_id, vehicle in vehicles.items()
        if vehicle.get("phone_identity_id")
    ]
    async_add_entities(entities)


class RivianClimateEntity(RivianVehicleControlEntity, ClimateEntity):
    """Representation of a Rivian climate entity."""

    _attr_hvac_mode = HVACMode.OFF
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT_COOL, HVACMode.HEAT]
    _attr_max_temp = 29
    _attr_min_temp = 16
    _attr_precision = PRECISION_WHOLE
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_OFF
        | ClimateEntityFeature.TURN_ON
    )
    _attr_target_temperature_step = PRECISION_WHOLE
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS

    def __init__(
        self,
        coordinator: VehicleCoordinator,
        config_entry: ConfigEntry,
        description: ClimateEntityDescription,
        vehicle: dict[str, Any],
    ) -> None:
        """Create the climate entity, with the presets the vehicle supports."""
        super().__init__(coordinator, config_entry, description, vehicle)
        features = vehicle.get("supported_features", [])
        self._attr_preset_modes = [
            "LO",
            "HI",
            DEFROST_DEFOG,
            *([PET_COMFORT] if "PET_COMFORT_CONTROL" in features else []),
            *([CLIMATE_HOLD] if "CLM_HOLD" in features else []),
        ]

    @property
    def native_current_temperature(self) -> float | None:
        """Return the current temperature."""
        return self._get_value("cabinClimateInteriorTemperature")

    @property
    def native_target_temperature(self) -> float | None:
        """Return the temperature we try to reach."""
        return self._get_value("hvacTargetTemperature")

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Return hvac operation mode."""
        if self._defrosting:
            return HVACMode.HEAT
        if (
            self._get_value("cabinPreconditioningType") != "none"
            # Pet mode and climate hold keep the cabin conditioned too
            or self._get_value("petModeStatus") == "on"
            or self._get_value("climateHoldStatus") == "on"
        ):
            return HVACMode.HEAT_COOL
        return HVACMode.OFF

    @property
    def preset_mode(self) -> str | None:
        """Return the current preset mode."""
        if self._get_value("petModeStatus") == "on":
            return PET_COMFORT
        if self._get_value("climateHoldStatus") == "on":
            return CLIMATE_HOLD
        if self._defrosting:
            return DEFROST_DEFOG
        return {0: "LO", 63.5: "HI"}.get(self.native_target_temperature)

    @property
    def _defrosting(self) -> bool:
        """Return whether defrost/defog is on."""
        return self._get_value("defrostDefogStatus") != "off"

    def _check_pet_comfort_off(self) -> None:
        """Raise if pet comfort is on: only its switch turns it off.

        The Rivian app asks to confirm there are no pets inside first.
        """
        if self._get_value("petModeStatus") == "on":
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="pet_comfort_on"
            )

    async def _end_climate_hold(self) -> bool:
        """End a climate hold, which stopping preconditioning leaves running.

        Return whether one was running.
        """
        if self._get_value("climateHoldStatus") != "on":
            return False
        await self.coordinator.send_parallax_command(
            SET_CLIMATE_HOLD_RVM, encode_climate_hold(False)
        )
        return True

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new target hvac mode."""
        if hvac_mode == HVACMode.HEAT:
            await self.async_set_preset_mode(DEFROST_DEFOG)
            return
        if hvac_mode == HVACMode.OFF:
            self._check_pet_comfort_off()
            await self._end_climate_hold()
        await self.coordinator.send_vehicle_command(
            command=VehicleCommand.VEHICLE_CABIN_PRECONDITION_DISABLE
            if hvac_mode == HVACMode.OFF
            else VehicleCommand.VEHICLE_CABIN_PRECONDITION_ENABLE
        )

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set new preset mode."""
        if preset_mode != PET_COMFORT:
            self._check_pet_comfort_off()
        ended_hold = False
        if preset_mode not in (PET_COMFORT, CLIMATE_HOLD):
            ended_hold = await self._end_climate_hold()
        if preset_mode == PET_COMFORT:
            await self.coordinator.send_parallax_command(
                SET_PET_MODE_RVM, encode_pet_mode(True)
            )
            return
        if preset_mode == CLIMATE_HOLD:
            await self.coordinator.send_parallax_command(
                SET_CLIMATE_HOLD_RVM, encode_climate_hold(True)
            )
            return
        if preset_mode == DEFROST_DEFOG:
            await self.coordinator.send_vehicle_command(
                command=VehicleCommand.CABIN_HVAC_DEFROST_DEFOG, params={"level": 1}
            )
            return
        if ended_hold:
            # The hold kept the cabin conditioned, without preconditioning
            await self.coordinator.send_vehicle_command(
                command=VehicleCommand.VEHICLE_CABIN_PRECONDITION_ENABLE
            )
        await self.async_set_temperature(
            temperature={"LO": 0, "HI": 63.5}.get(preset_mode)
        )

    async def async_set_temperature(self, **kwargs) -> None:
        """Set new target temperature."""
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is None:
            return
        if self._defrosting:
            # need to turn off defrost/defog before we can adjust
            await self.coordinator.send_vehicle_command(
                command=VehicleCommand.CABIN_HVAC_DEFROST_DEFOG, params={"level": 0}
            )
        if self.hvac_mode == HVACMode.OFF:
            # must turn on preconditioning to adjust temperature
            await self.coordinator.send_vehicle_command(
                command=VehicleCommand.VEHICLE_CABIN_PRECONDITION_ENABLE
            )
        await self.coordinator.send_vehicle_command(
            command=VehicleCommand.CABIN_PRECONDITIONING_SET_TEMP,
            params={"HVAC_set_temp": temperature},
        )
