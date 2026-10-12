from __future__ import annotations

import base64
import mimetypes
from collections import Counter
from functools import cache
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from jinja2 import Environment, PackageLoader, select_autoescape
from markdown_it import MarkdownIt
from markupsafe import Markup, escape

from seoaudit.core.config import ReportConfig
from seoaudit.core.i18n import pick
from seoaudit.core.models import AuditReport, Finding, RuleResult, RuleStatus, Severity
from seoaudit.core.urls import pretty_url

_markdown = MarkdownIt("commonmark", {"html": False}).enable("table")


@cache
def _environment() -> Environment:
    env = Environment(
        loader=PackageLoader("seoaudit.reporting", "templates"),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["md"] = lambda text: Markup(_markdown.render(text or ""))
    env.filters["md_inline"] = lambda text: Markup(_markdown.renderInline(text or ""))
    env.filters["wbr"] = wbr
    env.filters["by_url"] = by_url
    env.filters["pretty_url"] = pretty_url
    env.filters["score_class"] = score_class
    env.filters["plural"] = plural
    env.filters["local_urls"] = local_urls
    return env


@cache
def _strings(lang: str) -> dict:
    root = Path(__file__).with_name("i18n")
    variants = {p.stem: p for p in root.glob("*.yaml")}
    path = pick(variants, lang)
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def plural(n: int, forms: list[str]) -> str:
    """Two forms -> English-like rule, three forms -> East Slavic rule."""
    if len(forms) == 2:
        return forms[0] if n == 1 else forms[1]
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return forms[1]
    return forms[2]


def local_urls(text: str | None, origin: str) -> str:
    """Site's own URLs as paths: the host is on the cover, repeating it only buries the paths."""
    return (text or "").replace(origin + "/", "/")


def score_class(score: int | None) -> str:
    # Same cut-offs as grades B and C, so a gauge is never amber next to "good".
    if score is None:
        return "na"
    return "good" if score >= 75 else "average" if score >= 60 else "poor"


def wbr(url: str) -> Markup:
    """Long URLs wrap at slashes and hyphens instead of mid-word."""
    return Markup(str(escape(url)).replace("/", "/<wbr>").replace("-", "-<wbr>"))


def by_url(findings: list[Finding]) -> list[Finding]:
    """One row per URL: several findings on one page read as duplicated rows."""
    rows: dict[str, Finding] = {}
    for finding in findings:
        if finding.url is None:
            rows[f"#{len(rows)}"] = finding
            continue
        row = rows.get(finding.url)
        if row is None:
            rows[finding.url] = finding.model_copy()
        elif finding.message and finding.message not in (row.message or ""):
            row.message = f"{row.message}; {finding.message}" if row.message else finding.message
    return list(rows.values())


PHASES = {Severity.CRITICAL: "now", Severity.HIGH: "now", Severity.MEDIUM: "next", Severity.LOW: "later"}


def work_plan(report: AuditReport) -> dict[str, list[RuleResult]]:
    plan: dict[str, list[RuleResult]] = {phase: [] for phase in dict.fromkeys(PHASES.values())}
    for rule in report.top_issues(limit=len(report.rules())):
        plan[PHASES[rule.severity]].append(rule)
    return {phase: rules for phase, rules in plan.items() if rules}


def distribution(values: list, limit: int | None = None) -> list[tuple[str, int, int]]:
    """(label, count, percent of the largest bucket) rows for the bar charts."""
    counts = sorted(Counter(values).items(), key=lambda item: (item[0] is None, item[0]))
    if limit is not None and len(counts) > limit:
        head, tail = counts[:limit], counts[limit:]
        counts = [*head, (f"{head[-1][0] + 1}+", sum(n for _, n in tail))]
    top = max((n for _, n in counts), default=1)
    return [(label, n, round(100 * n / top)) for label, n in counts]


def render_html(report: AuditReport, config: ReportConfig) -> str:
    template = _environment().get_template("report.html.j2")
    css = Path(__file__).with_name("templates").joinpath("report.css").read_text(encoding="utf-8")
    parts = urlsplit(report.final_url)
    scored = [Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW]
    pages = report.pages
    # A section with nothing measured and nothing found would be a page of dashes.
    shown = [c for c in report.categories if c.score is not None or c.failed]
    numbers = {
        rule.id: f"{section}.{index}"
        for section, category in enumerate(shown, 1)
        for index, rule in enumerate((r for r in category.failed if r.severity != Severity.INFO), 1)
    }
    return template.render(
        report=report,
        config=config,
        t=_strings(report.lang),
        css=Markup(css),
        host=pretty_url(parts.hostname or report.final_url),
        origin=pretty_url(f"{parts.scheme}://{parts.netloc}"),
        category_names={c.id: c.name for c in report.categories},
        logo=_logo_data_uri(report.meta.logo),
        Severity=Severity,
        Status=RuleStatus,
        severity_counts={s: report.count_failed(s) for s in Severity},
        scored_severities=scored,
        shown_categories=shown,
        issue_numbers=numbers,
        issue_count=report.count_failed(*scored),
        plan=work_plan(report),
        status_chart=distribution([f"{p.status // 100}xx" if p.status else "—" for p in pages]),
        depth_chart=distribution([p.depth for p in pages if p.depth is not None], limit=6),
        problem_pages=[p for p in pages if p.status != 200],
        slow_pages=sorted((p for p in pages if p.ttfb_ms), key=lambda p: -p.ttfb_ms)[:10],
    )


def write_html(report: AuditReport, path: Path, config: ReportConfig) -> None:
    path.write_text(render_html(report, config), encoding="utf-8")


def _logo_data_uri(logo: str | None) -> str | None:
    if not logo:
        return None
    if logo.startswith(("http://", "https://", "data:")):
        return logo
    path = Path(logo)
    if not path.is_file():
        return None
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"
