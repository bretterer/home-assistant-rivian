"""Tests for the Rivian actions."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import voluptuous as vol

from custom_components.rivian import services
from homeassistant.exceptions import Unauthorized, UnknownUser

DEVICE_ID = "vehicle-device"
ENTITIES = {
    DEVICE_ID: ["switch.r1t_departure_schedule_weekdays", "sensor.r1t_battery"],
    "other-device": ["switch.other_vehicle_departure_schedule"],
}


def _user(
    allowed: set[str] | None = None,
    admin: bool = False,
    policies: frozenset[str] = frozenset({"control"}),
) -> SimpleNamespace:
    """A user with the given policies on the allowed entities."""
    allowed = allowed or set()
    return SimpleNamespace(
        is_admin=admin,
        permissions=SimpleNamespace(
            check_entity=lambda entity_id, policy: (
                entity_id in allowed and policy in policies
            )
        ),
    )


def _call(user_id: str | None) -> SimpleNamespace:
    # keyed by the module's own constants, which the test harness may mock
    return SimpleNamespace(
        context=SimpleNamespace(user_id=user_id),
        data={
            services.ATTR_DEVICE_ID: DEVICE_ID,
            services.ATTR_SCHEDULE_ID: "schedule",
        },
    )


@pytest.fixture
def hass(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Home Assistant stand-in with the registries the checks use."""
    monkeypatch.setattr(
        services,
        "er",
        SimpleNamespace(
            async_get=lambda hass: "registry",
            async_entries_for_device=lambda registry, device_id: [
                SimpleNamespace(entity_id=entity_id)
                for entity_id in ENTITIES.get(device_id, [])
            ],
        ),
        raising=False,
    )
    return SimpleNamespace(auth=SimpleNamespace(async_get_user=AsyncMock()))


