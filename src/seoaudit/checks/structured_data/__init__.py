from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

from seoaudit.checks.structured_data.vocab import check_root, roots, vocabulary
from seoaudit.core.check import Check
from seoaudit.core.page import Page

_DATE_PROPS = {
    "startDate", "endDate", "datePublished", "dateModified", "dateCreated",
    "uploadDate", "datePosted", "validThrough", "validFrom", "priceValidUntil",
}  # fmt: skip
_PRICE_PROPS = {"price", "lowPrice", "highPrice"}
_URL_PROPS = {"url", "image", "logo", "thumbnailUrl", "contentUrl"}
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$")
# Google wants a plain number: no currency sign, no thousands separator, a dot for decimals.
_PRICE = re.compile(r"^\d+(?:\.\d+)?$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_OBSOLETE_CURRENCIES = {"RUR"}


class StructuredDataCheck(Check):
    id = "structured_data"

    async def setup(self) -> None:
        self._found_any = False
        self._home_types: set[str] | None = None
        self._home_same_as: bool = False
        self._vocab = vocabulary()
        self._retired: dict[str, list[str]] = {}

    async def check_page(self, page: Page) -> None:
        blocks: list[Any] = []
        for raw in page.jsonld_blocks:
            try:
                blocks.append(json.loads(raw))
            except json.JSONDecodeError as exc:
                self.fail("jsonld_invalid", page.url, line=exc.lineno, col=exc.colno)
        if "itemscope" in (page.html or ""):
            blocks.append(_microdata(page.html, page.url))
        retired = set(self.params("rich_result_retired")["types"])
        items = _merge_by_id(item for block in blocks for item in walk_items(block))
        # Google's lists and formats no longer matter inside markup for a feature it has switched off.
        featured = _merge_by_id(item for block in blocks for item in walk_items(block, skip=retired))

        types = {t for item in items for t in item_types(item)}
        self._found_any |= bool(items)
        if page.url == self.ctx.site.base_url:
            self._home_types = types
            organizations = set(self.params("organization_missing")["types"])
            self._home_same_as = any(
                organizations & set(item_types(item)) and not _empty(item.get("sameAs")) for item in items
            )
        for type_ in types & retired:
            self._retired.setdefault(type_, []).append(page.url)

        self._vocabulary(page, blocks)
        self._missing(page, featured, "schema_required", "required")
        self._missing(page, featured, "schema_recommended", "recommended")
        self._values(page, featured)
        if page.indexable and page.depth and "BreadcrumbList" not in types:
            self.fail("breadcrumbs_missing", page.url)

    async def check_site(self) -> None:
        if not self._found_any:
            self.fail("structured_data_missing", self.ctx.site.base_url)
        if self._home_types is not None:
            expected = set(self.params("organization_missing")["types"])
            if not expected & self._home_types:
                self.fail("organization_missing", self.ctx.site.base_url)
                self.not_applicable("organization_same_as", "no_organization")
            elif not self._home_same_as:
                self.fail("organization_same_as", self.ctx.site.base_url)
        for type_, urls in sorted(self._retired.items()):
            self.fail("rich_result_retired", urls[0], type=type_, pages=len(urls))

    def _vocabulary(self, page: Page, blocks: list[Any]) -> None:
        problems: dict[str, list[str]] = {}
        for block in blocks:
            for root in roots(block):
                check_root(root, self._vocab, lambda key, text: problems.setdefault(key, []).append(text))
        self._report(page, "schema_vocabulary", problems)

    def _missing(self, page: Page, items: list[dict[str, Any]], rule: str, param: str) -> None:
        """Google's property lists per type; subtypes inherit the list of their nearest listed ancestor."""
        table: dict[str, list[str]] = self.params(rule)[param]
        reported = set()
        for item in items:
            for type_ in item_types(item):
                listed = next((t for t in self._vocab.lineage(type_) if t in table), None)
                if listed is None:
                    continue
                missing = [
                    "/".join(alternatives)
                    for alternatives in (entry.split("|") for entry in table[listed])
                    if all(_absent(item, p) for p in alternatives)
                ]
                if missing and (type_, tuple(missing)) not in reported:
                    reported.add((type_, tuple(missing)))
                    self.fail(rule, page.url, type=type_, missing=missing)

    def _values(self, page: Page, items: list[dict[str, Any]]) -> None:
        problems: dict[str, list[str]] = {}
        for item in items:
            for key, found in _value_problems(item).items():
                problems.setdefault(key, []).extend(found)
        self._report(page, "schema_values", problems)

    def _report(self, page: Page, rule: str, problems: dict[str, list[str]]) -> None:
        for key, found in problems.items():
            unique = list(dict.fromkeys(found))
            self.fail(rule, page.url, key=key, count=len(unique), examples=unique[:3])


def _broken_url(value: str) -> bool:
    """Relative URLs resolve against the page like in HTML; a scheme-less host or a local scheme never does."""
    value = value.strip()
    return not value or value.lower().startswith(("www.", "file:", "javascript:", "localhost")) or (
        " " in value and "://" not in value
    )


def _value_problems(item: dict[str, Any]) -> dict[str, list[str]]:
    """Google rejects values in the wrong format even when the property is present."""
    type_ = next(iter(item_types(item)), "Thing")
    result: dict[str, list[str]] = {}

    def add(key: str, prop: str, value: Any) -> None:
        result.setdefault(key, []).append(f"{type_}.{prop} = «{value}»")

    for prop, value in item.items():
        for v in value if isinstance(value, list) else [value]:
            if prop in _DATE_PROPS and isinstance(v, str) and not _ISO_DATE.match(v.strip()):
                add("date", prop, v)
            elif prop in _PRICE_PROPS and isinstance(v, str) and not _PRICE.match(v.strip()):
                add("price", prop, v)
            elif prop == "priceCurrency" and isinstance(v, str) and (not _CURRENCY.match(v) or v in _OBSOLETE_CURRENCIES):
                add("currency", prop, v)
            elif prop in _URL_PROPS and isinstance(v, str) and _broken_url(v):
                add("url", prop, v)
    if type_ == "BreadcrumbList":
        elements = item.get("itemListElement")
        elements = [e for e in (elements if isinstance(elements, list) else [elements]) if isinstance(e, dict)]
        for index, element in enumerate(elements, 1):
            target = element.get("item")
            name = element.get("name") or (target.get("name") if isinstance(target, dict) else None)
            if _empty(name):
                result.setdefault("breadcrumb_name", []).append(f"ListItem {index}")
            if _empty(target) and index < len(elements):
                result.setdefault("breadcrumb_item", []).append(f"ListItem {index}")
            if not str(element.get("position", "")).strip().isdigit():
                result.setdefault("breadcrumb_position", []).append(f"ListItem {index}")
    return result


def _merge_by_id(items: Iterator[dict[str, Any]]) -> list[dict[str, Any]]:
    """Google joins nodes sharing an @id, so a bare reference is not an incomplete object."""
    merged: dict[str, dict[str, Any]] = {}
    result: list[dict[str, Any]] = []
    for item in items:
        node_id = item.get("@id")
        if not isinstance(node_id, str):
            result.append(item)
        elif node_id in merged:
            merged[node_id].update({k: v for k, v in item.items() if not _empty(v)})
        else:
            merged[node_id] = dict(item)
            result.append(merged[node_id])
    return result


def walk_items(node: Any, skip: set[str] = frozenset()) -> Iterator[dict[str, Any]]:
    if isinstance(node, list):
        for item in node:
            yield from walk_items(item, skip)
    elif isinstance(node, dict):
        if "@type" in node:
            if skip & set(item_types(node)):
                return
            yield node
        for key, value in node.items():
            if key != "@context":
                yield from walk_items(value, skip)


def _absent(item: dict[str, Any], path: str) -> bool:
    """A nested path such as offers.availability only counts when its parent object is there."""
    head, _, rest = path.partition(".")
    value = item.get(head)
    if not rest:
        return _empty(value)
    children = [v for v in (value if isinstance(value, list) else [value]) if isinstance(v, dict)]
    return any(_absent(child, rest) for child in children)


def _microdata(html: str, url: str) -> list[dict[str, Any]]:
    import extruct

    try:
        data = extruct.extract(html, base_url=url, syntaxes=["microdata"], uniform=True)
    except Exception:
        return []
    return data.get("microdata", [])


def item_types(item: dict[str, Any]) -> list[str]:
    raw = item.get("@type")
    values = raw if isinstance(raw, list) else [raw]
    return [str(v).rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1] for v in values if v]


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}
