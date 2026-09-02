"""Test configuration and fixtures for Rivian integration tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
import inspect
import sys
import types
from typing import Any, Generic, TypeVar
from unittest.mock import AsyncMock, MagicMock

import pytest

T = TypeVar("T")


class SubscriptableMock:
    """Mock class that supports generic subscription (e.g. Cls[T])."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def __class_getitem__(cls, item: Any) -> Any:
        return cls


class DynamicEnumMeta(type):
    """Metaclass that provides attributes dynamically as lowercase strings."""

    def __getattr__(cls, name: str) -> str:
        return name.lower()


class DynamicEnum(metaclass=DynamicEnumMeta):
    """Base dynamic class for device and state classes."""


class DynamicModule(types.ModuleType):
    """Module type that returns MagicMock or classes for undefined attributes."""

    def __init__(self, name: str, is_package: bool = True) -> None:
        super().__init__(name)
        if is_package:
            self.__path__: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return SubscriptableMock


class DynamicExceptionModule(types.ModuleType):
    """Module type that returns Exception subclasses for undefined attributes."""

    def __init__(self, name: str, is_package: bool = False) -> None:
        super().__init__(name)
        if is_package:
            self.__path__: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return Exception


def _setup_mock_environment() -> None:
    """Mock external libraries if not installed in the current environment."""
    if "voluptuous" not in sys.modules:
        try:
            import voluptuous  # noqa: F401
        except ImportError:
            vol_mod = DynamicModule("voluptuous", is_package=False)

            class Schema:
                def __init__(
                    self, schema: Any = None, *args: Any, **kwargs: Any
                ) -> None:
                    self.schema = schema

                def __call__(self, val: Any) -> Any:
                    return val

            def optional_fn(key: Any, *args: Any, **kwargs: Any) -> Any:
                return key

            def identity(val: Any, *args: Any, **kwargs: Any) -> Any:
                return val

            vol_mod.Schema = Schema  # type: ignore[attr-defined]
            vol_mod.Required = optional_fn  # type: ignore[attr-defined]
            vol_mod.Optional = optional_fn  # type: ignore[attr-defined]
            vol_mod.Coerce = identity  # type: ignore[attr-defined]
            vol_mod.In = identity  # type: ignore[attr-defined]
            vol_mod.All = identity  # type: ignore[attr-defined]
            vol_mod.Any = identity  # type: ignore[attr-defined]
            sys.modules["voluptuous"] = vol_mod

    if "homeassistant" not in sys.modules:
        ha_mod = DynamicModule("homeassistant", is_package=True)
        sys.modules["homeassistant"] = ha_mod

        # core
        core_mod = DynamicModule("homeassistant.core", is_package=False)

        class MockServiceCall:
            """Mock Home Assistant ServiceCall."""

            def __init__(self, data: dict[str, Any] | None = None) -> None:
                self.data = data or {}

        class MockServiceRegistry:
            """Mock Home Assistant ServiceRegistry."""

            def __init__(self) -> None:
                self.services: dict[tuple[str, str], Any] = {}

            def has_service(self, domain: str, service: str) -> bool:
                return (domain, service) in self.services

            def async_register(
                self, domain: str, service: str, handler: Any, schema: Any = None
            ) -> None:
                self.services[(domain, service)] = handler

            def async_remove(self, domain: str, service: str) -> None:
                self.services.pop((domain, service), None)

            async def async_call(
                self,
                domain: str,
                service: str,
                service_data: dict[str, Any] | None = None,
                blocking: bool = True,
            ) -> None:
                handler = self.services.get((domain, service))
                if handler:
                    call = MockServiceCall(data=service_data)
                    if inspect.iscoroutinefunction(handler):
                        await handler(call)
                    else:
                        handler(call)

        class MockHomeAssistant:
            """Mock Home Assistant core instance."""

            def __init__(self) -> None:
                self.data: dict[str, Any] = {}
                self.loop = None
                self.config = MagicMock()
                self.config.config_dir = "/config"
                self.config.path = lambda p: f"/config/{p}"
                self.services = MockServiceRegistry()

            def async_create_task(self, target: Any, *args: Any, **kwargs: Any) -> Any:
                if asyncio.iscoroutine(target):
                    try:
                        return asyncio.create_task(target)
                    except RuntimeError:
                        return asyncio.run(target)
                return target

            async def async_add_executor_job(self, target: Any, *args: Any) -> Any:
                loop = asyncio.get_running_loop()
                return await loop.run_in_executor(None, target, *args)

        def callback(func: Any) -> Any:
            return func

        core_mod.HomeAssistant = MockHomeAssistant  # type: ignore[attr-defined]
        core_mod.ServiceCall = MockServiceCall  # type: ignore[attr-defined]
        core_mod.callback = callback  # type: ignore[attr-defined]
        sys.modules["homeassistant.core"] = core_mod
        ha_mod.core = core_mod  # type: ignore[attr-defined]

        # const
        const_mod = DynamicModule("homeassistant.const", is_package=False)

        class Platform(StrEnum):
            BINARY_SENSOR = "binary_sensor"
            BUTTON = "button"
            CLIMATE = "climate"
            COVER = "cover"
            DEVICE_TRACKER = "device_tracker"
            IMAGE = "image"
            LOCK = "lock"
            NUMBER = "number"
            SELECT = "select"
            SENSOR = "sensor"
            SWITCH = "switch"
            TIME = "time"
            UPDATE = "update"

        const_mod.Platform = Platform  # type: ignore[attr-defined]
        const_mod.DEGREE = "°"  # type: ignore[attr-defined]
        const_mod.PERCENTAGE = "%"  # type: ignore[attr-defined]
        const_mod.CONF_EMAIL = "email"  # type: ignore[attr-defined]
        const_mod.CONF_LATITUDE = "latitude"  # type: ignore[attr-defined]
        const_mod.CONF_LONGITUDE = "longitude"  # type: ignore[attr-defined]
        const_mod.CONF_ZONE = "zone"  # type: ignore[attr-defined]
        const_mod.STATE_UNAVAILABLE = "unavailable"  # type: ignore[attr-defined]
        const_mod.EntityCategory = DynamicEnum  # type: ignore[attr-defined]
        const_mod.UnitOfElectricCurrent = DynamicEnum  # type: ignore[attr-defined]
        const_mod.UnitOfElectricPotential = DynamicEnum  # type: ignore[attr-defined]
        const_mod.UnitOfEnergy = DynamicEnum  # type: ignore[attr-defined]
        const_mod.UnitOfLength = DynamicEnum  # type: ignore[attr-defined]
        const_mod.UnitOfPower = DynamicEnum  # type: ignore[attr-defined]
        const_mod.UnitOfPressure = DynamicEnum  # type: ignore[attr-defined]
        const_mod.UnitOfSpeed = DynamicEnum  # type: ignore[attr-defined]
        const_mod.UnitOfTemperature = DynamicEnum  # type: ignore[attr-defined]
        const_mod.UnitOfTime = DynamicEnum  # type: ignore[attr-defined]
        sys.modules["homeassistant.const"] = const_mod
        ha_mod.const = const_mod  # type: ignore[attr-defined]

        # exceptions
        exc_mod = DynamicExceptionModule("homeassistant.exceptions", is_package=False)
        exc_mod.ConfigEntryNotReady = type("ConfigEntryNotReady", (Exception,), {})  # type: ignore[attr-defined]
        exc_mod.ConfigEntryAuthFailed = type("ConfigEntryAuthFailed", (Exception,), {})  # type: ignore[attr-defined]
        sys.modules["homeassistant.exceptions"] = exc_mod
        ha_mod.exceptions = exc_mod  # type: ignore[attr-defined]

        # config_entries
        config_entries_mod = DynamicModule(
            "homeassistant.config_entries", is_package=False
        )
        config_entries_mod.ConfigEntry = SubscriptableMock  # type: ignore[attr-defined]
        sys.modules["homeassistant.config_entries"] = config_entries_mod
        ha_mod.config_entries = config_entries_mod  # type: ignore[attr-defined]

        # helpers
        helpers_mod = DynamicModule("homeassistant.helpers", is_package=True)

        @dataclass(kw_only=True)
        class EntityDescription:
            key: str = ""
            device_class: Any = None
            entity_category: Any = None
            entity_registry_enabled_default: bool = True
            entity_registry_visible_default: bool = True
            has_entity_name: bool = False
            icon: str | None = None
            name: str | None = None
            translation_key: str | None = None
            translation_placeholders: dict[str, str] | None = None
            unit_of_measurement: str | None = None
            options: list[str] | None = None
            state_class: Any = None
            native_unit_of_measurement: str | None = None
            suggested_display_precision: int | None = None
            suggested_unit_of_measurement: str | None = None

        class DeviceInfo(dict[str, Any]):
            """Mock DeviceInfo."""

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                for k, v in kwargs.items():
                    self[k] = v

            def __getattr__(self, item: str) -> Any:
                return self.get(item)

        class MockEntity:
            """Mock base Entity."""

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                self.hass: Any = None
                self._attr_has_entity_name: bool = True
                self._attr_unique_id: str | None = None
                self._attr_device_info: Any = None
                self._attr_extra_state_attributes: dict[str, Any] = {}

            @property
            def has_entity_name(self) -> bool:
                return getattr(self, "_attr_has_entity_name", True)

            @property
            def unique_id(self) -> str | None:
                return getattr(self, "_attr_unique_id", None)

            @property
            def device_info(self) -> Any:
                return getattr(self, "_attr_device_info", None)

            def async_write_ha_state(self) -> None:
                pass

            def async_on_remove(self, func: Any) -> None:
                pass

            async def async_added_to_hass(self) -> None:
                pass

        helpers_mod.entity = DynamicModule(
            "homeassistant.helpers.entity", is_package=False
        )
        helpers_mod.entity.EntityDescription = EntityDescription  # type: ignore[attr-defined]
        helpers_mod.entity.Entity = MockEntity  # type: ignore[attr-defined]
        helpers_mod.entity.DeviceInfo = DeviceInfo  # type: ignore[attr-defined]

        helpers_mod.device_registry = DynamicModule(
            "homeassistant.helpers.device_registry", is_package=False
        )
        helpers_mod.device_registry.DeviceEntry = SubscriptableMock  # type: ignore[attr-defined]
        helpers_mod.issue_registry = DynamicModule(
            "homeassistant.helpers.issue_registry", is_package=False
        )
        helpers_mod.issue_registry.IssueSeverity = MagicMock()  # type: ignore[attr-defined]
        helpers_mod.issue_registry.async_create_issue = AsyncMock()  # type: ignore[attr-defined]
        helpers_mod.issue_registry.async_delete_issue = AsyncMock()  # type: ignore[attr-defined]

        class MockDataUpdateCoordinator(Generic[T]):
            """Mock DataUpdateCoordinator."""

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

        class MockCoordinatorEntity(MockEntity, Generic[T]):
            """Mock CoordinatorEntity."""

            def __init__(
                self, coordinator: Any = None, *args: Any, **kwargs: Any
            ) -> None:
                super().__init__(*args, **kwargs)
                self.coordinator = coordinator

        helpers_mod.update_coordinator = DynamicModule(
            "homeassistant.helpers.update_coordinator", is_package=False
        )
        helpers_mod.update_coordinator.DataUpdateCoordinator = (  # type: ignore[attr-defined]
            MockDataUpdateCoordinator
        )
        helpers_mod.update_coordinator.CoordinatorEntity = (  # type: ignore[attr-defined]
            MockCoordinatorEntity
        )
        helpers_mod.update_coordinator.UpdateFailed = type(
            "UpdateFailed", (Exception,), {}
        )  # type: ignore[attr-defined]

        helpers_mod.aiohttp_client = DynamicModule(
            "homeassistant.helpers.aiohttp_client", is_package=False
        )
        helpers_mod.aiohttp_client.async_get_clientsession = MagicMock()  # type: ignore[attr-defined]

        # helpers.config_validation
        cv_mod = DynamicModule(
            "homeassistant.helpers.config_validation", is_package=False
        )
        cv_mod.string = str  # type: ignore[attr-defined]
        cv_mod.boolean = bool  # type: ignore[attr-defined]
        cv_mod.ensure_list = lambda x: x if isinstance(x, list) else [x]  # type: ignore[attr-defined]
        cv_mod.positive_int = int  # type: ignore[attr-defined]
        sys.modules["homeassistant.helpers.config_validation"] = cv_mod
        helpers_mod.config_validation = cv_mod

        # helpers.event
        event_mod = DynamicModule("homeassistant.helpers.event", is_package=False)

        def mock_async_call_later(
            hass: Any, delay: float, action: Any, *args: Any
        ) -> Any:
            loop = getattr(hass, "loop", None)
            if loop is None:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None

            if loop is not None:
                handle = loop.call_later(
                    delay,
                    lambda: (
                        asyncio.create_task(action(None))
                        if inspect.iscoroutinefunction(action)
                        else action(None)
                    ),
                )
                return handle.cancel

            mock_handle = MagicMock()
            return mock_handle

        event_mod.async_call_later = mock_async_call_later  # type: ignore[attr-defined]
        helpers_mod.event = event_mod
        sys.modules["homeassistant.helpers.event"] = event_mod

        helpers_mod.entity_platform = DynamicModule(
            "homeassistant.helpers.entity_platform", is_package=False
        )
        helpers_mod.entity_platform.AddEntitiesCallback = Any  # type: ignore[attr-defined]
        sys.modules["homeassistant.helpers.entity_platform"] = (
            helpers_mod.entity_platform
        )

        helpers_mod.typing = DynamicModule(
            "homeassistant.helpers.typing", is_package=False
        )
        helpers_mod.typing.StateType = Any  # type: ignore[attr-defined]
        sys.modules["homeassistant.helpers.typing"] = helpers_mod.typing

        sys.modules["homeassistant.helpers"] = helpers_mod
        sys.modules["homeassistant.helpers.entity"] = helpers_mod.entity
        sys.modules["homeassistant.helpers.device_registry"] = (
            helpers_mod.device_registry
        )
        sys.modules["homeassistant.helpers.issue_registry"] = helpers_mod.issue_registry
        sys.modules["homeassistant.helpers.update_coordinator"] = (
            helpers_mod.update_coordinator
        )
        sys.modules["homeassistant.helpers.aiohttp_client"] = helpers_mod.aiohttp_client
        ha_mod.helpers = helpers_mod  # type: ignore[attr-defined]

        # helpers.storage
        storage_mod = DynamicModule("homeassistant.helpers.storage", is_package=False)

        class MockStore(Generic[T]):
            """Mock Home Assistant storage Store."""

            def __init__(
                self,
                hass: Any,
                version: int,
                key: str,
                minor_version: int = 1,
                **kwargs: Any,
            ) -> None:
                self.hass = hass
                self.version = version
                self.key = key
                self.minor_version = minor_version
                self._data: Any = None
                self.async_load = AsyncMock(side_effect=self._mock_async_load)
                self.async_save = AsyncMock(side_effect=self._mock_async_save)
                self.async_remove = AsyncMock(side_effect=self._mock_async_remove)

            async def _mock_async_load(self) -> Any:
                return self._data

            async def _mock_async_save(self, data: Any) -> None:
                self._data = data

            async def _mock_async_remove(self) -> None:
                self._data = None

        storage_mod.Store = MockStore  # type: ignore[attr-defined]
        sys.modules["homeassistant.helpers.storage"] = storage_mod
        helpers_mod.storage = storage_mod  # type: ignore[attr-defined]

        # components
        comp_mod = DynamicModule("homeassistant.components", is_package=True)
        sys.modules["homeassistant.components"] = comp_mod
        ha_mod.components = comp_mod  # type: ignore[attr-defined]

        # diagnostics
        diag_mod = DynamicModule(
            "homeassistant.components.diagnostics", is_package=True
        )
        diag_util_mod = DynamicModule(
            "homeassistant.components.diagnostics.util", is_package=False
        )
        diag_util_mod.async_redact_data = lambda data, to_redact: data  # type: ignore[attr-defined]
        diag_mod.util = diag_util_mod  # type: ignore[attr-defined]
        sys.modules["homeassistant.components.diagnostics"] = diag_mod
        sys.modules["homeassistant.components.diagnostics.util"] = diag_util_mod
        comp_mod.diagnostics = diag_mod

        # Entity descriptions for platforms
        @dataclass(kw_only=True)
        class SensorEntityDescription(EntityDescription):
            pass

        @dataclass(kw_only=True)
        class BinarySensorEntityDescription(EntityDescription):
            pass

        @dataclass(kw_only=True)
        class ButtonEntityDescription(EntityDescription):
            pass

        @dataclass(kw_only=True)
        class CoverEntityDescription(EntityDescription):
            pass

        @dataclass(kw_only=True)
        class LockEntityDescription(EntityDescription):
            pass

        @dataclass(kw_only=True)
        class NumberEntityDescription(EntityDescription):
            native_max_value: float = 100.0
            native_min_value: float = 0.0
            native_step: float = 1.0

        @dataclass(kw_only=True)
        class SelectEntityDescription(EntityDescription):
            pass

        @dataclass(kw_only=True)
        class SwitchEntityDescription(EntityDescription):
            pass

        @dataclass(kw_only=True)
        class TimeEntityDescription(EntityDescription):
            pass

        @dataclass(kw_only=True)
        class UpdateEntityDescription(EntityDescription):
            pass

        descs: dict[str, type] = {
            "binary_sensor": BinarySensorEntityDescription,
            "button": ButtonEntityDescription,
            "climate": EntityDescription,
            "cover": CoverEntityDescription,
            "device_tracker": EntityDescription,
            "image": EntityDescription,
            "lock": LockEntityDescription,
            "number": NumberEntityDescription,
            "select": SelectEntityDescription,
            "sensor": SensorEntityDescription,
            "switch": SwitchEntityDescription,
            "time": TimeEntityDescription,
            "update": UpdateEntityDescription,
        }

        for comp_name, desc_cls in descs.items():
            submod = DynamicModule(
                f"homeassistant.components.{comp_name}", is_package=False
            )
            desc_attr_name = (
                "".join(part.capitalize() for part in comp_name.split("_"))
                + "EntityDescription"
            )
            setattr(submod, desc_attr_name, desc_cls)
            setattr(
                submod,
                f"{''.join(part.capitalize() for part in comp_name.split('_'))}DeviceClass",
                DynamicEnum,
            )
            submod.SensorStateClass = DynamicEnum  # type: ignore[attr-defined]
            sys.modules[f"homeassistant.components.{comp_name}"] = submod
            setattr(comp_mod, comp_name, submod)

        zone_mod = DynamicModule("homeassistant.components.zone", is_package=False)
        zone_mod.in_zone = lambda *args, **kwargs: True  # type: ignore[attr-defined]
        sys.modules["homeassistant.components.zone"] = zone_mod
        comp_mod.zone = zone_mod  # type: ignore[attr-defined]

    # Mock rivian client library if not present
    if "rivian" not in sys.modules:
        rivian_mod = DynamicModule("rivian", is_package=True)
        rivian_mod.Rivian = SubscriptableMock  # type: ignore[attr-defined]
        rivian_mod.VehicleCommand = SubscriptableMock  # type: ignore[attr-defined]
        exc_submod = DynamicExceptionModule("rivian.exceptions", is_package=False)
        rivian_mod.exceptions = exc_submod  # type: ignore[attr-defined]
        sys.modules["rivian"] = rivian_mod
        sys.modules["rivian.exceptions"] = exc_submod


_setup_mock_environment()


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers."""
    config.addinivalue_line("markers", "asyncio: mark test as asyncio coroutine")


def pytest_pyfunc_call(pyfuncitem: pytest.Function) -> bool | None:
    """Run async test functions with asyncio.run if pytest-asyncio is not active."""
    testfunction = pyfuncitem.obj
    if inspect.iscoroutinefunction(testfunction):
        argnames = pyfuncitem._fixtureinfo.argnames
        kwargs = {
            name: pyfuncitem.funcargs[name]
            for name in argnames
            if name in pyfuncitem.funcargs
        }
        asyncio.run(testfunction(**kwargs))
        return True
    return None


@pytest.fixture
def mock_hass() -> Any:
    """Fixture to provide a mock HomeAssistant instance."""
    from homeassistant.core import HomeAssistant

    return HomeAssistant()


@pytest.fixture
def mock_config_entry() -> MagicMock:
    """Mock ConfigEntry instance."""
    entry = MagicMock()
    entry.entry_id = "test_entry_rivian_123"
    entry.options = {}
    entry.add_update_listener = MagicMock()
    entry.async_on_unload = MagicMock()
    return entry
