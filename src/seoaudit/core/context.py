from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field

import httpx

from seoaudit.core.config import AuditConfig
from seoaudit.core.http import fetch
from seoaudit.core.page import Link, Page
from seoaudit.core.probe import ResourceProber
from seoaudit.core.site import SiteInfo
from seoaudit.core.urls import UrlScope


@dataclass
class AuditContext:
    """Shared state of a single audit run, available to every check."""

    config: AuditConfig
    client: httpx.AsyncClient
    site: SiteInfo
    scope: UrlScope
    prober: ResourceProber
    pages: dict[str, Page] = field(default_factory=dict)
    inlinks: dict[str, list[tuple[str, Link]]] = field(default_factory=lambda: defaultdict(list))
    blocked: set[str] = field(default_factory=set)
    diagnostics: list[str] = field(default_factory=list)

    @property
    def home(self) -> Page | None:
        return self.pages.get(self.site.base_url)

    def html_pages(self) -> Iterator[Page]:
        return (p for p in self.pages.values() if p.ok)

    def page_count(self) -> int:
        return sum(1 for _ in self.html_pages())

    async def fetch_page(self, url: str) -> Page:
        """A page outside the crawl, parsed as the crawler would; only indexability data is kept."""
        response = await fetch(self.client, url)
        page = Page(url=url, response=response, scope=self.scope, robots_agent=self.config.crawl.robots_agent)
        page.robots, page.canonicals  # noqa: B018 - cache before dropping the HTML
        page.release()
        return page

    async def load_pages(self, urls: list[str], limit: int) -> dict[str, Page]:
        """Crawled pages plus a bounded fetch of the rest: canonical and hreflang targets aren't links."""
        result = {u: self.pages[u] for u in urls if u in self.pages}
        missing = [u for u in urls if u not in result and u not in self.blocked][:limit]
        semaphore = asyncio.Semaphore(self.config.crawl.concurrency)

        async def load(url: str) -> Page:
            async with semaphore:
                return await self.fetch_page(url)

        for page in await asyncio.gather(*(load(u) for u in missing)):
            result[page.url] = page
        return result

    async def status_of(self, url: str) -> tuple[int | None, str | None, str | None]:
        """Returns (status, final_url, error) using crawl data first, then a network probe."""
        page = self.pages.get(url)
        result = page.response if page else await self.prober.probe(url)
        return result.status, result.final_url, result.error
