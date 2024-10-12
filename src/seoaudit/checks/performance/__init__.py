from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from seoaudit.checks._utils import report_group
from seoaudit.core.check import Check
from seoaudit.core.http import FetchResult, fetch
from seoaudit.core.page import Page

_MAX_AGE = re.compile(r"(?:s-)?max-age=(\d+)")
_COMPRESSIBLE = ("text/", "application/javascript", "application/x-javascript", "application/json", "image/svg+xml")
_MODERN_HTTP = ("HTTP/2", "HTTP/3")


class PerformanceCheck(Check):
    id = "performance"

    async def setup(self) -> None:
        self._static: dict[str, str] = {}

    async def check_page(self, page: Page) -> None:
        response = page.response
        if response.ttfb_ms and response.ttfb_ms > self.params("ttfb_slow")["max_ms"]:
            self.fail("ttfb_slow", page.url, ttfb=round(response.ttfb_ms))

        size_kb = round((response.size or 0) / 1024)
        if size_kb > self.params("html_too_large")["max_kb"]:
            self.fail("html_too_large", page.url, size_kb=size_kb)
        if (response.size or 0) > 1024 and not response.headers.get("content-encoding"):
            self.fail("compression_missing", page.url, size_kb=size_kb)

        blocking = [
            s.src for s in page.scripts if s.src and s.in_head and not (s.is_async or s.defer) and s.type != "module"
        ]
        report_group(self, "render_blocking_scripts", page.url, blocking)

        external = [s.src for s in page.scripts if s.src] + page.stylesheets
        if len(external) > self.params("too_many_resources")["max_resources"]:
            self.fail("too_many_resources", page.url, count=len(external))

        for url in [*external, *(i.src for i in page.images if i.src)]:
            if self.ctx.scope.is_internal(url):
                self._static.setdefault(url, page.url)

    async def check_site(self) -> None:
        version = self.ctx.site.home.http_version
        if version and not version.startswith(_MODERN_HTTP):
            self.fail("http2_missing", self.ctx.site.base_url, version=version)
        await self._static_resources()

    async def _static_resources(self) -> None:
        urls = list(self._static)[: self.options["max_static_resources"]]
        if not urls:
            self.not_applicable("cache_missing", "no_static_resources")
            self.not_applicable("static_compression_missing", "no_static_resources")
            return

        semaphore = asyncio.Semaphore(self.ctx.config.crawl.concurrency)

        async def headers_only(url: str) -> FetchResult:
            async with semaphore:
                return await fetch(self.ctx.client, url, read_body=False)

        min_seconds = self.params("cache_missing")["min_seconds"]
        results = await asyncio.gather(*(headers_only(u) for u in urls))
        for url, result in zip(urls, results):
            if not result.ok:
                continue
            seconds = _cache_lifetime(result.headers)
            if seconds is None or seconds < min_seconds:
                self.fail("cache_missing", url, cache=result.headers.get("cache-control") or "—", page=self._static[url])
            if result.content_type.startswith(_COMPRESSIBLE) and not result.headers.get("content-encoding"):
                self.fail("static_compression_missing", url, type=result.content_type)


def _cache_lifetime(headers: dict[str, str]) -> int | None:
    cache_control = headers.get("cache-control", "").lower()
    if "no-store" in cache_control or "no-cache" in cache_control:
        return 0
    if match := _MAX_AGE.search(cache_control):
        return int(match.group(1))
    if expires := headers.get("expires"):
        try:
            return int((parsedate_to_datetime(expires) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            return 0
    return None
