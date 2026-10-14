from __future__ import annotations

import httpx

from conftest import OFFLINE, html
from seoaudit.core import robots_txt
from seoaudit.core.config import AuditConfig
from seoaudit.core.engine import Auditor
from seoaudit.core.models import AuditReport, RuleStatus

SITE = "https://site.test"
TEXT = " ".join(f"слово{i}" for i in range(80))
HTML = {"content-type": "text/html; charset=utf-8"}


def page(title: str, body: str, head: str = "") -> tuple[int, dict, bytes]:
    return 200, HTML, html(title, body, head).encode()


def audit(pages: dict, fallback: tuple[int, dict, bytes] | None = None, **config) -> AuditReport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "site.test":
            return httpx.Response(404)
        entry = pages.get(request.url.raw_path.decode()) or fallback or (404, HTML, b"<h1>404</h1>")
        status, headers, body = entry(request) if callable(entry) else entry
        return httpx.Response(status, headers=headers, content=b"" if request.method == "HEAD" else body)

    cfg = AuditConfig.model_validate({**OFFLINE, **config})
    return Auditor(cfg, transport=httpx.MockTransport(handler)).run_sync(SITE)


def rule(report: AuditReport, rule_id: str):
    return next(r for r in report.rules() if r.id == rule_id)


def failed(report: AuditReport, rule_id: str) -> dict[str, str | None]:
    return {f.url: f.key for f in rule(report, rule_id).findings}


def test_catch_all_200_is_soft_404_and_hides_url_variants():
    report = audit({"/": page("Home", f"<h1>Home</h1><p>{TEXT}</p>")}, fallback=page("Home", "<p>anything</p>"))
    assert rule(report, "indexability.soft_404_setup").status == RuleStatus.FAILED
    assert rule(report, "indexability.url_variants_duplicate").status == RuleStatus.NOT_APPLICABLE


def test_url_variants_without_redirect_or_canonical():
    article = page("Article", f"<h1>Article</h1><p>{TEXT}</p>")
    report = audit(
        {
            "/": page("Home", f'<h1>Home</h1><p>{TEXT}</p><a href="/article">Статья о сервисе</a>'),
            "/article": article,
            "/article/": article,
            "/index.php": page("Home", "<h1>Home</h1>", f'<link rel="canonical" href="{SITE}/">'),
        }
    )
    assert failed(report, "indexability.url_variants_duplicate") == {f"{SITE}/article/": "slash"}
    assert rule(report, "indexability.soft_404_setup").status == RuleStatus.PASSED


def test_robots_syntax_and_blocked_resources():
    robots = "Disallow: /tmp\nUser-agent: *\nDisallow: /assets/\nNoindex: /x\nSitemap: /sitemap.xml\nClean-param: sort /\n"
    report = audit(
        {
            "/robots.txt": (200, {"content-type": "text/plain"}, robots.encode()),
            "/": page("Home", f"<h1>Home</h1><p>{TEXT}</p>", '<script src="/assets/app.js"></script>'),
            "/assets/app.js": (200, {"content-type": "application/javascript"}, b""),
        }
    )
    assert {f.key for f in rule(report, "indexability.robots_syntax").findings} == {"no_agent", "unknown", "sitemap"}
    assert failed(report, "indexability.blocked_resources") == {f"{SITE}/assets/app.js": None}


def test_robots_txt_helpers():
    text = "﻿User-agent: Yandex\nClean-param: utm_source&sort /catalog/*\nbroken line\nDisallow: /каталог\n"
    assert robots_txt.lint(text) == {"syntax": ["3"], "non_ascii": ["4"]}
    rules = robots_txt.clean_params(text)
    assert robots_txt.is_cleaned(rules, "/catalog/phones", {"sort"})
    assert not robots_txt.is_cleaned(rules, "/news/", {"sort"})
    assert not robots_txt.is_cleaned(rules, "/catalog/", {"sort", "page"})


