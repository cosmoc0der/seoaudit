from __future__ import annotations

import re

from seoaudit.core.check import Check
from seoaudit.core.page import Page
from seoaudit.core.urls import pretty_url

_LANG_CODE = re.compile(r"^[a-z]{2,3}(-[a-z]{4})?(-([a-z]{2}|\d{3}))?$")


class InternationalCheck(Check):
    id = "international"

    async def setup(self) -> None:
        self._alternates: dict[str, dict[str, str]] = {}

    async def check_page(self, page: Page) -> None:
        if not page.hreflangs:
            return
        invalid_codes = set(self.params("invalid_code")["known_mistakes"])
        mapping: dict[str, str] = {}
        invalid = []
        for lang, url in page.hreflangs:
            code = lang.lower()
            if code != "x-default" and (not _LANG_CODE.match(code) or code in invalid_codes):
                invalid.append(lang)
            mapping[code] = url

        if invalid:
            self.fail("invalid_code", page.url, codes=invalid)
        if page.url not in mapping.values():
            self.fail("no_self_reference", page.url)
        if "x-default" not in mapping:
            self.fail("no_x_default", page.url)
        self._alternates[page.url] = mapping

    async def check_site(self) -> None:
        if not self._alternates:
            for rule in self.rules:
                self.not_applicable(rule, "no_hreflang")
            return

        targets = {t for m in self._alternates.values() for t in m.values()}
        unknown = [t for t in targets if t not in self.ctx.pages][: self.params("broken_target")["max_probes"]]
        probed = await self.ctx.prober.probe_many(unknown)

        for page_url, mapping in self._alternates.items():
            for lang, target in mapping.items():
                if target == page_url:
                    continue
                page = self.ctx.pages.get(target)
                result = page.response if page else probed.get(target)
                if result is None:
                    continue
                if result.broken or result.redirects:
                    self.fail("broken_target", page_url, lang=lang, target=pretty_url(target), status=result.status or result.error)
                elif page is not None and page.ok and page_url not in self._alternates.get(target, {}).values():
                    self.fail("no_return_link", page_url, lang=lang, target=pretty_url(target))
