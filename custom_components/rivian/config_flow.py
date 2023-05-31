"""Rivian (Unofficial)"""
from __future__ import annotations

import logging
from typing import Any

from rivian import Rivian
from rivian.exceptions import RivianUnauthenticated
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResult

from .const import CONF_OTP, DOMAIN

_LOGGER = logging.getLogger(__name__)
STEP_OTP_DATA_SCHEMA = vol.Schema({vol.Required(CONF_OTP): str})


def _get_schema_credential_fields(
    user_input: dict[str, Any], default_dict: dict[str, Any]
) -> vol.Schema:
    """Get a schema using the default_dict as a backup."""
    if user_input is None:
        user_input = {}

    def _get_default(key: str, fallback_default: Any = None) -> None:
        """Get default value for key."""
        return user_input.get(key, default_dict.get(key, fallback_default))

    return vol.Schema(
        {
            vol.Required(CONF_USERNAME, default=_get_default(CONF_USERNAME)): str,
            vol.Required(CONF_PASSWORD, default=_get_default(CONF_PASSWORD)): str,
        }
    )


class RivianFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Rivian"""

    VERSION = 1

    def __init__(self):
        """Initalize"""
        self._rivian = Rivian("", "")
        self._data = {}
        self._errors = {}

        self._access_token = None
        self._refresh_token = None
        self._session_token = None
        self._user_session_token = None

    # pylint: disable=protected-access
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the flow"""
        if user_input is None:
            return await self._show_credential_fields(user_input)

        self._data.update(user_input)

        if (otp := user_input.get(CONF_OTP)) is not None:
            username = self._data[CONF_USERNAME]
            try:
                otpauth = await self._rivian.validate_otp_graphql(username, otp)
            except RivianUnauthenticated as err:
                show_otp = False
                try:
                    error = err.args[1]["errors"][0]
                    reason = error["extensions"]["reason"]
                    msg = f"{error['message']}: {reason}"
                    show_otp = reason == "INVALID_OTP_TOKEN"
                except Exception:  # pylint: disable=broad-except
                    msg = str(err)
                _LOGGER.error(msg)
                self._errors["base"] = msg
                if show_otp:
                    return await self._show_otp_field()
                return await self._show_credential_fields(user_input)

            if otpauth.status == 200:
                self._access_token = self._rivian._access_token
                self._refresh_token = self._rivian._refresh_token
                self._user_session_token = self._rivian._user_session_token
                return await self._async_create_entry()

            self._errors["base"] = "communication"
            return await self._show_credential_fields(user_input)

        username = user_input[CONF_USERNAME]
        password = user_input[CONF_PASSWORD]
        await self._rivian.create_csrf_token()
        try:
            await self._rivian.authenticate_graphql(username, password)
        except RivianUnauthenticated as err:
            _LOGGER.error(err)
            self._errors["base"] = "invalid_auth"
            return await self._show_credential_fields(user_input)

        if self._rivian._otp_needed:
            return await self._show_otp_field()

        self._access_token = self._rivian._access_token
        self._refresh_token = self._rivian._refresh_token
        self._user_session_token = self._rivian._user_session_token
        return await self._async_create_entry()

    async def async_step_reauth(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Perform reauth upon an API authentication error."""
        return await self.async_step_user(user_input)

    async def _async_create_entry(self) -> FlowResult:
        """Create the config entry."""
        config_data = {
            "access_token": self._access_token,
            "refresh_token": self._refresh_token,
            "user_session_token": self._user_session_token,
        }

        self._data.update(config_data)
        await self._rivian.close()

        entry_id = self.context["entry_id"]
        if existing_entry := self.hass.config_entries.async_get_entry(entry_id):
            self.hass.config_entries.async_update_entry(existing_entry, data=self._data)
            await self.hass.config_entries.async_reload(existing_entry.entry_id)
            return self.async_abort(reason="reauth_successful")

        return self.async_create_entry(title="Rivian (Unofficial)", data=self._data)

    async def _show_credential_fields(self, user_input: dict[str, Any]) -> FlowResult:
        """Show the configuration form to add credentials."""
        return self.async_show_form(
            step_id="user",
            data_schema=_get_schema_credential_fields(user_input, self._data),
            errors=self._errors,
        )

    async def _show_otp_field(self) -> FlowResult:
        """Show the configuration form to verify otp."""
        return self.async_show_form(
            step_id="user",
            data_schema=STEP_OTP_DATA_SCHEMA,
            errors=self._errors,
        )
