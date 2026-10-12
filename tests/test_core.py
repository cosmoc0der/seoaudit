from __future__ import annotations

import asyncio
import gzip
from pathlib import Path
from typing import ClassVar

import httpx
import pytest

from seoaudit.checks.indexability import IndexabilityCheck
from seoaudit.core.check import Check
from seoaudit.core.config import AuditConfig, CrawlConfig
from seoaudit.core.context import AuditContext
from seoaudit.core.crawler import Crawler
from seoaudit.core.engine import Auditor
from seoaudit.core.http import FetchResult, _Http1Fallback, create_client, decode_body, fetch
from seoaudit.core.models import Finding, RuleResult, RuleScope, RuleStatus, Severity
from seoaudit.core.page import Page
from seoaudit.core.probe import ResourceProber
from seoaudit.core.scoring import rule_penalty
from seoaudit.core.site import _load_sitemaps, discover_site
from seoaudit.core.urls import UrlScope, normalize_url

HTML = {"content-type": "text/html; charset=utf-8"}
TEXT = {"content-type": "text/plain"}
XML = {"content-type": "application/xml"}
Pages = dict[tuple[str, str], tuple[int, dict[str, str], bytes]]


def page(body: str = "", head: str = "") -> tuple[int, dict[str, str], bytes]:
    return 200, HTML, f"<html><head>{head}</head><body>{body}</body></html>".encode()


def site(pages: dict, host: str = "s.test") -> Pages:
    return {(host, path): response for path, response in pages.items()}


def transport(pages: Pages, delays: dict[str, float] | None = None, log: list[str] | None = None):
    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode()
        if log is not None:
            log.append(path)
        await asyncio.sleep((delays or {}).get(path, 0))
        status, headers, body = pages.get((request.url.host, path), (404, HTML, b"not found"))
        return httpx.Response(status, headers=headers, content=b"" if request.method == "HEAD" else body)

    return httpx.MockTransport(handler)


async def crawl(pages: Pages, delays=None, log=None, **crawl_cfg) -> AuditContext:
    cfg = AuditConfig.model_validate({"crawl": {"use_sitemap": False, **crawl_cfg}})
    client = create_client(cfg.crawl, transport=transport(pages, delays, log))
    auditor = Auditor(cfg, checks={})
    try:
        info = await discover_site(client, "http://s.test/", cfg.crawl, auditor._scope("http://s.test/"))
        ctx = AuditContext(cfg, client, info, auditor._scope(info.base_url), ResourceProber(client))
        await Crawler(ctx, lambda p: asyncio.sleep(0)).run()
    finally:
        await client.aclose()
    return ctx


def make_page(html: str = "", headers: dict | None = None, url: str = "https://ex.com/dir/p", **kwargs) -> Page:
    response = FetchResult(url=url, final_url=url, status=200, headers={**HTML, **(headers or {})}, body=html.encode())
    return Page(url=url, response=response, scope=UrlScope("ex.com"), **kwargs)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://ex.com/%EF%F0%E8?q=%EF%F0%E8", "https://ex.com/%EF%F0%E8?q=%EF%F0%E8"),
        ("https://ex.com/a%2Fb", "https://ex.com/a%2Fb"),
        ("https://ex.com/p?amp", "https://ex.com/p?amp"),
        ("https://ex.com/p?b=1;c=2&next=/x", "https://ex.com/p?b=1;c=2&next=/x"),
        ("https://ex.com/%7euser/%d0%bf", "https://ex.com/~user/%D0%BF"),
        ("https://ex.com/100%", "https://ex.com/100%25"),
        ("https://ex.com/a b?q=a b", "https://ex.com/a%20b?q=a%20b"),
        ("https://ex.com/p?utm_source=x&id=1&UTM_medium=y", "https://ex.com/p?id=1"),
        ("https://faß.de/", "https://xn--fa-hia.de/"),
        ("https://[::1]:8080/x", "https://[::1]:8080/x"),
    ],
)
def test_normalize_keeps_escapes_and_query(raw, expected):
    assert normalize_url(raw, strip_params=("utm_*",)) == expected


def test_from_is_not_stripped_by_default():
    assert "from" not in CrawlConfig().strip_params