def test_canonical_target_non_indexable():
    report = audit(
        {
            "/": page("Home", f'<h1>Home</h1><p>{TEXT}</p><a href="/a">Раздел A</a> <a href="/c">Раздел C</a>'),
            "/a": page("A", "<h1>A</h1>", f'<link rel="canonical" href="{SITE}/b">'),
            "/b": page("B", "<h1>B</h1>", '<meta name="robots" content="noindex">'),
            "/c": page("C", "<h1>C</h1>", f'<link rel="canonical" href="{SITE}/d">'),
            "/d": page("D", "<h1>D</h1>", f'<link rel="canonical" href="{SITE}/e">'),
            "/e": page("E", f"<h1>E</h1><p>{TEXT}</p>"),
        },
        crawl={"use_sitemap": False},
    )
    assert failed(report, "indexability.canonical_target_non_indexable") == {f"{SITE}/b": "noindex", f"{SITE}/d": "chain"}


def test_soft_404_pages_and_js_shell():
    report = audit(
        {
            "/": page(
                "Home",
                f'<h1>Home</h1><p>{TEXT}</p><a href="/gone">Старая акция</a> <a href="/empty">Пустая категория</a> '
                '<a href="/gallery">Галерея работ</a> <a href="/app">Приложение</a>',
            ),
            "/gone": page("Страница не найдена", "<header><a href='/'>Главная</a></header><h1>Ошибка</h1>"),
            "/empty": page("Архивы: тег", f"<header>{TEXT}</header><main></main><footer>{TEXT}</footer>"),
            "/gallery": page("Галерея", "<main><h1>Галерея</h1><img src='/1.jpg' alt='Работа'></main>"),
            "/app": page("App", '<div id="root"></div><script src="/a.js"></script>'),
        },
        crawl={"use_sitemap": False},
    )
    assert failed(report, "content.soft_404") == {f"{SITE}/gone": "not_found", f"{SITE}/empty": "empty"}
    assert failed(report, "content.js_rendered") == {f"{SITE}/app": None}


def test_param_duplicates_respect_clean_param():
    body = f"<h1>Catalog</h1><p>{TEXT}</p>"
    report = audit(
        {
            "/robots.txt": (200, {"content-type": "text/plain"}, b"User-agent: *\nClean-param: sort\n"),
            "/": page("Home", f'<h1>Home</h1><p>{TEXT} дом</p><a href="/c">Каталог</a> <a href="/c?sort=price">Дешевле</a>'),
            "/c": page("Catalog", body),
            "/c?sort=price": page("Catalog", body),
        }
    )
    assert failed(report, "content.param_duplicates") == {f"{SITE}/c?sort=price": "clean_param"}
    assert rule(report, "content.duplicate_content").status == RuleStatus.PASSED


def test_link_profile_rules():
    report = audit(
        {
            "/": page(
                "Home",
                f'<h1>Home</h1><p>{TEXT}</p><a href="/hidden" rel="nofollow">Скрытый раздел</a> '
                '<a href="/closed">Подробнее</a> <a href="/search?q=test">Поиск</a>',
            ),
            "/hidden": page("Hidden", f'<h1>Hidden</h1><p>{TEXT}</p><a href="/">Главная</a>'),
            "/closed": page("Closed", "<h1>Closed</h1>", '<meta name="robots" content="noindex">'),
            "/search?q=test": page("Search", f'<h1>Search</h1><p>{TEXT}</p><a href="/">Главная</a>'),
        },
        crawl={"use_sitemap": False},
    )
    assert failed(report, "links.nofollow_inlinks_only") == {f"{SITE}/hidden": None}
    assert failed(report, "links.to_non_indexable") == {f"{SITE}/closed": "noindex"}
    assert failed(report, "links.no_outlinks") == {f"{SITE}/closed": None}
    assert rule(report, "links.non_descriptive_anchor").findings[0].data["text"] == "подробнее"
    assert failed(report, "indexability.search_pages_indexable") == {f"{SITE}/search?q=test": None}


