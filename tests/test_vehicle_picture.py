"""Tests for the once-per-vehicle configurator picture."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.rivian import image as image_platform
from custom_components.rivian.analytics_db import VehiclePicture
from custom_components.rivian.vehicle_picture import (
    RETRY_AFTER_SECONDS,
    async_fetch_vehicle_pictures,
    async_picture_from_url,
    guessed_picture_candidates,
    order_vehicle_config,
    picture_url,
    user_vehicle_configs,
)

VIN = "7PDSGABA8NN000000"
META = {"vehicle": "R1S", "version": "2.2", "country": "US"}


def _order(vin: str = VIN, options: list[tuple[str, bool]] | None = None) -> dict:
    options = options or [
        ("EXP-LGR", True),
        ("WHL-1RD", True),
        ("PKG-LCH", True),
        ("INT-BMV", False),
    ]
    return {
        "data": {
            "order": {
                "vin": vin,
                "items": [
                    {"configuration": None},  # accessories carry no configuration
                    {
                        "configuration": {
                            "ruleset": {"meta": META},
                            "options": [
                                {
                                    "optionId": oid,
                                    "optionDetails": {"visualExterior": ext},
                                }
                                for oid, ext in options
                            ],
                        }
                    },
                ],
            }
        }
    }


def test_order_config_keeps_only_exterior_options() -> None:
    vin, meta, exterior = order_vehicle_config(_order())
    assert vin == VIN
    assert meta == META
    assert exterior == ["EXP-LGR", "WHL-1RD", "PKG-LCH"]  # interior excluded


def test_order_without_vin_or_configuration_yields_nothing() -> None:
    assert order_vehicle_config({"data": {"order": {"items": []}}}) is None
    assert order_vehicle_config({"data": {"order": {"vin": VIN, "items": []}}}) is None
    assert order_vehicle_config(None) is None


def test_picture_url_sorts_and_lowercases_codes() -> None:
    """Matches the configurator's documented form (codes alphabetical)."""
    url = picture_url(META, ["WHL-1RD", "EXP-LGR", "PKG-LCH"])
    assert url == (
        "https://rivian.com/compimg/r1s/2.2/us/exp-lgr_pkg-lch_whl-1rd@3072x2688.front.webp"
    )
    assert picture_url({"vehicle": "r1s"}, ["EXP-LGR"]) is None  # no ruleset version
    assert picture_url(META, []) is None


def _client(responses: list[Any]) -> MagicMock:
    client = MagicMock()

    async def graphql(_headers: dict, _url: str, _body: dict) -> Any:
        response = MagicMock()
        response.json = AsyncMock(return_value=responses.pop(0))
        return response

    client._Rivian__graphql_query = AsyncMock(side_effect=graphql)
    return client


@pytest.mark.asyncio
async def test_lookup_downloads_the_matching_vehicles_picture() -> None:
    orders = {"data": {"orders": {"data": [{"id": "other"}, {"id": "mine"}]}}}
    client = _client([orders, _order(vin="SOMEONEELSE00000"), _order()])
    with patch(
        "custom_components.rivian.vehicle_picture.download_image",
        AsyncMock(return_value=("image/webp", b"RIFF-webp")),
    ) as download:
        result = await async_fetch_vehicle_pictures(MagicMock(), client, {VIN})

    picture = result[VIN]
    assert picture.status == "ok"
    assert picture.image == b"RIFF-webp"
    assert picture.options == ["EXP-LGR", "WHL-1RD", "PKG-LCH"]
    download.assert_awaited_once()
    # The order query asks for no addresses or payments.
    order_body = client._Rivian__graphql_query.await_args_list[1].args[2]
    assert (
        "Address" not in order_body["query"] and "payments" not in order_body["query"]
    )


@pytest.mark.asyncio
async def test_any_failure_yields_a_failed_record_and_never_raises() -> None:
    client = MagicMock()
    client._Rivian__graphql_query = AsyncMock(side_effect=RuntimeError("api down"))
    result = await async_fetch_vehicle_pictures(MagicMock(), client, {VIN})
    assert result[VIN].status == "failed"
    assert result[VIN].image is None


class _Store:
    def __init__(self, saved: VehiclePicture | None) -> None:
        self.saved = saved
        self.writes: list[VehiclePicture] = []

    async def async_get_vehicle_picture(self) -> VehiclePicture | None:
        return self.saved

    async def async_save_vehicle_picture(self, picture: VehiclePicture) -> None:
        self.writes.append(picture)
        self.saved = picture


def _ok(ts: float) -> VehiclePicture:
    return VehiclePicture("ok", "image/webp", b"img", "https://x", ["EXP-LGR"], ts)


def _failed(ts: float) -> VehiclePicture:
    return VehiclePicture("failed", None, None, None, [], ts)


async def _setup(
    store: _Store, fetched: VehiclePicture | None = None
) -> tuple[list, AsyncMock]:
    fetch = AsyncMock(return_value={VIN: fetched or _ok(time.time())})
    with (
        patch.object(image_platform, "async_fetch_vehicle_pictures", fetch),
        patch.object(
            image_platform,
            "RivianVehiclePictureEntity",
            lambda _h, vin, pic: (vin, pic),
        ),
    ):
        entities = await image_platform._async_picture_entities(
            MagicMock(), MagicMock(), {"veh-1": {"vin": VIN}}, {"veh-1": store}
        )
    return entities, fetch


