from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable

from seoaudit.core.check import Check
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


def truncate(text: str, limit: int = 120) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
