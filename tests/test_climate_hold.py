"""Tests for the climate hold switch and duration."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from custom_components.rivian import coordinator, number, switch
from custom_components.rivian.const import ATTR_COORDINATOR, ATTR_VEHICLE, DOMAIN
from custom_components.rivian.entity import RivianVehicleControlEntity

DURATION = number.CLIMATE_HOLD_DURATION_NUMBER
SWITCH = next(d for d in switch.SWITCHES if d.key == "climate_hold")


def test_duration_in_minutes() -> None:
    """The vehicle's duration in seconds is shown in minutes, unset as unknown."""
    entity = object.__new__(number.RivianClimateHoldDurationEntity)
    entity.entity_description = DURATION
    entity.coordinator = MagicMock()
    for seconds, minutes in ((28800, 480), (0, None)):
        entity.coordinator.get.side_effect = {"climateHoldDuration": seconds}.get
        assert entity.native_value == minutes


def test_switch_follows_status() -> None:
    """The switch is on only while a hold is running."""
    coor = MagicMock()
    for status, on in (("on", True), ("off", False), ("unavailable", False)):
        coor.get.side_effect = {"climateHoldStatus": status}.get
        assert SWITCH.is_on(coor) is on


async def test_duration_without_a_paired_phone() -> None:
    """Every climate hold vehicle gets the duration, paired phone or not."""
    features = {
        "keyless": {"supported_features": ["CLM_HOLD"]},
        "paired": {"supported_features": ["CLM_HOLD"], "phone_identity_id": "id"},
        "no_hold": {"supported_features": [], "phone_identity_id": "id"},
    }
    vehicles = {
        v: {"id": v, "vin": v, "name": v, "model": "R1S", **f}
        for v, f in features.items()
    }
    coordinators = {v: MagicMock(vehicle_id=v) for v in vehicles}
    hass = MagicMock()
    hass.data = {
        DOMAIN: {
            "entry": {
                ATTR_VEHICLE: vehicles,
                ATTR_COORDINATOR: {ATTR_VEHICLE: coordinators},
            }
        }
    }
    added: list[Any] = []
    with (
        patch.object(number, "RivianChargingScheduleAmperageEntity", MagicMock()),
        patch.object(number, "async_remove_stale_entities"),
    ):
        await number.async_setup_entry(hass, MagicMock(entry_id="entry"), added.extend)
    durations = [e for e in added if e.entity_description is DURATION]
    assert {e.coordinator.vehicle_id for e in durations} == {"keyless", "paired"}
    # Not a control entity: those look up the paired phone when added
    assert not any(isinstance(e, RivianVehicleControlEntity) for e in durations)


def test_duration_available_without_a_key() -> None:
    """The duration is available whenever the vehicle reports one."""
    entity = object.__new__(number.RivianClimateHoldDurationEntity)
    entity.entity_description = DURATION
    entity._available = True
    entity.coordinator = MagicMock()
    entity.coordinator.get.side_effect = {"climateHoldDuration": 7200}.get
    assert entity.available
    assert entity.native_value == 120


