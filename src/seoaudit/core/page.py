from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cached_property
from selectolax.lexbor import LexborHTMLParser, LexborNode

from seoaudit.core.http import FetchResult
from seoaudit.core.srcset import candidate_urls, pick_candidate, source_applies
from seoaudit.core.urls import UrlScope, is_service_url

_HTML_TYPES = ("text/html", "application/xhtml+xml")
_WORD = re.compile(r"\w+", re.UNICODE)
_NON_CONTENT_TAGS = ["script", "style", "noscript", "template", "svg", "iframe"]
# Directives with a "name: value" form; any other "token:" prefix names the crawler the rest applies to.
_VALUED_DIRECTIVES = {"max-snippet", "max-image-preview", "max-video-preview", "unavailable_after"}
_LINK_HEADER = re.compile(r"<([^>]*)>([^<]*)")
_CANONICAL_PARAM = re.compile(r'\brel\s*=\s*"?[^";,]*\bcanonical\b', re.I)
_RAW_HEAD = re.compile(r"<head\b[^>]*>(.*?)(?:</head\s*>|<body\b)", re.I | re.S)
_RAW_SKIP = re.compile(r"<!--.*?-->|<(script|style|noscript|template)\b.*?</\1\s*>", re.I | re.S)
_HEAD_TAGS = re.compile(r"<(?:title|meta|link|base)\b", re.I)
_NOSCRIPT_IMG = re.compile(r"<noscript\b(?:(?!</noscript).)*<img\b", re.I | re.S)
_HEAD_META = {"robots", "description", "googlebot", "yandex", "viewport"}
_HEAD_LINKS = {"canonical", "alternate"}


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
    delivered: str | None
    variants: tuple[str, ...] = ()


@dataclass(frozen=True)
class Script:
    src: str | None
    in_head: bool
    is_async: bool
    defer: bool
    type: str


@dataclass(frozen=True)
class HeadBreak:
    breaker: str
    moved: list[str]
    noscript_img: bool