async def test_depth_is_shortest_path_under_concurrency():
    pages = site({
        "/": page('<a href="/a">a</a><a href="/b">b</a>'),
        "/a": page('<a href="/x">x</a>'),
        "/b": page('<a href="/c">c</a>'),
        "/c": page('<a href="/x">x</a>'),
        "/x": page(),
    })
    ctx = await crawl(pages, delays={"/a": 0.2})
    assert ctx.pages["http://s.test/x"].depth == 2


async def test_max_depth_still_limits_crawl():
    pages = site({"/": page('<a href="/a">a</a>'), "/a": page('<a href="/b">b</a>'), "/b": page()})
    ctx = await crawl(pages, max_depth=1)
    assert sorted(ctx.pages) == ["http://s.test/", "http://s.test/a"]


async def test_self_links_are_not_inlinks():
    pages = site({"/": page('<a href="/">home</a><a href="/a">a</a>'), "/a": page('<a href="/a">self</a>')})
    ctx = await crawl(pages)
    assert "http://s.test/a" not in ctx.inlinks.get("http://s.test/a", [])
    assert [src for src, _ in ctx.inlinks["http://s.test/a"]] == ["http://s.test/"]
    assert "http://s.test/" not in ctx.inlinks


async def test_redirect_target_respects_robots_and_page_limit():
    pages = site({
        "/robots.txt": (200, TEXT, b"User-agent: *\nDisallow: /private\n"),
        "/": page('<a href="/old">old</a>' + "".join(f'<a href="/r{i}">r</a>' for i in range(5))),
        "/old": (301, {"location": "/private/x"}, b""),
        "/private/x": page(),
        **{f"/r{i}": (301, {"location": f"/t{i}"}, b"") for i in range(5)},
        **{f"/t{i}": page() for i in range(5)},
    })
    ctx = await crawl(pages)
    assert "http://s.test/private/x" not in ctx.pages
    assert "http://s.test/private/x" in ctx.blocked

    ctx = await crawl(pages, max_pages=4)
    assert len(ctx.pages) <= 4


async def test_robots_with_bom_is_applied():
    pages = site({
        "/robots.txt": (200, TEXT, b"\xef\xbb\xbfUser-agent: *\nDisallow: /private\n"),
        "/": page('<a href="/private/x">p</a>'),
    })
    ctx = await crawl(pages)
    assert ctx.blocked == {"http://s.test/private/x"}


async def test_robots_served_as_html_type_is_parsed_but_html_body_is_not():
    rules = (200, HTML, b"User-agent: *\nDisallow: /private\n")
    ctx = await crawl(site({"/robots.txt": rules, "/": page('<a href="/private/x">p</a>')}))
    assert ctx.site.robots_parser and ctx.blocked == {"http://s.test/private/x"}

    soft_404 = (200, HTML, b"<!DOCTYPE html><html><body>Not found</body></html>")
    ctx = await crawl(site({"/robots.txt": soft_404, "/": page('<a href="/private/x">p</a>')}))
    assert ctx.site.robots_parser is None and "http://s.test/private/x" in ctx.pages


async def test_robots_5xx_crawls_only_start_page_and_reports_error():
    pages = site({"/robots.txt": (503, TEXT, b""), "/": page('<a href="/a">a</a>'), "/a": page()})
    ctx = await crawl(pages)
    assert list(ctx.pages) == ["http://s.test/"]
    assert ctx.blocked == {"http://s.test/a"}

    cfg = AuditConfig.model_validate({"lang": "en", "crawl": {"use_sitemap": False}})
    auditor = Auditor(cfg, checks={"indexability": IndexabilityCheck}, transport=transport(pages))
    report = await auditor.run("http://s.test/")
    assert next(r for r in report.rules() if r.id == "indexability.robots_error").status == RuleStatus.FAILED


async def test_robots_is_per_origin_with_subdomains():
    pages = {
        **site({"/robots.txt": (200, TEXT, b"User-agent: *\nDisallow: /docs\n"),
                "/": page('<a href="http://blog.s.test/docs">d</a><a href="http://blog.s.test/admin">a</a>')}),
        **site({"/robots.txt": (200, TEXT, b"User-agent: *\nDisallow: /admin\n"), "/docs": page(), "/admin": page()},
               host="blog.s.test"),
    }
    ctx = await crawl(pages, include_subdomains=True)
    assert "http://blog.s.test/docs" in ctx.pages
    assert ctx.blocked == {"http://blog.s.test/admin"}


