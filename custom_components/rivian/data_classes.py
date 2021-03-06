"""Rivian Specific Data Classes"""
from __future__ import annotations
from ast import Expression

from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription,
)


@dataclass
class RivianSensorEntity(SensorEntity):
    """Rivian Specific Sensor Entity"""

    entity_description: SensorEntityDescription
    value_lambda: Expression | None = None


@dataclass
class RivianSensorEntityDescription(SensorEntityDescription):
    """Rivian Sensor Entity Description"""
