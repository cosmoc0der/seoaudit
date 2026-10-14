from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from seoaudit.checks.structured_data import item_types, walk_items
from seoaudit.checks.structured_data.vocab import vocabulary
from seoaudit.core.check import Check
from seoaudit.core.page import Page

_ARTICLE_MICRODATA = re.compile(r"itemtype=[\"']?https?://schema\.org/(?:\w*Article|\w*BlogPosting)\b", re.I)
# A substring match: BEM classes like card__author have no word boundary before "author".
_AUTHOR_MARKUP = re.compile(r"itemprop=[\"']?author\b|rel=[\"']?author\b|class=[\"'][^\"']*(?:author|byline)", re.I)
_DATE_MARKUP = re.compile(r"itemprop=[\"']?date(?:Published|Modified)\b|<time\b[^>]*\bdatetime=", re.I)
_AUTHOR_META = ("author", "article:author")
_DATE_META = ("article:published_time", "article:modified_time", "og:updated_time")
_W3C_DATE = re.compile(r"^\d{4}(?:-\d{2}(?:-\d{2}(?:T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2}))?)?)?$")
# Bing keeps such pages out of Copilot answers or limits them to the title and snippet.
_BING_RESTRICTIONS = {"noarchive", "nocache"}


class GeoCheck(Check):
    """What AI search needs beyond crawler access: snippet permission, authorship, freshness and trust pages."""

    id = "geo"

    async def setup(self) -> None:
        self._vocab = vocabulary()
        patterns = self.params("trust_pages_missing")["patterns"]
        self._trust = {kind: re.compile(pattern, re.I) for kind, pattern in patterns.items()}
        self._trust_found: set[str] = set()

    async def check_page(self, page: Page) -> None:
        self._trust_links(page)
        if not page.indexable:
            return
        self._snippets(page)
        self._article(page)
        params = self.params("unstructured_text")
        if page.word_count >= params["min_words"] and not any(level in (2, 3) for level, _ in page.headings):
            self.fail("unstructured_text", page.url, words=page.word_count)

    async def check_site(self) -> None:
        for kind in self._trust:
            if kind not in self._trust_found:
                self.fail("trust_pages_missing", self.ctx.site.base_url, key=kind)
        self._lastmod()

    def _trust_links(self, page: Page) -> None:
        targets = [(page.url, ""), *((link.url, link.text) for link in page.links if link.internal)]
        for url, text in targets:
            haystack = f"{urlsplit(url).path} {text}"
            self._trust_found.update(kind for kind, pattern in self._trust.items() if pattern.search(haystack))

    def _snippets(self, page: Page) -> None:
        robots = page.robots
        if "nosnippet" in robots:
            self.fail("snippet_restricted", page.url, key="nosnippet")
        limit = next((int(v) for d in robots if d.startswith("max-snippet:") and _is_int(v := d.partition(":")[2])), None)
        if limit is not None and 0 <= limit < self.params("snippet_restricted")["min_snippet"]:
            self.fail("snippet_restricted", page.url, key="max_snippet", value=limit)
        if bing := page.robots_for("bingbot") & _BING_RESTRICTIONS:
            self.fail("snippet_restricted", page.url, key="bing", directives=sorted(bing))

    def _article(self, page: Page) -> None:
        html = page.html or ""
        articles = [i for i in _jsonld_items(page) if any("Article" in self._vocab.lineage(t) for t in item_types(i))]
        if not (articles or _ARTICLE_MICRODATA.search(html) or self._og_article(page)):
            return
        has_author = (
            any(i.get("author") for i in articles)
            or any(page.meta(name) for name in _AUTHOR_META)
            or page.link_rel("author")
            or _AUTHOR_MARKUP.search(html)
        )
        if not has_author:
            self.fail("author_missing", page.url)
        has_date = (
            any(i.get("datePublished") or i.get("dateModified") for i in articles)
            or any(page.meta(name) for name in _DATE_META)
            or _DATE_MARKUP.search(html)
        )
        if not has_date:
            self.fail("date_missing", page.url)

    def _og_article(self, page: Page) -> bool:
        """Some site builders put og:type article on every page, listings included; a real article has body text."""
        if page.meta("og:type") != "article":
            return False
        words = self.options["paragraph_words"]
        long = sum(1 for node in page.tree.css("p") if len(node.text().split()) >= words)
        return long >= self.options["article_paragraphs"]

    def _lastmod(self) -> None:
        sitemaps = [s for s in self.ctx.site.sitemaps if s.lastmods]
        if not sitemaps:
            self.not_applicable("sitemap_lastmod", "no_sitemap")
            return
        params = self.params("sitemap_lastmod")
        url = sitemaps[0].url
        values = [v for s in sitemaps for v in s.lastmods]
        present = [v for v in values if v]
        if len(values) - len(present) > len(values) * params["max_missing_share"]:
            self.fail("sitemap_lastmod", url, key="missing", missing=len(values) - len(present), total=len(values))
        if invalid := [v for v in present if not _W3C_DATE.match(v)]:
            self.fail("sitemap_lastmod", url, key="invalid", count=len(invalid), examples=invalid[:3])
        valid = [v for v in present if _W3C_DATE.match(v)]
        # Generators that stamp every URL with the build time make lastmod meaningless to crawlers.
        if len(valid) >= params["min_urls_same"] and len({v[:10] for v in valid}) == 1:
            self.fail("sitemap_lastmod", url, key="same", total=len(valid), value=valid[0][:10])
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
        if future := [v for v in valid if v[:10] > tomorrow]:
            self.fail("sitemap_lastmod", url, key="future", count=len(future), examples=future[:3])


def _jsonld_items(page: Page) -> list[dict[str, Any]]:
    items = []
    for raw in page.jsonld_blocks:
        try:
            items.extend(walk_items(json.loads(raw)))
        except ValueError:
            continue
    return items


def _is_int(value: str) -> bool:
    return value.strip().lstrip("-").isdigit()