def test_links_to_canonicalised_filters_are_one_finding():
    variant = page("Catalog", "<h1>Catalog</h1>", f'<link rel="canonical" href="{SITE}/c">')
    report = audit(
        {
            "/": page("Home", f'<h1>Home</h1><p>{TEXT}</p><a href="/c?f=1">Красные</a> <a href="/c?f=2">Синие</a>'),
            "/c?f=1": variant,
            "/c?f=2": variant,
        },
        crawl={"use_sitemap": False},
    )
    findings = rule(report, "links.to_non_indexable").findings
    assert [(f.url, f.key, f.data["count"]) for f in findings] == [(f"{SITE}/c", "variants", 2)]


def test_resources_size_and_redirect_rules():
    big = "<p>" + "x" * (2 * 1024 * 1024 + 10) + "</p>"
    report = audit(
        {
            "/": page(
                "Home",
                f'<h1>Home</h1><p>{TEXT}</p><a href="/big">Большая</a> <a href="/old">Старая</a>',
                '<script src="/missing.js"></script><meta name="description" content="a">'
                '<meta name="description" content="b">',
            ),
            "/big": page("Big", big),
            "/old": (301, {"location": "http://site.test/new"}, b""),
            "/new": page("New", f"<h1>New</h1><p>{TEXT}</p>"),
        },
        crawl={"use_sitemap": False},
    )
    assert failed(report, "performance.html_over_2mb") == {f"{SITE}/big": None}
    assert failed(report, "performance.broken_resources") == {f"{SITE}/missing.js": None}
    assert failed(report, "indexability.redirect_https_to_http") == {f"{SITE}/old": None}
    assert failed(report, "meta.description_multiple") == {f"{SITE}/": None}


def test_home_error_and_slow_site():
    report = audit(
        {"/": (500, HTML, html("Error", "<h1>Error</h1>").encode())},
        rules={"performance.site_slow_response": {"params": {"max_ms": 0}}},
    )
    assert failed(report, "indexability.home_unavailable") == {f"{SITE}/": None}

    report = audit(
        {"/": page("Home", f"<h1>Home</h1><p>{TEXT}</p>")},
        rules={"performance.site_slow_response": {"params": {"max_ms": 0}}},
    )
    assert rule(report, "performance.site_slow_response").status == RuleStatus.FAILED


def test_sitemap_coverage_and_hreflang_targets():
    sitemap = f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>{SITE}/</loc></url></urlset>'
    report = audit(
        {
            "/sitemap.xml": (200, {"content-type": "application/xml"}, sitemap.encode()),
            "/": page(
                "Home",
                f'<h1>Home</h1><p>{TEXT}</p><a href="/about">О компании</a>',
                f'<link rel="alternate" hreflang="ru" href="{SITE}/"><link rel="alternate" hreflang="en" href="{SITE}/en">',
            ),
            "/about": page("About", f'<h1>About</h1><p>{TEXT}</p><a href="/">Главная</a> <a href="/news/page/2/">Ещё</a>'),
            "/news/page/2/": page("News 2", f"<h1>News</h1><p>{TEXT}</p>"),
            "/en": page("En", "<h1>En</h1>", '<meta name="robots" content="noindex">'),
        }
    )
    assert failed(report, "indexability.not_in_sitemap") == {f"{SITE}/about": None}
    assert failed(report, "international.target_non_indexable") == {f"{SITE}/en": "noindex"}


