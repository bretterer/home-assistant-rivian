"""Tests for the 2FA check around vehicle control."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rivian.const import (
    CONF_ACCESS_TOKEN,
    CONF_MFA_VERIFIED,
    CONF_OTP,
    CONF_REFRESH_TOKEN,
    CONF_USER_SESSION_TOKEN,
    CONF_VEHICLE_CONTROL,
    DOMAIN,
)
from custom_components.rivian.helpers import has_verified_2fa
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import homeassistant.helpers.device_registry as dr

VEHICLE_ID = "01-123"
PUBLIC_KEY = "existing-public-key"
ENTRY_DATA = {
    CONF_USERNAME: "user@example.com",
    CONF_ACCESS_TOKEN: "access",
    CONF_REFRESH_TOKEN: "refresh",
    CONF_USER_SESSION_TOKEN: "session",
}


def _user_data(
    registration_channels: list[dict[str, str]], enrolled_key: str | None = None
) -> dict[str, Any]:
    """Build a currentUser payload."""
    phones = []
    if enrolled_key:
        phones.append(
            {
                "vas": {"vasPhoneId": "phone-1", "publicKey": enrolled_key},
                "enrolled": [{"vehicleId": VEHICLE_ID, "identityId": "identity-1"}],
            }
        )
    return {
        "id": "user-1",
        "registrationChannels": registration_channels,
        "enrolledPhones": phones,
        "vehicles": [
            {"id": VEHICLE_ID, "name": "Truck", "vehicle": {"id": VEHICLE_ID}}
        ],
    }


def _mock_api(user_data: dict[str, Any]) -> MagicMock:
    """Return a Rivian client whose getUserInfo returns user_data."""
    response = MagicMock(status=200)
    response.json = AsyncMock(return_value={"data": {"currentUser": user_data}})
    api = MagicMock()
    api.get_user_information = AsyncMock(return_value=response)
    api.enroll_phone = AsyncMock(return_value=True)
    api.disenroll_phone = AsyncMock(return_value=True)
    api.close = AsyncMock()
    return api


def _mock_login(otp_needed: bool) -> MagicMock:
    """Return a Rivian client for the config flow login steps."""
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


def _add_entry(
    hass: HomeAssistant, data: dict[str, Any], options: dict[str, Any] | None = None
) -> tuple[MockConfigEntry, str]:
    """Add a config entry and a vehicle device; return both."""
    entry = MockConfigEntry(domain=DOMAIN, data=data, options=options or {})
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, VEHICLE_ID)},
        manufacturer="Rivian",
        model="R1T",
        name="Truck",
    )
    return entry, device.id


async def _submit_vehicle_control(
    hass: HomeAssistant, entry: MockConfigEntry, device_id: str, api: MagicMock
):
    """Run the options flow selecting the vehicle for control."""
    with patch(
        "custom_components.rivian.config_flow.get_rivian_api_from_entry",
        return_value=api,
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        return await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_VEHICLE_CONTROL: [device_id]}
        )


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
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    assert has_verified_2fa(entry, {"registrationChannels": channels}) is expected


@pytest.mark.parametrize(("otp_needed", "expected"), [(True, True), (False, False)])
async def test_login_records_mfa_verified(
    hass: HomeAssistant, otp_needed: bool, expected: bool
) -> None:
    """The entry records whether Rivian asked for an OTP at login."""
    client = _mock_login(otp_needed)
    with (
        patch("custom_components.rivian.config_flow.Rivian", return_value=client),
        patch("custom_components.rivian.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "pw"},
        )
        if otp_needed:
            assert result["type"] is FlowResultType.FORM
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {CONF_OTP: "123456"}
            )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MFA_VERIFIED] is expected


async def test_new_enrollment_without_2fa_starts_reauth(hass: HomeAssistant) -> None:
    """No OTP login and no SMS channel: block enrollment and ask to reauth."""
    entry, device_id = _add_entry(hass, ENTRY_DATA)
    api = _mock_api(_user_data([]))

    result = await _submit_vehicle_control(hass, entry, device_id, api)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "2fa_unverified"}
    api.enroll_phone.assert_not_called()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_new_enrollment_after_otp_login(hass: HomeAssistant) -> None:
    """An authenticator/email OTP login allows enrollment with no SMS channel."""
    entry, device_id = _add_entry(hass, ENTRY_DATA | {CONF_MFA_VERIFIED: True})
    api = _mock_api(_user_data([]))

    result = await _submit_vehicle_control(hass, entry, device_id, api)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    api.enroll_phone.assert_awaited_once()


async def test_new_enrollment_with_sms_channel(hass: HomeAssistant) -> None:
    """Entries from before mfa_verified still pass with an SMS channel."""
    entry, device_id = _add_entry(hass, ENTRY_DATA)
    api = _mock_api(_user_data([{"type": "TEXT"}]))

    result = await _submit_vehicle_control(hass, entry, device_id, api)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    api.enroll_phone.assert_awaited_once()


async def test_already_enrolled_key_is_not_blocked(hass: HomeAssistant) -> None:
    """Re-saving options for an enrolled key needs no 2FA proof."""
    options = {"public_key": PUBLIC_KEY, "private_key": "existing-private-key"}
    entry, device_id = _add_entry(hass, ENTRY_DATA, options)
    api = _mock_api(_user_data([], enrolled_key=PUBLIC_KEY))

    result = await _submit_vehicle_control(hass, entry, device_id, api)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    api.enroll_phone.assert_not_called()
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
