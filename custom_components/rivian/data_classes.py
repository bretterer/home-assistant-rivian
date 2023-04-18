"""Rivian Specific Data Classes"""
from __future__ import annotations

from ast import Expression
from dataclasses import dataclass

from homeassistant.components.binary_sensor import BinarySensorEntityDescription
from homeassistant.components.sensor import SensorEntityDescription
from homeassistant.helpers.entity import EntityDescription


@dataclass
class RivianSensorRequiredKeysMixin:
    """A class that describes Rivian sensor required keys."""

    field: str


@dataclass
class RivianSensorEntityDescription(
    SensorEntityDescription, RivianSensorRequiredKeysMixin
):
    """Rivian Sensor Entity Description"""

    value_lambda: Expression | None = None
    old_key: str | None = None


@dataclass
class RivianBinarySensorRequiredKeysMixin:
    """A class that describes Rivian binary sensor required keys."""

    field: str | set[str]


@dataclass
class RivianBinarySensorEntityDescription(
    BinarySensorEntityDescription, RivianBinarySensorRequiredKeysMixin
):
    """Describes a Rivian binary sensor."""

    # Value to consider binary sensor to be "on"
    on_value: bool | float | int | str | list[str] = True
    negate: bool = False
    old_key: str | None = None


@dataclass
class RivianTrackerEntityDescription(EntityDescription):
    """Rivian tracker entity Description."""

    old_key: str | None = None


@dataclass
class RivianWallboxRequiredKeysMixin:
    """A class that describes Rivian wallbox sensor entity required keys."""

    field: str


@dataclass
class RivianWallboxSensorEntityDescription(
    SensorEntityDescription, RivianWallboxRequiredKeysMixin
):
    """A class that describes Rivian wallbox sensor entities."""
