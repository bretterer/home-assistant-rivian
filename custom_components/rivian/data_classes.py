"""Rivian Specific Data Classes"""

from __future__ import annotations

from ast import Expression
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import BinarySensorEntityDescription
from homeassistant.components.button import ButtonEntityDescription
from homeassistant.components.cover import CoverEntityDescription
from homeassistant.components.lock import LockEntityDescription
from homeassistant.components.number import NumberEntityDescription
from homeassistant.components.select import SelectEntityDescription
from homeassistant.components.sensor import SensorEntityDescription
from homeassistant.components.switch import SwitchEntityDescription
from homeassistant.helpers.entity import EntityDescription

if TYPE_CHECKING:
    from .coordinator import VehicleCoordinator


@dataclass(kw_only=True)
class RivianVehicleControlAvailableMixin:
    """Rivian vehicle control available mixin."""

    available: Callable[[VehicleCoordinator], bool] | None = None


@dataclass(kw_only=True)
class RivianBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describes a Rivian binary sensor."""

    field: str | set[str]
    # Value to consider binary sensor to be "on"
    on_value: bool | float | int | str | list[str] = True
    negate: bool = False


@dataclass(kw_only=True)
class RivianButtonEntityDescription(
    ButtonEntityDescription, RivianVehicleControlAvailableMixin
):
    """Rivian button entity description."""

    press_fn: Callable[[VehicleCoordinator], Awaitable[None]]


@dataclass(kw_only=True)
class RivianCoverEntityDescription(CoverEntityDescription):
    """Rivian cover entity description."""

    is_closed: Callable[[VehicleCoordinator], bool]
    close_cover: Callable[[VehicleCoordinator], Awaitable[None]]
    open_cover: Callable[[VehicleCoordinator], Awaitable[None]]


@dataclass(kw_only=True)
class RivianLockEntityDescription(LockEntityDescription):
    """Rivian lock entity description."""

    is_locked: Callable[[VehicleCoordinator], bool]
    lock: Callable[[VehicleCoordinator], Awaitable[None]]
    unlock: Callable[[VehicleCoordinator], Awaitable[None]]


@dataclass(kw_only=True)
class RivianNumberEntityDescription(NumberEntityDescription):
    """Rivian number entity description."""

    field: str
    set_fn: Callable[[VehicleCoordinator, float], Awaitable[None]]


@dataclass(kw_only=True)
class RivianSelectEntityDescription(SelectEntityDescription):
    """Rivian select entity description."""

    field: str
    select: Callable[[VehicleCoordinator, str], Awaitable[None]]


@dataclass(kw_only=True)
class RivianSensorEntityDescription(SensorEntityDescription):
    """Rivian Sensor Entity Description"""

    field: str
    value_fn: Callable[[VehicleCoordinator], Any] | None = None
    value_lambda: Expression | None = None


@dataclass(kw_only=True)
class RivianSwitchEntityDescription(
    SwitchEntityDescription, RivianVehicleControlAvailableMixin
):
    """Rivian switch entity description."""

    is_on: Callable[[VehicleCoordinator], bool]
    turn_off: Callable[[VehicleCoordinator], Awaitable[None]]
    turn_on: Callable[[VehicleCoordinator], Awaitable[None]]


@dataclass(kw_only=True)
class RivianTrackerEntityDescription(EntityDescription):
    """Rivian tracker entity Description."""


@dataclass(kw_only=True)
class RivianWallboxSensorEntityDescription(SensorEntityDescription):
    """A class that describes Rivian wallbox sensor entities."""

    field: str