def test_canonical_head_and_directive_problems():
    home_canonical = f'<link rel="canonical" href="{SITE}/">'
    links = "".join(f'<a href="/p{i}">Раздел {i}</a> ' for i in range(3))
    report = audit(
        {
            "/": page("Home", f'<h1>Home</h1><p>{TEXT}</p>{links}<a href="/body">Тело</a> <a href="/broken">Шапка</a> '
                      '<a href="/conflict">Конфликт</a> <a href="/pixel">Пиксель</a>'),
            **{f"/p{i}": page(f"P{i}", f"<h1>P{i}</h1><p>{TEXT}</p>", home_canonical) for i in range(3)},
            "/body": page("Body", f'<h1>Body</h1><link rel="canonical" href="{SITE}/body"><p>{TEXT}</p>'),
            "/broken": page("Broken", f"<h1>Broken</h1><p>{TEXT}</p>", f'<div>ad</div><link rel="canonical" href="{SITE}/broken">'),
            "/pixel": page("Pixel", f"<h1>Pixel</h1><p>{TEXT}</p>", '<noscript><img src="/px.gif"></noscript><meta name="robots" content="noindex">'),
            "/conflict": (
                200,
                {**HTML, "x-robots-tag": "noindex"},
                html("Conflict", f"<h1>Conflict</h1><p>{TEXT}</p>", '<meta name="robots" content="index, follow">').encode(),
            ),
        },
        crawl={"use_sitemap": False},
    )
    invalid = {f.url: f.key for f in rule(report, "indexability.canonical_invalid").findings}
    assert invalid == {f"{SITE}/body": "outside_head", f"{SITE}/": "homepage"}
    assert failed(report, "indexability.head_broken") == {f"{SITE}/broken": None, f"{SITE}/pixel": "noscript_img"}
    assert failed(report, "indexability.robots_directives_conflict") == {f"{SITE}/conflict": "contradiction"}


def test_raw_href_problems():
    report = audit(
        {
            "/": page(
                "Home",
                f'<h1>Home</h1><p>{TEXT}</p><a href="/promo?utm_source=banner">Акция</a> '
                '<a href="javascript:go(\'/secret\')">Тайный раздел</a> <a href="javascript:void(0)">Меню</a> '
                '<a href="http://localhost:3000/admin">Админка</a> <a href="https//site.test/x">Опечатка</a>',
            ),
        },
        crawl={"use_sitemap": False},
    )
    assert failed(report, "links.tracking_params") == {f"{SITE}/promo?utm_source=banner": None}
    malformed = {f.url: f.key for f in rule(report, "links.malformed").findings}
    assert malformed == {
        "javascript:go('/secret')": "javascript",
        "http://localhost:3000/admin": "local",
        "https//site.test/x": "scheme",
    }


def test_sitemap_limits_and_duplicate_hreflang(monkeypatch):
    import seoaudit.checks.indexability as indexability

    monkeypatch.setattr(indexability, "_SITEMAP_MAX_URLS", 2)
    locs = "".join(f"<url><loc>{u}</loc></url>" for u in [f"{SITE}/", "https://other.test/a", "http://site.test/b"])
    sitemap = f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{locs}</urlset>'
    alternates = f'<link rel="alternate" hreflang="en" href="{SITE}/en"><link rel="alternate" hreflang="en" href="{SITE}/en2">'
    report = audit(
        {
            "/sitemap.xml": (200, {"content-type": "application/xml"}, sitemap.encode()),
            "/": page("Home", f"<h1>Home</h1><p>{TEXT}</p>", alternates),
        }
    )
    assert {f.key for f in rule(report, "indexability.sitemap_limits").findings} == {"too_many_urls", "foreign", "http"}
    assert failed(report, "international.duplicate_lang") == {f"{SITE}/": None}


def test_structured_data_values():
    jsonld = """<script type="application/ld+json">[
      {"@context": "https://schema.org", "@type": "Product", "name": "Item", "image": "www.site.test/img/item.jpg",
       "offers": {"@type": "Offer", "price": "1 490 ₽", "priceCurrency": "RUR"},
       "aggregateRating": {"@type": "AggregateRating", "ratingValue": "4.5"}},
      {"@context": "https://schema.org", "@type": "Article", "headline": "A", "datePublished": "15.03.2026"},
      {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": 1, "item": "https://site.test/"},
        {"@type": "ListItem", "position": 2, "name": "Item"}]}
    ]</script>"""
    report = audit({"/": page("Home", f"<h1>Home</h1><p>{TEXT}</p>", jsonld)}, crawl={"use_sitemap": False})
    keys = {f.key for f in rule(report, "structured_data.schema_values").findings}
    assert keys == {"url", "price", "currency", "date", "breadcrumb_name"}
    missing = [f.data["missing"] for f in rule(report, "structured_data.schema_required").findings]
    assert ["ratingCount/reviewCount"] in missing


