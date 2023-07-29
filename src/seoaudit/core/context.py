from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field

import httpx

from seoaudit.core.config import AuditConfig
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

    async def status_of(self, url: str) -> tuple[int | None, str | None, str | None]:
        """Returns (status, final_url, error) using crawl data first, then a network probe."""
        page = self.pages.get(url)
        result = page.response if page else await self.prober.probe(url)
        return result.status, result.final_url, result.error