@pytest.fixture
def coordinator(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    coordinator = MagicMock()
    coordinator.delete_departure_schedule = AsyncMock()
    monkeypatch.setattr(
        services, "_get_coordinator", lambda hass, device_id: coordinator
    )
    return coordinator


def _register(hass: SimpleNamespace) -> tuple[dict[str, Any], dict[str, Any]]:
    """Set up the actions; return their handlers and schemas by name."""
    handlers: dict[str, Any] = {}
    schemas: dict[str, Any] = {}

    def register(
        domain: str, service: str, handler: Any, schema: Any, **kwargs: Any
    ) -> None:
        handlers[service] = handler
        schemas[service] = schema

    hass.services = SimpleNamespace(async_register=register)
    services.async_setup_services(hass)
    return handlers, schemas


def _handlers(hass: SimpleNamespace) -> dict[str, Any]:
    return _register(hass)[0]


async def test_user_without_vehicle_control_is_denied(
    hass: SimpleNamespace, coordinator: MagicMock
) -> None:
    hass.auth.async_get_user.return_value = _user(
        {"switch.other_vehicle_departure_schedule"}
    )
    delete = _handlers(hass)[services.SERVICE_DELETE_DEPARTURE_SCHEDULE]

    with pytest.raises(Unauthorized):
        await delete(_call("user"))
    coordinator.delete_departure_schedule.assert_not_awaited()


async def test_user_with_vehicle_control_is_allowed(
    hass: SimpleNamespace, coordinator: MagicMock
) -> None:
    hass.auth.async_get_user.return_value = _user(
        {"switch.r1t_departure_schedule_weekdays"}
    )
    delete = _handlers(hass)[services.SERVICE_DELETE_DEPARTURE_SCHEDULE]

    await delete(_call("user"))
    coordinator.delete_departure_schedule.assert_awaited_once_with("schedule")


async def test_unknown_user_is_denied(
    hass: SimpleNamespace, coordinator: MagicMock
) -> None:
    hass.auth.async_get_user.return_value = None
    delete = _handlers(hass)[services.SERVICE_DELETE_DEPARTURE_SCHEDULE]

    with pytest.raises(UnknownUser):
        await delete(_call("missing"))
    coordinator.delete_departure_schedule.assert_not_awaited()


async def test_call_without_user_is_trusted(
    hass: SimpleNamespace, coordinator: MagicMock
) -> None:
    """Automations and scripts call without a user and are not checked."""
    delete = _handlers(hass)[services.SERVICE_DELETE_DEPARTURE_SCHEDULE]

    await delete(_call(None))
    hass.auth.async_get_user.assert_not_awaited()
    coordinator.delete_departure_schedule.assert_awaited_once_with("schedule")


async def test_user_who_can_only_read_the_vehicle_is_denied(
    hass: SimpleNamespace, coordinator: MagicMock
) -> None:
    """Reading a vehicle's entities isn't controlling it."""
    hass.auth.async_get_user.return_value = _user(
        {"switch.r1t_departure_schedule_weekdays"}, policies=frozenset({"read"})
    )
    delete = _handlers(hass)[services.SERVICE_DELETE_DEPARTURE_SCHEDULE]

    with pytest.raises(Unauthorized):
        await delete(_call("reader"))
    coordinator.delete_departure_schedule.assert_not_awaited()


async def test_admin_is_allowed_without_entities(
    hass: SimpleNamespace, coordinator: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An admin may act on a vehicle whose entities are all disabled."""
    monkeypatch.setitem(ENTITIES, DEVICE_ID, [])
    hass.auth.async_get_user.return_value = _user(admin=True)
    delete = _handlers(hass)[services.SERVICE_DELETE_DEPARTURE_SCHEDULE]

    await delete(_call("admin"))
    coordinator.delete_departure_schedule.assert_awaited_once_with("schedule")


@pytest.mark.parametrize(
    "service",
    [
        services.SERVICE_CREATE_DEPARTURE_SCHEDULE,
        services.SERVICE_UPDATE_DEPARTURE_SCHEDULE,
        services.SERVICE_DELETE_DEPARTURE_SCHEDULE,
        services.SERVICE_PRECONDITION_CABIN,
    ],
)
async def test_every_schedule_action_checks_permission(
    hass: SimpleNamespace, coordinator: MagicMock, service: str
) -> None:
    hass.auth.async_get_user.return_value = _user()
    handler = _handlers(hass)[service]

    with pytest.raises(Unauthorized):
        await handler(_call("user"))
    assert not coordinator.method_calls


@pytest.mark.parametrize(
    ("data", "expected"),
    [({}, (None, None)), ({"lead_time": 30, "temperature": 72}, (30, 22))],
    ids=["defaults", "lead_and_temperature"],
)
async def test_precondition_cabin(
    monkeypatch: pytest.MonkeyPatch,
    hass: SimpleNamespace,
    coordinator: MagicMock,
    data: dict[str, Any],
    expected: tuple[Any, Any],
) -> None:
    """The lead time is passed on, and the temperature converted to Celsius."""
    coordinator.precondition_now = AsyncMock()
    hass.config = SimpleNamespace(units=SimpleNamespace(temperature_unit="°F"))
    call = _call(None)
    call.data = {services.ATTR_DEVICE_ID: DEVICE_ID}
    if "lead_time" in data:
        call.data[services.ATTR_LEAD_TIME] = data["lead_time"]
    if "temperature" in data:
        call.data[services.ATTR_TEMPERATURE] = data["temperature"]

    await _handlers(hass)[services.SERVICE_PRECONDITION_CABIN](call)

    coordinator.precondition_now.assert_awaited_once_with(*expected)


@pytest.mark.parametrize(
    ("service", "data"),
    [
        (services.SERVICE_PRECONDITION_CABIN, {"device_id": "d"}),
        (
            services.SERVICE_PRECONDITION_CABIN,
            {"device_id": "d", "lead_time": "30", "temperature": "72"},
        ),
        (
            services.SERVICE_CREATE_DEPARTURE_SCHEDULE,
            {
                "device_id": "d",
                "name": "Weekdays",
                "departure_time": "07:30:00",
                "days": ["Monday", "Friday"],
            },
        ),
        (
            services.SERVICE_UPDATE_DEPARTURE_SCHEDULE,
            {"device_id": "d", "schedule_id": "s", "enabled": False},
        ),
        (
            services.SERVICE_DELETE_DEPARTURE_SCHEDULE,
            {"device_id": "d", "schedule_id": "s"},
        ),
    ],
)
def test_action_schema_accepts(
    hass: SimpleNamespace, service: str, data: dict[str, Any]
) -> None:
    schema = _register(hass)[1][service]
    schema(data)


@pytest.mark.parametrize(
    ("service", "data"),
    [
        (services.SERVICE_PRECONDITION_CABIN, {}),
        (services.SERVICE_PRECONDITION_CABIN, {"device_id": "d", "lead_time": 15}),
        (services.SERVICE_PRECONDITION_CABIN, {"device_id": "d", "lead_time": 61}),
        (
            services.SERVICE_CREATE_DEPARTURE_SCHEDULE,
            {
                "device_id": "d",
                "name": " ha precondition now",
                "departure_time": "07:30:00",
                "days": ["Monday"],
            },
        ),
        (
            services.SERVICE_CREATE_DEPARTURE_SCHEDULE,
            {
                "device_id": "d",
                "name": "Weekdays",
                "departure_time": "07:30:00",
                "days": ["Someday"],
            },
        ),
        (services.SERVICE_UPDATE_DEPARTURE_SCHEDULE, {"device_id": "d"}),
        (
            services.SERVICE_UPDATE_DEPARTURE_SCHEDULE,
            {"device_id": "d", "schedule_id": "s", "time": "07:30:00"},
        ),
    ],
    ids=[
        "precondition-no-vehicle",
        "precondition-lead-too-short",
        "precondition-lead-too-long",
        "create-reserved-name",
        "create-unknown-day",
        "update-no-schedule",
        "update-old-time-field",
    ],
)
def test_action_schema_rejects(
    hass: SimpleNamespace, service: str, data: dict[str, Any]
) -> None:
    schema = _register(hass)[1][service]
    with pytest.raises(vol.Invalid):
        schema(data)


@pytest.mark.parametrize(
    ("temperature", "celsius"),
    [(60.8, 16), (84.2, 29), (72, 22)],
    ids=["minimum", "maximum", "middle"],
)
def test_fahrenheit_limits_are_accepted(temperature: float, celsius: float) -> None:
    """The limits as shown in °F aren't rejected for a conversion rounding error."""
    hass = SimpleNamespace(
        config=SimpleNamespace(units=SimpleNamespace(temperature_unit="°F"))
    )
    assert services._to_celsius(hass, temperature) == celsius


@pytest.mark.parametrize("temperature", [60.7, 84.3])
def test_fahrenheit_outside_the_limits_is_rejected(
    monkeypatch: pytest.MonkeyPatch, temperature: float
) -> None:
    monkeypatch.setattr(services, "ServiceValidationError", _TranslatedError)
    hass = SimpleNamespace(
        config=SimpleNamespace(units=SimpleNamespace(temperature_unit="°F"))
    )
    with pytest.raises(_TranslatedError) as err:
        services._to_celsius(hass, temperature)
    assert err.value.translation_key == "temperature_out_of_range"


async def test_precondition_cabin_from_validated_data(
    hass: SimpleNamespace, coordinator: MagicMock
) -> None:
    """Strings from the UI are coerced by the schema before the handler runs."""
    coordinator.precondition_now = AsyncMock()
    hass.config = SimpleNamespace(units=SimpleNamespace(temperature_unit="°F"))
    handlers, schemas = _register(hass)
    call = _call(None)
    call.data = schemas[services.SERVICE_PRECONDITION_CABIN](
        {"device_id": DEVICE_ID, "lead_time": "30", "temperature": "72"}
    )

    await handlers[services.SERVICE_PRECONDITION_CABIN](call)

    coordinator.precondition_now.assert_awaited_once_with(30, 22)


@pytest.mark.parametrize(
    ("departure_time", "valid"),
    [("7:30", True), ("07:30:00", True), ("T07:30:00", False), ("7", False)],
)
def test_departure_time_formats(
    hass: SimpleNamespace, departure_time: str, valid: bool
) -> None:
    """Departure times are parsed as Home Assistant parses times."""
    schema = _register(hass)[1][services.SERVICE_UPDATE_DEPARTURE_SCHEDULE]
    data = {"device_id": "d", "schedule_id": "s", "departure_time": departure_time}
    if valid:
        schema(data)
    else:
        with pytest.raises(vol.Invalid):
            schema(data)


class _TranslatedError(Exception):
    """Stands in for Home Assistant's errors, keeping their translation."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(kwargs.get("translation_key"))
        self.translation_key = kwargs.get("translation_key")
        self.translation_placeholders = kwargs.get("translation_placeholders")


@pytest.fixture
def devices(monkeypatch: pytest.MonkeyPatch) -> dict[str, SimpleNamespace]:
    """Device registry stand-in; tests add devices by id."""
    registry: dict[str, SimpleNamespace] = {}
    monkeypatch.setattr(
        services,
        "dr",
        SimpleNamespace(async_get=lambda hass: SimpleNamespace(async_get=registry.get)),
        raising=False,
    )
    monkeypatch.setattr(services, "ServiceValidationError", _TranslatedError)
    return registry


def _loaded(hass: SimpleNamespace, coordinator: MagicMock) -> None:
    entry = SimpleNamespace(
        entry_id="entry",
        runtime_data=SimpleNamespace(
            vehicles={"vid": {"vin": "VIN"}},
            vehicle_coordinators={"vid": coordinator},
        ),
    )
    hass.config_entries = SimpleNamespace(
        async_loaded_entries=lambda domain: [entry] if domain == services.DOMAIN else []
    )


@pytest.mark.parametrize("identifier", ["vid", "VIN"])
def test_vehicle_found_by_id_or_vin(
    hass: SimpleNamespace, devices: dict[str, SimpleNamespace], identifier: str
) -> None:
    coordinator = MagicMock()
    _loaded(hass, coordinator)
    devices["device"] = SimpleNamespace(
        identifiers={(services.DOMAIN, identifier)}, config_entries={"entry"}
    )
    assert services._get_coordinator(hass, "device") is coordinator


async def test_unknown_device_is_invalid_not_unauthorized(
    hass: SimpleNamespace, devices: dict[str, SimpleNamespace]
) -> None:
    """The vehicle lookup runs before the permission check."""
    _loaded(hass, MagicMock())
    hass.auth.async_get_user.return_value = _user()
    delete = _handlers(hass)[services.SERVICE_DELETE_DEPARTURE_SCHEDULE]

    with pytest.raises(_TranslatedError) as err:
        await delete(_call("user"))
    assert err.value.translation_key == "invalid_device"
    assert err.value.translation_placeholders == {"device_id": DEVICE_ID}


def test_wallbox_device_is_invalid(
    hass: SimpleNamespace, devices: dict[str, SimpleNamespace]
) -> None:
    """A device of a loaded entry that isn't a vehicle is a wallbox."""
    _loaded(hass, MagicMock())
    devices["wallbox"] = SimpleNamespace(
        identifiers={(services.DOMAIN, "serial")}, config_entries={"entry"}
    )
    with pytest.raises(_TranslatedError) as err:
        services._get_coordinator(hass, "wallbox")
    assert err.value.translation_key == "invalid_device"


def test_vehicle_of_unloaded_entry(
    hass: SimpleNamespace, devices: dict[str, SimpleNamespace]
) -> None:
    _loaded(hass, MagicMock())
    devices["device"] = SimpleNamespace(
        identifiers={(services.DOMAIN, "other")}, config_entries={"unloaded"}
    )
    with pytest.raises(_TranslatedError) as err:
        services._get_coordinator(hass, "device")
    assert err.value.translation_key == "vehicle_not_loaded"


def _location_call(user_id: str | None = None, **data: Any) -> SimpleNamespace:
    """A send_location call, by default from an automation."""
    return SimpleNamespace(
        context=SimpleNamespace(user_id=user_id),
        data={
            services.ATTR_DEVICE_ID: DEVICE_ID,
            services.ATTR_LOCATION: {
                services.ATTR_LATITUDE: 39.05,
                services.ATTR_LONGITUDE: -94.48,
            },
        }
        | data,
    )


def _sharing(coordinator: MagicMock, result: dict[str, Any] | None) -> AsyncMock:
    """Have the coordinator's API answer a shared location with this result."""
    response = MagicMock(
        json=AsyncMock(
            return_value={"data": {"parseAndShareLocationToVehicle": result}}
        )
    )
    coordinator.vehicle_id = "vehicle"
    coordinator.api.send_location_to_vehicle = AsyncMock(return_value=response)
    return coordinator.api.send_location_to_vehicle


ACCEPTED = {"publishResponse": {"result": 0}}


async def test_send_location(hass: SimpleNamespace, coordinator: MagicMock) -> None:
    """The coordinates are sent to the device's vehicle."""
    share = _sharing(coordinator, ACCEPTED)
    send = _handlers(hass)[services.SERVICE_SEND_LOCATION]

    await send(_location_call())
    share.assert_awaited_once_with("vehicle", "39.050000,-94.480000")


async def test_send_location_near_zero(
    hass: SimpleNamespace, coordinator: MagicMock
) -> None:
    """Coordinates near 0 stay in decimal notation."""
    share = _sharing(coordinator, ACCEPTED)
    send = _handlers(hass)[services.SERVICE_SEND_LOCATION]

    await send(
        _location_call(
            location={
                services.ATTR_LATITUDE: -0.00001,
                services.ATTR_LONGITUDE: 0.00001,
            }
        )
    )
    assert share.await_args.args[1] == "-0.000010,0.000010"


async def test_send_location_rejected(
    monkeypatch: pytest.MonkeyPatch, hass: SimpleNamespace, coordinator: MagicMock
) -> None:
    """A location Rivian doesn't accept fails the action."""
    monkeypatch.setattr(services, "HomeAssistantError", _TranslatedError)
    _sharing(coordinator, None)
    send = _handlers(hass)[services.SERVICE_SEND_LOCATION]

    with pytest.raises(_TranslatedError) as err:
        await send(_location_call())
    assert err.value.translation_key == "location_rejected"


async def test_send_location_api_error(
    monkeypatch: pytest.MonkeyPatch, hass: SimpleNamespace, coordinator: MagicMock
) -> None:
    """An API error, e.g. a rate limit, fails the action with the error."""
    monkeypatch.setattr(services, "HomeAssistantError", _TranslatedError)
    _sharing(coordinator, ACCEPTED).side_effect = services.RivianApiException(
        "rate limited"
    )
    send = _handlers(hass)[services.SERVICE_SEND_LOCATION]

    with pytest.raises(_TranslatedError) as err:
        await send(_location_call())
    assert err.value.translation_key == "location_failed"
    assert err.value.translation_placeholders == {"error": "rate limited"}


async def test_send_location_defaults_to_home(
    hass: SimpleNamespace, coordinator: MagicMock
) -> None:
    """Without a location, Home Assistant's home location is sent."""
    share = _sharing(coordinator, ACCEPTED)
    hass.config = SimpleNamespace(latitude=38.9, longitude=-94.6)
    send = _handlers(hass)[services.SERVICE_SEND_LOCATION]
    call = _location_call()
    del call.data[services.ATTR_LOCATION]

    await send(call)
    share.assert_awaited_once_with("vehicle", "38.900000,-94.600000")


async def test_send_location_checks_permission(
    hass: SimpleNamespace, coordinator: MagicMock
) -> None:
    """A user who can't control the vehicle can't send it a location."""
    share = _sharing(coordinator, ACCEPTED)
    hass.auth.async_get_user.return_value = _user()
    send = _handlers(hass)[services.SERVICE_SEND_LOCATION]

    with pytest.raises(Unauthorized):
        await send(_location_call("user"))
    share.assert_not_awaited()


STOPS = [{"name": "Stadium", "location": {"latitude": 39.05, "longitude": -94.48}}]


def _trip_call(**data: Any) -> SimpleNamespace:
    """A send_trip call from an automation."""
    return SimpleNamespace(
        context=SimpleNamespace(user_id=None),
        data={
            services.ATTR_DEVICE_ID: DEVICE_ID,
            "stops": STOPS,
            services.ATTR_ARRIVAL_RANGE: 30,
        }
        | data,
    )


@pytest.fixture
def vehicle(coordinator: MagicMock) -> MagicMock:
    """A vehicle at 40.1, -92.5, signed in as "user", that accepts trips."""
    coordinator.vehicle_id = "vehicle"
    coordinator.data = {"gnssLocation": {"latitude": 40.1, "longitude": -92.5}}
    coordinator.config_entry.runtime_data.user_coordinator.data = {"id": "user"}
    coordinator.api.end_trip = AsyncMock(return_value=True)
    coordinator.api.send_trip_to_vehicle = AsyncMock(return_value=True)
    return coordinator


async def test_send_trip(hass: SimpleNamespace, vehicle: MagicMock) -> None:
    """Any active trip is ended, then stops are sent from the vehicle's location."""
    calls = MagicMock()
    calls.attach_mock(vehicle.api.end_trip, "end_trip")
    calls.attach_mock(vehicle.api.send_trip_to_vehicle, "send_trip")
    # a rejected end, with no trip to end, doesn't stop the send
    vehicle.api.end_trip.return_value = False
    send = _handlers(hass)[services.SERVICE_SEND_TRIP]

    await send(_trip_call())
    assert [name for name, *_ in calls.mock_calls] == ["end_trip", "send_trip"]
    vehicle.api.end_trip.assert_awaited_once_with("vehicle", "user")
    vehicle.api.send_trip_to_vehicle.assert_awaited_once_with(
        "vehicle", (40.1, -92.5), [("Stadium", 39.05, -94.48)], 30
    )


async def test_send_trip_stop_defaults_to_home(
    hass: SimpleNamespace, vehicle: MagicMock
) -> None:
    """A stop without a location is Home Assistant's home location."""
    hass.config = SimpleNamespace(latitude=38.9, longitude=-94.6)
    send = _handlers(hass)[services.SERVICE_SEND_TRIP]

    await send(_trip_call(stops=[{"name": "Home"}, *STOPS]))
    assert vehicle.api.send_trip_to_vehicle.await_args.args[2] == [
        ("Home", 38.9, -94.6),
        ("Stadium", 39.05, -94.48),
    ]


async def test_send_trip_stop_names_default(
    hass: SimpleNamespace, vehicle: MagicMock
) -> None:
    """A stop without a name, or with an empty one, is named by its number."""
    send = _handlers(hass)[services.SERVICE_SEND_TRIP]
    location = STOPS[0]["location"]

    await send(
        _trip_call(
            stops=[{"location": location}, *STOPS, {"name": "", "location": location}]
        )
    )
    assert [
        name for name, *_ in vehicle.api.send_trip_to_vehicle.await_args.args[2]
    ] == [
        "Stop 1",
        "Stadium",
        "Stop 3",
    ]


async def test_send_trip_without_position(
    monkeypatch: pytest.MonkeyPatch, hass: SimpleNamespace, vehicle: MagicMock
) -> None:
    """A trip can't be planned without the vehicle's location."""
    monkeypatch.setattr(services, "HomeAssistantError", _TranslatedError)
    vehicle.data = {}
    send = _handlers(hass)[services.SERVICE_SEND_TRIP]

    with pytest.raises(_TranslatedError) as err:
        await send(_trip_call())
    assert err.value.translation_key == "location_unknown"
    vehicle.api.send_trip_to_vehicle.assert_not_awaited()


async def test_end_trip(hass: SimpleNamespace, vehicle: MagicMock) -> None:
    """The trip is ended for the signed-in user."""
    end = _handlers(hass)[services.SERVICE_END_TRIP]

    await end(_trip_call())
    vehicle.api.end_trip.assert_awaited_once_with("vehicle", "user")


@pytest.mark.parametrize(
    ("service", "key"),
    [
        (services.SERVICE_SEND_TRIP, "trip_rejected"),
        (services.SERVICE_END_TRIP, "end_trip_rejected"),
    ],
)
async def test_trip_refused(
    monkeypatch: pytest.MonkeyPatch,
    hass: SimpleNamespace,
    vehicle: MagicMock,
    service: str,
    key: str,
) -> None:
    """A trip, or ending one, that Rivian refuses fails the action."""
    monkeypatch.setattr(services, "HomeAssistantError", _TranslatedError)
    vehicle.api.end_trip.return_value = False
    vehicle.api.send_trip_to_vehicle.return_value = False
    handler = _handlers(hass)[service]

    with pytest.raises(_TranslatedError) as err:
        await handler(_trip_call())
    assert err.value.translation_key == key


@pytest.mark.parametrize(
    "service", [services.SERVICE_SEND_TRIP, services.SERVICE_END_TRIP]
)
async def test_trip_actions_check_permission(
    hass: SimpleNamespace, vehicle: MagicMock, service: str
) -> None:
    """A user who can't control the vehicle can't send or end its trip."""
    hass.auth.async_get_user.return_value = _user()
    handler = _handlers(hass)[service]
    call = _trip_call()
    call.context.user_id = "user"

    with pytest.raises(Unauthorized):
        await handler(call)
    vehicle.api.end_trip.assert_not_awaited()
    vehicle.api.send_trip_to_vehicle.assert_not_awaited()


@pytest.mark.parametrize(
    ("service", "failing", "key"),
    [
        (services.SERVICE_SEND_TRIP, "send_trip_to_vehicle", "trip_failed"),
        # the end send_trip makes first
        (services.SERVICE_SEND_TRIP, "end_trip", "trip_failed"),
        (services.SERVICE_END_TRIP, "end_trip", "end_trip_failed"),
    ],
)
async def test_trip_api_error(
    monkeypatch: pytest.MonkeyPatch,
    hass: SimpleNamespace,
    vehicle: MagicMock,
    service: str,
    failing: str,
    key: str,
) -> None:
    """An API error, e.g. a rate limit, fails the action with the error."""
    monkeypatch.setattr(services, "HomeAssistantError", _TranslatedError)
    getattr(vehicle.api, failing).side_effect = services.RivianApiException(
        "rate limited"
    )
    handler = _handlers(hass)[service]

    with pytest.raises(_TranslatedError) as err:
        await handler(_trip_call())
    assert err.value.translation_key == key
    assert err.value.translation_placeholders == {"error": "rate limited"}


PLACE = {
    "id": "place_id",
    "name": "REI",
    "location": {
        "formattedAddress": "5901 Mills Civic Pkwy, West Des Moines, IA",
        "coordinate": {"latitude": 41.56028, "longitude": -93.78724},
        "navCoordinate": {"latitude": 41.56001, "longitude": -93.78730},
    },
    "driveDetails": {"driveDistance": 239062, "driveDuration": 9960},
}


def _search_call(**data: Any) -> SimpleNamespace:
    """A search_places call from an automation."""
    return SimpleNamespace(
        context=SimpleNamespace(user_id=None),
        data={
            services.ATTR_DEVICE_ID: DEVICE_ID,
            "query": "REI",
            "max_results": 2,
            "autocomplete": False,
        }
        | data,
    )


@pytest.fixture
def places(vehicle: MagicMock) -> MagicMock:
    """A vehicle whose searches find three places and suggest one search."""
    vehicle.api.search_places = AsyncMock(return_value=[PLACE] * 3)
    vehicle.api.suggest_places = AsyncMock(return_value=["restaurants"])
    return vehicle


async def test_search_places(hass: SimpleNamespace, places: MagicMock) -> None:
    """Results come from the vehicle's location, trimmed, at their entrances."""
    search = _handlers(hass)[services.SERVICE_SEARCH_PLACES]

    response = await search(_search_call())
    places.api.search_places.assert_awaited_once_with("REI", 40.1, -92.5)
    assert response == {
        "predictions": [],
        "results": [
            {
                "place_id": "place_id",
                "name": "REI",
                "address": "5901 Mills Civic Pkwy, West Des Moines, IA",
                "location": {"latitude": 41.56001, "longitude": -93.78730},
                "drive_distance": 239062,
                "drive_duration": 9960,
            }
        ]
        * 2,
    }


async def test_search_places_near_location(
    hass: SimpleNamespace, places: MagicMock
) -> None:
    """Given a location, the search is from there, even with no vehicle position."""
    places.data = {}
    search = _handlers(hass)[services.SERVICE_SEARCH_PLACES]

    await search(_search_call(location={"latitude": 1.0, "longitude": 2.0}))
    places.api.search_places.assert_awaited_once_with("REI", 1.0, 2.0)


async def test_search_places_suggestions(
    hass: SimpleNamespace, places: MagicMock
) -> None:
    """With autocomplete, suggested search texts come back instead of places."""
    search = _handlers(hass)[services.SERVICE_SEARCH_PLACES]

    response = await search(_search_call(query="RE", autocomplete=True))
    assert response == {"predictions": ["restaurants"], "results": []}
    places.api.suggest_places.assert_awaited_once_with("RE", 40.1, -92.5)
    places.api.search_places.assert_not_awaited()


async def test_search_places_fails(
    monkeypatch: pytest.MonkeyPatch, hass: SimpleNamespace, places: MagicMock
) -> None:
    """No vehicle position, or a failed search, fails the action."""
    monkeypatch.setattr(services, "HomeAssistantError", _TranslatedError)
    search = _handlers(hass)[services.SERVICE_SEARCH_PLACES]

    places.data = {}
    with pytest.raises(_TranslatedError) as err:
        await search(_search_call())
    assert err.value.translation_key == "location_unknown"

    places.data = {"gnssLocation": {"latitude": 40.1, "longitude": -92.5}}
    places.api.search_places.side_effect = services.RivianApiException("bad")
    with pytest.raises(_TranslatedError) as err:
        await search(_search_call())
    assert err.value.translation_key == "search_failed"
    assert err.value.translation_placeholders == {"error": "bad"}


async def test_search_places_checks_permission(
    hass: SimpleNamespace, places: MagicMock
) -> None:
    """A user who can't control the vehicle can't search from it."""
    hass.auth.async_get_user.return_value = _user()
    search = _handlers(hass)[services.SERVICE_SEARCH_PLACES]
    call = _search_call()
    call.context.user_id = "user"

    with pytest.raises(Unauthorized):
        await search(call)
    places.api.search_places.assert_not_awaited()


def test_search_places_returns_a_response(hass: SimpleNamespace) -> None:
    """Search places is registered to return its results."""
    registered: dict[str, Any] = {}
    hass.services = SimpleNamespace(
        async_register=lambda domain, service, handler, schema, **kwargs: (
            registered.__setitem__(service, kwargs)
        )
    )
    services.async_setup_services(hass)
    assert registered[services.SERVICE_SEARCH_PLACES] == {
        "supports_response": services.SupportsResponse.ONLY
    }


async def test_send_location_by_place(
    monkeypatch: pytest.MonkeyPatch, hass: SimpleNamespace, coordinator: MagicMock
) -> None:
    """With a place id, the app's place link is sent so the vehicle shows its name."""
    share = _sharing(coordinator, ACCEPTED)
    link = MagicMock(return_value="link")
    monkeypatch.setattr(services, "place_url", link)
    send = _handlers(hass)[services.SERVICE_SEND_LOCATION]

    await send(_location_call(place_id="place_id", name="REI", address="1 Main"))
    link.assert_called_once_with("place_id", "REI", 39.05, -94.48, "1 Main")
    share.assert_awaited_once_with("vehicle", "link")


async def test_send_place_without_location(
    monkeypatch: pytest.MonkeyPatch, hass: SimpleNamespace, coordinator: MagicMock
) -> None:
    """A place isn't sent to Home Assistant's home when its location is left out."""
    monkeypatch.setattr(services, "ServiceValidationError", _TranslatedError)
    share = _sharing(coordinator, ACCEPTED)
    hass.config = SimpleNamespace(latitude=38.9, longitude=-94.6)
    send = _handlers(hass)[services.SERVICE_SEND_LOCATION]
    call = _location_call(place_id="place_id", name="REI")
    del call.data[services.ATTR_LOCATION]

    with pytest.raises(_TranslatedError) as err:
        await send(call)
    assert err.value.translation_key == "place_without_location"
    share.assert_not_awaited()