def test_structured_data_vocabulary():
    jsonld = """<script type="application/ld+json">{"@context": "https://schema.org", "@graph": [
      {"@type": "Prodcut", "name": "X"},
      {"@type": "Product", "name": "Item", "descripton": "Text", "brand": {"@type": "Brand", "name": "B"},
       "offers": {"@type": "Offer", "price": "10", "priceCurrency": "RUB", "availability": "В наличии"}},
      {"@type": "Article", "headline": "A", "author": {"@type": "WebSite", "name": "S"}},
      {"@type": "Organization", "@id": "#org", "name": "O", "url": "https://site.test/",
       "logo": {"@type": "WebPage", "url": "https://site.test/"}},
      {"@type": "WebPage", "publisher": {"@type": "Organization", "@id": "#org"}},
      {"@type": "WebSite", "potentialAction": {"@type": "SearchAction", "target": "https://site.test/?q={q}",
       "query-input": "required name=q"}},
      {"@type": "Organization", "name": "M", "url": "https://site.test/", "member": {"@type": "OrganizationRole", "member": {"@type": "Person", "name": "P"}}},
      {"@type": "Restaurant", "name": "Cafe"},
      {"@type": "Recipe", "name": "R", "image": "https://site.test/r.jpg", "recipeInstructions": [
        {"@type": "HowToStep", "text": "Mix"}, {"@type": "HowToStep", "name": "Bake"}]},
      {"@type": "HowTo", "name": "H", "step": [{"@type": "HowToSection", "name": "S"}]}
    ]}</script>
    <script type="application/ld+json">{"@type": "Organization", "name": "No context"}</script>
    <script type="application/ld+json">{"@context": "https://example.com/vocab", "@type": "Whatever", "foo": 1}</script>"""
    microdata = '<div itemscope itemtype="http://data-vocabulary.org/Breadcrumb"><span itemprop="title">A</span></div>'
    report = audit(
        {"/": page("Home", f"<h1>Home</h1><p>{TEXT}</p>{microdata}", jsonld)}, crawl={"use_sitemap": False}
    )
    examples = {f.key: f.data["examples"] for f in rule(report, "structured_data.schema_vocabulary").findings}
    assert examples == {
        "type": ["Prodcut → Product"],
        "property": ["Product.descripton → description"],
        "enum": ["Offer.availability = «В наличии» (BackOrder, Discontinued, InStock, InStoreOnly, …)"],
        "range": ["Organization.logo = WebPage (ImageObject, URL)"],
        "missing": ["Organization"],
        "obsolete": ["http://data-vocabulary.org"],
    }
    required = {(f.data["type"], tuple(f.data["missing"])) for f in rule(report, "structured_data.schema_required").findings}
    assert ("Restaurant", ("address",)) in required
    assert ("HowToStep", ("text/itemListElement",)) in required and len(required) == 2
    recommended = {f.data["type"]: f.data["missing"] for f in rule(report, "structured_data.schema_recommended").findings}
    assert recommended["Organization"] == ["url"]
    assert recommended["Product"] == ["image", "description", "sku", "gtin/gtin8/gtin12/gtin13/gtin14/mpn/isbn"]
    assert "Offer" not in recommended
    assert [f.data["type"] for f in rule(report, "structured_data.rich_result_retired").findings] == ["HowTo", "SearchAction"]


