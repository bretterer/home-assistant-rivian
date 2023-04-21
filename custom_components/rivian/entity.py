"""Rivian entities."""
from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta
import logging
from typing import Any

from rivian import Rivian
from rivian.exceptions import RivianExpiredTokenError

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo, EntityDescription
import homeassistant.helpers.entity_registry as er
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)
from homeassistant.util import slugify

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_USER_SESSION_TOKEN,
    DOMAIN,
    INVALID_SENSOR_STATES,
    UPDATE_INTERVAL,
    VEHICLE_STATE_API_FIELDS,
)

_LOGGER = logging.getLogger(__name__)


class RivianDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the API."""

    def __init__(self, hass: HomeAssistant, client: Rivian, entry: ConfigEntry):
        """Initialize."""
        self._hass = hass
        self._api = client
        self._entry = entry
        self._login_attempts = 0
        self._vehicles: dict[str, dict[str, Any]] | None = None
        self._wallboxes: list[dict[str, Any]] | None = None

        # sync tokens from initial configuration
        self._api._access_token = entry.data.get(CONF_ACCESS_TOKEN)
        self._api._refresh_token = entry.data.get(CONF_REFRESH_TOKEN)
        self._api._user_session_token = entry.data.get(CONF_USER_SESSION_TOKEN)

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )

    @property
    def vehicles(self) -> dict[str, dict[str, Any]]:
        """Return the vehicles."""
        return self._vehicles or {}

    @property
    def wallboxes(self) -> list[dict[str, Any]]:
        """Return the wallboxes."""
        return self._wallboxes or []

    async def process_new_data(self, vin: str, data: dict[str, Any]) -> None:
        """Process new data."""
        if self.data:
            self.data[vin] = self._build_vehicle_info_dict(vin, data["payload"])
            self.async_update_listeners()

    async def _update_api_data(self):
        """Update data via api."""
        try:
            if self._login_attempts >= 5:
                raise Exception("too many attempts to login - aborting")

            # determine if we need to authenticate and/or refresh csrf
            if not self._api._csrf_token:
                _LOGGER.info("Rivian csrf token not set - creating")
                await self._api.create_csrf_token()
            if (
                not self._api._app_session_token
                or not self._api._user_session_token
                or not self._api._refresh_token
            ):
                _LOGGER.info(
                    "Rivian app_session_token, user_session_token or refresh_token not set - authenticating"
                )
                self._login_attempts += 1
                await self._api.authenticate_graphql(
                    self._entry.data.get(CONF_USERNAME),
                    self._entry.data.get(CONF_PASSWORD),
                )

            if self._vehicles is None:
                await self._fetch_vehicles()

                # set up subscriptions
                for vin in self._vehicles:
                    await self._api.subscribe_for_vehicle_updates(
                        vin,
                        properties=VEHICLE_STATE_API_FIELDS,
                        callback=lambda data: self.process_new_data(vin, data),
                    )

            # fetch vehicle sensor data
            vehicle_states: dict[str, Any] = {}
            for vin in self._vehicles:
                vehicle_info = await self._api.get_vehicle_state(
                    vin=vin, properties=VEHICLE_STATE_API_FIELDS
                )
                vijson = await vehicle_info.json()
                vehicle_states[vin] = self._build_vehicle_info_dict(vin, vijson)
            self._login_attempts = 0

            if self._wallboxes or self._wallboxes is None:
                resp = await self._api.get_registered_wallboxes()
                if resp.status == 200:
                    wbjson = await resp.json()
                    _LOGGER.debug(wbjson)
                    self._wallboxes = wbjson["data"]["getRegisteredWallboxes"]

            return vehicle_states
        except (
            RivianExpiredTokenError
        ):  # graphql is always 200 - no exception parsing yet
            _LOGGER.info("Rivian token expired, refreshing")

            await self._api.create_csrf_token()
            return await self._update_api_data()
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.error(
                "Unknown Exception while updating Rivian data: %s", err, exc_info=1
            )
            raise Exception("Error communicating with API") from err

    async def _async_update_data(self):
        """Update data via library, refresh token if necessary."""
        try:
            return await self._update_api_data()
        except (
            RivianExpiredTokenError
        ):  # graphql is always 200 - no exception parsing yet
            _LOGGER.info("Rivian token expired, refreshing")
            await self._api.create_csrf_token()
            return await self._update_api_data()
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.error(
                "Unknown Exception while updating Rivian data: %s", err, exc_info=1
            )
            raise Exception("Error communicating with API") from err

    async def _fetch_vehicles(self) -> None:
        """Fetch user's accessible vehicles."""
        user_information = await self._api.get_user_information()
        uijson = await user_information.json()
        _LOGGER.debug(uijson)
        if uijson:
            self._vehicles = {
                vehicle["vin"]: vehicle["vehicle"]
                for vehicle in uijson["data"]["currentUser"]["vehicles"]
            }
        else:
            self._vehicles = {}

    def _build_vehicle_info_dict(
        self, vin: str, vijson: dict
    ) -> dict[str, dict[str, Any]]:
        """Take the json output of vehicle_info and build a dictionary."""
        _LOGGER.debug("VIN: %s, data: %s", vin, vijson)
        items = vijson["data"]["vehicleState"]

        if not (prev_items := (self.data or {}).get(vin) or items) or not items:
            return prev_items

        for i in items:
            if not items[i]:
                items[i] = prev_items.get(i)
            if not items[i]:
                continue
            if i == "gnssLocation":
                continue
            value = items[i]["value"]
            prev_value = prev_items[i]["value"]
            prev_items[i].setdefault("history", set()).add(value)
            if str(value).lower() in INVALID_SENSOR_STATES and value != prev_value:
                items[i] = prev_items[i]
            items[i]["history"] = prev_items[i]["history"]
        return items


