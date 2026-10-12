from __future__ import annotations

import asyncio
import re
import socket
import ssl
from collections import defaultdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from seoaudit.checks._utils import report_group, report_target
from seoaudit.core.check import Check
from seoaudit.core.http import FetchResult, fetch
from seoaudit.core.page import Page
from seoaudit.core.urls import is_service_url

_MAX_AGE = re.compile(r"(?:s-)?max-age=(\d+)")
_COMPRESSIBLE = ("text/", "application/javascript", "application/x-javascript", "application/json", "image/svg+xml")
_MODERN_HTTP = ("HTTP/2", "HTTP/3")
# CDNs (seen on Cloudflare) sometimes pick HTTP/1.1 for a single connection, so one answer proves nothing.
_ALPN_ATTEMPTS = 3


async def _supports_modern_http(home: FetchResult, timeout: float) -> bool:
    if "h3=" in home.headers.get("alt-svc", ""):
        return True
    parts = urlsplit(home.final_url)
    if parts.scheme != "https" or not parts.hostname:
        return False
    for _ in range(_ALPN_ATTEMPTS):
        try:
            if await asyncio.to_thread(_negotiates_h2, parts.hostname, parts.port or 443, timeout):
                return True
        except (OSError, ssl.SSLError):
            return False
    return False


def _negotiates_h2(host: str, port: int, timeout: float) -> bool:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.set_alpn_protocols(["h2", "http/1.1"])
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with context.wrap_socket(sock, server_hostname=host) as tls:
            return tls.selected_alpn_protocol() == "h2"


class PerformanceCheck(Check):
    id = "performance"

    async def setup(self) -> None:
        self._static: dict[str, str] = {}
        self._slow: dict[str, float] = {}
        self._code: dict[str, list[str]] = defaultdict(list)

    async def check_page(self, page: Page) -> None:
        response = page.response
        if response.ttfb_ms and response.ttfb_ms > self.params("ttfb_slow")["max_ms"]:
            self._slow[page.url] = response.ttfb_ms

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
            if self.ctx.scope.is_internal(url) and not is_service_url(url):
                self._static.setdefault(url, page.url)
        self._page_extras(page)

    async def check_site(self) -> None:
        home = self.ctx.site.home
        version = home.http_version
        if (
            version
            and not version.startswith(_MODERN_HTTP)
            and not await _supports_modern_http(home, self.ctx.config.crawl.timeout)
        ):
            self.fail("http2_missing", self.ctx.site.base_url, version=version)
        await self._ttfb()
        await self._static_resources()
        await self._site_extras()

    async def _ttfb(self) -> None:
        """The crawl's own parallel requests slow the server down, so slow pages are timed again one by one."""
        params = self.params("ttfb_slow")
        slowest = sorted(self._slow, key=self._slow.get, reverse=True)
        for index, url in enumerate(slowest):
            crawl = round(self._slow[url])
            recheck = None
            if index < params["recheck"]:
                result = await fetch(self.ctx.client, url, read_body=False, attempts=1)
                recheck = round(result.ttfb_ms) if result.ttfb_ms else None
            if recheck is None:
                self.fail("ttfb_slow", url, ttfb=crawl, crawl=crawl)
            elif recheck > params["max_ms"]:
                self.fail("ttfb_slow", url, key="rechecked", ttfb=min(crawl, recheck), crawl=crawl, recheck=recheck)

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
                self.fail(
                    "cache_missing",
                    url,
                    key="short" if seconds and seconds > 0 else None,
                    cache=result.headers.get("cache-control") or "—",
                    hours=round(max(seconds or 0, 0) / 3600, 1),
                    page=self._static[url],
                )
            if result.content_type.startswith(_COMPRESSIBLE) and not result.headers.get("content-encoding"):
                self.fail("static_compression_missing", url, type=result.content_type)

    def _page_extras(self, page: Page) -> None:
        size = page.response.size or 0
        if size > self.params("html_over_2mb")["max_mb"] * 1024 * 1024:
            self.fail("html_over_2mb", page.url, size_mb=round(size / 1024 / 1024, 1))
        for url in [s.src for s in page.scripts if s.src] + page.stylesheets:
            if self.ctx.scope.is_internal(url) and not is_service_url(url):
                self._code[url].append(page.url)

    async def _site_extras(self) -> None:
        await asyncio.gather(self._broken_resources(), self._site_response())

    async def _broken_resources(self) -> None:
        urls = list(self._code)[: self.params("broken_resources")["max_probes"]]
        if not urls:
            self.not_applicable("broken_resources", "no_static_resources")
            return
        for url, result in (await self.ctx.prober.probe_many(urls)).items():
            # Only a real HTTP error: transport failures under crawl load are not a missing file.
            if (result.status or 0) >= 400:
                report_target(self, "broken_resources", url, self._code[url], status=result.status)

    async def _site_response(self) -> None:
        """Yandex flags a site whose average response exceeds 3 s; crawl timings are re-measured one by one first."""
        params = self.params("site_slow_response")
        pages = list(self.ctx.html_pages())
        timings = [p.response.ttfb_ms for p in pages if p.response.ttfb_ms]
        if not timings:
            return
        crawl_mean = sum(timings) / len(timings)
        if crawl_mean <= params["max_ms"]:
            return
        rechecked = []
        for page in pages[: params["recheck"]]:
            result = await fetch(self.ctx.client, page.url, read_body=False, attempts=1)
            if result.ttfb_ms:
                rechecked.append(result.ttfb_ms)
        mean = sum(rechecked) / len(rechecked) if rechecked else crawl_mean
        if mean > params["max_ms"]:
            self.fail("site_slow_response", self.ctx.site.base_url, mean=round(mean / 1000, 1), pages=len(timings))


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