@pytest.mark.asyncio
async def test_first_sighting_fetches_once_and_saves() -> None:
    store = _Store(None)
    entities, fetch = await _setup(store)
    fetch.assert_awaited_once()
    assert len(store.writes) == 1 and store.saved.status == "ok"
    assert [vin for vin, _ in entities] == [VIN]


@pytest.mark.asyncio
async def test_saved_picture_is_served_without_any_fetch() -> None:
    store = _Store(_ok(time.time() - 400 * 86400))  # old is fine: it never expires
    entities, fetch = await _setup(store)
    fetch.assert_not_awaited()
    assert store.writes == []
    assert [pic.image for _, pic in entities] == [b"img"]


@pytest.mark.asyncio
async def test_recent_failure_is_not_retried_but_an_old_one_is() -> None:
    recent = _Store(_failed(time.time() - 60))
    entities, fetch = await _setup(recent)
    fetch.assert_not_awaited()
    assert entities == []

    stale = _Store(_failed(time.time() - RETRY_AFTER_SECONDS - 60))
    _entities, fetch = await _setup(stale)
    fetch.assert_awaited_once()


def _user(
    vin: str = VIN, paint: str | None = "EXP-FOR", trim: str | None = "PKG-ADV"
) -> dict:
    mobile = {
        "exteriorColorOption": {"optionId": paint} if paint else None,
        "trimOption": {"optionId": trim} if trim else None,
    }
    return {
        "data": {
            "currentUser": {
                "vehicles": [
                    {
                        "vin": vin,
                        "vehicle": {"model": "R1S", "mobileConfiguration": mobile},
                    }
                ]
            }
        }
    }


def test_user_configs_need_model_paint_and_trim() -> None:
    assert user_vehicle_configs(_user()) == {VIN: ("R1S", "EXP-FOR", "PKG-ADV")}
    assert user_vehicle_configs(_user(paint=None)) == {}
    assert user_vehicle_configs(None) == {}


def test_guess_tries_each_known_wheel_with_the_cars_paint_and_trim() -> None:
    candidates = guessed_picture_candidates("R1S", "EXP-FOR", "PKG-ADV")
    assert len(candidates) == 4
    url, codes = candidates[0]
    assert url == (
        "https://rivian.com/compimg/r1s/2.2/us/"
        "exp-for_ord-ad3_pkg-adv_whl-1rd@3072x2688.front.webp"
    )
    assert codes == ["EXP-FOR", "ORD-AD3", "PKG-ADV", "WHL-1RD"]


@pytest.mark.asyncio
async def test_guess_is_used_when_the_account_has_no_orders() -> None:
    no_orders = {"data": {"orders": {"data": []}}}
    client = _client([no_orders, _user()])
    downloads = AsyncMock(side_effect=[None, ("image/webp", b"guessed")])
    with patch("custom_components.rivian.vehicle_picture.download_image", downloads):
        result = await async_fetch_vehicle_pictures(MagicMock(), client, {VIN})

    assert result[VIN].status == "ok"
    assert result[VIN].image == b"guessed"
    assert result[VIN].options == ["EXP-FOR", "ORD-AD3", "PKG-ADV", "WHL-2SD"]
    assert downloads.await_count == 2  # stops at the first render that exists


@pytest.mark.asyncio
async def test_no_matching_render_is_a_failure_not_an_error() -> None:
    client = _client([{"data": {"orders": {"data": []}}}, _user()])
    with patch(
        "custom_components.rivian.vehicle_picture.download_image",
        AsyncMock(return_value=None),
    ):
        result = await async_fetch_vehicle_pictures(MagicMock(), client, {VIN})
    assert result[VIN].status == "failed"


@pytest.mark.asyncio
async def test_manual_picture_rejects_something_that_is_not_an_image() -> None:
    with (
        patch(
            "custom_components.rivian.vehicle_picture.download_image",
            AsyncMock(return_value=None),
        ),
        pytest.raises(ValueError),
    ):
        await async_picture_from_url(MagicMock(), "https://example.com/page.html")

    with patch(
        "custom_components.rivian.vehicle_picture.download_image",
        AsyncMock(return_value=("image/png", b"png")),
    ):
        picture = await async_picture_from_url(
            MagicMock(), "https://example.com/car.png"
        )
    assert (picture.status, picture.image, picture.options) == (
        "ok",
        b"png",
        ["manual"],
    )


def test_a_set_picture_updates_the_entity_or_adds_one() -> None:
    """rivian.set_vehicle_picture applies live, without a reload."""
    existing = MagicMock()
    added: list = []
    hass = MagicMock()
    hass.data = {
        image_platform.DOMAIN: {
            "entry1": {
                image_platform.PICTURE_PLATFORM: {
                    "add": added.extend,
                    "entities": {VIN: existing},
                }
            }
        }
    }
    picture = _ok(time.time())

    assert image_platform.async_apply_vehicle_picture(hass, "entry1", VIN, picture)
    existing.async_set_picture.assert_called_once_with(picture)

    with patch.object(
        image_platform, "RivianVehiclePictureEntity", lambda _h, vin, pic: (vin, pic)
    ):
        assert image_platform.async_apply_vehicle_picture(
            hass, "entry1", "OTHERVIN", picture
        )
    assert added == [("OTHERVIN", picture)]

    assert not image_platform.async_apply_vehicle_picture(
        hass, "no-such-entry", VIN, picture
    )
