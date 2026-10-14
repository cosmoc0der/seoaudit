from __future__ import annotations

import asyncio
import re
import uuid
from collections import defaultdict
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit

from seoaudit.checks._utils import is_paginated, report_target
from seoaudit.core import robots_txt
from seoaudit.core.check import Check
from seoaudit.core.http import fetch
from seoaudit.core.page import Page
from seoaudit.core.site import SitemapFile
from seoaudit.core.urls import pretty_url

_TEMPORARY_REDIRECTS = {302, 303, 307}
# Google applies the most restrictive one, so "index" next to "noindex" is a mistake waiting to bite.
_CONTRADICTIONS = (({"index", "all"}, {"noindex", "none"}), ({"follow", "all"}, {"nofollow", "none"}))
_SITEMAP_MAX_URLS = 50_000
_SITEMAP_MAX_BYTES = 50 * 1024 * 1024
_XML_POSITION = re.compile(r"line (\d+), column (\d+)")


class IndexabilityCheck(Check):
    id = "indexability"

    async def setup(self) -> None:
        self._canonical_refs: dict[str, list[str]] = defaultdict(list)
        self._resources: dict[str, list[str]] = defaultdict(list)

    async def check_page(self, page: Page) -> None:
        is_home = page.url == self.ctx.site.base_url
        noindex = "noindex" in page.robots
        if noindex:
            self.fail("home_noindex" if is_home else "noindex", page.url, source=_robots_source(page))

        canonicals = list(dict.fromkeys(page.canonicals))
        if not canonicals:
            if not noindex:
                self.fail("canonical_missing", page.url)
        elif len(canonicals) > 1:
            self.fail("canonical_multiple", page.url, values=[pretty_url(c) for c in canonicals])
        elif canonicals[0] != page.url:
            self._canonical_refs[canonicals[0]].append(page.url)
            self.fail("canonical_non_self", page.url, target=pretty_url(canonicals[0]))

        max_depth = self.params("deep_pages")["max_depth"]
        if page.indexable and page.depth is not None and page.depth > max_depth and not is_paginated(page.url):
            self.fail("deep_pages", page.url, depth=page.depth)
        self._page_extras(page)

    async def check_site(self) -> None:
        self._robots()
        await self._sitemaps()
        self._redirects()
        self._blocked()
        self._orphans()
        await asyncio.gather(self._canonical_targets(), self._host_duplicate())
        await self._site_extras()

    def _robots(self) -> None:
        site = self.ctx.site
        robots = site.robots
        url = f"{site.origin}/robots.txt"
        if robots is None or robots.status == 404 or (robots.status == 200 and not site.robots_parser):
            self.fail("robots_missing", url)
        # Google treats 429 like 5xx: the whole site is considered closed to crawling.
        elif robots.error or (robots.status or 0) >= 500 or robots.status == 429:
            self.fail("robots_error", url, status=robots.status or robots.error)
        elif robots.status and robots.status >= 400:
            self.fail("robots_missing", url)

        if not site.robots_parser:
            self.not_applicable("robots_blocks_all")
            self.not_applicable("robots_no_sitemap")
            return
        for agent in self.params("robots_blocks_all")["agents"]:
            if not site.robots_parser.can_fetch(site.base_url, agent):
                self.fail("robots_blocks_all", url, agent=agent)
        if not any(s.declared_in_robots for s in site.sitemaps):
            self.fail("robots_no_sitemap", url)

    async def _sitemaps(self) -> None:
        sitemaps = self.ctx.site.sitemaps
        if not any(s.status == 200 and not s.error for s in sitemaps) and not await self._sitemap_index_exists():
            self.fail("sitemap_missing", self.ctx.site.origin + "/sitemap.xml")
        for sitemap in sitemaps:
            # An undeclared /sitemap.xml guess that 404s is just absent; a declared one is a broken directive.
            if sitemap.error and (sitemap.status != 404 or sitemap.declared_in_robots):
                self.fail("sitemap_invalid", sitemap.url, **_sitemap_error(sitemap))

        for url in self.ctx.site.sitemap_urls:
            if url in self.ctx.blocked:
                self.fail("sitemap_bad_urls", url, key="blocked")
                continue
            page = self.ctx.pages.get(url)
            if page is None:
                continue
            if page.response.broken:
                self.fail("sitemap_bad_urls", url, key="error", status=page.status or page.response.error)
            elif page.is_redirect:
                self.fail("sitemap_bad_urls", url, key="redirect", target=pretty_url(page.response.final_url))
            elif page.ok and "noindex" in page.robots:
                self.fail("sitemap_bad_urls", url, key="noindex")
            elif page.ok and page.canonicals and page.canonicals[0] != url:
                self.fail("sitemap_bad_urls", url, key="canonical", target=pretty_url(page.canonicals[0]))

    def _redirects(self) -> None:
        max_hops = self.params("redirect_chain")["max_hops"]
        for page in self.ctx.pages.values():
            hops = page.response.redirects
            if len(hops) > max_hops:
                chain = [f"{r.status} {pretty_url(r.url)}" for r in hops] + [pretty_url(page.response.final_url)]
                self.fail("redirect_chain", page.url, hops=len(hops), chain=" → ".join(chain))
            if hops and hops[0].status in _TEMPORARY_REDIRECTS and self.ctx.scope.is_internal(page.response.final_url):
                self.fail("redirect_temporary", page.url, status=hops[0].status, target=pretty_url(page.response.final_url))

    async def _sitemap_index_exists(self) -> bool:
        """Many CMS (WordPress/Yoast) serve the index at /sitemap_index.xml and nothing at /sitemap.xml."""
        result = await self.ctx.prober.probe(self.ctx.site.origin + "/sitemap_index.xml")
        return result.ok and "xml" in result.content_type

    def _blocked(self) -> None:
        for url in sorted(self.ctx.blocked):
            # Sitemap-only URLs without links are reported by sitemap_bad_urls.
            if sources := [src for src, _ in self.ctx.inlinks.get(url, [])]:
                self.fail("blocked_by_robots", url, inlinks=len(sources))

    def _orphans(self) -> None:
        if len(self.ctx.pages) >= self.ctx.config.crawl.max_pages:
            self.not_applicable("orphan_pages", "crawl_limit_reached")
            return
        if not self.ctx.site.sitemap_urls:
            self.not_applicable("orphan_pages", "no_sitemap")
            return
        for page in self.ctx.html_pages():
            if page.source == "sitemap" and page.url not in self.ctx.inlinks:
                self.fail("orphan_pages", page.url)

    async def _canonical_targets(self) -> None:
        limit = self.params("canonical_broken")["max_probes"]
        targets = list(self._canonical_refs)[:limit]
        statuses = await asyncio.gather(*(self.ctx.status_of(t) for t in targets))
        for target, (status, final_url, error) in zip(targets, statuses):
            problem = None
            if error or (status or 0) >= 400:
                problem = {"key": "error", "status": status or error}
            elif final_url and (self.ctx.scope.normalize(final_url) or final_url) != target:
                problem = {"key": "redirect", "status": status}
            if problem:
                for url in self._canonical_refs[target]:
                    self.fail("canonical_broken", url, target=pretty_url(target), **problem)

    async def _host_duplicate(self) -> None:
        parts = urlsplit(self.ctx.site.base_url)
        host = parts.hostname or ""
        alt_host = host[4:] if host.startswith("www.") else f"www.{host}"
        alt_url = urlunsplit((parts.scheme, alt_host, "/", "", ""))
        result = await self.ctx.prober.probe(alt_url)
        if result.status == 200 and not result.redirects:
            self.fail("host_duplicate", alt_url, main=self.ctx.site.base_url)

    def _page_extras(self, page: Page) -> None:
        resources = [s.src for s in page.scripts if s.src] + page.stylesheets + [i.delivered for i in page.images if i.delivered]
        for url in resources:
            if self.ctx.scope.is_internal(url):
                self._resources[url].append(page.url)
        if page.indexable and _is_search_url(page.url, self.params("search_pages_indexable")):
            self.fail("search_pages_indexable", page.url)
        self._canonical_invalid(page)
        self._robots_conflict(page)
        if brk := page.head_break:
            key = "noscript_img" if brk.noscript_img else "text" if brk.breaker == "#text" else None
            self.fail("head_broken", page.url, key=key, tag=brk.breaker, moved=list(dict.fromkeys(brk.moved))[:5])

    def _canonical_invalid(self, page: Page) -> None:
        problems = []
        # A canonical pushed out of <head> by a broken head is reported once, by head_broken.
        pushed_out = bool(page.head_break and '<link rel="canonical">' in page.head_break.moved)
        for href, in_head in page.canonical_tags:
            if not in_head and not pushed_out:
                problems.append(("outside_head", href))
            elif not page.absolute(href):
                problems.append(("empty", href))
            elif "#" in href:
                problems.append(("fragment", href))
        if page.url.startswith("https://"):
            problems += [("http", url) for url in page.canonicals if url.startswith("http://")]
        if problems:
            key, value = problems[0]
            self.fail("canonical_invalid", page.url, key=key, value=pretty_url(value) or "—")

    def _robots_conflict(self, page: Page) -> None:
        directives = set().union(*page.robots_sources.values())
        clash = next(
            (f"{min(directives & a)} + {min(directives & b)}" for a, b in _CONTRADICTIONS if directives & a and directives & b),
            None,
        )
        if clash:
            self.fail("robots_directives_conflict", page.url, key="contradiction", value=clash)
        elif "noindex" in page.robots and page.canonicals and page.canonicals[0] != page.url:
            self.fail("robots_directives_conflict", page.url, key="noindex_canonical", target=pretty_url(page.canonicals[0]))

    async def _site_extras(self) -> None:
        self._home_unavailable()
        self._robots_syntax()
        self._blocked_resources()
        await self._canonical_target_indexability()
        self._not_in_sitemap()
        self._sitemap_limits()
        self._canonical_to_home()
        self._https_to_http()
        if await self._soft_404_setup():
            # A catch-all 200 makes every URL variant "exist"; that is the same problem, reported once.
            self.not_applicable("url_variants_duplicate", "catch_all_200")
        else:
            await self._url_variants()

    def _home_unavailable(self) -> None:
        home = self.ctx.site.home
        if home.status != 200:
            self.fail("home_unavailable", self.ctx.site.base_url, status=home.status or home.error)

    def _robots_syntax(self) -> None:
        robots = self.ctx.site.robots
        if robots is None or not self.ctx.site.robots_parser:
            self.not_applicable("robots_syntax")
            return
        url = f"{self.ctx.site.origin}/robots.txt"
        if (robots.size or 0) > robots_txt.MAX_BYTES:
            self.fail("robots_syntax", url, key="too_large", size_kb=round(robots.size / 1024))
        for key, lines in robots_txt.lint(robots.text()).items():
            self.fail("robots_syntax", url, key=key, count=len(lines), lines=lines[:5])

    def _blocked_resources(self) -> None:
        site = self.ctx.site
        if not site.robots_parser:
            self.not_applicable("blocked_resources")
            return
        agent = self.params("blocked_resources")["agent"]
        for url, pages in self._resources.items():
            if not site.can_fetch(url, agent):
                report_target(self, "blocked_resources", url, pages)

    async def _canonical_target_indexability(self) -> None:
        agent = self.params("blocked_resources")["agent"]
        limit = self.params("canonical_target_non_indexable")["max_fetches"]
        pages = await self.ctx.load_pages(list(self._canonical_refs), limit)
        for target, sources in self._canonical_refs.items():
            page = pages.get(target)
            final = target
            if target in self.ctx.blocked or not self.ctx.site.can_fetch(target, agent):
                key = "blocked"
            elif page is None or not page.ok:
                continue
            elif "noindex" in page.robots:
                key = "noindex"
            elif page.canonicals and page.canonicals[0] != target:
                final = page.canonicals[0]
                key = "loop" if final in sources else "chain"
            else:
                continue
            report_target(self, "canonical_target_non_indexable", target, sources, key=key, final=pretty_url(final))

    def _not_in_sitemap(self) -> None:
        site = self.ctx.site
        if not site.sitemap_urls:
            self.not_applicable("not_in_sitemap", "no_sitemap")
            return
        if site.sitemaps_truncated:
            self.not_applicable("not_in_sitemap", "sitemap_truncated")
            return
        for page in self.ctx.html_pages():
            if page.indexable and page.url not in site.sitemap_urls and not is_paginated(page.url):
                self.fail("not_in_sitemap", page.url)

    def _sitemap_limits(self) -> None:
        https = self.ctx.site.base_url.startswith("https://")
        for sitemap in self.ctx.site.sitemaps:
            if sitemap.loc_count > _SITEMAP_MAX_URLS:
                self.fail("sitemap_limits", sitemap.url, key="too_many_urls", count=sitemap.loc_count)
            if (sitemap.size or 0) > _SITEMAP_MAX_BYTES:
                self.fail("sitemap_limits", sitemap.url, key="too_large", size_mb=round(sitemap.size / 1024 / 1024))
            foreign = [u for u in sitemap.urls if not self.ctx.scope.is_internal(u)]
            insecure = [u for u in sitemap.urls if https and u.startswith("http://") and u not in foreign]
            for key, urls in (("foreign", foreign), ("http", insecure)):
                if urls:
                    self.fail("sitemap_limits", sitemap.url, key=key, count=len(urls), examples=[pretty_url(u) for u in urls[:3]])

    def _canonical_to_home(self) -> None:
        """One page pointing its canonical at the home page may be a real duplicate; a pattern is a template bug."""
        home = self.ctx.site.base_url
        sources = [u for u in self._canonical_refs.get(home, []) if not urlsplit(u).query and not _is_index_file(u)]
        if len(sources) >= self.params("canonical_invalid")["home_pattern_min"]:
            report_target(self, "canonical_invalid", home, sources, key="homepage")

    def _https_to_http(self) -> None:
        for page in self.ctx.pages.values():
            hops = [r.url for r in page.response.redirects] + [page.response.final_url]
            for current, following in zip(hops, hops[1:]):
                if current.startswith("https://") and following.startswith("http://") and self.ctx.scope.is_internal(following):
                    self.fail("redirect_https_to_http", page.url, target=pretty_url(following))
                    break

    async def _soft_404_setup(self) -> bool:
        origin = self.ctx.site.origin
        token = f"seoaudit-{uuid.uuid4().hex[:12]}"
        probes = [f"{origin}/{token}"]
        # Catch-all routes often live under a section (/blog/<slug>), so one probe goes there too.
        nested = next((p for p in self.ctx.html_pages() if urlsplit(p.url).path.strip("/").count("/") >= 1), None)
        if nested:
            probes.append(f"{origin}/{urlsplit(nested.url).path.strip('/').split('/')[0]}/{token}")
        results = await asyncio.gather(*(fetch(self.ctx.client, url, read_body=False) for url in probes))
        for url, result in zip(probes, results):
            if result.status == 200:
                key = "redirect" if result.redirects else None
                self.fail("soft_404_setup", url, key=key, target=pretty_url(result.final_url))
                return True
        return False

    async def _url_variants(self) -> None:
        params = self.params("url_variants_duplicate")
        site = self.ctx.site
        samples = sorted(
            (p for p in self.ctx.html_pages() if p.indexable and urlsplit(p.url).path != "/" and not urlsplit(p.url).query),
            key=lambda p: (p.depth is None, p.depth or 0, p.url),
        )[: params["max_samples"]]
        candidates = [(kind, variant, p.url) for p in samples for kind, variant in _variants(p.url, self.ctx.scope)]
        candidates += [("index", site.origin + name, site.base_url) for name in params["index_files"]]

        pages = await self.ctx.load_pages([variant for _, variant, _ in candidates], len(candidates))
        found: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for kind, variant, original in candidates:
            if (page := pages.get(variant)) and page.indexable:
                found[kind].append((variant, original))
        for kind, pairs in found.items():
            variant, original = pairs[0]
            self.fail("url_variants_duplicate", variant, key=kind, original=pretty_url(original), count=len(pairs))