async def test_cp1251_link_is_requested_unchanged():
    log: list[str] = []
    ctx = await crawl(site({"/": page('<a href="/cat/%EF%F0%E8">c</a>'), "/cat/%EF%F0%E8": page()}), log=log)
    assert "/cat/%EF%F0%E8" in log
    assert ctx.pages["http://s.test/cat/%EF%F0%E8"].status == 200


@pytest.mark.parametrize(
    ("header", "noindex"),
    [
        ("googlebot: noindex", True),
        ("googlebot: none", True),
        ("otherbot: noindex", False),
        ("otherbot: noindex, nofollow", False),
        ("noindex, otherbot: nofollow", True),
        ("max-snippet: -1, noindex", True),
        ("unavailable_after: 25 Jun 2030 15:00:00 PST", False),
    ],
)
def test_x_robots_tag_respects_crawler_prefix(header, noindex):
    assert ("noindex" in make_page(headers={"x-robots-tag": header}).robots) is noindex


def test_otherbot_nofollow_does_not_leak():
    assert "nofollow" not in make_page(headers={"x-robots-tag": "otherbot: noindex, nofollow"}).robots


def test_meta_robots_only_for_audited_crawler():
    assert "noindex" not in make_page('<meta name="yandex" content="noindex">').robots
    assert "noindex" in make_page('<meta name="googlebot" content="noindex">').robots
    assert "noindex" in make_page('<meta name="yandex" content="noindex">', robots_agent="Yandex").robots
    robots = make_page('<meta name="robots" content="max-snippet:-1, max-image-preview:large">').robots
    assert robots == {"max-snippet:-1", "max-image-preview:large"}


@pytest.mark.parametrize("base", ['<base href="#">', '<base href=" ">', '<base href="ftp://x/">'])
def test_unusable_base_href_falls_back_to_document_url(base):
    p = make_page(f'<html><head>{base}<link rel="canonical" href="p"></head><body><a href="rel">x</a></body></html>')
    assert [link.url for link in p.links] == ["https://ex.com/dir/rel"]
    assert p.canonicals == ["https://ex.com/dir/p"]


def test_canonical_only_from_head_or_link_header():
    assert make_page('<html><head></head><body><link rel="canonical" href="/other"></body></html>').canonicals == []

    header = {"link": '<https://cdn.ex.com/x.css>; rel=preload, </dir/q>; rel="canonical"'}
    p = make_page('<html><head><base href="/base/"></head><body></body></html>', header)
    assert p.canonicals == ["https://ex.com/dir/q"]
    assert not p.indexable


def test_hreflang_only_from_head():
    p = make_page('<html><head></head><body><link rel="alternate" hreflang="en" href="/en"></body></html>')
    assert p.hreflangs == []


def test_decode_body_bom_and_late_meta_charset():
    assert decode_body("﻿Привет".encode(), "text/html; charset=windows-1251") == "Привет"
    late = (" " * 5000 + '<meta charset="windows-1251"><title>Привет</title>').encode("cp1251")
    assert decode_body(late, "text/html").endswith("<title>Привет</title>")


async def test_redirect_without_location_is_a_final_response():
    handler = lambda request: httpx.Response(301)  # noqa: E731
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await fetch(client, "http://s.test/a")
    assert (result.status, result.error, result.redirects) == (301, None, [])


