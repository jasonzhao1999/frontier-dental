"""
HTTP fetcher with rate limiting, retries, and polite delays.

Wraps httpx to keep the rest of the codebase from dealing with
transport-level concerns.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from scraper.config import ScrapingConfig

log = logging.getLogger(__name__)


class Fetcher:
    def __init__(self, cfg: ScrapingConfig):
        self.cfg = cfg
        self._last_request_time = 0.0
        self._semaphore = asyncio.Semaphore(cfg.max_concurrency)
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(
            headers={
                "User-Agent": self.cfg.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            },
            timeout=httpx.Timeout(self.cfg.timeout),
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *exc):
        if self._client:
            await self._client.aclose()

    async def _throttle(self):
        """Enforce minimum delay between requests."""
        elapsed = time.monotonic() - self._last_request_time
        if elapsed < self.cfg.request_delay:
            await asyncio.sleep(self.cfg.request_delay - elapsed)
        self._last_request_time = time.monotonic()

    @retry(
        retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TransportError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=15),
    )
    async def _do_get(self, url: str) -> httpx.Response:
        resp = await self._client.get(url)
        # Raise on 5xx so tenacity retries; 4xx we want to handle ourselves
        if resp.status_code >= 500:
            resp.raise_for_status()
        return resp

    async def get(self, url: str) -> httpx.Response:
        async with self._semaphore:
            await self._throttle()
            log.info(f"GET {url}")
            try:
                resp = await self._do_get(url)
                log.debug(f"  -> {resp.status_code} ({len(resp.text)} chars)")
                return resp
            except Exception:
                log.exception(f"Failed to fetch {url}")
                raise

    async def get_text(self, url: str) -> tuple[str, int]:
        """Convenience — returns (html, status_code)."""
        resp = await self.get(url)
        return resp.text, resp.status_code
