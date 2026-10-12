from __future__ import annotations

from fnmatch import fnmatch
from urllib.parse import parse_qsl, unquote, urlsplit

from seoaudit.core.check import Check
from seoaudit.core.page import Page
from seoaudit.core.urls import pretty_url


class UrlsCheck(Check):
    id = "urls"

    async def check_page(self, page: Page) -> None:
        parts = urlsplit(page.url)
        path = unquote(parts.path)
        url = pretty_url(page.url)

        if len(url) > self.params("too_long")["max_length"]:
            self.fail("too_long", page.url, length=len(url))
        if path != path.lower():
            self.fail("uppercase", page.url)
        if "_" in path:
            self.fail("underscores", page.url)
        if "//" in path:
            self.fail("double_slash", page.url)
        if " " in path:
            self.fail("spaces", page.url)
        if parts.query and page.indexable and not self._only_pagination(parts.query):
            self.fail("query_params", page.url, query=unquote(parts.query))

    def _only_pagination(self, query: str) -> bool:
        patterns = self.params("query_params")["pagination"]
        keys = [key.lower() for key, _ in parse_qsl(query, keep_blank_values=True)]
        return bool(keys) and all(any(fnmatch(key, p) for p in patterns) for key in keys)