def test_bots_access_cloaking_and_llms_txt():
    robots = "User-agent: *\nAllow: /\n\nUser-agent: GPTBot\nUser-agent: PerplexityBot\nDisallow: /\n"
    spam = '<a href="https://casino.example/">Казино</a> <a href="https://pills.example/">Таблетки</a>'

    def home(request: httpx.Request) -> tuple[int, dict, bytes]:
        agent = request.headers["user-agent"]
        if "ClaudeBot" in agent or "OAI-SearchBot" in agent:
            return 403, HTML, b"Forbidden"
        body = f'<h1>Home</h1><p>{TEXT} {TEXT}</p><a href="/premium">Платная статья</a>'
        return page("Home", body + (spam if "Googlebot" in agent else ""))

    def premium(request: httpx.Request) -> tuple[int, dict, bytes]:
        paywall = '<script type="application/ld+json">{"@type": "NewsArticle", "isAccessibleForFree": false}</script>'
        full = f"<p>{TEXT * 3}</p>{spam}" if "Googlebot" in request.headers["user-agent"] else ""
        return page("Premium", f"<h1>Premium</h1><p>{TEXT} {TEXT}</p>{full}", paywall)

    report = audit(
        {
            "/": home,
            "/premium": premium,
            "/robots.txt": (200, {"content-type": "text/plain"}, robots.encode()),
            "/llms.txt": (200, {"content-type": "text/plain"}, b"Site\n\n- [Docs](/docs)\n- [Home](/)\n"),
        },
        crawl={"use_sitemap": False},
    )
    messages = lambda rule_id: sorted(f.message for f in rule(report, rule_id).findings)  # noqa: E731
    assert messages("bots.ai_search_blocked") == ["OAI-SearchBot: сервер отвечает 403", "PerplexityBot: запрещён в robots.txt"]
    assert messages("bots.ai_training_blocked") == ["ClaudeBot (сервер: 403)", "GPTBot (robots.txt)"]
    assert rule(report, "bots.search_bot_denied").status == RuleStatus.PASSED
    assert [(f.key, f.data["hosts"]) for f in rule(report, "bots.cloaking").findings] == [
        ("links", ["casino.example", "pills.example"])
    ]
    assert {f.key for f in rule(report, "bots.llms_txt_invalid").findings} == {"no_title", "broken"}
    assert rule(report, "bots.llms_txt_missing").status == RuleStatus.PASSED


def test_rendered_dom_is_compared_with_raw_html(monkeypatch):
    from seoaudit.checks import rendering

    body = f"<h1>Home</h1><p>{TEXT}</p>"
    added = (
        f"<p>{TEXT} {TEXT}</p>"
        '<a href="/a">Раздел A</a> <a href="/b">Раздел B</a> <a href="/c">Раздел C</a>'
        '<script type="application/ld+json">{"@context": "https://schema.org", "@type": "Product", "name": "X"}</script>'
    )
    dom = html("Real title", body + added, f'<link rel="canonical" href="{SITE}/other">')

    async def fake_render(urls, user_agent, locale, mobile, options):
        return {url: rendering.Rendered(url, dom) for url in urls}

    monkeypatch.setattr(rendering, "render", fake_render)
    report = audit(
        {"/": page("App", body, f'<link rel="canonical" href="{SITE}/">')},
        crawl={"use_sitemap": False},
        checks={"pagespeed": {"enabled": False}, "html": {"options": {"validator_pages": 0}}},
    )
    messages = lambda rule_id: [f.message for f in rule(report, rule_id).findings]  # noqa: E731
    assert messages("rendering.js_indexing_changed") == [f"Canonical в HTML: {SITE}/, после JS: {SITE}/other"]
    assert messages("rendering.js_meta_changed") == ["Title в HTML: «App», после JS: «Real title»"]
    assert messages("rendering.js_content") == ["Слов в HTML: 81, после JS: 247"]
    assert rule(report, "rendering.js_links").findings[0].data["count"] == 3
    assert rule(report, "rendering.js_structured_data").findings[0].data["types"] == ["Product"]


def test_title_width_is_measured_in_pixels():
    wide = "Широкий заголовок ЖШЩЮ для проверки ширины сниппета"
    report = audit({"/": page(wide, f"<h1>Home</h1><p>{TEXT}</p>")}, crawl={"use_sitemap": False})
    finding = rule(report, "meta.title_length").findings[0]
    assert finding.key == "long" and finding.data["width"] > 561 and finding.data["length"] < 60


