"""Rivian (Unofficial)"""
from __future__ import annotations
import logging
from typing import Any

from rivian import Rivian
import voluptuous as vol
from homeassistant.core import callback
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_USERNAME,
)

from .const import (
    DOMAIN,
    CONF_OTP,
    CONF_VIN,
)

_LOGGER = logging.getLogger(__name__)


def _get_schema_credential_fields(user_input: list, default_dict: list) -> Any:
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


def _get_schema_vin_field(user_input: list, default_dict: list) -> Any:
    """Get a schema using the default_dict as a backup."""
    if user_input is None:
        user_input = {}

    def _get_default(key: str, fallback_default: Any = None) -> None:
        """Get default value for key."""
        return user_input.get(key, default_dict.get(key, fallback_default))

    return vol.Schema(
        {
            vol.Required(CONF_VIN): str,
        }
    )


def _get_schema_otp_field(user_input: list, default_dict: list) -> Any:
    """Get a schema using the default_dict as a backup."""
    if user_input is None:
        user_input = {}

    def _get_default(key: str, fallback_default: Any = None) -> None:
        """Get default value for key."""
        return user_input.get(key, default_dict.get(key, fallback_default))

    return vol.Schema(
        {
            vol.Required(CONF_OTP): str,
        }
    )


@config_entries.HANDLERS.register(DOMAIN)
class RivianFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Rivian"""

    VERSION = 1

    def __init__(self):
        """Initalize"""
        self._rivian = Rivian("", "")
        self._data = {}
        self._errors = {}

        self._username = None
        self._password = None
        self._otp = None
        self._vin = None
        self._access_token = None
        self._refresh_token = None
        self._session_token = None
        self._user_session_token = None

    async def async_step_user(self, user_input=None):
        """Handle the flow"""
        if user_input is not None:
            self._data.update(user_input)

        if user_input is None:
            return await self._show_credential_fields(user_input)

        if user_input.get(CONF_OTP) is not None:
            self._otp = user_input[CONF_OTP]
            otpauth = await self._rivian.validate_otp_graphql(self._username, self._otp)

            if otpauth.status == 200:
                self._access_token = self._rivian._access_token
                self._refresh_token = self._rivian._refresh_token
                self._user_session_token = self._rivian._user_session_token
                return await self._show_vin_field(user_input)

            self._errors["base"] = "communication"
            return await self._show_credential_fields(user_input)

        if user_input.get(CONF_VIN) is not None:
            self._vin = user_input[CONF_VIN]

            return await self._async_create_entry()

        self._username = user_input[CONF_USERNAME]
        self._password = user_input[CONF_PASSWORD]

        await self._rivian.create_csrf_token()
        auth = await self._rivian.authenticate_graphql(self._username, self._password)

        if self._rivian._otp_needed:
            self._data.update({"otp_token": self._rivian._otp_token})
            return await self._show_otp_field(user_input)

        self._access_token = self._rivian._access_token
        self._refresh_token = self._rivian._refresh_token
        self._user_session_token = self._rivian._user_session_token

        return await self._show_vin_field(user_input)

    async def _async_create_entry(self) -> FlowResult:
        """Create the config entry."""
        config_data = {
            "access_token": self._access_token,
            "refresh_token": self._refresh_token,
            "user_session_token": self._user_session_token,
            "vin": self._vin,
        }

        self._data.update(config_data)

        return self.async_create_entry(title="Rivian (Unofficial)", data=self._data)

    async def _show_credential_fields(self, user_input):
        """Show the configuration form to add credentials."""
        defaults = {}

        return self.async_show_form(
            step_id="user",
            data_schema=_get_schema_credential_fields(user_input, defaults),
            errors=self._errors,
        )

    async def _show_otp_field(self, user_input):
        """Show the configuration form to verify otp."""
        defaults = {}

        return self.async_show_form(
            step_id="user",
            data_schema=_get_schema_otp_field(user_input, defaults),
            errors=self._errors,
        )

    async def _show_vin_field(self, user_input):
        """Show the configuration form to add VIN."""
        defaults = {}
        return self.async_show_form(
            step_id="user",
            data_schema=_get_schema_vin_field(user_input, defaults),
            errors=self._errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Redirect to options flow."""
        return RivianOptionsFlow(config_entry)


class RivianOptionsFlow(config_entries.OptionsFlow):
    """Options flow for Rivian."""

    def __init__(self, config_entry):
        """Initialize."""
        self.config = config_entry
        self._rivian = Rivian("", "")
        self._data = dict(config_entry.options)
        self._errors = {}

    async def async_step_init(self, user_input=None):
        """Manage Rivian options."""
        if user_input is None:
            return await self._show_credential_fields(user_input)

        if user_input.get(CONF_OTP) is not None:
            self._data.update(user_input)
            otpauth = await self._rivian.validate_otp_graphql(
                self._data.get(CONF_USERNAME), self._data.get(CONF_OTP)
            )

            if otpauth.status == 200:
                self._data.update({"access_token": self._rivian._access_token})
                self._data.update({"refresh_token": self._rivian._refresh_token})
                self._data.update(
                    {"user_session_token": self._rivian._user_session_token}
                )
                return await self._show_vin_field(user_input)

            self._errors["base"] = "communication"
            return await self._show_credential_fields(user_input)

        if user_input.get(CONF_VIN) is not None:
            self._data.update(user_input)

            return self.async_create_entry(title="", data=self._data)

        self._data.update(user_input)

        await self._rivian.create_csrf_token()
        await self._rivian.authenticate_graphql(
            self._data.get(CONF_USERNAME), self._data.get(CONF_PASSWORD)
        )

        if self._rivian._otp_needed:
            self._data.update({"otp_token": self._rivian._otp_token})
            return await self._show_otp_field(user_input)

        self._data.update({"access_token": self._rivian._access_token})
        self._data.update({"refresh_token": self._rivian._refresh_token})
        self._data.update({"user_session_token": self._rivian._user_session_token})

        return await self._show_vin_field(user_input)

    async def _show_credential_fields(self, user_input):
        """Show the configuration form to edit data."""

        return self.async_show_form(
            step_id="init",
            data_schema=_get_schema_credential_fields(user_input, self._data),
            errors=self._errors,
        )

    async def _show_vin_field(self, user_input):
        """Show the configuration form to edit data."""

        return self.async_show_form(
            step_id="init",
            data_schema=_get_schema_vin_field(user_input, self._data),
            errors=self._errors,
        )

    async def _show_otp_field(self, user_input):
        """Show the configuration form to edit data."""

        return self.async_show_form(
            step_id="init",
            data_schema=_get_schema_otp_field(user_input, self._data),
            errors=self._errors,
        )