def _sitemap_error(sitemap: SitemapFile) -> dict[str, str]:
    """Parser messages are English; the report only needs where the XML breaks."""
    if sitemap.error_kind != "parse":
        return {"key": sitemap.error_kind, "error": pretty_url(sitemap.error)}
    match = _XML_POSITION.search(sitemap.error)
    return {"key": "parse", "line": match[1], "col": match[2]} if match else {"key": "parse_structure"}


def _robots_source(page: Page) -> str:
    return "X-Robots-Tag" if "noindex" in page.headers.get("x-robots-tag", "").lower() else "meta robots"


def _is_index_file(url: str) -> bool:
    return urlsplit(url).path.rsplit("/", 1)[-1].lower().startswith(("index.", "default."))


def _is_search_url(url: str, params: dict) -> bool:
    parts = urlsplit(url)
    if not parts.query:
        return False
    keys = {k.lower() for k, _ in parse_qsl(parts.query, keep_blank_values=True)}
    segments = {s.lower() for s in unquote(parts.path).split("/") if s}
    return bool(keys & set(params["params"]) or segments & set(params["paths"]))


def _variants(url: str, scope) -> list[tuple[str, str]]:
    parts = urlsplit(url)
    path = unquote(parts.path)
    paths = []
    if path.endswith("/"):
        paths.append(("slash", path.rstrip("/") or "/"))
    elif "." not in path.rsplit("/", 1)[-1]:
        paths.append(("slash", path + "/"))
    # Only ASCII letters: an uppercased Cyrillic slug is a different URL, not a case variant.
    upper = "".join(c.upper() if c.isascii() else c for c in path)
    if upper != path:
        paths.append(("case", upper))
    return [(kind, v) for kind, p in paths if (v := scope.normalize(urlunsplit((parts.scheme, parts.netloc, p, "", ""))))]
