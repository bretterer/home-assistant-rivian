"""Test configuration and fixtures for Rivian integration tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC
from enum import StrEnum
import inspect
import os
import sys
import tempfile
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

    def __call__(self, value: Any = None, *args: Any, **kwargs: Any) -> Any:
        # An instance used as a voluptuous validator (e.g. a mocked HA
        # selector inside a schema) passes the value through, so module-level
        # schemas compile under the real voluptuous as CI installs it.
        return value


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
                # A private temp dir, never a real "/config": setup creates the
                # analytics DB under it (on Linux CI "/config" isn't writable,
                # and on Windows it left a stray C:\config behind).
                config_dir = tempfile.mkdtemp(prefix="rivian-test-config-")
                self.config.config_dir = config_dir
                self.config.path = lambda *p: os.path.join(config_dir, *p)
                # No components (e.g. "recorder") loaded by default, so
                # statistics.py's `"recorder" not in hass.config.components`
                # guard deterministically skips long-term statistics writes.
                self.config.components = []
                self.services = MockServiceRegistry()
                self.bus = MagicMock()
                self.states = MagicMock()
                # Sentinel that never matches a real threading.get_ident() value,
                # so AnalyticsDatabase's executor-thread guard never fires when
                # tests call its (normally executor-bound) methods directly from
                # the main test thread. Tests that specifically exercise the
                # guard override this to threading.get_ident().
                self.loop_thread_id: int = -1

            def async_create_task(self, target: Any, *args: Any, **kwargs: Any) -> Any:
                if asyncio.iscoroutine(target):
                    try:
                        return asyncio.create_task(target)
                    except RuntimeError:
                        return asyncio.run(target)
                return target

            def async_create_background_task(
                self, target: Any, *args: Any, **kwargs: Any
            ) -> Any:
                return self.async_create_task(target, *args, **kwargs)

            async def async_add_executor_job(self, target: Any, *args: Any) -> Any:
                loop = asyncio.get_running_loop()
                return await loop.run_in_executor(None, target, *args)

        def callback(func: Any) -> Any:
            # Like homeassistant.core.callback: mark it, so tests can check
            # an event listener runs on the loop (not in a worker thread).
            func._hass_callback = True
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

        class MockConfigFlow:
            """Mock ConfigFlow base class tolerating `class Foo(ConfigFlow, domain=...)`."""

            def __init_subclass__(cls, **kwargs: Any) -> None:
                super().__init_subclass__()

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

        config_entries_mod.ConfigFlow = MockConfigFlow  # type: ignore[attr-defined]
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
        helpers_mod.issue_registry.async_create_issue = MagicMock()  # type: ignore[attr-defined]
        helpers_mod.issue_registry.async_delete_issue = MagicMock()  # type: ignore[attr-defined]

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

        # helpers.selector / helpers.schema_config_entry_flow / data_entry_flow:
        # these back config_flow.py (imported transitively by __init__.py). Their
        # attribute access already falls back to SubscriptableMock via
        # DynamicModule.__getattr__, but Python's import machinery needs a real
        # sys.modules entry for `from homeassistant.helpers.selector import X`
        # style imports to resolve at all.
        helpers_mod.selector = DynamicModule(
            "homeassistant.helpers.selector", is_package=False
        )
        # Mode enums are accessed as class attributes (e.g. SelectSelectorMode.DROPDOWN)
        # at config_flow.py import time; DynamicEnum resolves any attribute lookup.
        helpers_mod.selector.SelectSelectorMode = DynamicEnum  # type: ignore[attr-defined]
        helpers_mod.selector.NumberSelectorMode = DynamicEnum  # type: ignore[attr-defined]
        sys.modules["homeassistant.helpers.selector"] = helpers_mod.selector

        helpers_mod.schema_config_entry_flow = DynamicModule(
            "homeassistant.helpers.schema_config_entry_flow", is_package=False
        )
        sys.modules["homeassistant.helpers.schema_config_entry_flow"] = (
            helpers_mod.schema_config_entry_flow
        )

        data_entry_flow_mod = DynamicModule(
            "homeassistant.data_entry_flow", is_package=False
        )
        sys.modules["homeassistant.data_entry_flow"] = data_entry_flow_mod
        ha_mod.data_entry_flow = data_entry_flow_mod  # type: ignore[attr-defined]

        # helpers.config_validation
        cv_mod = DynamicModule(
            "homeassistant.helpers.config_validation", is_package=False
        )
        cv_mod.string = str  # type: ignore[attr-defined]
        cv_mod.boolean = bool  # type: ignore[attr-defined]
        cv_mod.ensure_list = lambda x: x if isinstance(x, list) else [x]  # type: ignore[attr-defined]
        cv_mod.positive_int = int  # type: ignore[attr-defined]
        cv_mod.url = str  # type: ignore[attr-defined]
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

        # helpers.dispatcher: a tiny working per-hass registry, so tests can
        # send a signal and see connected callbacks run.
        dispatcher_mod = DynamicModule(
            "homeassistant.helpers.dispatcher", is_package=False
        )

        def mock_async_dispatcher_connect(hass: Any, signal: str, target: Any) -> Any:
            listeners = hass.data.setdefault("_test_dispatcher", {})
            listeners.setdefault(signal, []).append(target)

            def _remove() -> None:
                if target in listeners.get(signal, []):
                    listeners[signal].remove(target)

            return _remove

        def mock_async_dispatcher_send(hass: Any, signal: str, *args: Any) -> None:
            for target in list(hass.data.get("_test_dispatcher", {}).get(signal, [])):
                result = target(*args)
                if inspect.iscoroutine(result):
                    asyncio.ensure_future(result)

        dispatcher_mod.async_dispatcher_connect = mock_async_dispatcher_connect  # type: ignore[attr-defined]
        dispatcher_mod.async_dispatcher_send = mock_async_dispatcher_send  # type: ignore[attr-defined]
        helpers_mod.dispatcher = dispatcher_mod
        sys.modules["homeassistant.helpers.dispatcher"] = dispatcher_mod

        # helpers.service: admin services register like ordinary ones here.
        service_mod = DynamicModule("homeassistant.helpers.service", is_package=False)

        def mock_async_register_admin_service(
            hass: Any, domain: str, service: str, handler: Any, schema: Any = None
        ) -> None:
            hass.services.async_register(domain, service, handler, schema=schema)
            hass.data.setdefault("_test_admin_services", set()).add((domain, service))

        service_mod.async_register_admin_service = mock_async_register_admin_service  # type: ignore[attr-defined]
        helpers_mod.service = service_mod
        sys.modules["homeassistant.helpers.service"] = service_mod

        # helpers.restore_state: select.py's RestoreEntity mixin.
        restore_mod = DynamicModule(
            "homeassistant.helpers.restore_state", is_package=False
        )

        class RestoreEntity:
            async def async_added_to_hass(self) -> None:
                return None

            async def async_get_last_state(self) -> Any:
                return None

            def async_on_remove(self, func: Any) -> None:
                return None

        restore_mod.RestoreEntity = RestoreEntity  # type: ignore[attr-defined]
        helpers_mod.restore_state = restore_mod
        sys.modules["homeassistant.helpers.restore_state"] = restore_mod

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

        # homeassistant.components.websocket_api: backs websocket_api.py
        # (registered from __init__.py). No test in this suite exercises the
        # WebSocket command handler itself; this only needs to import and
        # register without error.
        ws_api_mod = DynamicModule(
            "homeassistant.components.websocket_api", is_package=False
        )
        ws_api_mod.async_register_command = MagicMock()  # type: ignore[attr-defined]

        class _MockWSSchema:
            def extend(self, *args: Any, **kwargs: Any) -> Any:
                return self

        ws_api_mod.BASE_COMMAND_MESSAGE_SCHEMA = _MockWSSchema()  # type: ignore[attr-defined]
        ws_api_mod.websocket_command = lambda *a, **k: lambda fn: fn  # type: ignore[attr-defined]
        ws_api_mod.async_response = lambda fn: fn  # type: ignore[attr-defined]

        def _mock_require_admin(func: Any) -> Any:
            """Mirror HA's require_admin: reject a connection.user with is_admin=False.

            A connection with no `.user` at all (as in tests that don't
            exercise admin-gating) is treated as allowed, so existing tests
            that never set `.user` are unaffected.
            """

            async def _wrapped(hass: Any, connection: Any, msg: dict[str, Any]) -> None:
                user = getattr(connection, "user", None)
                if user is not None and not getattr(user, "is_admin", True):
                    connection.send_error(msg["id"], "unauthorized", "Unauthorized")
                    return
                await func(hass, connection, msg)

            return _wrapped

        ws_api_mod.require_admin = _mock_require_admin  # type: ignore[attr-defined]
        sys.modules["homeassistant.components.websocket_api"] = ws_api_mod
        comp_mod.websocket_api = ws_api_mod  # type: ignore[attr-defined]

        zone_mod = DynamicModule("homeassistant.components.zone", is_package=False)
        zone_mod.in_zone = lambda *args, **kwargs: True  # type: ignore[attr-defined]
        sys.modules["homeassistant.components.zone"] = zone_mod
        comp_mod.zone = zone_mod  # type: ignore[attr-defined]

        # homeassistant.util.dt: backs statistics.py (imported transitively via
        # __init__.py -> history_backfill.py -> statistics.py). No test in this
        # suite targets statistics.py directly, so these only need to import
        # cleanly and behave reasonably if ever invoked.
        util_mod = DynamicModule("homeassistant.util", is_package=True)
        sys.modules["homeassistant.util"] = util_mod
        ha_mod.util = util_mod  # type: ignore[attr-defined]

        dt_mod = DynamicModule("homeassistant.util.dt", is_package=False)

        def _parse_datetime(value: str) -> Any:
            from datetime import datetime as _dt

            try:
                return _dt.fromisoformat(str(value))
            except (ValueError, TypeError):
                return None

        def _utc_from_timestamp(value: float) -> Any:
            from datetime import datetime as _dt

            return _dt.fromtimestamp(value, tz=UTC)

        def _utcnow() -> Any:
            from datetime import datetime as _dt

            return _dt.now(tz=UTC)

        dt_mod.UTC = UTC  # type: ignore[attr-defined]
        dt_mod.parse_datetime = _parse_datetime  # type: ignore[attr-defined]
        dt_mod.utc_from_timestamp = _utc_from_timestamp  # type: ignore[attr-defined]
        dt_mod.utcnow = _utcnow  # type: ignore[attr-defined]
        sys.modules["homeassistant.util.dt"] = dt_mod
        util_mod.dt = dt_mod  # type: ignore[attr-defined]

        # homeassistant.components.recorder: backs statistics.py similarly.
        recorder_pkg_mod = DynamicModule(
            "homeassistant.components.recorder", is_package=True
        )
        sys.modules["homeassistant.components.recorder"] = recorder_pkg_mod
        comp_mod.recorder = recorder_pkg_mod  # type: ignore[attr-defined]

        recorder_models_mod = DynamicModule(
            "homeassistant.components.recorder.models", is_package=False
        )
        sys.modules["homeassistant.components.recorder.models"] = recorder_models_mod
        recorder_pkg_mod.models = recorder_models_mod  # type: ignore[attr-defined]

        recorder_statistics_mod = DynamicModule(
            "homeassistant.components.recorder.statistics", is_package=False
        )
        recorder_statistics_mod.async_add_external_statistics = MagicMock()  # type: ignore[attr-defined]
        recorder_statistics_mod.get_last_statistics = MagicMock(return_value={})  # type: ignore[attr-defined]
        recorder_statistics_mod.statistics_during_period = MagicMock(return_value={})  # type: ignore[attr-defined]
        sys.modules["homeassistant.components.recorder.statistics"] = (
            recorder_statistics_mod
        )
        recorder_pkg_mod.statistics = recorder_statistics_mod  # type: ignore[attr-defined]

        # recorder.get_instance(hass): a fake recorder instance whose
        # async_add_executor_job runs the target inline (via the real event
        # loop's executor) and whose async_clear_statistics is a no-op mock,
        # so statistics.py's rewrite/clear paths import and run cleanly even
        # when a test doesn't override them.
        class _FakeRecorderInstance:
            async def async_add_executor_job(self, target: Any, *args: Any) -> Any:
                import asyncio as _asyncio

                loop = _asyncio.get_running_loop()
                return await loop.run_in_executor(None, target, *args)

            def async_clear_statistics(self, statistic_ids: list[str]) -> None:
                pass

        recorder_pkg_mod.get_instance = MagicMock(  # type: ignore[attr-defined]
            return_value=_FakeRecorderInstance()
        )

    # Mock rivian client library if not present
    if "rivian" not in sys.modules:
        rivian_mod = DynamicModule("rivian", is_package=True)
        rivian_mod.Rivian = SubscriptableMock  # type: ignore[attr-defined]
        rivian_mod.VehicleCommand = SubscriptableMock  # type: ignore[attr-defined]
        exc_submod = DynamicExceptionModule("rivian.exceptions", is_package=False)
        rivian_mod.exceptions = exc_submod  # type: ignore[attr-defined]
        sys.modules["rivian"] = rivian_mod
        sys.modules["rivian.exceptions"] = exc_submod

        utils_submod = DynamicModule("rivian.utils", is_package=False)

        def _mock_generate_key_pair(*args: Any, **kwargs: Any) -> tuple[str, str]:
            return ("mock_public_key", "mock_private_key")

        utils_submod.generate_key_pair = _mock_generate_key_pair  # type: ignore[attr-defined]
        rivian_mod.utils = utils_submod  # type: ignore[attr-defined]
        sys.modules["rivian.utils"] = utils_submod


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