class RivianEntity(CoordinatorEntity[RivianDataUpdateCoordinator]):
    """Base class for Rivian entities."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: RivianDataUpdateCoordinator,
        config_entry: ConfigEntry,
        description: EntityDescription,
        vin: str,
    ) -> None:
        """Construct a RivianEntity."""
        super().__init__(coordinator)
        self._config_entry = config_entry
        self.entity_description = description
        self._vin = vin
        self._attr_unique_id = f"{vin}-{description.key}"

        self._available = True

        manufacturer = "Rivian"
        model = coordinator.vehicles[vin]["model"]
        model_year = coordinator.vehicles[vin]["modelYear"]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, vin), get_device_identifier(config_entry)},
            name=f"{manufacturer} {model}" if model else manufacturer,
            manufacturer=manufacturer,
            model=f"{model_year} {manufacturer} {model}"
            if model and model_year
            else None,
            sw_version=self._get_value("otaCurrentVersion"),
        )

    @property
    def available(self) -> bool:
        """Return the availability of the entity."""
        return self._available

    def _get_value(self, key: str) -> Any | None:
        """Get a data value from the coordinator."""
        if entity := self.coordinator.data[self._vin].get(key, {}):
            return entity.get("value")
        return None


class RivianWallboxEntity(CoordinatorEntity[RivianDataUpdateCoordinator]):
    """Base class for Rivian wallbox entities."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: RivianDataUpdateCoordinator,
        description: EntityDescription,
        wallbox: dict[str, Any],
    ) -> None:
        """Construct a Rivian wallbox entity."""
        super().__init__(coordinator)
        self.entity_description = description
        self.wallbox = wallbox

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, wallbox["serialNumber"])},
            name=wallbox["name"],
            manufacturer="Rivian",
            model=wallbox["model"],
            sw_version=wallbox["softwareVersion"],
        )
        self._attr_unique_id = f"{wallbox['serialNumber']}-{description.key}"

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        wallbox = next(
            (
                wallbox
                for wallbox in self.coordinator.wallboxes
                if wallbox["wallboxId"] == self.wallbox["wallboxId"]
            ),
            self.wallbox,
        )
        if self.wallbox != wallbox:
            self.wallbox = wallbox
            self.async_write_ha_state()


def get_device_identifier(
    entry: ConfigEntry, name: str | None = None
) -> tuple[str, str]:
    """Get a device identifier."""
    if name:
        return (DOMAIN, f"{entry.entry_id}:{DOMAIN}:{slugify(name)}")
    return (DOMAIN, f"{DOMAIN}:{entry.entry_id}")


def async_update_unique_id(
    hass: HomeAssistant, domain: str, entities: Iterable[RivianEntity]
) -> None:
    """Update unique ID to be based on VIN and entity description key instead of name."""
    ent_reg = er.async_get(hass)
    for entity in entities:
        if not (old_key := getattr(entity.entity_description, "old_key")):
            continue
        old_unique_id = f"{DOMAIN}_{old_key}_{entity._config_entry.entry_id}"  # pylint: disable=protected-access
        if entity_id := ent_reg.async_get_entity_id(domain, DOMAIN, old_unique_id):
            new_unique_id = f"{entity._vin}-{entity.entity_description.key}"  # pylint: disable=protected-access
            ent_reg.async_update_entity(entity_id, new_unique_id=new_unique_id)
