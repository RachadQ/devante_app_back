"""Small cached client for no-key reference exchange rates."""

import asyncio
import time

import httpx

_rate_cache: dict[tuple[str, str], tuple[float, float]] = {}
_CACHE_SECONDS = 60 * 60 * 12


async def rates_for(currencies: set[str], target: str) -> dict[str, float]:
    """Return latest rates into target; unavailable pairs are omitted."""
    target = target.upper()
    result = {target: 1.0}
    now = time.monotonic()

    async def fetch(source: str) -> None:
        source = source.upper()
        if source == target:
            return
        key = (source, target)
        cached = _rate_cache.get(key)
        if cached and now - cached[1] < _CACHE_SECONDS:
            result[source] = cached[0]
            return
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(3.0, connect=1.5),
                                         follow_redirects=False) as client:
                response = await client.get(
                    f"https://api.frankfurter.dev/v2/rate/{source.lower()}/{target.lower()}"
                )
                response.raise_for_status()
                rate = float(response.json()["rate"])
                if rate > 0:
                    _rate_cache[key] = (rate, now)
                    result[source] = rate
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            return

    await asyncio.gather(*(fetch(code) for code in currencies))
    return result
