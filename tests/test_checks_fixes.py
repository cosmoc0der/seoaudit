from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from conftest import OFFLINE, html
from seoaudit.checks.html import _is_plugin
from seoaudit.checks.meta import _max_scale
from seoaudit.checks.pagespeed import PageSpeedCheck
from seoaudit.checks.pagespeed.runners import Measurement
from seoaudit.checks.security import SecurityCheck, _cookie_issues, _tls_error
from seoaudit.core.check import FindingSink
from seoaudit.core.config import AuditConfig
from seoaudit.core.engine import Auditor
from seoaudit.core.models import AuditReport, RuleStatus
from seoaudit.core.rules import build_rules, load_definition

SITE = "http://fix.test"
HTML = {"content-type": "text/html; charset=utf-8"}
TEXT = "Достаточно длинный текст страницы для проверок. " * 60


def page(body: str, head: str = "", title: str = "Страница тестового сайта с нормальным заголовком") -> tuple:
    description = '<meta name="description" content="Описание страницы тестового сайта для проверки правил аудита.">'
    return 200, HTML, html(title, f"<h1>Заголовок</h1><p>{TEXT}</p>{body}", description + head).encode()


def audit(pages: dict[str, tuple], **config) -> AuditReport:
    def handler(request: httpx.Request) -> httpx.Response:
        status, headers, body = pages.get(request.url.raw_path.decode(), (404, HTML, b"<h1>404</h1>"))
        return httpx.Response(status, headers=headers, content=b"" if request.method == "HEAD" else body)

    cfg = AuditConfig.model_validate({**OFFLINE, **config})
    return Auditor(cfg, transport=httpx.MockTransport(handler)).run_sync(SITE)


def findings(report: AuditReport, rule_id: str) -> dict[str, str | None]:
    rule = next(r for r in report.rules() if r.id == rule_id)
    return {f.url: f.key for f in rule.findings}


def test_canonical_to_trailing_slash_redirect_is_broken():
    report = audit(
        {
            "/": page('<a href="/products/">Products</a>'),
            "/products/": page("", f'<link rel="canonical" href="{SITE}/products">'),
            "/products": (301, {"location": "/products/"}, b""),
        }
    )
    assert findings(report, "indexability.canonical_broken") == {f"{SITE}/products/": "redirect"}


def test_sitemap_url_closed_in_robots_is_a_sitemap_problem():
    sitemap = f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>{SITE}/checkout</loc></url></urlset>'
    report = audit(
        {
            "/robots.txt": (200, {"content-type": "text/plain"}, f"User-agent: *\nDisallow: /checkout\nSitemap: {SITE}/sitemap.xml".encode()),
            "/sitemap.xml": (200, {"content-type": "application/xml"}, sitemap.encode()),
            "/": page(""),
        }
    )
    assert findings(report, "indexability.sitemap_bad_urls") == {f"{SITE}/checkout": "blocked"}
    assert findings(report, "indexability.blocked_by_robots") == {}


def test_sitemap_index_counts_as_sitemap():
    index = f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>{SITE}/s.xml</loc></sitemap></sitemapindex>'
    report = audit({"/": page(""), "/sitemap_index.xml": (200, {"content-type": "text/xml"}, index.encode())})
    assert findings(report, "indexability.sitemap_missing") == {}


def test_seo_rules_skip_non_indexable_pages():
    noindex = '<meta name="robots" content="noindex">'
    report = audit(
        {
            "/": page('<a href="/hidden">Hidden</a> <a href="/short">Short</a>'),
            "/hidden": (200, HTML, html("Скрытая", "<p>Мало</p>", noindex).encode()),
            "/short": (200, HTML, html("Видимая", "<p>Мало</p>", "").encode()),
        }
    )
    for rule_id in ("meta.description_missing", "content.thin_content", "content.h1_missing", "meta.title_length"):
        assert set(findings(report, rule_id)) == {f"{SITE}/short"}, rule_id


def test_lazy_loading_respects_explicit_intent():
    images = "".join(f'<img src="/a{i}.jpg" alt="" width="1" height="1">' for i in range(3))
    images += '<img src="/eager.jpg" loading="eager" alt="">'
    images += '<img src="/priority.jpg" fetchpriority="high" alt="">'
    images += '<img data-src="/js-lazy.jpg" class="lazyload" alt="">'
    images += '<img src="/plain.jpg" alt="">'
    report = audit({"/": page(images)})
    rule = next(r for r in report.rules() if r.id == "images.lazy_loading")
    assert rule.findings[0].data["examples"] == [f"{SITE}/plain.jpg"]


