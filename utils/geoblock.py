"""Polymarket geographic eligibility checks."""

from dataclasses import dataclass

import httpx


GEOBLOCK_URL = "https://polymarket.com/api/geoblock"


class GeoblockCheckError(RuntimeError):
    """Raised when the geoblock endpoint cannot be evaluated safely."""


@dataclass
class GeoblockStatus:
    """Geographic eligibility for the current egress IP."""

    blocked: bool
    ip: str
    country: str
    region: str

    @property
    def location(self) -> str:
        """Return a compact location label."""
        if self.region:
            return f"{self.country}-{self.region}"
        return self.country


async def check_polymarket_geoblock(timeout: float = 10.0) -> GeoblockStatus:
    """
    Check whether the current egress IP can place Polymarket orders.

    Raises:
        GeoblockCheckError: If the endpoint cannot be reached or the response
            is malformed.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(GEOBLOCK_URL)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise GeoblockCheckError(f"geoblock request failed: {exc!r}") from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise GeoblockCheckError("geoblock response was not valid JSON") from exc

    if not isinstance(data, dict) or "blocked" not in data:
        raise GeoblockCheckError(f"unexpected geoblock response: {data!r}")

    return GeoblockStatus(
        blocked=bool(data["blocked"]),
        ip=str(data.get("ip", "")),
        country=str(data.get("country", "")),
        region=str(data.get("region", "")),
    )
