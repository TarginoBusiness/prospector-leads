"""Helpers compartilhados por todos os scrapers."""
from __future__ import annotations

import asyncio
import logging
import random
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fake_useragent import UserAgent
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")

_ua = UserAgent()


def random_headers() -> dict[str, str]:
    return {
        "User-Agent": _ua.random,
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
    }


async def human_delay(lo: float = 2.0, hi: float = 7.0) -> None:
    await asyncio.sleep(random.uniform(lo, hi))


@asynccontextmanager
async def http_client(**kwargs: Any):
    """httpx async client com defaults bons (timeout, http2, headers rotativos)."""
    timeout = httpx.Timeout(30.0, connect=10.0)
    async with httpx.AsyncClient(
        http2=True,
        timeout=timeout,
        follow_redirects=True,
        headers=random_headers(),
        **kwargs,
    ) as client:
        yield client


@retry(
    retry=retry_if_exception_type((httpx.HTTPError, asyncio.TimeoutError)),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    stop=stop_after_attempt(3),
    reraise=True,
)
async def fetch(client: httpx.AsyncClient, url: str) -> httpx.Response:
    """GET com retry exponencial. Lanca exception apos 3 falhas."""
    resp = await client.get(url)
    resp.raise_for_status()
    return resp
