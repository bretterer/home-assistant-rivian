"""Rivian entities."""
from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta
import logging
from typing import Any

from rivian import Rivian
from rivian.exceptions import RivianExpiredTokenError

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_MODEL, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo, EntityDescription
import homeassistant.helpers.entity_registry as er
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)
from homeassistant.util import slugify

from .const import (
    BINARY_SENSORS,
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_USER_SESSION_TOKEN,
    CONF_VIN,
    DOMAIN,
    INVALID_SENSOR_STATES,
    SENSORS,
    UPDATE_INTERVAL,
    UPDATE_SENSOR_FIELDS,
)
from .helpers import get_model_and_year

_LOGGER = logging.getLogger(__name__)


class RivianDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the API."""

    def __init__(self, hass: HomeAssistant, client: Rivian, entry: ConfigEntry):
        """Initialize."""
        self._hass = hass
        self._api = client
        self._entry = entry
        self._vin = entry.data.get(CONF_VIN)
        self._login_attempts = 0
        self._previous_vehicle_info_items = None
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
    def wallboxes(self) -> list[dict[str, Any]]:
        """Return the wallboxes."""
        return self._wallboxes or []

    async def _update_api_data(self):
        """Update data via api."""
        sensors: set[str] = {"gnssLocation"}
        sensors.update(sensor.field for sensor in SENSORS)
        for sensor in BINARY_SENSORS:
            fields = [sensor.field] if isinstance(sensor.field, str) else sensor.field
            sensors.update(fields)
        sensors.update(UPDATE_SENSOR_FIELDS)

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

            # fetch vehicle sensor data
            vehicle_info = await self._api.get_vehicle_state(
                vin=self._vin,
                properties=sensors,
            )
            vijson = await vehicle_info.json()
            self._login_attempts = 0

            if self._wallboxes or self._wallboxes is None:
                resp = await self._api.get_registered_wallboxes()
                if resp.status == 200:
                    wbjson = await resp.json()
                    _LOGGER.debug(wbjson)
                    self._wallboxes = wbjson["data"]["getRegisteredWallboxes"]

            return self.build_vehicle_info_dict(vijson)
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

    def build_vehicle_info_dict(self, vijson) -> dict[str, dict[str, Any]]:
        """take the json output of vehicle_info and build a dictionary"""

        _LOGGER.debug(vijson)
        items = vijson["data"]["vehicleState"]

        if not self._previous_vehicle_info_items:
            for i in list(filter(lambda x: items[x] is not None, items)):
                if i == "gnssLocation":
                    continue
                value = str(items[i]["value"]).lower()
                items[i]["history"] = {items[i]["value"]}
                self._previous_vehicle_info_items = items
            return items
        elif not items:
            return self._previous_vehicle_info_items

        for i in list(filter(lambda x: items[x] is not None, items)):
            if i == "gnssLocation":
                continue
            value = str(items[i]["value"]).lower()
            prev_value = self._previous_vehicle_info_items[i]["value"]
            self._previous_vehicle_info_items[i]["history"].add(items[i]["value"])
            if value in INVALID_SENSOR_STATES and prev_value:
                items[i] = self._previous_vehicle_info_items[i]
            else:
                items[i]["history"] = self._previous_vehicle_info_items[i]["history"]

        self._previous_vehicle_info_items = items
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
        model, year = get_model_and_year(vin)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, vin), get_device_identifier(config_entry)},
            name=f"{manufacturer} {model}" if model else manufacturer,
            manufacturer=manufacturer,
            model=f"{year} {manufacturer} {model}" if model and year else None,
            sw_version=self._get_value("otaCurrentVersion"),
        )

    @property
    def available(self) -> bool:
        """Return the availability of the entity."""
        return self._available

    def _get_model(self) -> str:
        """Get the Rivian model string."""
        return str(self.hass.data[DOMAIN][self._config_entry.entry_id][ATTR_MODEL])

    def _get_value(self, key: str) -> Any | None:
        """Get a data value from the coordinator."""
        if entity := self.coordinator.data.get(key, {}):
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
    else:
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
