"""Rivian Specific Data Classes"""
from __future__ import annotations

from ast import Expression
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.components.sensor import SensorEntity, SensorEntityDescription


@dataclass
class RivianSensorEntity(SensorEntity):
    """Rivian Specific Sensor Entity"""

    entity_description: SensorEntityDescription
    value_lambda: Expression | None = None


@dataclass
class RivianSensorEntityDescription(SensorEntityDescription):
    """Rivian Sensor Entity Description"""


@dataclass
class RivianBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describes a Rivian binary sensor."""

    # Value to consider binary sensor to be "on"
    on_value: bool | float | int | str = True
    negate: bool = False


@dataclass
class RivianBinarySensorEntity(BinarySensorEntity):
    """Rivian Specific Sensor Entity"""

    entity_description: RivianBinarySensorEntityDescription


@dataclass
class RivianWallboxRequiredKeysMixin:
    """A class that describes Rivian wallbox sensor entity required keys."""

    field: str


@dataclass
class RivianWallboxSensorEntityDescription(
    SensorEntityDescription, RivianWallboxRequiredKeysMixin
):
    """A class that describes Rivian wallbox sensor entities."""
