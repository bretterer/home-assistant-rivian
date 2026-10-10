"""Tests for the 2FA check around vehicle control."""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# The conftest mocks the rivian client without its parallax submodule, which
# the coordinator imports. Stub it only when the client is the mock (no __spec__).
if "rivian" in sys.modules and sys.modules["rivian"].__spec__ is None:
    sys.modules.setdefault("rivian.parallax", MagicMock())

from custom_components.rivian import config_flow
from custom_components.rivian.const import (
    CONF_MFA_VERIFIED,
    CONF_OTP,
    CONF_VEHICLE_CONTROL,
    DOMAIN,
)
from custom_components.rivian.helpers import has_verified_2fa
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME

VEHICLE_ID = "01-123"
DEVICE_ID = "device-1"
PUBLIC_KEY = "existing-public-key"


class FlowError(Exception):
    """Stand-in for SchemaFlowError, which the HA mocks don't make an exception."""


def _entry(
    data: dict[str, Any] | None = None, options: dict[str, Any] | None = None
) -> MagicMock:
    entry = MagicMock()
    entry.data = data or {}
    entry.options = options or {}
    entry.async_start_reauth = MagicMock()
    return entry


def _user(channels: list[dict[str, str]], enrolled: bool = False) -> MagicMock:
    """A UserCoordinator double for one vehicle."""
    user = MagicMock()
    user.async_refresh = AsyncMock()
    user.data = {"id": "user-1", "registrationChannels": channels}
    user.get_vehicles.return_value = {VEHICLE_ID: {"id": VEHICLE_ID, "name": "Truck"}}
    user.get_enrolled_phone_data.return_value = (
        ("phone-1", {VEHICLE_ID: "identity-1"}) if enrolled else None
    )
    return user


def _api() -> MagicMock:
    api = MagicMock()
    api.enroll_phone = AsyncMock(return_value=True)
    api.disenroll_phone = AsyncMock(return_value=True)
    api.close = AsyncMock()
    return api


async def _submit_vehicle_control(
    entry: MagicMock, user: MagicMock, api: MagicMock
) -> dict[str, Any]:
    """Run the options-flow validator with the vehicle selected for control."""
    handler = MagicMock()
    handler.parent_handler.hass = MagicMock()
    handler.parent_handler.config_entry = entry
    registry = MagicMock()
    registry.async_get.return_value = MagicMock(identifiers={(DOMAIN, VEHICLE_ID)})
    with (
        patch.object(config_flow, "get_rivian_api_from_entry", return_value=api),
        patch.object(config_flow, "UserCoordinator", return_value=user),
        patch.object(config_flow.dr, "async_get", return_value=registry),
        patch.object(config_flow, "SchemaFlowError", FlowError),
    ):
        return await config_flow.validate_vehicle_control(
            handler, {CONF_VEHICLE_CONTROL: [DEVICE_ID]}
        )


def _login_client(otp_needed: bool) -> MagicMock:
    """A Rivian client double for the config flow login steps."""
    client = MagicMock()
    client._otp_needed = False
    client._access_token = None

    def _set_tokens() -> None:
        client._access_token = "access"
        client._refresh_token = "refresh"
        client._user_session_token = "session"

    async def authenticate(username: str, password: str) -> None:
        if otp_needed:
            client._otp_needed = True
        else:
            _set_tokens()

    async def validate_otp(username: str, otp: str) -> None:
        _set_tokens()

    client.create_csrf_token = AsyncMock()
    client.authenticate = AsyncMock(side_effect=authenticate)
    client.validate_otp = AsyncMock(side_effect=validate_otp)
    client.close = AsyncMock()
    return client


def _flow(client: MagicMock, existing_entry: MagicMock | None = None) -> Any:
    flow = config_flow.RivianFlowHandler()
    flow._rivian = client
    flow.hass = MagicMock()
    flow.hass.config_entries.async_get_entry.return_value = existing_entry
    flow.hass.config_entries.async_reload = AsyncMock()
    flow.context = {"entry_id": "entry-1"} if existing_entry else {}
    flow.async_show_form = MagicMock(return_value={"type": "form"})
    flow.async_create_entry = MagicMock(
        side_effect=lambda title, data: {"type": "create_entry", "data": data}
    )
    flow.async_abort = MagicMock(
        side_effect=lambda reason: {"type": "abort", "reason": reason}
    )
    return flow


