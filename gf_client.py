"""
Read-only Gravity Forms REST API v2 client.
Authenticates with Consumer Key + Consumer Secret via HTTP Basic Auth.
No wp-login.php, no Cloudflare challenge.

Usage:
  async with GFClient() as gf:
      forms   = await gf.get("/wp-json/gf/v2/forms")
      entries = await gf.entries(form_id=3, search={"field_filters": [...]})
"""

import os
import logging
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

# Site is configured via env so the deployment target isn't baked into source.
BASE_URL = os.environ.get("SITE_BASE_URL", "https://example.com")
TIMEOUT  = aiohttp.ClientTimeout(total=15)


class GFClient:
    def __init__(self):
        self._auth = {
            "consumer_key":    os.environ["GF_CONSUMER_KEY"],
            "consumer_secret": os.environ["GF_CONSUMER_SECRET"],
        }
        self._headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
        }
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *_):
        if self._session:
            await self._session.close()

    async def get(self, path: str, params: dict | None = None) -> Any:
        url = f"{BASE_URL}{path}"
        merged = {**(params or {}), **self._auth}
        async with self._session.get(
            url, params=merged, headers=self._headers, timeout=TIMEOUT
        ) as resp:
            if resp.status == 401:
                raise RuntimeError("GF auth failed — check GF_CONSUMER_KEY / GF_CONSUMER_SECRET")
            resp.raise_for_status()
            return await resp.json()

    async def forms(self) -> list[dict]:
        data = await self.get("/wp-json/gf/v2/forms")
        return list(data.values()) if isinstance(data, dict) else data

    async def entries(self, form_id: int, params: dict | None = None) -> list[dict]:
        data = await self.get(f"/wp-json/gf/v2/forms/{form_id}/entries", params=params)
        return data.get("entries", data) if isinstance(data, dict) else data
