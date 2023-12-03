from __future__ import annotations

from collections import defaultdict

from seoaudit.checks._utils import report_group
from seoaudit.core.check import Check
from seoaudit.core.page import Page
from seoaudit.core.urls import pretty_url


class ImagesCheck(Check):
    id = "images"

    async def setup(self) -> None:
        self._usage: dict[str, list[str]] = defaultdict(list)

    async def check_page(self, page: Page) -> None:
        images = page.images
        allow_empty = self.params("alt_missing")["allow_empty"]
        report_group(
            self,
            "alt_missing",
            page.url,
            [i.src or i.raw_src for i in images if i.alt is None or (not allow_empty and not i.alt.strip())],
        )
        report_group(
            self,
            "missing_dimensions",
            page.url,
            [i.src or i.raw_src for i in images if not (i.width and i.height) and not _is_vector(i.raw_src)],
        )
        skip_first = self.params("lazy_loading")["skip_first"]
        report_group(
            self, "lazy_loading", page.url, [i.src or i.raw_src for i in images[skip_first:] if i.loading != "lazy"]
        )
        for image in images:
            if image.src:
                self._usage[image.src].append(page.url)

    async def check_site(self) -> None:
        urls = list(self._usage)[: self.options["max_images"]]
        if not urls:
            for rule in ("too_large", "legacy_format", "broken_images"):
                self.not_applicable(rule, "no_images")
            return

        max_kb = self.params("too_large")["max_kb"]
        legacy = self.params("legacy_format")
        results = await self.ctx.prober.probe_many(urls, need_size=True)
        for url, result in results.items():
            pages = list(dict.fromkeys(self._usage[url]))
            usage = {"pages": len(pages), "page": pretty_url(pages[0])}
            if result.broken:
                self.fail("broken_images", url, status=result.status or result.error, **usage)
                continue
            size_kb = round((result.size or 0) / 1024)
            if size_kb > max_kb:
                self.fail("too_large", url, size_kb=size_kb, **usage)
            if result.content_type in legacy["formats"] and size_kb >= legacy["min_kb"]:
                self.fail("legacy_format", url, format=result.content_type, size_kb=size_kb, **usage)


def _is_vector(src: str) -> bool:
    return src.lower().split("?")[0].endswith(".svg") or src.startswith("data:image/svg")
