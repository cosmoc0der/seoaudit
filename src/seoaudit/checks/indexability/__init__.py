from __future__ import annotations

import asyncio
from collections import defaultdict
from urllib.parse import urlsplit, urlunsplit

from seoaudit.core.check import Check
from seoaudit.core.page import Page
from seoaudit.core.urls import pretty_url

_TEMPORARY_REDIRECTS = {302, 303, 307}


class IndexabilityCheck(Check):
    id = "indexability"

    async def setup(self) -> None:
        self._canonical_refs: dict[str, list[str]] = defaultdict(list)

    async def check_page(self, page: Page) -> None:
        is_home = page.url == self.ctx.site.base_url
        if "noindex" in page.robots:
            self.fail("home_noindex" if is_home else "noindex", page.url, source=_robots_source(page))

        canonicals = list(dict.fromkeys(page.canonicals))
        if not canonicals:
            self.fail("canonical_missing", page.url)
        elif len(canonicals) > 1:
            self.fail("canonical_multiple", page.url, values=[pretty_url(c) for c in canonicals])
        elif canonicals[0] != page.url:
            self._canonical_refs[canonicals[0]].append(page.url)
            self.fail("canonical_non_self", page.url, target=pretty_url(canonicals[0]))

        max_depth = self.params("deep_pages")["max_depth"]
        if page.depth is not None and page.depth > max_depth:
            self.fail("deep_pages", page.url, depth=page.depth)

    async def check_site(self) -> None:
        self._robots()
        self._sitemaps()
        self._redirects()
        self._blocked()
        self._orphans()
        await asyncio.gather(self._canonical_targets(), self._host_duplicate())

    def _robots(self) -> None:
        site = self.ctx.site
        robots = site.robots
        url = f"{site.origin}/robots.txt"
        if robots is None or robots.status == 404 or (robots.status == 200 and not site.robots_parser):
            self.fail("robots_missing", url)
        elif robots.error or (robots.status or 0) >= 500:
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

    def _sitemaps(self) -> None:
        sitemaps = self.ctx.site.sitemaps
        if not any(s.status == 200 and not s.error for s in sitemaps):
            self.fail("sitemap_missing", self.ctx.site.origin + "/sitemap.xml")
        for sitemap in sitemaps:
            if sitemap.error and sitemap.status != 404:
                self.fail("sitemap_invalid", sitemap.url, key=sitemap.error_kind, error=pretty_url(sitemap.error))

        for url in self.ctx.site.sitemap_urls:
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

    def _blocked(self) -> None:
        for url in sorted(self.ctx.blocked):
            sources = [src for src, _ in self.ctx.inlinks.get(url, [])]
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
            elif final_url and final_url.rstrip("/") != target.rstrip("/"):
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


def _robots_source(page: Page) -> str:
    return "X-Robots-Tag" if "noindex" in page.headers.get("x-robots-tag", "").lower() else "meta robots"
