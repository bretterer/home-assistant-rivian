"""Rivian Specific Data Classes"""
from __future__ import annotations

from ast import Expression
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

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


@dataclass
class RivianButtonRequiredKeysMixin:
    """A class that describes Rivian button required keys."""

    press_fn: Callable[[VehicleCoordinator], Awaitable[None]]


@dataclass
class RivianButtonEntityDescription(
    ButtonEntityDescription, RivianButtonRequiredKeysMixin
):
    """Rivian button entity description."""


@dataclass
class RivianCoverRequiredKeysMixin:
    """A class that describes Rivian cover required keys."""

    is_closed: Callable[[VehicleCoordinator], bool]
    close_cover: Callable[[VehicleCoordinator], Awaitable[None]]
    open_cover: Callable[[VehicleCoordinator], Awaitable[None]]


@dataclass
class RivianCoverEntityDescription(
    CoverEntityDescription, RivianCoverRequiredKeysMixin
):
    """Rivian cover entity description."""


@dataclass
class RivianLockRequiredKeysMixin:
    """A class that describes Rivian lock required keys."""

    is_locked: Callable[[VehicleCoordinator], bool]
    lock: Callable[[VehicleCoordinator], Awaitable[None]]
    unlock: Callable[[VehicleCoordinator], Awaitable[None]]


@dataclass
class RivianLockEntityDescription(LockEntityDescription, RivianLockRequiredKeysMixin):
    """Rivian lock entity description."""


@dataclass
class RivianNumberRequiredKeysMixin:
    """A class that describes Rivian number required keys."""

    field: str
    set_fn: Callable[[VehicleCoordinator, float], Awaitable[None]]


@dataclass
class RivianNumberEntityDescription(
    NumberEntityDescription, RivianNumberRequiredKeysMixin
):
    """Rivian number entity description."""


@dataclass
class RivianSelectRequiredKeysMixin:
    """A class that describes Rivian select required keys."""

    field: str
    select: Callable[[VehicleCoordinator, str], Awaitable[None]]


@dataclass
class RivianSelectEntityDescription(
    SelectEntityDescription, RivianSelectRequiredKeysMixin
):
    """Rivian select entity description."""


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
class RivianSwitchRequiredKeysMixin:
    """A class that describes Rivian switch required keys."""

    is_on: Callable[[VehicleCoordinator], bool]
    turn_off: Callable[[VehicleCoordinator], Awaitable[None]]
    turn_on: Callable[[VehicleCoordinator], Awaitable[None]]


@dataclass
class RivianSwitchEntityDescription(
    SwitchEntityDescription, RivianSwitchRequiredKeysMixin
):
    """Rivian switch entity description."""

    available: Callable[[VehicleCoordinator], bool] | None = None


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
