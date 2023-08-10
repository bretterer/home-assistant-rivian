"""Rivian (Unofficial)"""
from __future__ import annotations

import logging
from typing import Any

from rivian import Rivian
from rivian.exceptions import RivianPhoneLimitReachedError, RivianUnauthenticated
from rivian.utils import generate_key_pair
import voluptuous as vol

from homeassistant.components.zone import DOMAIN as ZONE_DOMAIN
from homeassistant.config_entries import ConfigEntry, ConfigFlow
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, CONF_ZONE
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
import homeassistant.helpers.device_registry as dr
from homeassistant.helpers.schema_config_entry_flow import (
    SchemaCommonFlowHandler,
    SchemaFlowError,
    SchemaFlowFormStep,
    SchemaOptionsFlowHandler,
)
from homeassistant.helpers.selector import (
    DeviceFilterSelectorConfig,
    DeviceSelector,
    DeviceSelectorConfig,
    EntitySelector,
    EntitySelectorConfig,
)

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_OTP,
    CONF_REFRESH_TOKEN,
    CONF_USER_SESSION_TOKEN,
    CONF_VEHICLE_CONTROL,
    DOMAIN,
)
from .coordinator import UserCoordinator
from .helpers import get_rivian_api_from_entry

_LOGGER = logging.getLogger(__name__)


STEP_OTP_DATA_SCHEMA = vol.Schema({vol.Required(CONF_OTP): str})
R1S = DeviceFilterSelectorConfig(integration=DOMAIN, manufacturer="Rivian", model="R1S")
R1T = DeviceFilterSelectorConfig(integration=DOMAIN, manufacturer="Rivian", model="R1T")
OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_VEHICLE_CONTROL): DeviceSelector(
            DeviceSelectorConfig(multiple=True, filter=[R1S, R1T])
        ),
        vol.Optional(CONF_ZONE): EntitySelector(
            EntitySelectorConfig(domain=ZONE_DOMAIN, multiple=True)
        ),
    }
)


async def validate_vehicle_control(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Validate vehicle control."""
    hass = handler.parent_handler.hass
    entry = handler.parent_handler.config_entry
    api = get_rivian_api_from_entry(entry)
    user = UserCoordinator(hass=hass, client=api, include_phones=True)
    await user.async_config_entry_first_refresh()
    user_id = user.data["id"]
    vehicles = user.get_vehicles()
    vehicle_control = user_input.get(CONF_VEHICLE_CONTROL, [])
    device_registry = dr.async_get(hass)

    if vehicle_control and not user.data.get("registrationChannels"):
        await api.close()
        raise SchemaFlowError("2fa_error")

    if vehicle_control and not entry.options.get("private_key"):
        public_key, private_key = generate_key_pair()
        user_input["public_key"] = public_key
        user_input["private_key"] = private_key
    else:
        public_key = entry.options.get("public_key")
    if enrolled_data := user.get_enrolled_phone_data(public_key=public_key):
        vehicle_identity = enrolled_data[1]
    else:
        vehicle_identity = {}

    if vehicle_control:
        control_vehicles = {
            identifier[1]: device_id
            for device_id in vehicle_control
            if (device := device_registry.async_get(device_id))
            for identifier in device.identifiers
            if identifier[0] == DOMAIN and identifier[1] in vehicles
        }
    else:
        control_vehicles = {}

    for vehicle_id, vehicle in vehicles.items():
        vehicle_name = vehicle["name"]

        # check if phone needs to be enrolled
        if vehicle_id in control_vehicles and vehicle_id not in vehicle_identity:
            success = False
            try:
                if not (
                    success := await api.enroll_phone(
                        user_id=user_id,
                        vehicle_id=vehicle["id"],
                        device_type="HA",
                        device_name=hass.config.location_name,
                        public_key=public_key,
                    )
                ):
                    _LOGGER.warning("Unable to enable control for %s", vehicle_name)
            except RivianPhoneLimitReachedError:
                _LOGGER.error(
                    "Unable to enable control for %s: phone limit reached", vehicle_name
                )
            except Exception as ex:  # pylint: disable=broad-except
                _LOGGER.error("Unable to enable control for %s: %s", vehicle_name, ex)
            if not success:
                user_input[CONF_VEHICLE_CONTROL] = [
                    vehicle
                    for vehicle in user_input[CONF_VEHICLE_CONTROL]
                    if vehicle != control_vehicles[vehicle_id]
                ]

        # check if phone needs to be disenrolled
        elif vehicle_id not in control_vehicles and vehicle_id in vehicle_identity:
            success = False
            try:
                identity_id = vehicle_identity[vehicle_id]
                if not (success := await api.disenroll_phone(identity_id=identity_id)):
                    _LOGGER.warning("Unable to disable control for %s", vehicle_name)
            except Exception as ex:  # pylint: disable=broad-except
                _LOGGER.error("Unable to disable control for %s: %s", vehicle_name, ex)
            # should we do something else if unable to disenroll?

    await api.close()
    return user_input


OPTIONS_FLOW = {
    "init": SchemaFlowFormStep(
        OPTIONS_SCHEMA, validate_user_input=validate_vehicle_control
    )
}


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


class RivianFlowHandler(ConfigFlow, domain=DOMAIN):
    """Config flow for Rivian"""

    VERSION = 1

    def __init__(self):
        """Initalize"""
        self._rivian = Rivian()
        self._data = {}
        self._errors = {}

        self._access_token = None
        self._refresh_token = None
        self._session_token = None
        self._user_session_token = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SchemaOptionsFlowHandler:
        """Get the options flow for this handler."""
        return SchemaOptionsFlowHandler(config_entry, OPTIONS_FLOW)

    # pylint: disable=protected-access
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the flow."""
        if user_input is None:
            return await self._show_credential_fields()

        self._data.update(user_input)

        if (otp := user_input.get(CONF_OTP)) is not None:
            username = self._data[CONF_USERNAME]
            try:
                await self._rivian.validate_otp(username, otp)
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

            if self._rivian._access_token:
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
            await self._rivian.authenticate(username, password)
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
        self._data.update(user_input)
        return await self._show_credential_fields(user_input)

    async def _async_create_entry(self) -> FlowResult:
        """Create the config entry."""
        await self._rivian.close()
        config_data = {
            CONF_USERNAME: self._data[CONF_USERNAME],
            CONF_ACCESS_TOKEN: self._access_token,
            CONF_REFRESH_TOKEN: self._refresh_token,
            CONF_USER_SESSION_TOKEN: self._user_session_token,
        }

        entry_id = self.context.get("entry_id")
        if existing_entry := self.hass.config_entries.async_get_entry(entry_id):
            self.hass.config_entries.async_update_entry(
                existing_entry, data=config_data
            )
            await self.hass.config_entries.async_reload(existing_entry.entry_id)
            return self.async_abort(reason="reauth_successful")

        return self.async_create_entry(title="Rivian (Unofficial)", data=config_data)

    async def _show_credential_fields(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show the configuration form to add credentials."""
        return self.async_show_form(
            step_id="user",
            data_schema=_get_schema_credential_fields(user_input, self._data),
            errors=self._errors,
        )

    async def _show_otp_field(self) -> FlowResult:
        """Show the configuration form to verify otp."""
        return self.async_show_form(
            step_id="user", data_schema=STEP_OTP_DATA_SCHEMA, errors=self._errors
        )
