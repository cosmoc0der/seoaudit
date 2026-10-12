from __future__ import annotations

import re
from collections import Counter

from seoaudit.checks._utils import truncate
from seoaudit.core.check import Check
from seoaudit.core.page import Page

from .validators import Validator, ValidatorUnavailable, create_validator

_DOCTYPE = re.compile(r"^\s*(?:<!--.*?-->\s*)*<!doctype\s+html", re.I | re.S)
# Exact types and extensions: bare "java"/"flash" also match javascript or flashlight.svg.
_PLUGIN_MARKERS = ("application/x-shockwave-flash", ".swf", "application/x-java", "x-silverlight", "clsid:")


class HtmlCheck(Check):
    id = "html"

    async def setup(self) -> None:
        self._validator: Validator | None = None
        self._validator_error: str | None = None
        self._queued = 0
        if not self.enabled("w3c_errors") or self.options["validator_pages"] == 0:
            return
        try:
            self._validator = await create_validator(self.options, self.ctx.client, self.ctx.diagnostics)
        except ValidatorUnavailable as exc:
            self._validator_error = str(exc)

    async def check_page(self, page: Page) -> None:
        html = (page.html or "").lstrip("﻿")
        if not _DOCTYPE.match(html):
            self.fail("doctype_missing", page.url)

        ids = Counter(n.attributes.get("id") for n in page.tree.css("[id]") if n.attributes.get("id"))
        duplicates = [i for i, count in ids.items() if count > 1]
        if duplicates:
            self.fail("duplicate_ids", page.url, count=len(duplicates), examples=duplicates[:5])

        deprecated = self.params("deprecated_tags")["tags"]
        found = sorted({n.tag for n in page.tree.css(", ".join(deprecated))})
        if found:
            self.fail("deprecated_tags", page.url, found=[f"<{t}>" for t in found])

        plugins = [n for n in page.tree.css("object, embed, applet") if _is_plugin(n.attributes)]
        if plugins:
            self.fail("obsolete_plugins", page.url, count=len(plugins))

        validator = self._validator
        if validator and html and (validator.max_pages is None or self._queued < validator.max_pages):
            self._queued += 1
            validator.add(page.url, html)

    async def check_site(self) -> None:
        if not self.enabled("w3c_errors"):
            return
        if self._validator_error:
            self.ctx.diagnostics.append(f"html: {self._validator_error}")
            self.error("w3c_errors", "validator_unavailable")
            return
        if not self._queued:
            self.not_applicable("w3c_errors")
            return
        try:
            results, errors = await self._validator.run()
        except ValidatorUnavailable as exc:
            results, errors = {}, [str(exc)]
        for error in errors:
            self.ctx.diagnostics.append(f"html: {self._validator.name}: {error}")
        if not results:
            self.error("w3c_errors", "validator_unavailable")
            return
        limit = self.params("w3c_errors")["max_errors"]
        # CSS parser complaints are not markup errors and inflate the count shown to the client.
        by_page = {
            url: [m for m in messages if m.get("type") == "error" and not m.get("message", "").startswith("CSS:")]
            for url, messages in results.items()
        }
        for url, errors in sorted(by_page.items(), key=lambda item: -len(item[1])):
            if len(errors) > limit:
                distinct: dict[str, dict] = {}
                for m in errors:
                    distinct.setdefault(m.get("message", ""), m)
                examples = [f"L{m.get('lastLine', '?')}: {truncate(text, 100)}" for text, m in list(distinct.items())[:5]]
                self.fail("w3c_errors", url, count=len(errors), examples=examples)


def _is_plugin(attrs: dict) -> bool:
    value = " ".join(str(attrs.get(k) or "") for k in ("type", "data", "src", "classid")).lower()
    return any(marker in value for marker in _PLUGIN_MARKERS)
