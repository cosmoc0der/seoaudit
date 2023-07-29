from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cached_property

from selectolax.lexbor import LexborHTMLParser, LexborNode

from seoaudit.core.http import FetchResult
from seoaudit.core.urls import UrlScope

_HTML_TYPES = ("text/html", "application/xhtml+xml")
_WORD = re.compile(r"\w+", re.UNICODE)
_NON_CONTENT_TAGS = ["script", "style", "noscript", "template", "svg", "iframe"]


@dataclass(frozen=True)
class Link:
    url: str
    text: str
    rel: frozenset[str]
    internal: bool
    tag: str = "a"
    target: str | None = None

    @property
    def nofollow(self) -> bool:
        return bool({"nofollow", "ugc", "sponsored"} & self.rel)


@dataclass(frozen=True)
class Image:
    src: str | None
    raw_src: str
    alt: str | None
    width: str | None
    height: str | None
    loading: str | None
    srcset: str | None


@dataclass(frozen=True)
class Script:
    src: str | None
    in_head: bool
    is_async: bool
    defer: bool
    type: str


@dataclass(eq=False)
class Page:
    url: str
    response: FetchResult
    scope: UrlScope
    depth: int | None = 0
    source: str = "link"
    referrer: str | None = None
    html: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.html is None and self.is_html and self.response.body:
            self.html = self.response.text()
        self.response.body = b""

    @property
    def status(self) -> int | None:
        return self.response.status

    @property
    def headers(self) -> dict[str, str]:
        return self.response.headers

    @property
    def is_html(self) -> bool:
        return self.response.content_type in _HTML_TYPES

    @property
    def is_redirect(self) -> bool:
        return bool(self.response.redirects)

    @property
    def ok(self) -> bool:
        return self.status == 200 and self.is_html and not self.is_redirect

    @property
    def indexable(self) -> bool:
        if not self.ok or "noindex" in self.robots:
            return False
        canonicals = self.canonicals
        return not canonicals or canonicals[0] == self.url

    def release(self) -> None:
        """Drops heavy parsed data; extracted links/meta stay cached."""
        self.html = None
        for name in ("tree", "text_content"):
            self.__dict__.pop(name, None)

    @cached_property
    def tree(self) -> LexborHTMLParser:
        return LexborHTMLParser(self.html or "")

    @cached_property
    def base_url(self) -> str:
        node = self.tree.css_first("base[href]")
        href = node.attributes.get("href") if node else None
        return self.scope.normalize(href, self.url) if href else self.url

    def absolute(self, raw: str | None) -> str | None:
        return self.scope.normalize(raw, self.base_url) if raw else None

    @cached_property
    def lang(self) -> str | None:
        node = self.tree.css_first("html")
        return (node.attributes.get("lang") or "").strip() or None if node else None

    @cached_property
    def titles(self) -> list[str]:
        return [_text(n) for n in self.tree.css("title") if not _inside(n, "svg")]

    @property
    def title(self) -> str | None:
        return self.titles[0] if self.titles else None

    @cached_property
    def meta_tags(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for node in self.tree.css("meta"):
            attrs = node.attributes
            key = attrs.get("name") or attrs.get("property") or attrs.get("http-equiv")
            if key:
                result.setdefault(key.strip().lower(), []).append((attrs.get("content") or "").strip())
            if "charset" in attrs:
                result.setdefault("charset", []).append(attrs.get("charset") or "")
        return result

    def meta(self, name: str) -> str | None:
        values = self.meta_tags.get(name.lower())
        return values[0] if values else None

    @cached_property
    def robots(self) -> set[str]:
        values = [v for key in ("robots", "googlebot", "yandex") for v in self.meta_tags.get(key, [])]
        values.append(self.headers.get("x-robots-tag", ""))
        directives = {d.strip().lower() for v in values for d in v.split(",")}
        if "none" in directives:
            directives |= {"noindex", "nofollow"}
        return directives - {""}

    def link_rel(self, rel: str) -> list[LexborNode]:
        return [n for n in self.tree.css("link[rel]") if rel in (n.attributes.get("rel") or "").lower().split()]

    @cached_property
    def canonicals(self) -> list[str]:
        return [u for n in self.link_rel("canonical") if (u := self.absolute(n.attributes.get("href")))]

    @cached_property
    def hreflangs(self) -> list[tuple[str, str]]:
        result = []
        for node in self.link_rel("alternate"):
            lang = node.attributes.get("hreflang")
            url = self.absolute(node.attributes.get("href"))
            if lang and url:
                result.append((lang.strip(), url))
        return result

    @cached_property
    def headings(self) -> list[tuple[int, str]]:
        return [(int(n.tag[1]), _text(n)) for n in self.tree.css("h1, h2, h3, h4, h5, h6")]

    @cached_property
    def links(self) -> list[Link]:
        result = []
        for node in self.tree.css("a[href], area[href]"):
            url = self.absolute(node.attributes.get("href"))
            if not url:
                continue
            attrs = node.attributes
            text = _text(node) or (attrs.get("aria-label") or attrs.get("title") or "").strip()
            if not text:
                text = " ".join((img.attributes.get("alt") or "").strip() for img in node.css("img")).strip()
            result.append(
                Link(
                    url=url,
                    text=text,
                    rel=frozenset((attrs.get("rel") or "").lower().split()),
                    internal=self.scope.is_internal(url),
                    tag=node.tag,
                    target=attrs.get("target"),
                )
            )
        return result

    @cached_property
    def images(self) -> list[Image]:
        result = []
        for node in self.tree.css("img"):
            attrs = node.attributes
            raw = (attrs.get("src") or attrs.get("data-src") or "").strip()
            result.append(
                Image(
                    src=self.absolute(raw),
                    raw_src=raw,
                    alt=attrs.get("alt") if "alt" in attrs else None,
                    width=attrs.get("width"),
                    height=attrs.get("height"),
                    loading=(attrs.get("loading") or "").lower() or None,
                    srcset=attrs.get("srcset"),
                )
            )
        return result

    @cached_property
    def scripts(self) -> list[Script]:
        result = []
        for node in self.tree.css("script"):
            attrs = node.attributes
            result.append(
                Script(
                    src=self.absolute(attrs.get("src")),
                    in_head=_inside(node, "head"),
                    is_async="async" in attrs,
                    defer="defer" in attrs,
                    type=(attrs.get("type") or "").lower(),
                )
            )
        return result

    @cached_property
    def stylesheets(self) -> list[str]:
        return [u for n in self.link_rel("stylesheet") if (u := self.absolute(n.attributes.get("href")))]

    @cached_property
    def jsonld_blocks(self) -> list[str]:
        return [
            n.text(deep=True)
            for n in self.tree.css("script")
            if (n.attributes.get("type") or "").lower().strip() == "application/ld+json"
        ]

    @cached_property
    def text_content(self) -> str:
        tree = LexborHTMLParser(self.html or "")
        tree.strip_tags(_NON_CONTENT_TAGS)
        body = tree.body
        return " ".join(body.text(separator=" ").split()) if body else ""

    @cached_property
    def word_count(self) -> int:
        return len(_WORD.findall(self.text_content))


def _text(node: LexborNode) -> str:
    return " ".join(node.text(deep=True, separator=" ").split())


def _inside(node: LexborNode, tag: str) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.tag == tag:
            return True
        parent = parent.parent
    return False
