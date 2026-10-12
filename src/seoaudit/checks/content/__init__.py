from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from selectolax.lexbor import LexborHTMLParser

from seoaudit.checks._utils import truncate
from seoaudit.core import robots_txt
from seoaudit.core.check import Check
from seoaudit.core.page import Page
from seoaudit.core.urls import pretty_url

_SHINGLE = 3
_WORD = re.compile(r"\w+", re.UNICODE)
_NOT_FOUND = re.compile(
    r"\b404\b|\bnot found\b|page (?:does not|doesn't) exist|не найден|не существует|ничего не найдено|nothing found",
    re.I,
)
_CHROME = "header, nav, footer, aside, [role=navigation], [role=banner], [role=contentinfo]"
_MEDIA = "img, picture, video, iframe, canvas, object, embed, form, input, select"
_APP_ROOT = "#root, #app, #__next, #__nuxt, #___gatsby, app-root, [data-reactroot], [ng-version]"


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
        self._page_extras(page)

    async def check_site(self) -> None:
        duplicates: set[str] = set()
        for urls in self._exact.values():
            if len(urls) > 1:
                duplicates.update(urls)
                remaining = self._param_duplicates(urls)
                if len(remaining) > 1:
                    self._report_group("duplicate_content", remaining)
        self._near_duplicates(duplicates)

    def _report_group(self, rule: str, urls: list[str]) -> None:
        """The first page lists the group; the rest point to it instead of repeating the whole list."""
        first, *copies = urls
        self.fail(rule, first, count=len(copies), others=[pretty_url(u) for u in copies[:5]])
        for url in copies:
            self.fail(rule, url, key="copy", original=pretty_url(first))

    def _headings(self, page: Page) -> None:
        h1 = [text for level, text in page.headings if level == 1]
        if not h1:
            if page.indexable:
                self.fail("h1_missing", page.url)
        elif len(h1) > 1:
            self.fail("h1_multiple", page.url, count=len(h1), values=[truncate(t, 60) for t in h1[:3]])
        if any(not text for text in h1) and _has_empty_h1(page):
            self.fail("heading_empty", page.url)

        previous = 0
        for level, text in page.headings:
            if previous and level > previous + 1:
                self.fail("heading_hierarchy", page.url, prev=f"h{previous}", next=f"h{level}", value=truncate(text, 60))
                break
            previous = level

    def _volume(self, page: Page) -> None:
        words = page.word_count
        if page.indexable and words < self.params("thin_content")["min_words"]:
            self.fail("thin_content", page.url, words=words)

        html_size = len(page.html or "")
        if page.indexable and html_size:
            ratio = round(100 * len(page.text_content) / html_size, 1)
            if ratio < self.params("text_ratio")["min_percent"]:
                self.fail("text_ratio", page.url, ratio=ratio)

        markers = self.params("placeholder_text")["markers"]
        text = page.text_content
        lowered = text.lower()
        found = [m for m in markers if m in lowered]
        if found:
            at = lowered.index(found[0])
            context = text[max(0, at - 50) : at + len(found[0]) + 50].strip()
            self.fail("placeholder_text", page.url, markers=found, context=truncate(context))

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
        # Similarity isn't transitive, but a template family is one problem: pairs are merged into groups.
        parent = {u: u for u, _ in items}

        def root(url: str) -> str:
            while parent[url] != url:
                url = parent[url]
            return url

        for i, (url_a, hash_a) in enumerate(items):
            for url_b, hash_b in items[i + 1 :]:
                if (hash_a ^ hash_b).bit_count() <= max_distance:
                    parent[root(url_b)] = root(url_a)
        groups: dict[str, list[str]] = {}
        for url, _ in items:
            groups.setdefault(root(url), []).append(url)
        for urls in groups.values():
            if len(urls) > 1:
                self._report_group("near_duplicate", urls)

    def _page_extras(self, page: Page) -> None:
        if _looks_js_rendered(page, self.params("js_rendered")):
            self.fail("js_rendered", page.url, words=page.word_count)
        elif "noindex" not in page.robots:
            self._soft_404(page)

    def _soft_404(self, page: Page) -> None:
        params = self.params("soft_404")
        # Reparsing is only worth it for pages that are short even with their navigation counted.
        if page.word_count > params["max_page_words"]:
            return
        words, has_media = _main_content(page.html or "")
        h1 = next((text for level, text in page.headings if level == 1), "")
        signal = next((text for text in (page.title or "", h1) if _NOT_FOUND.search(text)), None)
        if signal and words < params["max_main_words"]:
            self.fail("soft_404", page.url, key="not_found", value=truncate(signal, 80))
        elif words < params["min_main_words"] and not has_media:
            self.fail("soft_404", page.url, key="empty", words=words)

    def _param_duplicates(self, urls: list[str]) -> list[str]:
        """Reports copies of a page made by GET parameters; returns the rest of the duplicate group."""
        group = set(urls)
        site = self.ctx.site
        rules = robots_txt.clean_params(site.robots.text()) if site.robots_parser and site.robots else []
        remaining = []
        for url in urls:
            parts = urlsplit(url)
            base = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
            if not parts.query or base not in group:
                remaining.append(url)
                continue
            params = {k for k, _ in parse_qsl(parts.query, keep_blank_values=True)}
            key = "clean_param" if robots_txt.is_cleaned(rules, parts.path, params) else None
            self.fail("param_duplicates", url, key=key, original=pretty_url(base), params=sorted(params))
        return remaining


def _has_empty_h1(page: Page) -> bool:
    """An H1 holding only an image with alt text is not empty: the alt is its text for search engines."""
    return any(
        not node.text(deep=True).strip() and not any((img.attributes.get("alt") or "").strip() for img in node.css("img"))
        for node in page.tree.css("h1")
    )


def _simhash(words: list[str]) -> int:
    weights = [0] * 64
    for i in range(max(1, len(words) - _SHINGLE + 1)):
        shingle = " ".join(words[i : i + _SHINGLE])
        value = int.from_bytes(hashlib.blake2b(shingle.encode(), digest_size=8).digest(), "big")
        for bit in range(64):
            weights[bit] += 1 if value >> bit & 1 else -1
    return sum(1 << bit for bit, weight in enumerate(weights) if weight > 0)


def _main_content(html: str) -> tuple[int, bool]:
    """Words and media left once site-wide chrome (header, menus, footer) is removed."""
    tree = LexborHTMLParser(html)
    tree.strip_tags(["script", "style", "noscript", "template", "svg"])
    for node in tree.css(_CHROME):
        node.decompose()
    root = tree.css_first("main") or tree.body
    if root is None:
        return 0, False
    return len(_WORD.findall(root.text(separator=" "))), root.css_first(_MEDIA) is not None


def _looks_js_rendered(page: Page, params: dict) -> bool:
    if page.word_count >= params["max_words"] or sum(link.internal for link in page.links) >= params["max_links"]:
        return False
    empty_root = any(len(_WORD.findall(node.text(deep=True))) < 5 for node in page.tree.css(_APP_ROOT))
    scripts = sum(1 for s in page.scripts if s.src)
    return empty_root or (page.word_count < 10 and scripts >= params["min_scripts"])
