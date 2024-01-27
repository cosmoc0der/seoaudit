from __future__ import annotations

import base64
import mimetypes
from functools import cache
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from jinja2 import Environment, PackageLoader, select_autoescape
from markdown_it import MarkdownIt
from markupsafe import Markup

from seoaudit.core.config import ReportConfig
from seoaudit.core.i18n import pick
from seoaudit.core.models import AuditReport, RuleStatus, Severity
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
    env.filters["pretty_url"] = pretty_url
    env.filters["score_class"] = score_class
    env.filters["plural"] = plural
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


def score_class(score: int | None) -> str:
    if score is None:
        return "na"
    return "good" if score >= 90 else "average" if score >= 50 else "poor"


def render_html(report: AuditReport, config: ReportConfig) -> str:
    template = _environment().get_template("report.html.j2")
    css = Path(__file__).with_name("templates").joinpath("report.css").read_text(encoding="utf-8")
    return template.render(
        report=report,
        config=config,
        t=_strings(report.lang),
        css=Markup(css),
        host=pretty_url(urlsplit(report.final_url).hostname or report.final_url),
        category_names={c.id: c.name for c in report.categories},
        logo=_logo_data_uri(report.meta.logo),
        Severity=Severity,
        Status=RuleStatus,
        severity_counts={s: report.count_failed(s) for s in Severity},
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
