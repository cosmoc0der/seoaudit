from __future__ import annotations

import re
from collections import defaultdict

from seoaudit.checks._utils import report_target
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
        self._duplicate_langs(page)

    def _duplicate_langs(self, page: Page) -> None:
        urls: dict[str, list[str]] = defaultdict(list)
        for lang, url in page.hreflangs:
            if url not in urls[lang.lower()]:
                urls[lang.lower()].append(url)
        duplicated = {lang: found for lang, found in urls.items() if len(found) > 1}
        if duplicated:
            lang, found = next(iter(duplicated.items()))
            self.fail("duplicate_lang", page.url, lang=lang, count=len(duplicated), urls=[pretty_url(u) for u in found[:3]])

    async def check_site(self) -> None:
        if not self._alternates:
            for rule in self.rules:
                self.not_applicable(rule, "no_hreflang")
            return

        targets = {t for m in self._alternates.values() for t in m.values()}
        unknown = [t for t in targets if t not in self.ctx.pages][: self.params("broken_target")["max_probes"]]
        probed = await self.ctx.prober.probe_many(unknown)

        broken: dict[tuple[str, str], dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        for page_url, mapping in self._alternates.items():
            for lang, target in mapping.items():
                if target == page_url:
                    continue
                page = self.ctx.pages.get(target)
                result = page.response if page else probed.get(target)
                if result is None:
                    continue
                if result.broken:
                    broken[target, str(result.status or result.error)][lang].append(page_url)
                elif result.redirects:
                    redirect = f"{result.redirects[0].status} → {pretty_url(result.final_url)}"
                    broken[target, redirect][lang].append(page_url)
                elif page is not None and page.ok and page_url not in self._alternates.get(target, {}).values():
                    self.fail("no_return_link", page_url, lang=lang, target=pretty_url(target))
        for (target, status), by_lang in broken.items():
            sources = [url for urls in by_lang.values() for url in urls]
            report_target(self, "broken_target", target, sources, lang=", ".join(by_lang), status=status)
        await self._non_indexable_targets()

    async def _non_indexable_targets(self) -> None:
        refs: dict[str, list[str]] = defaultdict(list)
        for page_url, mapping in self._alternates.items():
            for target in mapping.values():
                if target != page_url:
                    refs[target].append(page_url)
        pages = await self.ctx.load_pages(list(refs), self.params("target_non_indexable")["max_fetches"])
        for target, sources in refs.items():
            page = pages.get(target)
            if page is None or not page.ok or page.indexable:
                continue
            key = "noindex" if "noindex" in page.robots else "canonical"
            final = pretty_url(page.canonicals[0]) if page.canonicals else ""
            report_target(self, "target_non_indexable", target, sources, key=key, final=final)
