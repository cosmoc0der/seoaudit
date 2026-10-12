from __future__ import annotations

import re

from seoaudit.checks._utils import DuplicateTracker, truncate
from seoaudit.core.check import Check
from seoaudit.core.page import Page

from .pixels import text_width

_MAX_SCALE = re.compile(r"maximum-scale=([\d.]+)")


def _max_scale(viewport: str) -> float:
    """WCAG requires zoom to at least 200%, so a lower maximum-scale blocks it."""
    match = _MAX_SCALE.search(viewport)
    try:
        return float(match[1]) if match else float("inf")
    except ValueError:
        return float("inf")


class MetaCheck(Check):
    id = "meta"

    async def setup(self) -> None:
        self._titles = DuplicateTracker()
        self._descriptions = DuplicateTracker()
        self._home_has_icon: bool | None = None

    async def check_page(self, page: Page) -> None:
        self._title(page)
        self._description(page)
        self._viewport(page)
        self._basics(page)
        self._social(page)
        self._description_multiple(page)
        if page.url == self.ctx.site.base_url:
            self._home_has_icon = bool(page.link_rel("icon") or page.link_rel("apple-touch-icon"))

    async def check_site(self) -> None:
        self._titles.report(self, "title_duplicate")
        self._descriptions.report(self, "description_duplicate")
        if self._home_has_icon is False:
            favicon = await self.ctx.prober.probe(f"{self.ctx.site.origin}/favicon.ico")
            if not favicon.ok or not favicon.content_type.startswith("image"):
                self.fail("favicon_missing", self.ctx.site.base_url)

    def _title(self, page: Page) -> None:
        title = page.title
        if not title:
            self.fail("title_missing", page.url)
            return
        if len(page.titles) > 1:
            self.fail("title_multiple", page.url, count=len(page.titles))
        if page.indexable:
            self._length("title_length", page.url, title)
            self._titles.add(title, page.url)

    def _description(self, page: Page) -> None:
        # Snippets only matter for pages that can appear in search results.
        if not page.indexable:
            return
        description = page.meta("description")
        if not description:
            self.fail("description_missing", page.url)
            return
        self._length("description_length", page.url, description)
        self._descriptions.add(description, page.url)

    def _length(self, rule: str, url: str, value: str) -> None:
        """Google truncates snippets by rendered width, so wide letters run out of room long before 60 characters."""
        params = self.params(rule)
        width = text_width(value, params["font_px"])
        if width < params["min_px"]:
            self.fail(rule, url, key="short", value=truncate(value), width=width, length=len(value))
        elif width > params["max_px"]:
            self.fail(rule, url, key="long", value=truncate(value), width=width, length=len(value))

    def _viewport(self, page: Page) -> None:
        viewport = page.meta("viewport")
        if viewport is None:
            self.fail("viewport_missing", page.url)
            return
        value = viewport.replace(" ", "").lower()
        if "width=device-width" not in value:
            self.fail("viewport_invalid", page.url, key="width", value=viewport)
        elif "user-scalable=no" in value or "user-scalable=0" in value or _max_scale(value) < 2:
            self.fail("viewport_invalid", page.url, key="zoom", value=viewport)

    def _basics(self, page: Page) -> None:
        if not page.meta("charset") and "charset=" not in page.headers.get("content-type", "").lower():
            content_type = page.meta("content-type") or ""
            if "charset=" not in content_type.lower():
                self.fail("charset_missing", page.url)
        if not page.lang:
            self.fail("lang_missing", page.url)
        # A refresh without url= only reloads the page (live feeds), it is not a redirect.
        if (refresh := page.meta("refresh")) and "url=" in refresh.replace(" ", "").lower():
            self.fail("meta_refresh", page.url, value=refresh)

    def _social(self, page: Page) -> None:
        if not page.indexable:
            return
        missing = [tag for tag in self.params("og_missing")["required"] if not page.meta(tag)]
        if missing:
            self.fail("og_missing", page.url, missing=missing)
        if not page.meta("twitter:card"):
            self.fail("twitter_card_missing", page.url)

    def _description_multiple(self, page: Page) -> None:
        values = page.meta_tags.get("description", [])
        if len(values) > 1:
            self.fail("description_multiple", page.url, count=len(values), values=[truncate(v, 60) for v in values[:3]])
