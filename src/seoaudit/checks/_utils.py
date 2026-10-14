from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable
from fnmatch import fnmatch
from urllib.parse import parse_qsl, urlsplit

from seoaudit.core.check import Check
from seoaudit.core.page import Page
from seoaudit.core.urls import pretty_url


class DuplicateTracker:
    """Groups page URLs by a normalized value to report duplicates after the crawl."""

    def __init__(self, normalize: Callable[[str], str] = str.casefold):
        self._groups: dict[str, list[tuple[str, str]]] = defaultdict(list)
        self._normalize = normalize

    def add(self, value: str | None, url: str) -> None:
        if value:
            self._groups[self._normalize(value)].append((url, value))

    def report(self, check: Check, rule: str) -> None:
        for items in self._groups.values():
            if len(items) < 2:
                continue
            urls = [u for u, _ in items]
            for url, value in items:
                others = [u for u in urls if u != url]
                check.fail(rule, url, value=value, count=len(others), others=others[:5])


def report_group(check: Check, rule: str, page_url: str, items: list[str], examples: int = 5) -> None:
    """One finding per page listing offending items, instead of a finding per item."""
    unique = list(dict.fromkeys(items))
    if unique:
        check.fail(rule, page_url, count=len(unique), examples=[pretty_url(u) for u in unique[:examples]])


def report_target(check: Check, rule: str, target: str, sources: list[str], max_sources: int = 50, **data) -> None:
    """One finding per bad URL: it is fixed once, however many pages point to it."""
    sources = list(dict.fromkeys(sources))
    check.fail(
        rule,
        target,
        pages=len(sources),
        examples=[pretty_url(s) for s in sources[:5]],
        sources=sources[:max_sources],
        **data,
    )


_PAGINATION_PATH = re.compile(r"/page/\d+/?$")
_PAGINATION_PARAMS = ("page", "p", "pg", "paged", "pagen_*")


def is_paginated(url: str) -> bool:
    """/category/page/2/ or ?page=2: CMS keep such pages out of sitemaps, and their depth grows with the archive."""
    parts = urlsplit(url)
    if _PAGINATION_PATH.search(parts.path):
        return True
    keys = [key.lower() for key, _ in parse_qsl(parts.query, keep_blank_values=True)]
    return bool(keys) and all(any(fnmatch(key, p) for p in _PAGINATION_PARAMS) for key in keys)


def truncate(text: str, limit: int = 120) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def changed_signals(before: Page, after: Page) -> list[tuple[str, str, str]]:
    """Head values search engines go by that differ between two versions of a page, as (name, before, after)."""
    signals = {
        "title": lambda p: p.title or "",
        "description": lambda p: p.meta("description") or "",
        "canonical": lambda p: pretty_url(p.canonicals[0]) if p.canonicals else "",
        "robots": lambda p: ", ".join(sorted(p.robots)),
    }
    return [
        (name, truncate(value(before), 80) or "—", truncate(value(after), 80) or "—")
        for name, value in signals.items()
        if value(before) != value(after)
    ]
