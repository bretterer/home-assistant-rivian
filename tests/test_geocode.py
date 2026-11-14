"""Unit tests for geocode.py: Nominatim name-building, rate limiting, and failure handling."""

from __future__ import annotations

from typing import Any, Self

import pytest

from custom_components.rivian import geocode


class _FakeResponse:
    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self._payload = payload

    async def json(self, content_type: Any = None) -> Any:
        return self._payload

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeSession:
    def __init__(
        self, response: Any = None, raise_error: Exception | None = None
    ) -> None:
        self._response = response
        self._raise_error = raise_error
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> Any:
        self.calls.append({"url": url, **kwargs})
        if self._raise_error is not None:
            raise self._raise_error
        return self._response


class TestNameFromAddress:
    """_name_from_address: the POI/road/house-number preference order."""

    def test_poi_plus_road(self) -> None:
        address = {"shop": "Joe's Cafe", "road": "Main St"}
        assert geocode._name_from_address(address, None) == "Joe's Cafe, Main St"

    def test_amenity_plus_road(self) -> None:
        address = {"amenity": "Library", "road": "Oak Ave"}
        assert geocode._name_from_address(address, None) == "Library, Oak Ave"

    def test_house_number_plus_road_when_no_poi(self) -> None:
        address = {"house_number": "123", "road": "Elm St"}
        assert geocode._name_from_address(address, None) == "123 Elm St"

    def test_road_alone_when_no_house_number(self) -> None:
        address = {"road": "Elm St"}
        assert geocode._name_from_address(address, None) == "Elm St"

    def test_falls_back_to_display_name_first_part(self) -> None:
        address: dict[str, Any] = {}
        display_name = "Somewhere, Some County, Some State, USA"
        assert geocode._name_from_address(address, display_name) == "Somewhere"

    def test_returns_none_when_nothing_usable(self) -> None:
        assert geocode._name_from_address({}, None) is None

    def test_returns_none_for_blank_display_name(self) -> None:
        assert geocode._name_from_address({}, "   ") is None


class TestAsyncReverse:
    """async_reverse: success, HTTP-error, and network-error paths; rate limiting."""

    @pytest.mark.asyncio
    async def test_success_returns_built_name(self, monkeypatch: Any) -> None:
        payload = {
            "address": {"shop": "Joe's Cafe", "road": "Main St"},
            "display_name": "Joe's Cafe, Main St, Anytown",
        }
        session = _FakeSession(response=_FakeResponse(200, payload))
        monkeypatch.setattr(geocode, "async_get_clientsession", lambda hass: session)
        geocode._last_request_ts[0] = 0.0

        name = await geocode.async_reverse(object(), 37.0, -122.0)
        assert name == "Joe's Cafe, Main St"

    @pytest.mark.asyncio
    async def test_sends_expected_request_shape(self, monkeypatch: Any) -> None:
        payload = {"address": {"road": "Main St"}, "display_name": "Main St"}
        session = _FakeSession(response=_FakeResponse(200, payload))
        monkeypatch.setattr(geocode, "async_get_clientsession", lambda hass: session)
        geocode._last_request_ts[0] = 0.0

        await geocode.async_reverse(object(), 37.123456, -122.654321)
        assert len(session.calls) == 1
        call = session.calls[0]
        assert call["url"] == geocode.NOMINATIM_URL
        assert call["params"]["lat"] == "37.123456"
        assert call["params"]["lon"] == "-122.654321"
        assert call["params"]["format"] == "jsonv2"
        assert "User-Agent" in call["headers"]

    @pytest.mark.asyncio
    async def test_non_200_status_returns_none(self, monkeypatch: Any) -> None:
        session = _FakeSession(response=_FakeResponse(500, {}))
        monkeypatch.setattr(geocode, "async_get_clientsession", lambda hass: session)
        geocode._last_request_ts[0] = 0.0

        name = await geocode.async_reverse(object(), 37.0, -122.0)
        assert name is None

    @pytest.mark.asyncio
    async def test_network_error_returns_none(self, monkeypatch: Any) -> None:
        import aiohttp

        session = _FakeSession(raise_error=aiohttp.ClientError("boom"))
        monkeypatch.setattr(geocode, "async_get_clientsession", lambda hass: session)
        geocode._last_request_ts[0] = 0.0

        name = await geocode.async_reverse(object(), 37.0, -122.0)
        assert name is None

    @pytest.mark.asyncio
    async def test_no_address_data_returns_none(self, monkeypatch: Any) -> None:
        payload = {"display_name": None}
        session = _FakeSession(response=_FakeResponse(200, payload))
        monkeypatch.setattr(geocode, "async_get_clientsession", lambda hass: session)
        geocode._last_request_ts[0] = 0.0

        name = await geocode.async_reverse(object(), 0.0, 0.0)
        assert name is None

    @pytest.mark.asyncio
    async def test_non_dict_response_returns_none(self, monkeypatch: Any) -> None:
        session = _FakeSession(response=_FakeResponse(200, ["not", "a", "dict"]))
        monkeypatch.setattr(geocode, "async_get_clientsession", lambda hass: session)
        geocode._last_request_ts[0] = 0.0

        name = await geocode.async_reverse(object(), 0.0, 0.0)
        assert name is None

    @pytest.mark.asyncio
    async def test_rate_limit_waits_before_a_second_request(
        self, monkeypatch: Any
    ) -> None:
        import time

        payload = {"address": {"road": "Main St"}, "display_name": "Main St"}
        session = _FakeSession(response=_FakeResponse(200, payload))
        monkeypatch.setattr(geocode, "async_get_clientsession", lambda hass: session)

        sleep_calls: list[float] = []

        async def _fake_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        monkeypatch.setattr(geocode.asyncio, "sleep", _fake_sleep)
        geocode._last_request_ts[0] = time.monotonic()  # "just happened"

        await geocode.async_reverse(object(), 37.0, -122.0)
        assert sleep_calls  # a wait was requested since <1s elapsed
        assert sleep_calls[0] <= geocode.MIN_REQUEST_INTERVAL_SECONDS