class TranslatedError(Exception):
    """Stands in for Home Assistant's errors, keeping their translation."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__()
        self.translation_key = kwargs.get("translation_key")


@pytest.mark.parametrize(
    ("result", "key"),
    [
        ({"return_value": False}, "climate_hold_duration_rejected"),
        (
            {"side_effect": coordinator.RivianApiException("bad value")},
            "climate_hold_duration_failed",
        ),
    ],
)
async def test_duration_errors_are_translated(result: dict[str, Any], key: str) -> None:
    """A refused duration and an API error both raise a translated error."""
    coor = object.__new__(coordinator.VehicleCoordinator)
    coor.vehicle_id = "vehicle"
    coor.api = MagicMock(set_climate_hold_duration=AsyncMock(**result))
    with (
        patch.object(coordinator, "HomeAssistantError", TranslatedError),
        pytest.raises(TranslatedError) as err,
    ):
        await coor.set_climate_hold_duration(120)
    assert err.value.translation_key == key


def holding(
    sends: list[Any],
    updates: list[dict[str, str]],
    power_state: str | None = "sleep",
) -> coordinator.VehicleCoordinator:
    """A coordinator whose hold sends do `sends` in turn (None, or an exception).

    After the first send, the vehicle reports `updates` to the hold status.
    """
    coor = object.__new__(coordinator.VehicleCoordinator)
    coor._climate_hold_request = 0
    values = {
        "powerState": power_state,
        "climateHoldStatus": "off",
        "climateHoldAvailability": "available",
        "climateHoldUnavailabilityReason": "unknown",
    }
    listeners: list[Any] = []
    coor.listeners = listeners  # type: ignore[attr-defined]
    coor.values = values  # type: ignore[attr-defined]
    coor.get = values.get  # type: ignore[method-assign]
    coor.unsubscribe = MagicMock()
    coor.async_add_listener = lambda listener: (  # type: ignore[method-assign]
        listeners.append(listener) or coor.unsubscribe
    )
    sent = 0

    async def report() -> None:
        for update in updates:
            await asyncio.sleep(0)
            values.update(update)
            for listener in listeners:
                listener()

    async def send(*_args: Any) -> None:
        nonlocal sent
        sent += 1
        if sent == 1:
            asyncio.get_running_loop().create_task(report())
        if result := sends.pop(0):
            raise result

    coor.send_parallax_command = AsyncMock(side_effect=send)  # type: ignore[method-assign]
    return coor


WAKES = [{"powerState": "ready"}]
# A restart as the vehicle reports it: wholly unavailable, then available, then off
RESTARTS = [
    {"climateHoldStatus": "unavailable", "climateHoldAvailability": "unavailable"},
    {"climateHoldStatus": "unavailable", "climateHoldAvailability": "available"},
    {"climateHoldStatus": "off", "climateHoldAvailability": "available"},
]
FAILED = TranslatedError(translation_key="command_failed")


@pytest.fixture(autouse=True)
def short_waits() -> Any:
    """Wait briefly for the vehicle in tests."""
    with (
        patch.object(coordinator, "CLIMATE_HOLD_WAKE_WAIT", 0.05),
        patch.object(coordinator, "CLIMATE_HOLD_START_WAIT", 0.01),
        patch.object(coordinator, "CLIMATE_HOLD_START_UP_WAIT", 0.5),
        patch.object(coordinator, "HomeAssistantError", TranslatedError),
        patch.object(coordinator, "encode_climate_hold", lambda on: on),
    ):
        yield


async def test_hold_sent_once_to_an_awake_vehicle() -> None:
    """An awake vehicle, or one without a known power state, gets the hold once."""
    for power_state in ("ready", None):
        coor = holding([None], RESTARTS, power_state=power_state)
        await coor.start_climate_hold()
        assert coor.send_parallax_command.await_count == 1
    coor = holding([FAILED], WAKES, power_state="ready")
    with pytest.raises(TranslatedError):
        await coor.start_climate_hold()
    assert coor.send_parallax_command.await_count == 1


@pytest.mark.parametrize(
    ("first", "updates"),
    [(None, [*WAKES, *RESTARTS]), (FAILED, WAKES), (FAILED, [*WAKES, *RESTARTS])],
)
async def test_lost_hold_resent_once_the_vehicle_wakes(
    first: Any, updates: list[dict[str, str]]
) -> None:
    """A hold that failed, or restarted, as the vehicle woke is sent again."""
    coor = holding([first, None], updates)
    await coor.start_climate_hold()
    assert coor.send_parallax_command.await_args_list == [
        call(coordinator.SET_CLIMATE_HOLD_RVM, True),
        call(coordinator.SET_CLIMATE_HOLD_RVM, True),
    ]
    coor.unsubscribe.assert_called_once()


async def test_resend_waits_for_a_hold_still_starting_up() -> None:
    """A hold still restarting after the first wait is waited out, then resent."""
    coor = holding([None, None], [*WAKES, RESTARTS[0]])

    async def finish_starting_up() -> None:
        await asyncio.sleep(0.03)
        coor.values.update(RESTARTS[-1])  # type: ignore[attr-defined]
        for listener in coor.listeners:  # type: ignore[attr-defined]
            listener()

    task = asyncio.get_running_loop().create_task(finish_starting_up())
    await coor.start_climate_hold()
    await task
    assert coor.send_parallax_command.await_count == 2


async def test_restart_before_the_result_is_not_a_loss() -> None:
    """A restart during the command's own wake, before its result, isn't resent."""
    coor = holding([], [])
    sends = 0

    async def send(*_args: Any) -> None:
        nonlocal sends
        sends += 1
        for update in (*WAKES, *RESTARTS):
            coor.values.update(update)  # type: ignore[attr-defined]
            for listener in coor.listeners:  # type: ignore[attr-defined]
                listener()

    coor.send_parallax_command = AsyncMock(side_effect=send)  # type: ignore[method-assign]
    await coor.start_climate_hold()
    assert coor.send_parallax_command.await_count == 1


async def test_hold_restarting_at_the_result_is_waited_out() -> None:
    """A hold still restarting when the failed result comes is resent once done."""
    coor = holding([], [])
    sends = 0
    status_at_resend = None

    async def send(*_args: Any) -> None:
        nonlocal sends, status_at_resend
        sends += 1
        if sends == 2:
            status_at_resend = coor.values["climateHoldStatus"]  # type: ignore[attr-defined]
        if sends == 1:
            coor.values.update({**WAKES[0], **RESTARTS[0]})  # type: ignore[attr-defined]
            asyncio.get_running_loop().call_later(0.03, finish)
            raise FAILED

    def finish() -> None:
        coor.values.update(RESTARTS[-1])  # type: ignore[attr-defined]
        for listener in coor.listeners:  # type: ignore[attr-defined]
            listener()

    coor.send_parallax_command = AsyncMock(side_effect=send)  # type: ignore[method-assign]
    await coor.start_climate_hold()
    assert sends == 2
    assert status_at_resend == "off"


async def test_restart_already_past_its_start_is_not_a_loss() -> None:
    """A restart first seen past its start (hold available) began before the result."""
    coor = holding([None], [*WAKES, *RESTARTS[1:]])
    await coor.start_climate_hold()
    assert coor.send_parallax_command.await_count == 1


async def test_stale_replay_does_not_end_the_restart() -> None:
    """The stale "unspecified" status the vehicle replays doesn't end the wait."""
    coor = holding([None, None], [*WAKES, RESTARTS[0]])
    status_at_resend = []

    async def replay_then_finish() -> None:
        await asyncio.sleep(0.02)
        for update in ({"climateHoldStatus": "unspecified"}, RESTARTS[-1]):
            coor.values.update(update)  # type: ignore[attr-defined]
            for listener in coor.listeners:  # type: ignore[attr-defined]
                listener()
            await asyncio.sleep(0.03)

    original = coor.send_parallax_command.side_effect

    async def send(*args: Any) -> None:
        if coor.send_parallax_command.await_count == 2:
            status_at_resend.append(coor.values["climateHoldStatus"])  # type: ignore[attr-defined]
        await original(*args)

    coor.send_parallax_command.side_effect = send
    task = asyncio.get_running_loop().create_task(replay_then_finish())
    await coor.start_climate_hold()
    await task
    assert status_at_resend == ["off"]


