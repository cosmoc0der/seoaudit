from __future__ import annotations

import pytest

from conftest import SITE
from seoaudit.core.config import AuditConfig, ConfigError
from seoaudit.core.engine import Auditor
from seoaudit.core.models import AuditReport, RuleStatus
from seoaudit.core.urls import normalize_url, pretty_url
from seoaudit.reporting.html import plural, render_html

OFFLINE = {"checks": {"pagespeed": {"enabled": False}, "html": {"options": {"validator_pages": 0}}}}


def run(transport, **config) -> AuditReport:
    cfg = AuditConfig.model_validate({**OFFLINE, **config})
    return Auditor(cfg, transport=transport).run_sync(SITE)


def rule(report: AuditReport, rule_id: str):
    return next(r for r in report.rules() if r.id == rule_id)


async def test_full_audit(transport):
    cfg = AuditConfig.model_validate(OFFLINE)
    report = await Auditor(cfg, transport=transport).run(SITE)

    assert report.stats.html_pages == 2
    assert 0 < report.score < 100

    broken = rule(report, "links.broken_internal")
    assert broken.status == RuleStatus.FAILED
    assert broken.findings[0].data["target"] == f"{SITE}/broken"

    assert [f.url for f in rule(report, "content.h1_missing").findings] == [f"{SITE}/about"]
    assert rule(report, "meta.description_duplicate").affected == 2
    assert rule(report, "images.too_large").status == RuleStatus.FAILED
    assert rule(report, "images.alt_missing").status == RuleStatus.FAILED
    assert rule(report, "links.internal_redirect").status == RuleStatus.FAILED
    assert rule(report, "indexability.robots_no_sitemap").status == RuleStatus.FAILED
    assert rule(report, "indexability.blocked_by_robots").findings[0].url == f"{SITE}/private/x"
    assert rule(report, "security.https_missing").status == RuleStatus.FAILED
    assert rule(report, "international.invalid_code").status == RuleStatus.NOT_APPLICABLE
    assert rule(report, "meta.title_missing").status == RuleStatus.PASSED


def test_threshold_override_changes_result_and_texts(transport):
    report = run(transport, rules={"images.too_large": {"params": {"max_kb": 500}}})
    too_large = rule(report, "images.too_large")
    assert too_large.status == RuleStatus.PASSED
    assert "500" in too_large.title


def test_disabled_rule_is_excluded(transport):
    report = run(transport, rules={"meta.twitter_card_missing": {"enabled": False}})
    assert all(r.id != "meta.twitter_card_missing" for r in report.rules())


def test_unknown_override_is_rejected():
    with pytest.raises(ConfigError):
        Auditor(AuditConfig.model_validate({"rules": {"images.too_large": {"params": {"max_mb": 1}}}})).plan()
    with pytest.raises(ConfigError):
        Auditor(AuditConfig.model_validate({"rules": {"no.such_rule": {"enabled": False}}})).plan()


def test_report_renders(transport):
    html = render_html(run(transport), AuditConfig().report)
    assert "shop.test" in html and "Битые внутренние ссылки" in html


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTP://Example.COM:80/a/../b?utm_source=x&id=1#top", "http://example.com/b?id=1"),
        ("/каталог/", "https://site.ru/%D0%BA%D0%B0%D1%82%D0%B0%D0%BB%D0%BE%D0%B3/"),
        ("mailto:a@b.c", None),
        ("https://пример.рф/", "https://xn--e1afmkfd.xn--p1ai/"),
    ],
)
def test_normalize_url(raw, expected):
    assert normalize_url(raw, "https://site.ru/x", strip_params=("utm_*",)) == expected


def test_pretty_url():
    assert pretty_url("https://xn--e1afmkfd.xn--p1ai/%D0%BA") == "https://пример.рф/к"


@pytest.mark.parametrize(("n", "word"), [(1, "проблема"), (3, "проблемы"), (5, "проблем"), (11, "проблем"), (22, "проблемы")])
def test_plural(n, word):
    assert plural(n, ["проблема", "проблемы", "проблем"]) == word
