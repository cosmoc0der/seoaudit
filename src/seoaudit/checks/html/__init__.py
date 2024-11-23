from __future__ import annotations

import asyncio
import re
from collections import Counter

import httpx

from seoaudit.checks._utils import truncate
from seoaudit.core.check import Check
from seoaudit.core.page import Page

_DOCTYPE = re.compile(r"^\s*(?:<!--.*?-->\s*)*<!doctype\s+html", re.I | re.S)


class HtmlCheck(Check):
    id = "html"

    async def setup(self) -> None:
        self._validate: list[tuple[str, str]] = []

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

        if self.enabled("w3c_errors") and len(self._validate) < self.options["validator_pages"] and html:
            self._validate.append((page.url, html))

    async def check_site(self) -> None:
        if not self.enabled("w3c_errors"):
            return
        if not self._validate:
            self.not_applicable("w3c_errors")
            return
        endpoint = self.options["validator_url"]
        failures = 0
        for url, html in self._validate:
            try:
                messages = await self._validate_html(endpoint, html)
            except Exception as exc:
                failures += 1
                self.ctx.diagnostics.append(f"html: validator failed for {url}: {exc!r}")
                continue
            errors = [m for m in messages if m.get("type") == "error"]
            if len(errors) > self.params("w3c_errors")["max_errors"]:
                examples = [f"L{m.get('lastLine', '?')}: {truncate(m.get('message', ''), 100)}" for m in errors[:5]]
                self.fail("w3c_errors", url, count=len(errors), examples=examples)
            await asyncio.sleep(self.options["validator_delay"])
        if failures == len(self._validate):
            self.error("w3c_errors", "validator_unavailable")

    async def _validate_html(self, endpoint: str, html: str) -> list[dict]:
        response = await self.ctx.client.post(
            endpoint,
            params={"out": "json"},
            content=html.encode("utf-8"),
            headers={"Content-Type": "text/html; charset=utf-8"},
            timeout=60,
        )
        if response.status_code != 200:
            raise httpx.HTTPStatusError(f"HTTP {response.status_code}", request=response.request, response=response)
        return response.json().get("messages", [])


def _is_plugin(attrs: dict) -> bool:
    value = " ".join(str(attrs.get(k) or "") for k in ("type", "data", "src", "classid")).lower()
    return any(marker in value for marker in ("flash", ".swf", "java", "silverlight", "clsid"))