def test_geo_signals():
    article_ld = '<script type="application/ld+json">{"@context": "https://schema.org", "@type": "BlogPosting", "headline": "A"}</script>'
    organization = (
        '<script type="application/ld+json">{"@context": "https://schema.org", "@type": "Organization",'
        f' "name": "Site", "url": "{SITE}/"}}</script>'
    )
    long_text = " ".join(f"слово{i}" for i in range(900))
    entries = "".join(f"<url><loc>{SITE}/p{i}</loc><lastmod>2026-01-05</lastmod></url>" for i in range(25))
    sitemap = f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{entries}<url><loc>{SITE}/</loc><lastmod>2099-01-01</lastmod></url></urlset>'
    report = audit(
        {
            "/": page("Home", f'<h1>Home</h1><p>{TEXT}</p><a href="/blog/post">Статья</a> <a href="/contacts">Контакты</a>', organization),
            "/blog/post": page("Post", f"<h1>Post</h1><p>{long_text}</p>", article_ld + '<meta name="robots" content="max-snippet:20">'),
            "/contacts": (200, {**HTML, "x-robots-tag": "bingbot: noarchive"}, html("Contacts", f"<h1>Contacts</h1><p>{TEXT}</p>").encode()),
            "/sitemap.xml": (200, {"content-type": "application/xml"}, sitemap.encode()),
        }
    )
    assert failed(report, "geo.author_missing") == {f"{SITE}/blog/post": None}
    assert failed(report, "geo.date_missing") == {f"{SITE}/blog/post": None}
    assert {(f.url, f.key) for f in rule(report, "geo.snippet_restricted").findings} == {
        (f"{SITE}/blog/post", "max_snippet"),
        (f"{SITE}/contacts", "bing"),
    }
    assert {f.key for f in rule(report, "geo.trust_pages_missing").findings} == {"about", "privacy"}
    assert {f.key for f in rule(report, "geo.sitemap_lastmod").findings} == {"future"}
    assert failed(report, "geo.unstructured_text") == {f"{SITE}/blog/post": None}
    assert rule(report, "structured_data.organization_same_as").status == RuleStatus.FAILED
    assert {r.category for r in report.rules() if r.id.startswith(("geo.", "bots.llms"))} == {"geo"}


def test_report_language():
    pages = {"/": page("Home", f"<p>{TEXT}</p>")}
    english = audit(pages, lang="en", crawl={"use_sitemap": False})
    assert rule(english, "content.h1_missing").title == "Missing H1 heading"
    assert next(c for c in english.categories if c.id == "content").name == "Content & headings"

    russian = audit(pages, crawl={"use_sitemap": False})
    translated = Auditor(AuditConfig.model_validate({**OFFLINE, "lang": "en"})).localize(russian)
    assert translated.lang == "en"
    assert rule(translated, "content.h1_missing").title == "Missing H1 heading"


def test_rules_are_translated():
    def shape(text):
        message = text.message if isinstance(text.message, dict) else {"default": text.message}
        return {k for k, v in message.items() if v}, bool(text.recommendation), bool(text.fix)

    for plan in Auditor(AuditConfig()).plan():
        assert set(plan.definition.terms.get("ru", {})) == set(plan.definition.terms.get("en", {})), plan.cls.id
        for spec in plan.rules.values():
            assert {"ru", "en"} <= set(spec.text), spec.id
            assert shape(spec.text["ru"]) == shape(spec.text["en"]), spec.id


def test_og_article_needs_body_text():
    og = '<meta property="og:type" content="article">'
    paragraph = "<p>" + " ".join(f"слово{i}" for i in range(40)) + "</p>"
    listing = page("Blog", "<h1>Blog</h1>" + "<p>Короткий анонс.</p>" * 10, og)
    report = audit(
        {
            "/": page("Home", f'<h1>Home</h1><p>{TEXT}</p><a href="/blog">Блог</a> <a href="/post">Статья</a>', og),
            "/blog": listing,
            "/post": page("Post", '<h1>Post</h1><span class="post__author">Автор</span>' + paragraph * 6, og),
        },
        crawl={"use_sitemap": False},
    )
    assert failed(report, "geo.date_missing") == {f"{SITE}/post": None}
    assert rule(report, "geo.author_missing").status == RuleStatus.PASSED
