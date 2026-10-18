"""Only real raster images are ever stored as a vehicle picture."""

from __future__ import annotations

from typing import Any, Self
from unittest.mock import MagicMock, patch

import pytest

from custom_components.rivian import vehicle_picture
from custom_components.rivian.vehicle_picture import download_image, sniff_image_type

JPEG = bytes([0xFF, 0xD8, 0xFF, 0xE0]) + b"jpegdata"
PNG = bytes([0x89]) + b"PNG\r\n\x1a\n" + b"pngdata"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


@pytest.mark.parametrize(
    ("data", "kind"),
    [
        (JPEG, "image/jpeg"),
        (PNG, "image/png"),
        (b"GIF89a" + b"rest", "image/gif"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
        (SVG, None),
        (b"<html><body>hi</body></html>", None),
        (b"", None),
    ],
)
def test_sniff_image_type(data: bytes, kind: str | None) -> None:
    assert sniff_image_type(data) == kind


class _Content:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def iter_chunked(self, _size: int) -> Any:
        yield self._data


class _Response:
    def __init__(self, data: bytes, content_type: str) -> None:
        self.status = 200
        self.content_type = content_type
        self.content = _Content(data)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


def _session(data: bytes, content_type: str) -> MagicMock:
    session = MagicMock()
    session.get = MagicMock(return_value=_Response(data, content_type))
    return session


@pytest.mark.asyncio
async def test_svg_served_as_an_image_is_not_stored() -> None:
    """An SVG (which can carry script) is rejected even if labeled image/*."""
    with patch.object(
        vehicle_picture,
        "async_get_clientsession",
        return_value=_session(SVG, "image/svg+xml"),
    ):
        assert await download_image(MagicMock(), "https://example.com/car.svg") is None


@pytest.mark.asyncio
async def test_type_comes_from_the_bytes_not_the_header() -> None:
    with patch.object(
        vehicle_picture,
        "async_get_clientsession",
        return_value=_session(PNG, "image/jpeg"),
    ):
        result = await download_image(MagicMock(), "https://example.com/car.jpg")
    assert result == ("image/png", PNG)
