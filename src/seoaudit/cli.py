from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from seoaudit.core.config import AuditConfig, ConfigError, load_config
from seoaudit.core.engine import AuditError, Auditor
from seoaudit.core.events import Event, EventType
from seoaudit.core.models import AuditReport, RuleStatus
from seoaudit.reporting import render_report
from seoaudit.reporting.data import load_report

app = typer.Typer(help="Modular SEO audit.", no_args_is_help=True, add_completion=False)
console = Console()


ConfigOption = Annotated[Optional[Path], typer.Option("--config", "-c", help="YAML config file.")]


def _split(values: list[str] | None) -> list[str]:
    return [v.strip() for item in values or [] for v in item.split(",") if v.strip()]


def _load(config: Path | None, **overrides) -> AuditConfig:
    try:
        return load_config(config, **{k: v for k, v in overrides.items() if v})
    except (ConfigError, ValueError) as exc:
        console.print(f"[red]Config error:[/] {exc}")
        raise typer.Exit(2)


@app.command()
def audit(
    url: Annotated[str, typer.Argument(help="Site URL, e.g. https://example.com")],
    config: ConfigOption = None,
    out: Annotated[Path, typer.Option("--out", "-o", help="Output directory.")] = Path("reports"),
    formats: Annotated[Optional[list[str]], typer.Option("--format", "-f", help="json, csv")] = None,
    max_pages: Annotated[Optional[int], typer.Option(help="Crawl limit.")] = None,
    lang: Annotated[Optional[str], typer.Option(help="Report language.")] = None,
    disable: Annotated[Optional[list[str]], typer.Option("--disable", "-d", help="Checks to skip, e.g. pagespeed,html")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Crawl a site, run all checks and write reports."""
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    overrides = {
        "lang": lang,
        "crawl": {"max_pages": max_pages} if max_pages else None,
        "checks": {check: {"enabled": False} for check in _split(disable)} or None,
    }
    cfg = _load(config, **overrides)
    formats = _split(formats) or cfg.report.formats

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TextColumn("[dim]{task.fields[detail]}"),
        console=console,
        transient=True,
    ) as progress:
        crawl = progress.add_task("Crawling", total=cfg.crawl.max_pages, detail="")
        checks = progress.add_task("Checks", total=None, visible=False, detail="")

        def on_event(event: Event) -> None:
            if event.type == EventType.PAGE_CRAWLED:
                progress.update(crawl, completed=event.current, detail=event.message[-70:])
            elif event.type == EventType.CRAWL_FINISHED:
                progress.update(crawl, completed=event.current, total=event.current, detail="")
            elif event.type == EventType.CHECK_STARTED:
                progress.update(checks, visible=True, total=event.total, completed=event.current - 1, detail=event.message)
            elif event.type == EventType.CHECK_FINISHED:
                progress.update(checks, completed=event.current)

        try:
            report = Auditor(cfg, on_event=on_event).run_sync(url)
        except (AuditError, ConfigError) as exc:
            console.print(f"[red]Audit failed:[/] {exc}")
            raise typer.Exit(1)

    _print_summary(report)
    _write(report, out, cfg, formats)


@app.command()
def render(
    report_json: Annotated[Path, typer.Argument(help="Report JSON produced by `audit`.")],
    config: ConfigOption = None,
    out: Annotated[Path, typer.Option("--out", "-o")] = Path("reports"),
    formats: Annotated[Optional[list[str]], typer.Option("--format", "-f")] = None,
) -> None:
    """Re-render reports from a saved JSON without re-crawling."""
    cfg = _load(config)
    _write(load_report(report_json), out, cfg, _split(formats) or ["json"])


@app.command()
def rules(
    config: ConfigOption = None,
    check: Annotated[Optional[str], typer.Option(help="Filter by check id.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """List all rules with effective severity and params (after config overrides)."""
    cfg = _load(config)
    plans = Auditor(cfg).plan()
    specs = [r for p in plans for r in p.rules.values() if not check or r.check == check]
    if as_json:
        console.print_json(json.dumps([s.model_dump(exclude={"text"}) for s in specs], default=str))
        return

    table = Table(show_lines=False, header_style="bold")
    for column in ("rule", "severity", "category", "enabled", "params", "title"):
        table.add_column(column)
    for spec in specs:
        text = spec.localized(cfg.lang)
        params = ", ".join(f"{k}={v}" for k, v in spec.params.items() if not isinstance(v, (dict, list)) or len(str(v)) < 40)
        table.add_row(spec.id, spec.severity.value, spec.category, "yes" if spec.enabled else "[red]no", params, text.title)
    console.print(table)
    console.print(f"[dim]{len(specs)} rules in {len({s.check for s in specs})} checks[/]")


@app.command()
def checks(config: ConfigOption = None) -> None:
    """List available check plugins."""
    cfg = _load(config)
    table = Table(header_style="bold")
    for column in ("check", "module", "rules", "enabled"):
        table.add_column(column)
    for plan in Auditor(cfg).plan():
        enabled = cfg.check(plan.cls.id).enabled
        table.add_row(plan.cls.id, plan.cls.__module__, str(len(plan.active_rules)), "yes" if enabled else "[red]no")
    console.print(table)


def _write(report: AuditReport, out: Path, cfg: AuditConfig, formats: list[str]) -> None:
    try:
        paths = render_report(report, out, cfg.report, formats)
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2)
    for path in paths:
        console.print(f"[green]✓[/] {path}")


def _print_summary(report: AuditReport) -> None:
    color = "green" if report.score >= 90 else "yellow" if report.score >= 50 else "red"
    console.print(
        f"\n[bold]{report.final_url}[/]  score [bold {color}]{report.score}[/] ({report.grade}) · "
        f"pages: {report.stats.html_pages} · failed rules: {report.count(RuleStatus.FAILED)}/{len(report.rules())}"
    )
    table = Table(header_style="bold", box=None)
    table.add_column("category")
    table.add_column("score", justify="right")
    table.add_column("issues", justify="right")
    for category in report.categories:
        score = "—" if category.score is None else str(category.score)
        table.add_row(category.name, score, str(len(category.failed)))
    console.print(table)
