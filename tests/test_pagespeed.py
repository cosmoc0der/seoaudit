from __future__ import annotations

import pytest
import typer

from conftest import SITE
from seoaudit import cli
from seoaudit.checks.pagespeed import PageSpeedCheck
from seoaudit.checks.pagespeed.runners import Measurement, QuotaError, _with_headless
from seoaudit.core.config import AuditConfig
from seoaudit.core.engine import Auditor
from seoaudit.core.models import RuleStatus

LHR = {
    "categories": {
        "performance": {"score": 0.42},
        "seo": {"auditRefs": [{"id": "meta-description", "weight": 1}, {"id": "document-title", "weight": 1}]},
    },
    "audits": {
        "largest-contentful-paint": {"numericValue": 4100.4},
        "cumulative-layout-shift": {"numericValue": 0.02},
        "total-blocking-time": {"numericValue": 120},
        "meta-description": {"title": "No meta description", "score": 0},
        "document-title": {"title": "Has a title", "score": 1},
        "render-blocking-insight": {"title": "Render blocking", "score": 0, "metricSavings": {"FCP": 900, "LCP": 900}},
        "unused-css-rules": {"title": "Unused CSS", "score": 0.5, "details": {"type": "opportunity", "overallSavingsMs": 300}},
        "bootup-time": {"title": "JS execution", "score": 1, "metricSavings": {"TBT": 200}},
    },
}


class FakeRunner:
    name = "fake"
    concurrency = 1
    has_field_data = False

    def __init__(self, result):
        self.result = result

    async def run(self, url, strategy):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def audit(monkeypatch, transport, result):
    async def runner(self):
        return FakeRunner(result)

    monkeypatch.setattr(PageSpeedCheck, "_runner", runner)
    cfg = AuditConfig.model_validate({"checks": {"html": {"options": {"validator_pages": 0}}}})
    report = Auditor(cfg, transport=transport).run_sync(SITE)
    return {r.id: r for r in report.rules() if r.check == "pagespeed"}


def test_lighthouse_result_is_evaluated(monkeypatch, transport):
    rules = audit(monkeypatch, transport, Measurement(LHR))

    score = rules["pagespeed.performance_score"]
    assert score.status == RuleStatus.FAILED
    assert score.findings[0].data["opportunities"] == ["Render blocking (−0.9 с)", "Unused CSS (−0.3 с)"]
    assert rules["pagespeed.lcp_slow"].findings[0].data["value"] == 4100
    assert rules["pagespeed.cls_high"].status == RuleStatus.PASSED
    assert rules["pagespeed.lighthouse_seo"].findings[0].data["audits"] == ["No meta description"]
    assert rules["pagespeed.field_cwv"].status == RuleStatus.NOT_APPLICABLE
    assert rules["pagespeed.field_cwv"].note == "field_data_local"


def test_quota_error_skips_rules(monkeypatch, transport):
    rules = audit(monkeypatch, transport, QuotaError("HTTP 429"))
    assert {(r.status, r.note) for r in rules.values()} == {(RuleStatus.SKIPPED, "pagespeed_quota")}


def test_runtime_error_is_not_scored_as_zero(monkeypatch, transport):
    rules = audit(monkeypatch, transport, Measurement({"runtimeError": {"code": "NO_FCP", "message": "no paint"}}))
    assert {r.status for r in rules.values()} == {RuleStatus.ERROR}


@pytest.mark.parametrize(
    ("chrome", "flags", "expected"),
    [
        ("/ms-playwright/chromium_headless_shell-1/chrome-linux/chrome-headless-shell", "", ""),
        ("C:/Chrome/chrome.exe", "--no-sandbox", "--headless=new --no-sandbox"),
        (None, "", "--headless=new"),
        ("chrome", "--headless", "--headless"),
    ],
)
def test_headless_flag(chrome, flags, expected):
    assert _with_headless(chrome, flags) == expected


def test_default_pdf_falls_back_to_html(monkeypatch):
    monkeypatch.setattr(cli, "unavailable_formats", lambda formats: {"pdf": "hint"} if "pdf" in formats else {})
    assert cli._formats([], ["pdf", "html", "json"]) == ["html", "json"]
    with pytest.raises(typer.Exit):
        cli._formats(["pdf"], ["json"])
