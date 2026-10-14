from __future__ import annotations

import asyncio
import json

from seoaudit.checks._utils import changed_signals
from seoaudit.checks.structured_data.vocab import roots
from seoaudit.core.check import Check
from seoaudit.core.http import FetchResult, fetch
from seoaudit.core.page import Page
from seoaudit.core.urls import pretty_url

from .renderer import Rendered, RenderUnavailable, render

_SNIPPET_SIGNALS = ("title", "description")


class RenderingCheck(Check):
    """Compares the HTML a server sends with the DOM after JavaScript, on a sample of key pages."""

    id = "rendering"

    async def check_site(self) -> None:
        urls = self.ctx.key_pages(self.options["max_pages"])
        crawl = self.ctx.config.crawl
        locale = self.ctx.client.headers.get("accept-language", "en").split(",")[0]
        try:
            rendered, raw = await asyncio.gather(
                render(urls, crawl.effective_user_agent, locale, crawl.device == "mobile", self.options),
                asyncio.gather(*(self._raw(u) for u in urls)),
            )
        except RenderUnavailable as exc:
            self.ctx.diagnostics.append(f"rendering: {exc}")
            for rule in self.rules:
                self.skip(rule, "render_unavailable")
            return

        errors = []
        for page in raw:
            result = rendered[page.url]
            if isinstance(result, Exception):
                errors.append(f"{page.url}: {str(result).strip().splitlines()[0]}")
            elif page.ok:
                self._compare(page, result)
        self.ctx.diagnostics.extend(f"rendering: {e}" for e in errors)
        if errors and len(errors) == len(urls):
            for rule in self.rules:
                self.error(rule, errors[0])

    async def _raw(self, url: str) -> Page:
        response = await fetch(self.ctx.client, url)
        return Page(url=url, response=response, scope=self.ctx.scope, robots_agent=self.ctx.config.crawl.robots_agent)

    def _compare(self, raw: Page, result: Rendered) -> None:
        url = raw.url
        final = self.ctx.scope.normalize(result.url) or result.url
        if final != url:
            # Another page entirely: comparing its content with this one says nothing.
            self.fail("js_indexing_changed", url, key="url", rendered=pretty_url(final))
            return
        # Response headers (X-Robots-Tag, Link) are the same for both; only the document differs.
        response = FetchResult(url=url, final_url=url, status=200, headers={**raw.headers, "content-type": "text/html"})
        rendered = Page(url=url, response=response, scope=self.ctx.scope, html=result.html, robots_agent=raw.robots_agent)

        for key, before, after in changed_signals(raw, rendered):
            rule = "js_meta_changed" if key in _SNIPPET_SIGNALS else "js_indexing_changed"
            self.fail(rule, url, key=key, raw=before, rendered=after)

        params = self.params("js_content")
        if (
            rendered.word_count - raw.word_count >= params["min_words"]
            and raw.word_count < rendered.word_count * params["max_raw_share"]
        ):
            self.fail("js_content", url, raw=raw.word_count, rendered=rendered.word_count)

        links = _internal_links(rendered) - _internal_links(raw)
        if len(links) >= self.params("js_links")["min_links"]:
            self.fail("js_links", url, count=len(links), examples=[pretty_url(u) for u in sorted(links)[:5]])

        if types := _jsonld_types(rendered) - _jsonld_types(raw):
            self.fail("js_structured_data", url, types=sorted(types))


def _internal_links(page: Page) -> set[str]:
    return {link.url for link in page.links if link.internal and link.url != page.url}


def _jsonld_types(page: Page) -> set[str]:
    types = set()
    for block in page.jsonld_blocks:
        try:
            data = json.loads(block)
        except ValueError:
            continue
        for node in roots(data):
            value = node.get("@type")
            types.update(t for t in (value if isinstance(value, list) else [value]) if isinstance(t, str))
    return types