@dataclass(eq=False)
class Page:
    url: str
    response: FetchResult
    scope: UrlScope
    depth: int | None = 0
    source: str = "link"
    referrer: str | None = None
    html: str | None = field(default=None, repr=False)
    robots_agent: str = "Googlebot"

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
        tree = LexborHTMLParser(self.html or "")
        # Browsers and Googlebot run scripts, so <noscript> fallbacks (async CSS, lazy-load images) are not in their DOM.
        tree.strip_tags(["noscript"])
        return tree

    @cached_property
    def base_url(self) -> str:
        node = self.tree.css_first("base[href]")
        href = node.attributes.get("href") if node else None
        # An unusable base (e.g. "#", "ftp:") leaves the document URL as the base, as in browsers.
        return (self.scope.normalize(href, self.url) if href else None) or self.url

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
    def robots_sources(self) -> dict[str, set[str]]:
        """Directives for the audited crawler by where they come from: generic ones plus those addressed to it."""
        agent = self.robots_agent.lower()
        meta = [*self.meta_tags.get("robots", []), *self.meta_tags.get(agent, [])]
        return {
            "meta": {d for v in meta for d in _robots_directives(v, agent)},
            "header": _robots_directives(self.headers.get("x-robots-tag", ""), agent),
        }

    @cached_property
    def robots(self) -> set[str]:
        directives = set().union(*self.robots_sources.values())
        if "none" in directives:
            directives |= {"noindex", "nofollow"}
        return directives

    def link_rel(self, rel: str, head_only: bool = False) -> list[LexborNode]:
        nodes = self.tree.css("head link[rel]" if head_only else "link[rel]")
        return [n for n in nodes if rel in (n.attributes.get("rel") or "").lower().split()]

    @cached_property
    def canonicals(self) -> list[str]:
        """Google reads rel=canonical only in <head> or in the HTTP Link header."""
        html = [n.attributes.get("href") for n in self.link_rel("canonical", head_only=True)]
        header = [
            m[1] for m in _LINK_HEADER.finditer(self.headers.get("link", "")) if _CANONICAL_PARAM.search(m[2])
        ]
        # The Link header is resolved against the request URL, not <base href>.
        urls = [self.absolute(h) for h in html] + [self.scope.normalize(h, self.url) for h in header]
        return [u for u in urls if u]

    @cached_property
    def canonical_tags(self) -> list[tuple[str, bool]]:
        """Raw href and whether it sits in <head> for every <link rel=canonical>, ignored ones included."""
        return [((n.attributes.get("href") or "").strip(), _inside(n, "head")) for n in self.link_rel("canonical")]

    @cached_property
    def hrefs(self) -> list[str]:
        """Raw href of every link, including those `links` drops as unusable (javascript:, malformed)."""
        return [(n.attributes.get("href") or "") for n in self.tree.css("a[href], area[href]")]

    @cached_property
    def head_break(self) -> HeadBreak | None:
        """Search engines stop reading <head> at the first element that doesn't belong there (scripting off,
        so a <noscript> with an <img> counts); head tags written after it end up in <body> and are ignored."""
        match = _RAW_HEAD.search(self.html or "")
        if not match:
            return None
        written = len(_HEAD_TAGS.findall(_RAW_SKIP.sub("", match[1])))
        if len(self.tree.css("head title, head meta, head link, head base")) >= written:
            return None
        moved = [d for n in self.tree.css("body title, body meta, body link, body base") if (d := _describe_head_tag(n))]
        if not moved or self.tree.body is None:
            return None
        first = next((n for n in self.tree.body.iter(include_text=True) if n.tag != "-text" or n.text().strip()), None)
        breaker = "#text" if first is None or first.tag == "-text" else first.tag
        return HeadBreak(breaker, moved, bool(_NOSCRIPT_IMG.search(match[1])))

    @cached_property
    def hreflangs(self) -> list[tuple[str, str]]:
        result = []
        for node in self.link_rel("alternate", head_only=True):
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
            if not url or is_service_url(url):
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
            src = self.absolute(raw)
            result.append(
                Image(
                    src=src,
                    raw_src=raw,
                    alt=attrs.get("alt") if "alt" in attrs else None,
                    width=attrs.get("width"),
                    height=attrs.get("height"),
                    loading=(attrs.get("loading") or "").lower() or None,
                    delivered=self.absolute(_delivered(node)) or src,
                    variants=tuple(dict.fromkeys(u for raw in [raw, *_offered(node)] if (u := self.absolute(raw)))),
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


def _robots_directives(value: str, agent: str) -> set[str]:
    """Parses "noindex, googlebot: nofollow, otherbot: noindex": a crawler prefix applies to what follows it."""
    result = set()
    applies = True
    for item in value.lower().split(","):
        name, sep, rest = item.partition(":")
        if sep and name.strip() not in _VALUED_DIRECTIVES:
            applies = name.strip() == agent
            item = rest
        if applies and item.strip():
            result.add(item.strip())
    return result


def _delivered(img: LexborNode) -> str | None:
    """Browsers take the first fitting <picture> source, then the srcset candidate for the slot, not src."""
    parent = img.parent
    if parent is not None and parent.tag == "picture":
        for source in parent.css("source"):
            attrs = source.attributes
            if attrs.get("srcset") and source_applies(attrs.get("type"), attrs.get("media")):
                return pick_candidate(attrs.get("srcset"), attrs.get("sizes"))
    attrs = img.attributes
    return pick_candidate(attrs.get("srcset") or attrs.get("data-srcset"), attrs.get("sizes"))


def _offered(img: LexborNode) -> list[str]:
    """Every file the markup offers: which one loads depends on the screen, so a broken one hits someone."""
    parent = img.parent
    nodes = [*parent.css("source"), img] if parent is not None and parent.tag == "picture" else [img]
    return [u for n in nodes for u in candidate_urls(n.attributes.get("srcset") or n.attributes.get("data-srcset"))]


def _describe_head_tag(node: LexborNode) -> str | None:
    """Only tags that matter to search engines; <meta itemprop> and <link rel=stylesheet> are fine in <body>."""
    attrs = node.attributes
    if node.tag in ("title", "base"):
        return None if _inside(node, "svg") else f"<{node.tag}>"
    if node.tag == "meta" and (name := (attrs.get("name") or "").lower()) in _HEAD_META:
        return f'<meta name="{name}">'
    rel = set((attrs.get("rel") or "").lower().split()) & _HEAD_LINKS if node.tag == "link" else set()
    if "canonical" in rel or ("alternate" in rel and attrs.get("hreflang")):
        return '<link rel="canonical">' if "canonical" in rel else f'<link hreflang="{attrs.get("hreflang")}">'
    return None


def _text(node: LexborNode) -> str:
    return " ".join(node.text(deep=True, separator=" ").split())


def _inside(node: LexborNode, tag: str) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.tag == tag:
            return True
        parent = parent.parent
    return False
