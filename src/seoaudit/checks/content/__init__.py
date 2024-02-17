from __future__ import annotations

import hashlib
import re

from seoaudit.checks._utils import truncate
from seoaudit.core.check import Check
from seoaudit.core.page import Page

_SHINGLE = 3
_WORD = re.compile(r"\w+", re.UNICODE)


class ContentCheck(Check):
    id = "content"

    async def setup(self) -> None:
        self._exact: dict[str, list[str]] = {}
        self._fingerprints: list[tuple[str, int]] = []

    async def check_page(self, page: Page) -> None:
        self._headings(page)
        self._volume(page)
        if page.indexable:
            self._fingerprint(page)

    async def check_site(self) -> None:
        duplicates: set[str] = set()
        for urls in self._exact.values():
            if len(urls) > 1:
                for url in urls:
                    duplicates.add(url)
                    self.fail("duplicate_content", url, count=len(urls) - 1, others=[u for u in urls if u != url][:5])
        self._near_duplicates(duplicates)

    def _headings(self, page: Page) -> None:
        h1 = [text for level, text in page.headings if level == 1]
        if not h1:
            self.fail("h1_missing", page.url)
        elif len(h1) > 1:
            self.fail("h1_multiple", page.url, count=len(h1), values=[truncate(t, 60) for t in h1[:3]])
        if any(not text for text in h1):
            self.fail("heading_empty", page.url)

        previous = 0
        for level, text in page.headings:
            if previous and level > previous + 1:
                self.fail("heading_hierarchy", page.url, prev=f"h{previous}", next=f"h{level}", value=truncate(text, 60))
                break
            previous = level

    def _volume(self, page: Page) -> None:
        words = page.word_count
        if words < self.params("thin_content")["min_words"]:
            self.fail("thin_content", page.url, words=words)

        html_size = len(page.html or "")
        if html_size:
            ratio = round(100 * len(page.text_content) / html_size, 1)
            if ratio < self.params("text_ratio")["min_percent"]:
                self.fail("text_ratio", page.url, ratio=ratio)

        markers = self.params("placeholder_text")["markers"]
        lowered = page.text_content.lower()
        found = [m for m in markers if m in lowered]
        if found:
            self.fail("placeholder_text", page.url, markers=found)

    def _fingerprint(self, page: Page) -> None:
        words = [w.lower() for w in _WORD.findall(page.text_content)]
        if len(words) < self.params("near_duplicate")["min_words"]:
            return
        digest = hashlib.blake2b(" ".join(words).encode(), digest_size=16).hexdigest()
        self._exact.setdefault(digest, []).append(page.url)
        self._fingerprints.append((page.url, _simhash(words)))

    def _near_duplicates(self, exact: set[str]) -> None:
        max_distance = self.params("near_duplicate")["max_distance"]
        items = [(u, h) for u, h in self._fingerprints if u not in exact]
        matches: dict[str, list[str]] = {}
        for i, (url_a, hash_a) in enumerate(items):
            for url_b, hash_b in items[i + 1 :]:
                if (hash_a ^ hash_b).bit_count() <= max_distance:
                    matches.setdefault(url_a, []).append(url_b)
                    matches.setdefault(url_b, []).append(url_a)
        for url, similar in matches.items():
            self.fail("near_duplicate", url, count=len(similar), others=similar[:5])


def _simhash(words: list[str]) -> int:
    weights = [0] * 64
    for i in range(max(1, len(words) - _SHINGLE + 1)):
        shingle = " ".join(words[i : i + _SHINGLE])
        value = int.from_bytes(hashlib.blake2b(shingle.encode(), digest_size=8).digest(), "big")
        for bit in range(64):
            weights[bit] += 1 if value >> bit & 1 else -1
    return sum(1 << bit for bit, weight in enumerate(weights) if weight > 0)
