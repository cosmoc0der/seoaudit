from __future__ import annotations

import re
from collections import defaultdict
from fnmatch import fnmatch
from urllib.parse import parse_qsl, urljoin, urlsplit

from seoaudit.checks._utils import report_group, report_target
from seoaudit.core.check import Check
from seoaudit.core.http import FetchResult
from seoaudit.core.page import Page
from seoaudit.core.urls import pretty_url

_PUNCTUATION = re.compile(r"[^\w\s]+")
_MALFORMED = (
    # A no-op like javascript:void(0) is a button; code that navigates hides a real link from crawlers.
    ("javascript", re.compile(r"^javascript:(?!\s*(?:void\s*\(\s*0?\s*\)|;|//|$))", re.I)),
    ("local", re.compile(r"^(?:file:|\\\\|https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(?:[:/]|$))", re.I)),
    ("scheme", re.compile(r"^(?:https?:/(?!/)|https?:///|https?//|https?:\\|htp:|htps:|htttps?:|hhttps?:)", re.I)),
)


class LinksCheck(Check):
    id = "links"

    async def setup(self) -> None:
        self._external: dict[str, list[str]] = defaultdict(list)
        self._anchors: dict[str, list[str]] = defaultdict(list)
        self._malformed: dict[tuple[str, str], list[str]] = defaultdict(list)
        self._tracking: dict[str, list[str]] = defaultdict(list)
        self._tracking_names: dict[str, list[str]] = {}

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
        self._page_extras(page)

    async def check_site(self) -> None:
        await self._internal_links()
        await self._external_links()
        self._site_extras()

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
            sources = [src for src, _ in ctx.inlinks[target]]
            if result.broken:
                report_target(self, "broken_internal", target, sources, status=_status(result))
            elif result.redirects:
                report_target(
                    self,
                    "internal_redirect",
                    target,
                    sources,
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
            # No answer at all may be a regional block or a firewall against bots, not a dead page.
            if result.status is None and self.enabled("external_unreachable"):
                report_target(self, "external_unreachable", url, self._external[url], status=_status(result))
            elif result.broken and result.status not in ignored:
                report_target(self, "broken_external", url, self._external[url], status=_status(result))

    def _page_extras(self, page: Page) -> None:
        followed = [l for l in page.links if l.internal and not l.nofollow and l.url != page.url]
        if "nofollow" in page.robots:
            self.fail("no_outlinks", page.url, key="nofollow")
        elif not followed:
            self.fail("no_outlinks", page.url)

        phrases = set(self.params("non_descriptive_anchor")["phrases"])
        for link in page.links:
            if link.internal and (text := _anchor_key(link.text)) in phrases:
                self._anchors[text].append(page.url)
        self._raw_hrefs(page)

    def _raw_hrefs(self, page: Page) -> None:
        """Raw hrefs: normalization strips tracking parameters and drops unusable links before `links` sees them."""
        patterns = self.params("tracking_params")["params"]
        for raw in page.hrefs:
            href = raw.strip()
            if kind := _malformed(href):
                self._malformed[kind, href].append(page.url)
                continue
            if not href or href.startswith("#"):
                continue
            url = urljoin(page.base_url, href)
            if not self.ctx.scope.is_internal(url):
                continue
            parts = urlsplit(url)
            names = {k.lower() for k, _ in parse_qsl(parts.query, keep_blank_values=True)}
            found = sorted(n for n in names if any(fnmatch(n, p) for p in patterns))
            if ";jsessionid=" in parts.path.lower():
                found.append("jsessionid")
            if found:
                self._tracking[url].append(page.url)
                self._tracking_names[url] = found

    def _site_extras(self) -> None:
        for (kind, href), pages in self._malformed.items():
            report_target(self, "malformed", href, pages, key=kind)
        for url, pages in self._tracking.items():
            report_target(self, "tracking_params", url, pages, params=self._tracking_names[url])
        self._inlink_profile()
        for text, pages in self._anchors.items():
            unique = list(dict.fromkeys(pages))
            self.fail(
                "non_descriptive_anchor",
                unique[0],
                text=text,
                links=len(pages),
                pages=len(unique),
                examples=[pretty_url(u) for u in unique[:5]],
            )

    def _inlink_profile(self) -> None:
        ctx = self.ctx
        canonicalised: dict[str, list[tuple[str, list[str]]]] = defaultdict(list)
        for target, refs in ctx.inlinks.items():
            page = ctx.pages.get(target)
            if page is None or not page.ok:
                continue
            sources = list(dict.fromkeys(src for src, _ in refs if src != target))
            followed = list(
                dict.fromkeys(src for src, link in refs if src != target and not link.nofollow and _follows(ctx.pages.get(src)))
            )
            if not sources:
                continue
            if not page.indexable:
                if followed and "noindex" in page.robots:
                    report_target(self, "to_non_indexable", target, followed, key="noindex")
                elif followed and page.canonicals:
                    canonicalised[page.canonicals[0]].append((target, followed))
            elif not followed:
                report_target(self, "nofollow_inlinks_only", target, sources)
            elif len(followed) == 1 and target != ctx.site.base_url:
                self.fail("single_inlink", target, source=pretty_url(followed[0]))
        self._links_to_canonicalised(canonicalised)

    def _links_to_canonicalised(self, groups: dict[str, list[tuple[str, list[str]]]]) -> None:
        """Filter and sort links are one template problem per main page, not one per parameter combination."""
        for canonical, items in groups.items():
            sources = [src for _, followed in items for src in followed]
            if len(items) == 1:
                report_target(self, "to_non_indexable", items[0][0], sources, key="canonical", final=pretty_url(canonical))
            else:
                variants = [pretty_url(target) for target, _ in items]
                report_target(self, "to_non_indexable", canonical, sources, key="variants", count=len(items), variants=variants[:3])


def _status(result: FetchResult) -> str:
    return str(result.status) if result.status else (result.error or "no response")


def _follows(page: Page | None) -> bool:
    return page is None or "nofollow" not in page.robots


def _malformed(href: str) -> str | None:
    return next((kind for kind, pattern in _MALFORMED if pattern.match(href)), None)


def _anchor_key(text: str) -> str:
    return " ".join(_PUNCTUATION.sub(" ", text.casefold()).split())
