"""Rivian (Unofficial)"""
from __future__ import annotations
import logging
from typing import Any

from rivian import Rivian
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import config_validation as cv
from homeassistant.data_entry_flow import FlowResult
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_USERNAME,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET
)
from homeassistant.core import callback

from .const import (
    DOMAIN,
    CONF_OTP,
    CONF_VIN,
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_POLLING_INTERVAL,
    MIN_POLLING_INTERVAL,
    DEFAULT_POLLING_INTERVAL
)

_LOGGER = logging.getLogger(__name__)


@config_entries.HANDLERS.register(DOMAIN)
class RivianFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Rivian"""

    VERSION = 1

    def __init__(self):
        """Initalize"""
        self._rivian = None
        self._data = {}
        self._errors = {}

        self._username = None
        self._password = None
        self._client_id = None
        self._client_secret = None
        self._otp = None
        self._vin = None
        self._access_token = None
        self._refresh_token = None
        self._session_token = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Handle the flow"""
        if user_input is None:
            return await self._show_credential_fields()

        if user_input.get(CONF_OTP) is not None:
            self._otp = user_input[CONF_OTP]
            otpauth = await self._rivian.validate_otp(self._username, self._otp)

            if otpauth.status == 200:
                oauth_json_data = await otpauth.json()
                self._access_token = oauth_json_data["access_token"]
                self._refresh_token = oauth_json_data["refresh_token"]
                return await self._show_vin_field()

            self._errors["base"] = "communication"
            return await self._show_credential_fields()

        if user_input.get(CONF_VIN) is not None:
            self._vin = user_input[CONF_VIN]

            return await self._async_create_entry()

        self._username = user_input[CONF_USERNAME]
        self._password = user_input[CONF_PASSWORD]
        self._client_id = user_input[CONF_CLIENT_ID]
        self._client_secret = user_input[CONF_CLIENT_SECRET]

        self._rivian = Rivian(self._client_id, self._client_secret)
        auth = await self._rivian.authenticate(self._username, self._password)

        json_data = await auth.json()
        if auth.status == 401:
            self._session_token = json_data["session_token"]
            return await self._show_otp_field()

        self._access_token = json_data["access_token"]
        self._refresh_token = json_data["refresh_token"]

        return await self._show_vin_field()

    async def _async_create_entry(self) -> FlowResult:
        """Create the config entry."""
        config_data = {
            CONF_ACCESS_TOKEN: self._access_token,
            CONF_REFRESH_TOKEN: self._refresh_token,
            CONF_CLIENT_ID: self._client_id,
            CONF_CLIENT_SECRET: self._client_secret,
            CONF_VIN: self._vin,
        }

        return self.async_create_entry(title="Rivian (Unofficial)", data=config_data)

    async def _show_credential_fields(self):
        """Show the configuration form to add credentials."""

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Required(CONF_CLIENT_ID): str,
                    vol.Required(CONF_CLIENT_SECRET): str,
                }
            ),
            errors=self._errors,
        )

    async def _show_otp_field(self):
        """Show the configuration form to verify otp."""
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_OTP): str,
                }
            ),
            errors=self._errors,
        )

    async def _show_vin_field(self):
        """Show the configuration form to add VIN."""
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_VIN): str,
                }
            ),
            errors=self._errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return OptionsFlowHandler(config_entry)

class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle a option flow for Rivian."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Handle options flow."""
        if user_input is not None:
            return self.async_create_entry(title="Rivian (Unofficial)", data=user_input)

        data_schema = vol.Schema(
            {
                vol.Required(
                    CONF_POLLING_INTERVAL,
                    default=self.config_entry.options.get(
                        CONF_POLLING_INTERVAL, DEFAULT_POLLING_INTERVAL
                    ),
                ): vol.All(cv.positive_int, vol.Clamp(min=MIN_POLLING_INTERVAL)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=data_schema)