async def test_hold_not_lost_is_not_resent() -> None:
    """A hold that succeeded without a restart may still be starting: not resent."""
    coor = holding([None], WAKES)
    await coor.start_climate_hold()
    assert coor.send_parallax_command.await_count == 1


async def test_failed_hold_that_came_on_anyway() -> None:
    """A failed first send whose hold came on isn't resent or raised."""
    coor = holding([FAILED], [*WAKES, {"climateHoldStatus": "on"}])
    await coor.start_climate_hold()
    assert coor.send_parallax_command.await_count == 1


async def test_vehicle_already_awake_when_watching() -> None:
    """A vehicle that woke, unannounced, before the first result is seen as awake."""
    coor = holding([], [])
    sends = 0

    async def send(*_args: Any) -> None:
        nonlocal sends
        sends += 1
        if sends == 1:
            # Stored without notifying listeners, before the result
            coor.values["powerState"] = "ready"  # type: ignore[attr-defined]
            raise FAILED

    coor.send_parallax_command = AsyncMock(side_effect=send)  # type: ignore[method-assign]
    await coor.start_climate_hold()
    assert coor.send_parallax_command.await_count == 2


async def test_hold_not_resent_when_unavailable_for_a_reason() -> None:
    """A hold unavailable for e.g. a low battery isn't resent; its error is raised."""
    low = {
        "climateHoldStatus": "unavailable",
        "climateHoldUnavailabilityReason": "low_soc",
    }
    coor = holding([FAILED], [*WAKES, low])
    with pytest.raises(TranslatedError):
        await coor.start_climate_hold()
    assert coor.send_parallax_command.await_count == 1