async def _login(flow: Any, otp_needed: bool) -> dict[str, Any]:
    result = await flow.async_step_user(
        {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "pw"}
    )
    if otp_needed:
        assert result["type"] == "form"
        result = await flow.async_step_user({CONF_OTP: "123456"})
    return result


@pytest.mark.parametrize(
    ("data", "channels", "expected"),
    [
        ({CONF_MFA_VERIFIED: True}, [], True),
        ({}, [{"type": "TEXT"}], True),
        ({CONF_MFA_VERIFIED: False}, [], False),
        ({}, [], False),
    ],
)
def test_has_verified_2fa(
    data: dict[str, Any], channels: list[dict[str, str]], expected: bool
) -> None:
    """An OTP login or an SMS channel counts as 2FA."""
    assert has_verified_2fa(_entry(data), {"registrationChannels": channels}) is (
        expected
    )


@pytest.mark.parametrize(("otp_needed", "expected"), [(True, True), (False, False)])
async def test_login_records_mfa_verified(otp_needed: bool, expected: bool) -> None:
    """The entry records whether Rivian asked for an OTP at login."""
    result = await _login(_flow(_login_client(otp_needed)), otp_needed)

    assert result["type"] == "create_entry"
    assert result["data"][CONF_MFA_VERIFIED] is expected


async def test_reauth_with_otp_records_mfa_verified() -> None:
    """Re-authenticating with an OTP writes the flag onto the existing entry."""
    existing = _entry({CONF_USERNAME: "user@example.com"})
    flow = _flow(_login_client(otp_needed=True), existing)

    result = await _login(flow, otp_needed=True)

    assert result == {"type": "abort", "reason": "reauth_successful"}
    update = flow.hass.config_entries.async_update_entry
    update.assert_called_once()
    assert update.call_args.kwargs["data"][CONF_MFA_VERIFIED] is True


async def test_new_enrollment_without_2fa_starts_reauth() -> None:
    """No OTP login and no SMS channel: block enrollment and ask to reauth."""
    entry, api = _entry(), _api()

    with pytest.raises(FlowError, match="2fa_unverified"):
        await _submit_vehicle_control(entry, _user([]), api)

    api.enroll_phone.assert_not_called()
    entry.async_start_reauth.assert_called_once()


async def test_new_enrollment_after_otp_login() -> None:
    """An authenticator/email OTP login allows enrollment with no SMS channel."""
    entry, api = _entry({CONF_MFA_VERIFIED: True}), _api()

    result = await _submit_vehicle_control(entry, _user([]), api)

    assert result[CONF_VEHICLE_CONTROL] == [DEVICE_ID]
    api.enroll_phone.assert_awaited_once()
    entry.async_start_reauth.assert_not_called()


async def test_new_enrollment_with_sms_channel() -> None:
    """Entries from before mfa_verified still pass with an SMS channel."""
    entry, api = _entry(), _api()

    result = await _submit_vehicle_control(entry, _user([{"type": "TEXT"}]), api)

    assert result[CONF_VEHICLE_CONTROL] == [DEVICE_ID]
    api.enroll_phone.assert_awaited_once()


async def test_already_enrolled_key_is_not_blocked() -> None:
    """Re-saving options for an enrolled key needs no 2FA proof."""
    options = {"public_key": PUBLIC_KEY, "private_key": "existing-private-key"}
    entry, api = _entry(options=options), _api()

    result = await _submit_vehicle_control(entry, _user([], enrolled=True), api)

    assert result[CONF_VEHICLE_CONTROL] == [DEVICE_ID]
    api.enroll_phone.assert_not_called()
    entry.async_start_reauth.assert_not_called()
