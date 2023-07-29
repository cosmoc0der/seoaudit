from __future__ import annotations

import gzip
from dataclasses import dataclass, field

import httpx
from defusedxml import ElementTree
from protego import Protego

from seoaudit.core.config import CrawlConfig
from seoaudit.core.http import FetchResult, fetch
from seoaudit.core.urls import UrlScope, origin_of

_MAX_SITEMAP_FILES = 50


@dataclass
class SitemapFile:
    url: str
    status: int | None = None
    final_url: str | None = None
    error_kind: str | None = None
    error: str | None = None
    is_index: bool = False
    urls: list[str] = field(default_factory=list)
    declared_in_robots: bool = False


@dataclass
class SiteInfo:
    start_url: str
    home: FetchResult
    robots: FetchResult | None = None
    robots_parser: Protego | None = None
    sitemaps: list[SitemapFile] = field(default_factory=list)
    tls_error: str | None = None

    @property
    def base_url(self) -> str:
        return self.home.final_url

    @property
    def origin(self) -> str:
        return origin_of(self.base_url)

    @property
    def robots_found(self) -> bool:
        return bool(self.robots and self.robots.status == 200)

    @property
    def sitemap_urls(self) -> set[str]:
        return {u for s in self.sitemaps for u in s.urls}

    def can_fetch(self, url: str, agent: str) -> bool:
        return self.robots_parser.can_fetch(url, agent) if self.robots_parser else True


async def discover_site(client: httpx.AsyncClient, start_url: str, cfg: CrawlConfig, scope: UrlScope) -> SiteInfo:
    home = await fetch(client, start_url, read_body=False)
    site = SiteInfo(start_url=start_url, home=home)
    origin = origin_of(home.final_url)

    robots = await fetch(client, f"{origin}/robots.txt", max_bytes=512 * 1024)
    site.robots = robots
    declared: list[str] = []
    if robots.status == 200 and "html" not in robots.content_type:
        site.robots_parser = Protego.parse(robots.text())
        declared = [u for raw in site.robots_parser.sitemaps if (u := scope.normalize(raw))]

    if cfg.use_sitemap or declared:
        candidates = declared or [f"{origin}/sitemap.xml"]
        site.sitemaps = await _load_sitemaps(client, candidates, set(declared), cfg.max_sitemap_urls, scope)
    return site


async def _load_sitemaps(
    client: httpx.AsyncClient, urls: list[str], declared: set[str], limit: int, scope: UrlScope
) -> list[SitemapFile]:
    queue = list(dict.fromkeys(urls))
    seen: set[str] = set()
    result: list[SitemapFile] = []
    total = 0

    while queue and len(result) < _MAX_SITEMAP_FILES and total < limit:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        sitemap = SitemapFile(url=url, declared_in_robots=url in declared)
        result.append(sitemap)

        response = await fetch(client, url, max_bytes=50 * 1024 * 1024)
        sitemap.status = response.status
        sitemap.final_url = response.final_url
        if not response.ok:
            sitemap.error_kind, sitemap.error = "http", response.error or str(response.status)
            continue
        if "html" in response.content_type:
            sitemap.error_kind, sitemap.error = "html", response.final_url
            continue
        try:
            root = ElementTree.fromstring(_maybe_gunzip(response.body or b""))
        except Exception as exc:
            sitemap.error_kind, sitemap.error = "parse", str(exc)
            continue

        locs = [
            (el.text or "").strip()
            for el in root.iter()
            if isinstance(el.tag, str) and el.tag.rsplit("}", 1)[-1] == "loc"
        ]
        if root.tag.rsplit("}", 1)[-1] == "sitemapindex":
            sitemap.is_index = True
            queue.extend(u for raw in locs if (u := scope.normalize(raw)))
        else:
            sitemap.urls = [u for raw in locs if (u := scope.normalize(raw))][: limit - total]
            total += len(sitemap.urls)
    return result


def _maybe_gunzip(data: bytes) -> bytes:
    return gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data
