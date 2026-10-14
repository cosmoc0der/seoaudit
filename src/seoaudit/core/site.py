from __future__ import annotations

import asyncio
import codecs
import gzip
from dataclasses import dataclass, field

import httpx
from defusedxml import ElementTree
from protego import Protego

from seoaudit.core.config import CrawlConfig
from seoaudit.core.http import FetchResult, fetch
from seoaudit.core.urls import UrlScope, origin_of

_MAX_SITEMAP_FILES = 50
# Google treats a 5xx robots.txt as "disallow everything" until it can read it again.
_DISALLOW_ALL = Protego.parse("User-agent: *\nDisallow: /")


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
    # Before normalization and the audit's URL cap: what the 50,000-URL / 50 MB protocol limits apply to.
    loc_count: int = 0
    size: int | None = None
    # <lastmod> of each <url> entry of an XML urlset, None where absent; feeds and text sitemaps have none.
    lastmods: list[str | None] = field(default_factory=list)


@dataclass
class SiteInfo:
    start_url: str
    home: FetchResult
    robots: FetchResult | None = None
    robots_parser: Protego | None = None
    sitemaps: list[SitemapFile] = field(default_factory=list)
    # The audit stopped reading sitemaps at its URL or file cap, so "not in sitemap" can't be judged.
    sitemaps_truncated: bool = False
    tls_error: str | None = None
    # Every origin (subdomain, scheme) answers for itself; None means no rules.
    robots_rules: dict[str, Protego | None] = field(default_factory=dict)
    _robots_loading: dict[str, asyncio.Task[FetchResult]] = field(default_factory=dict, init=False, repr=False)

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
    def robots_unavailable(self) -> bool:
        return _is_unavailable(self.robots)

    @property
    def sitemap_urls(self) -> set[str]:
        return {u for s in self.sitemaps for u in s.urls}

    def can_fetch(self, url: str, agent: str) -> bool:
        """Origins not loaded via `load_robots` yet are allowed."""
        rules = self.robots_rules.get(origin_of(url))
        return rules.can_fetch(url, agent) if rules else True

    async def load_robots(self, client: httpx.AsyncClient, url: str) -> None:
        origin = origin_of(url)
        if origin in self.robots_rules:
            return
        if origin not in self._robots_loading:
            self._robots_loading[origin] = asyncio.ensure_future(_fetch_robots(client, origin))
        response = await self._robots_loading[origin]
        self.robots_rules[origin] = _robots_rules(response)


async def discover_site(client: httpx.AsyncClient, start_url: str, cfg: CrawlConfig, scope: UrlScope) -> SiteInfo:
    home = await fetch(client, start_url, read_body=False)
    site = SiteInfo(start_url=start_url, home=home)
    origin = origin_of(scope.normalize(home.final_url) or home.final_url)

    robots = await _fetch_robots(client, origin)
    site.robots = robots
    rules = site.robots_rules[origin] = _robots_rules(robots)
    # Checks read robots_parser as "the file we could parse", so the 5xx stand-in stays out of it.
    site.robots_parser = None if rules is _DISALLOW_ALL else rules
    declared = [u for raw in site.robots_parser.sitemaps if (u := scope.normalize(raw))] if site.robots_parser else []

    if cfg.use_sitemap or declared:
        candidates = declared or [f"{origin}/sitemap.xml"]
        site.sitemaps, site.sitemaps_truncated = await _load_sitemaps(
            client, candidates, set(declared), cfg.max_sitemap_urls, scope
        )
    return site


def _is_unavailable(response: FetchResult | None) -> bool:
    return bool(response and (response.status or 0) >= 500)


async def _fetch_robots(client: httpx.AsyncClient, origin: str) -> FetchResult:
    return await fetch(client, f"{origin}/robots.txt", max_bytes=512 * 1024)


def _robots_rules(response: FetchResult) -> Protego | None:
    if _is_unavailable(response):
        return _DISALLOW_ALL
    if response.status != 200:
        return None
    text = response.text()
    # Content-Type is often wrong for robots.txt and Google ignores it; an HTML body means a soft 404.
    return None if text.lstrip().startswith("<") else Protego.parse(text)


async def _load_sitemaps(
    client: httpx.AsyncClient, urls: list[str], declared: set[str], limit: int, scope: UrlScope
) -> tuple[list[SitemapFile], bool]:
    """Returns the files read and whether the URL or file cap left some unread."""
    queue = list(dict.fromkeys(urls))
    seen: set[str] = set()
    result: list[SitemapFile] = []
    total = 0
    truncated = False

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
            data = _maybe_gunzip(response.body or b"")
            sitemap.size = response.size if response.truncated else len(data)
            sitemap.is_index, locs, sitemap.lastmods = _parse_sitemap(data)
        except Exception as exc:
            sitemap.error_kind, sitemap.error = "parse", str(exc)
            continue

        sitemap.loc_count = len(locs)
        normalized = [u for raw in locs if (u := scope.normalize(raw))]
        if sitemap.is_index:
            queue.extend(normalized)
        else:
            truncated |= len(normalized) > limit - total
            sitemap.urls = normalized[: limit - total]
            total += len(sitemap.urls)
    return result, truncated or any(u not in seen for u in queue)


def _parse_sitemap(data: bytes) -> tuple[bool, list[str], list[str | None]]:
    """Returns (is_index, locs, lastmods) for XML sitemaps, RSS 2.0, Atom and plain-text sitemaps."""
    data = data.removeprefix(codecs.BOM_UTF8)
    if not data.lstrip().startswith(b"<"):
        return False, [line.strip() for line in data.decode("utf-8", "replace").splitlines() if line.strip()], []

    root = ElementTree.fromstring(data)
    kind = _local(root.tag)
    if kind == "rss":
        items = (item for item in root.iter() if _local(item.tag) == "item")
        return False, [(link.text or "").strip() for item in items for link in item if _local(link.tag) == "link"], []
    if kind == "feed":
        entries = (entry for entry in root if _local(entry.tag) == "entry")
        return False, [
            link.get("href", "").strip()
            for entry in entries
            for link in entry
            if _local(link.tag) == "link" and link.get("rel", "alternate") == "alternate"
        ], []
    # Only direct children: <image:loc> and <video:loc> inside <url> point at media, not pages.
    locs = [(loc.text or "").strip() for entry in root for loc in entry if _local(loc.tag) == "loc"]
    if kind == "sitemapindex":
        return True, locs, []
    lastmods = [next(((c.text or "").strip() for c in entry if _local(c.tag) == "lastmod"), None) for entry in root]
    return False, locs, lastmods


def _local(tag: object) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _maybe_gunzip(data: bytes) -> bytes:
    return gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data