async def test_vehicle_that_stays_asleep() -> None:
    """If the vehicle doesn't wake, the hold isn't resent and its error raised."""
    coor = holding([FAILED], [])
    with pytest.raises(TranslatedError):
        await coor.start_climate_hold()
    assert coor.send_parallax_command.await_count == 1
    coor.unsubscribe.assert_called_once()
    coor = holding([None], [])
    await coor.start_climate_hold()
    assert coor.send_parallax_command.await_count == 1


async def test_failed_resend_is_raised() -> None:
    """If the resend fails too, its error is raised."""
    coor = holding([FAILED, FAILED], WAKES)
    with pytest.raises(TranslatedError):
        await coor.start_climate_hold()
    assert coor.send_parallax_command.await_count == 2


async def test_rejected_hold_raised_at_once() -> None:
    """A hold the cloud rejects never reached the vehicle: raised without waiting."""
    coor = holding([TranslatedError(translation_key="command_rejected")], [])
    with (
        patch.object(coordinator, "CLIMATE_HOLD_WAKE_WAIT", 10),
        pytest.raises(TranslatedError) as err,
    ):
        await asyncio.wait_for(coor.start_climate_hold(), 1)
    assert err.value.translation_key == "command_rejected"
    assert coor.send_parallax_command.await_count == 1


async def test_hold_that_comes_on_returns_at_once() -> None:
    """Once the hold reads on, it's done without waiting out the start wait."""
    coor = holding([None], [*WAKES, {"climateHoldStatus": "on"}])
    with patch.object(coordinator, "CLIMATE_HOLD_START_WAIT", 10):
        await asyncio.wait_for(coor.start_climate_hold(), 1)
    assert coor.send_parallax_command.await_count == 1


async def test_hold_turned_off_while_waiting_is_not_resent() -> None:
    """A hold turned off while it waits to be sent again isn't sent again."""
    coor = holding([None, None], [*WAKES, *RESTARTS])
    with patch.object(coordinator, "CLIMATE_HOLD_START_WAIT", 0.2):
        task = asyncio.get_running_loop().create_task(coor.start_climate_hold())
        await asyncio.sleep(0.05)
        await coor.stop_climate_hold()
        await task
    assert coor.send_parallax_command.await_args_list == [
        call(coordinator.SET_CLIMATE_HOLD_RVM, True),
        call(coordinator.SET_CLIMATE_HOLD_RVM, False),
    ]


async def test_newer_hold_replaces_a_waiting_one() -> None:
    """A hold started again while an older one waits is the only one sent."""
    coor = holding([None, None], [*WAKES, *RESTARTS])
    with patch.object(coordinator, "CLIMATE_HOLD_START_WAIT", 0.2):
        first = asyncio.get_running_loop().create_task(coor.start_climate_hold())
        await asyncio.sleep(0.05)
        await coor.start_climate_hold()
        await first
    # The older one's resend is dropped; the newer one, to an awake vehicle, is sent
    assert coor.send_parallax_command.await_count == 2


async def test_switch_off_stops_a_waiting_hold() -> None:
    """The switch turns a hold off through the coordinator, which drops a resend."""
    coor = MagicMock(stop_climate_hold=AsyncMock())
    await SWITCH.turn_off(coor)
    coor.stop_climate_hold.assert_awaited_once()
