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
        self._variants: dict[str, list[str]] = defaultdict(list)

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
        # Vector icons are a few hundred bytes: deferring them saves nothing, and they must not use up
        # the skipped slots meant for the first-screen picture.
        raster = [i for i in images if not _is_vector(i.raw_src)]
        intended = _deliberately_loaded(page)
        report_group(
            self,
            "lazy_loading",
            page.url,
            [
                i.src or i.raw_src
                for i in raster[skip_first:]
                if i.loading not in ("lazy", "eager") and i.raw_src not in intended
            ],
        )
        for image in images:
            if image.delivered:
                self._usage[image.delivered].append(page.url)
            for url in image.variants:
                self._variants[url].append(page.url)

    async def check_site(self) -> None:
        urls = list(self._usage)[: self.options["max_images"]]
        if not urls:
            for rule in ("too_large", "legacy_format", "broken_images"):
                self.not_applicable(rule, "no_images")
            return

        max_kb = self.params("too_large")["max_kb"]
        legacy = self.params("legacy_format")
        # Weight and format matter for the file browsers download; any other offered variant only has to exist.
        results = await self.ctx.prober.probe_many(urls, need_size=True)
        others = [u for u in self._variants if u not in self._usage][: self.options["max_images"]]
        for url, result in (await self.ctx.prober.probe_many(others)).items():
            if result.broken:
                self.fail("broken_images", url, status=result.status or result.error, **_usage(self._variants[url]))
        # Heaviest first: they are the ones worth fixing and the ones that survive the report's URL limit.
        for url, result in sorted(results.items(), key=lambda item: -(item[1].size or 0)):
            usage = _usage(self._usage[url])
            if result.broken:
                self.fail("broken_images", url, status=result.status or result.error, **usage)
                continue
            size_kb = round((result.size or 0) / 1024)
            if size_kb > max_kb:
                self.fail("too_large", url, size_kb=size_kb, **usage)
            if result.content_type in legacy["formats"] and size_kb >= legacy["min_kb"]:
                self.fail("legacy_format", url, format=result.content_type, size_kb=size_kb, **usage)


def _usage(pages: list[str]) -> dict:
    pages = list(dict.fromkeys(pages))
    return {"pages": len(pages), "page": pretty_url(pages[0])}


def _is_vector(src: str) -> bool:
    return src.lower().split("?")[0].endswith(".svg") or src.startswith("data:image/svg")


def _deliberately_loaded(page: Page) -> set[str]:
    """Priority images are meant to load at once; JS lazy-loaders (data-src) already defer the rest."""
    result = set()
    for node in page.tree.css("img"):
        attrs = node.attributes
        src = (attrs.get("src") or "").strip()
        js_lazy = (not src or src.startswith("data:")) and (attrs.get("data-src") or attrs.get("data-srcset"))
        if js_lazy or (attrs.get("fetchpriority") or "").lower() == "high":
            result.add(src or (attrs.get("data-src") or "").strip())
    return result
