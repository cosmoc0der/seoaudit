from __future__ import annotations

import asyncio

import httpx

from seoaudit.core.http import FetchResult, fetch

_HEAD_UNSUPPORTED = {400, 403, 404, 405, 406, 429, 501}


class ResourceProber:
    """Cached, concurrency-limited status/size lookups for linked resources."""

    def __init__(self, client: httpx.AsyncClient, concurrency: int = 10, max_bytes: int = 30 * 1024 * 1024):
        self._client = client
        self._semaphore = asyncio.Semaphore(concurrency)
        self._max_bytes = max_bytes
        self._cache: dict[str, asyncio.Task[FetchResult]] = {}

    async def probe(self, url: str, need_size: bool = False) -> FetchResult:
        task = self._cache.get(url)
        if task is not None:
            result = await task
            if not need_size or result.size is not None or result.broken:
                return result
        task = asyncio.ensure_future(self._probe(url, need_size))
        self._cache[url] = task
        return await task

    async def probe_many(self, urls: list[str], need_size: bool = False) -> dict[str, FetchResult]:
        results = await asyncio.gather(*(self.probe(u, need_size) for u in urls))
        return dict(zip(urls, results))

    async def _probe(self, url: str, need_size: bool) -> FetchResult:
        async with self._semaphore:
            result = await fetch(self._client, url, method="HEAD", read_body=False)
            if result.error or result.status in _HEAD_UNSUPPORTED or (need_size and result.size is None):
                result = await fetch(self._client, url, keep_body=False, max_bytes=self._max_bytes)
            return result
