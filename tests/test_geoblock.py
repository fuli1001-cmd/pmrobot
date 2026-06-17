"""Tests for Polymarket geoblock checks."""

from unittest.mock import AsyncMock

import pytest

from utils.geoblock import GeoblockCheckError, check_polymarket_geoblock


class _MockResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _MockClient:
    def __init__(self, response):
        self._response = response
        self.get = AsyncMock(return_value=response)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_check_polymarket_geoblock_parses_response(monkeypatch):
    response = _MockResponse(
        {
            "blocked": False,
            "ip": "198.51.100.10",
            "country": "IE",
            "region": "L",
        }
    )

    monkeypatch.setattr(
        "utils.geoblock.httpx.AsyncClient",
        lambda **kwargs: _MockClient(response),
    )

    status = await check_polymarket_geoblock()

    assert status.blocked is False
    assert status.ip == "198.51.100.10"
    assert status.country == "IE"
    assert status.region == "L"
    assert status.location == "IE-L"


@pytest.mark.asyncio
async def test_check_polymarket_geoblock_rejects_malformed_payload(monkeypatch):
    response = _MockResponse(["not-a-dict"])

    monkeypatch.setattr(
        "utils.geoblock.httpx.AsyncClient",
        lambda **kwargs: _MockClient(response),
    )

    with pytest.raises(GeoblockCheckError):
        await check_polymarket_geoblock()