def test_heading_with_image_alt_is_not_empty():
    report = audit({"/": (200, HTML, html("Заголовок", f'<h1><img src="/logo.png" alt="Название"></h1><p>{TEXT}</p>').encode())})
    assert findings(report, "content.heading_empty") == {}


def test_pagination_query_is_not_a_duplicate_source():
    report = audit(
        {
            "/": page('<a href="/list?page=2">2</a> <a href="/list?sort=price">sort</a>'),
            "/list?page=2": page(""),
            "/list?sort=price": page(""),
        }
    )
    assert set(findings(report, "urls.query_params")) == {f"{SITE}/list?sort=price"}


def test_meta_refresh_without_url_is_not_a_redirect():
    report = audit(
        {
            "/": page('<a href="/feed">Feed</a>', '<meta http-equiv="refresh" content="300">'),
            "/feed": page("", '<meta http-equiv="refresh" content="0; url=/">'),
        }
    )
    assert set(findings(report, "meta.meta_refresh")) == {f"{SITE}/feed"}


def test_phpinfo_is_detected_although_it_is_html():
    report = audit({"/": page(""), "/phpinfo.php": (200, HTML, b"<html><h1>PHP Version 8.3</h1></html>")})
    assert set(findings(report, "security.sensitive_files")) == {f"{SITE}/phpinfo.php"}


@pytest.mark.parametrize(("viewport", "expected"), [("maximum-scale=10", 10), ("maximum-scale=1.0", 1), ("width=device-width", float("inf"))])
def test_max_scale(viewport, expected):
    assert _max_scale(viewport) == expected


def test_csrf_cookie_needs_no_httponly():
    assert _cookie_issues("XSRF-TOKEN=1; Secure; SameSite=Lax", True) == ("XSRF-TOKEN", [])
    assert _cookie_issues("sessionid=1; Secure; SameSite=Lax", True) == ("sessionid", ["HttpOnly"])


def test_tls_error_is_classified():
    assert _tls_error("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: certificate has expired") == "expired"
    assert _tls_error("something odd") == "untrusted"


def test_plugin_markers_are_exact():
    assert not _is_plugin({"data": "/img/flashlight.svg", "type": "image/svg+xml"})
    assert _is_plugin({"data": "/intro.swf"})


@pytest.mark.parametrize(
    ("hops", "key"),
    [
        ([("http://h.test/", 301, "http://www.h.test/"), ("http://www.h.test/", 301, "https://h.test/")], "chain"),
        ([("http://h.test/", 302, "https://h.test/")], "temporary"),
        ([("http://h.test/", 301, "https://h.test/")], None),
    ],
)
async def test_http_redirect(hops, key):
    redirects = {url: (status, location) for url, status, location in hops}

    def handler(request):
        status, location = redirects.get(str(request.url), (200, None))
        return httpx.Response(status, headers={"location": location} if location else {})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ctx = SimpleNamespace(client=client, site=SimpleNamespace(base_url="https://h.test/"))
        sink = FindingSink()
        rules = build_rules("security", load_definition(SecurityCheck.rules_file()), AuditConfig())
        await SecurityCheck(ctx, rules, {}, sink)._http_redirect()
    assert [f.key for f in sink.findings.get("security.http_redirect", [])] == ([key] if key else [])


def test_field_data_origin_fallback_is_reported_once(monkeypatch, transport):
    lhr = {"categories": {"performance": {"score": 0.9}}, "audits": {}}
    field = {"origin_fallback": True, "metrics": {"LARGEST_CONTENTFUL_PAINT_MS": {"category": "SLOW"}}}

    class Runner:
        name, concurrency, has_field_data = "fake", 1, True

        async def run(self, url, strategy):
            return Measurement(lhr, field)

    async def runner(self):
        return Runner()

    monkeypatch.setattr(PageSpeedCheck, "_runner", runner)
    cfg = AuditConfig.model_validate({"checks": {"rendering": {"enabled": False}, "html": {"options": {"validator_pages": 0}}}})
    report = Auditor(cfg, transport=transport).run_sync("http://shop.test")
    rule = next(r for r in report.rules() if r.id == "pagespeed.field_cwv")
    assert rule.status == RuleStatus.FAILED
    assert [f.message for f in rule.findings] == [
        "Смартфон: LCP — плохо (данные по сайту целиком: у отдельных страниц мало посещений)"
    ]