async def test_final_url_points_at_failed_hop():
    def handler(request):
        if request.url.host == "dead.test":
            raise httpx.ConnectError("dns")
        if request.url.path == "/loop":
            return httpx.Response(301, headers={"location": "/loop2"})
        if request.url.path == "/loop2":
            return httpx.Response(301, headers={"location": "/loop"})
        return httpx.Response(301, headers={"location": "http://dead.test/z"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        dead = await fetch(client, "http://s.test/a", attempts=1)
        loop = await fetch(client, "http://s.test/loop", attempts=1)
    assert dead.final_url == "http://dead.test/z" and dead.error
    assert loop.final_url in ("http://s.test/loop", "http://s.test/loop2") and "Too many redirects" in loop.error


async def test_sitemap_formats():
    ns = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'
    pages = site({
        "/index.xml": (200, XML, f"<sitemapindex {ns}><sitemap><loc>http://s.test/a.xml.gz</loc></sitemap>"
                                 f"</sitemapindex>".encode()),
        "/a.xml.gz": (200, {"content-type": "application/x-gzip"},
                      gzip.compress(f"<urlset {ns}><url><loc>http://s.test/1</loc></url></urlset>".encode())),
        "/s.txt": (200, TEXT, b"\xef\xbb\xbfhttp://s.test/2\r\n\r\nhttp://s.test/3\n"),
        "/rss.xml": (200, XML, b'<rss version="2.0"><channel><item><link>http://s.test/4</link></item></channel></rss>'),
        "/atom.xml": (200, XML, b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><link href="http://s.test/5"/>'
                                b'<link rel="enclosure" href="http://s.test/5.mp3"/></entry></feed>'),
    })
    urls = [f"http://s.test/{name}" for name in ("index.xml", "s.txt", "rss.xml", "atom.xml")]
    async with httpx.AsyncClient(transport=transport(pages)) as client:
        sitemaps, _ = await _load_sitemaps(client, urls, set(), 100, UrlScope("s.test"))
    assert all(s.error is None for s in sitemaps)
    assert sorted(u for s in sitemaps for u in s.urls) == [f"http://s.test/{i}" for i in range(1, 6)]


async def test_sitemap_cap_marks_truncation_and_keeps_raw_count():
    pages = site({"/s.txt": (200, TEXT, b"http://s.test/1\nhttp://s.test/2\n"), "/t.txt": (200, TEXT, b"http://s.test/3\n")})
    urls = ["http://s.test/s.txt", "http://s.test/t.txt"]
    async with httpx.AsyncClient(transport=transport(pages)) as client:
        sitemaps, truncated = await _load_sitemaps(client, urls, set(), 1, UrlScope("s.test"))
        _, complete = await _load_sitemaps(client, urls, set(), 10, UrlScope("s.test"))
    assert truncated and not complete
    assert sitemaps[0].loc_count == 2 and sitemaps[0].urls == ["http://s.test/1"]


def _rule(findings: list[Finding]) -> RuleResult:
    return RuleResult(
        id="links.broken_internal", check="links", category="links", severity=Severity.HIGH,
        scope=RuleScope.PAGE, status=RuleStatus.FAILED, title="t", findings=findings,
    )


def test_scoring_counts_source_pages_for_target_findings():
    sources = [f"https://s/p{i}" for i in range(300)]
    one_target_everywhere = _rule([Finding(url="https://s/dead", data={"pages": 300, "sources": sources[:50]})])
    assert one_target_everywhere.affected == 1
    assert one_target_everywhere.pages_affected == 300
    assert rule_penalty(one_target_everywhere, 300) == 6

    many_targets_one_page = _rule(
        [Finding(url=f"https://s/dead{i}", data={"pages": 1, "sources": [sources[0]]}) for i in range(30)]
    )
    assert many_targets_one_page.pages_affected == 1
    assert rule_penalty(many_targets_one_page, 300) == pytest.approx(6 * (0.4 + 0.6 / 300))

    per_page = _rule([Finding(url=u) for u in sources[:3]])
    assert per_page.pages_affected == per_page.affected == 3


class CrashingCheck(Check):
    id = "crashing"
    rules_path: ClassVar[Path]

    @classmethod
    def rules_file(cls) -> Path:
        return cls.rules_path

    async def check_page(self, page: Page) -> None:
        if page.url.endswith("/a"):
            raise RuntimeError("boom")


def test_check_page_crash_marks_rules_as_error(tmp_path):
    CrashingCheck.rules_path = tmp_path / "rules.yaml"
    CrashingCheck.rules_path.write_text("category: content\nrules:\n  thing:\n    severity: high\n", encoding="utf-8")
    pages = site({"/": page('<a href="/a">a</a>'), "/a": page()})
    cfg = AuditConfig.model_validate({"crawl": {"use_sitemap": False}})
    report = Auditor(cfg, checks={"crashing": CrashingCheck}, transport=transport(pages)).run_sync("http://s.test/")
    rule = report.rules()[0]
    assert rule.status == RuleStatus.ERROR
    assert "boom" in rule.note


async def test_h2_stream_limit_falls_back_to_http1():
    def overloaded(request):
        raise httpx.LocalProtocolError("Max outbound streams is 100, 100 open")

    fallback = _Http1Fallback(verify=True, limits=httpx.Limits())
    fallback._h2 = httpx.MockTransport(overloaded)
    fallback._h1 = httpx.MockTransport(lambda request: httpx.Response(200, content=b"ok"))
    async with httpx.AsyncClient(transport=fallback) as client:
        result = await fetch(client, "https://s.test/app.js")
    assert result.status == 200 and not result.broken
