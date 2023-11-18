from __future__ import annotations

from collections import defaultdict

from seoaudit.checks._utils import report_group
from seoaudit.core.check import Check
from seoaudit.core.http import FetchResult
from seoaudit.core.page import Page
from seoaudit.core.urls import pretty_url

_MAX_SOURCES_PER_TARGET = 50


class LinksCheck(Check):
    id = "links"

    async def setup(self) -> None:
        self._external: dict[str, list[str]] = defaultdict(list)

    async def check_page(self, page: Page) -> None:
        links = page.links
        if len(links) > self.params("too_many_links")["max_links"]:
            self.fail("too_many_links", page.url, count=len(links))

        report_group(self, "empty_anchor", page.url, [l.url for l in links if not l.text])
        report_group(self, "internal_nofollow", page.url, [l.url for l in links if l.internal and l.nofollow])
        if page.url.startswith("https://"):
            report_group(self, "insecure_links", page.url, [l.url for l in links if l.url.startswith("http://")])

        for link in links:
            if not link.internal:
                self._external[link.url].append(page.url)

    async def check_site(self) -> None:
        await self._internal_links()
        await self._external_links()

    async def _internal_links(self) -> None:
        ctx = self.ctx
        targets = [t for t in ctx.inlinks if t not in ctx.blocked]
        unknown = [t for t in targets if t not in ctx.pages][: self.params("broken_internal")["max_probes"]]
        probed = await ctx.prober.probe_many(unknown)

        for target in targets:
            page = ctx.pages.get(target)
            result: FetchResult | None = page.response if page else probed.get(target)
            if result is None:
                continue
            sources = list(dict.fromkeys(src for src, _ in ctx.inlinks[target]))[:_MAX_SOURCES_PER_TARGET]
            if result.broken:
                for source in sources:
                    self.fail("broken_internal", source, target=pretty_url(target), status=_status(result))
            elif result.redirects:
                for source in sources:
                    self.fail(
                        "internal_redirect",
                        source,
                        target=pretty_url(target),
                        final=pretty_url(result.final_url),
                        status=result.redirects[0].status,
                    )

    async def _external_links(self) -> None:
        params = self.params("broken_external")
        urls = list(self._external)[: params["max_probes"]]
        if not urls:
            return
        ignored = set(params["ignore_statuses"])
        results = await self.ctx.prober.probe_many(urls)
        for url, result in results.items():
            if result.broken and result.status not in ignored:
                for source in list(dict.fromkeys(self._external[url]))[:_MAX_SOURCES_PER_TARGET]:
                    self.fail("broken_external", source, target=pretty_url(url), status=_status(result))


def _status(result: FetchResult) -> str:
    return str(result.status) if result.status else (result.error or "no response")
