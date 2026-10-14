from __future__ import annotations

from datetime import datetime

from seoaudit.core.categories import get_category
from seoaudit.core.check import FindingSink
from seoaudit.core.context import AuditContext
from seoaudit.core.models import (
    AuditReport,
    CategoryResult,
    CrawlStats,
    PageSummary,
    RuleResult,
    RuleStatus,
)
from seoaudit.core.rules import RuleSpec
from seoaudit.core.scoring import category_score, grade, overall_score


def build_rule_result(spec: RuleSpec, sink: FindingSink, lang: str) -> RuleResult:
    findings = sink.findings.get(spec.id, [])
    status, note = sink.statuses.get(spec.id, (None, None))
    if findings:
        status = RuleStatus.FAILED
    elif status is None:
        status = RuleStatus.PASSED
    if spec.id in sink.truncated:
        note = note or "findings_truncated"

    result = RuleResult(
        id=spec.id,
        check=spec.check,
        category=spec.category,
        severity=spec.severity,
        scope=spec.scope,
        status=status,
        title=spec.id,
        refs=spec.refs,
        findings=findings,
        note=note,
    )
    _apply_text(result, spec, lang)
    return result


def _apply_text(result: RuleResult, spec: RuleSpec, lang: str) -> None:
    text = spec.localized(lang)
    result.title, result.description, result.recommendation, result.fix = (
        text.title, text.description, text.recommendation, text.fix
    )  # fmt: skip
    for finding in result.findings:
        finding.message = text.format_message(finding.key, {**spec.params, **finding.data})


def localize_report(report: AuditReport, specs: list[RuleSpec], lang: str, extra_categories: dict | None = None) -> None:
    """Rewrites a saved report's texts in another language; findings keep raw data, so nothing is re-crawled."""
    by_id = {spec.id: spec for spec in specs}
    for category in report.categories:
        meta = get_category(category.id, lang, extra_categories)
        category.name, category.description = meta.name, meta.description
        for rule in category.rules:
            if spec := by_id.get(rule.id):
                _apply_text(rule, spec, lang)
    report.lang = lang


def build_report(
    *,
    ctx: AuditContext,
    specs: list[RuleSpec],
    sink: FindingSink,
    target: str,
    started_at: datetime,
    crawl_seconds: float,
    version: str,
    extra_categories: dict | None = None,
) -> AuditReport:
    lang = ctx.config.lang
    page_count = ctx.page_count()

    grouped: dict[str, list[RuleResult]] = {}
    for spec in specs:
        grouped.setdefault(spec.category, []).append(build_rule_result(spec, sink, lang))

    categories = []
    for category_id, rules in grouped.items():
        meta = get_category(category_id, lang, extra_categories)
        rules.sort(key=lambda r: (r.status != RuleStatus.FAILED, r.severity.rank, r.id))
        category = CategoryResult(
            id=category_id, name=meta.name, description=meta.description, weight=meta.weight, rules=rules
        )
        category.score = category_score(category, page_count)
        categories.append((meta.order, category))
    categories = [c for _, c in sorted(categories, key=lambda item: item[0])]

    score = overall_score(categories)
    pages = sorted(ctx.pages.values(), key=lambda p: (p.depth is None, p.depth or 0, p.url))
    return AuditReport(
        tool_version=version,
        target=target,
        final_url=ctx.site.base_url,
        lang=lang,
        started_at=started_at,
        finished_at=datetime.now().astimezone(),
        score=score,
        grade=grade(score),
        stats=CrawlStats(
            pages_total=len(ctx.pages),
            html_pages=page_count,
            error_pages=sum(1 for p in ctx.pages.values() if p.response.broken),
            redirects=sum(1 for p in ctx.pages.values() if p.is_redirect),
            blocked_by_robots=len(ctx.blocked),
            sitemap_urls=len(ctx.site.sitemap_urls),
            duration_s=round(crawl_seconds, 1),
        ),
        categories=categories,
        pages=[
            PageSummary(
                url=p.url,
                status=p.response.redirects[0].status if p.is_redirect else p.status,
                depth=p.depth,
                title=p.title if p.ok else None,
                ttfb_ms=round(p.response.ttfb_ms) if p.response.ttfb_ms else None,
                size=p.response.size,
                source=p.source,
                error=p.response.error,
            )
            for p in pages
        ],
        diagnostics=ctx.diagnostics,
        meta=ctx.config.report.meta,
    )
