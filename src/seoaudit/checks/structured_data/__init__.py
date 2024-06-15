from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from seoaudit.core.check import Check
from seoaudit.core.page import Page


class StructuredDataCheck(Check):
    id = "structured_data"

    async def setup(self) -> None:
        self._found_any = False
        self._home_types: set[str] | None = None

    async def check_page(self, page: Page) -> None:
        items: list[dict[str, Any]] = []
        for raw in page.jsonld_blocks:
            try:
                items.extend(_walk(json.loads(raw)))
            except json.JSONDecodeError as exc:
                self.fail("jsonld_invalid", page.url, error=f"{exc.msg} (line {exc.lineno}, col {exc.colno})")
        if "itemscope" in (page.html or ""):
            items.extend(_microdata(page.html, page.url))

        types = {t for item in items for t in _types(item)}
        self._found_any |= bool(items)
        if page.url == self.ctx.site.base_url:
            self._home_types = types

        self._required_props(page, items)
        if page.depth and "BreadcrumbList" not in types:
            self.fail("breadcrumbs_missing", page.url)

    async def check_site(self) -> None:
        if not self._found_any:
            self.fail("structured_data_missing", self.ctx.site.base_url)
        if self._home_types is not None:
            expected = set(self.params("organization_missing")["types"])
            if not expected & self._home_types:
                self.fail("organization_missing", self.ctx.site.base_url)

    def _required_props(self, page: Page, items: list[dict[str, Any]]) -> None:
        params = self.params("schema_required")
        required: dict[str, list[str]] = params["required"]
        one_of: dict[str, list[str]] = params["one_of"]
        for item in items:
            for type_ in _types(item):
                missing = [prop for prop in required.get(type_, []) if _empty(item.get(prop))]
                alternatives = one_of.get(type_)
                if alternatives and all(_empty(item.get(p)) for p in alternatives):
                    missing.append(" | ".join(alternatives))
                if missing:
                    self.fail("schema_required", page.url, type=type_, missing=missing)


def _walk(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, list):
        for item in node:
            yield from _walk(item)
    elif isinstance(node, dict):
        if "@type" in node:
            yield node
        for key, value in node.items():
            if key != "@context":
                yield from _walk(value)


def _microdata(html: str, url: str) -> list[dict[str, Any]]:
    import extruct

    try:
        data = extruct.extract(html, base_url=url, syntaxes=["microdata"], uniform=True)
    except Exception:
        return []
    return list(_walk(data.get("microdata", [])))


def _types(item: dict[str, Any]) -> list[str]:
    raw = item.get("@type")
    values = raw if isinstance(raw, list) else [raw]
    return [str(v).rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1] for v in values if v]


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}
