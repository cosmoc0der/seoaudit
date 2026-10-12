from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from time import perf_counter

import httpx

from seoaudit.core.check import Check, FindingSink
from seoaudit.core.config import AuditConfig
from seoaudit.core.context import AuditContext
from seoaudit.core.crawler import Crawler
from seoaudit.core.events import Event, EventHandler, EventType
from seoaudit.core.http import create_client
from seoaudit.core.models import AuditReport, RuleStatus
from seoaudit.core.page import Page
from seoaudit.core.probe import ResourceProber
from seoaudit.core.registry import discover_checks
from seoaudit.core.report_builder import build_report
from seoaudit.core.rules import CheckDefinition, RuleSpec, build_rules, load_definition, validate_overrides
from seoaudit.core.site import discover_site
from seoaudit.core.urls import UrlScope, host_of, normalize_url

log = logging.getLogger(__name__)

_TLS_ERROR_MARKERS = ("certificate", "ssl", "tls")


class AuditError(RuntimeError):
    pass


@dataclass
class _Plan:
    cls: type[Check]
    definition: CheckDefinition
    rules: dict[str, RuleSpec]

    @property
    def active_rules(self) -> list[RuleSpec]:
        return [r for r in self.rules.values() if r.enabled]


class Auditor:
    """Runs a full audit: discovery -> crawl with page checks -> site checks -> report."""

    def __init__(
        self,
        config: AuditConfig | None = None,
        on_event: EventHandler | None = None,
        checks: dict[str, type[Check]] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.config = config or AuditConfig()
        self._on_event = on_event
        self._transport = transport
        self._catalog = checks if checks is not None else discover_checks(self.config.plugin_paths)

    def plan(self) -> list[_Plan]:
        plans = []
        for check_id, cls in sorted(self._catalog.items()):
            definition = load_definition(cls.rules_file())
            plans.append(_Plan(cls, definition, build_rules(check_id, definition, self.config)))
        validate_overrides(self.config, {r.id for p in plans for r in p.rules.values()})
        return plans

    def run_sync(self, url: str) -> AuditReport:
        return asyncio.run(self.run(url))

    async def run(self, url: str) -> AuditReport:
        from seoaudit import __version__

        started_at = datetime.now().astimezone()
        target = _with_scheme(url)
        plans = [p for p in self.plan() if self.config.check(p.cls.id).enabled and p.active_rules]
        self._emit(EventType.AUDIT_STARTED, target)

        cfg = self.config.crawl
        client = create_client(cfg, transport=self._transport)
        try:
            scope = self._scope(target)
            site = await discover_site(client, target, cfg, scope)
            if site.home.error and any(m in site.home.error.lower() for m in _TLS_ERROR_MARKERS):
                tls_error = site.home.error
                await client.aclose()
                client = create_client(cfg, verify=False, transport=self._transport)
                site = await discover_site(client, target, cfg, scope)
                site.tls_error = tls_error
            if site.home.status is None:
                raise AuditError(f"Site is unreachable: {site.home.error}")

            scope = self._scope(site.base_url)
            site.home.final_url = scope.normalize(site.home.final_url) or site.home.final_url
            self._emit(EventType.SITE_DISCOVERED, site.base_url, data={"sitemap_urls": len(site.sitemap_urls)})

            ctx = AuditContext(
                config=self.config,
                client=client,
                site=site,
                scope=scope,
                prober=ResourceProber(client, concurrency=cfg.concurrency * 2),
            )
            sink = FindingSink()
            checks = [
                p.cls(ctx, p.rules, {**p.definition.options, **self.config.check(p.cls.id).options}, sink)
                for p in plans
            ]
            for check in checks:
                await check.setup()

            crawl_started = perf_counter()
            await Crawler(ctx, lambda page: self._check_page(ctx, checks, sink, page)).run()
            crawl_seconds = perf_counter() - crawl_started
            self._emit(EventType.CRAWL_FINISHED, current=len(ctx.pages))

            for index, check in enumerate(checks, 1):
                self._emit(EventType.CHECK_STARTED, check.id, current=index, total=len(checks))
                await self._check_site(ctx, check, sink)
                self._emit(EventType.CHECK_FINISHED, check.id, current=index, total=len(checks))
        finally:
            await client.aclose()

        report = build_report(
            ctx=ctx,
            specs=[r for p in plans for r in p.active_rules],
            sink=sink,
            target=target,
            started_at=started_at,
            crawl_seconds=crawl_seconds,
            version=__version__,
            extra_categories={k: v for p in plans for k, v in p.definition.categories.items()},
        )
        self._emit(EventType.AUDIT_FINISHED, data={"score": report.score})
        return report

    def _scope(self, url: str) -> UrlScope:
        cfg = self.config.crawl
        return UrlScope(
            root_host=host_of(url),
            include_subdomains=cfg.include_subdomains,
            strip_params=tuple(p.lower() for p in cfg.strip_params),
            include=cfg.include,
            exclude=cfg.exclude,
        )

    async def _check_page(self, ctx: AuditContext, checks: list[Check], sink: FindingSink, page: Page) -> None:
        if page.ok:
            results = await asyncio.gather(*(c.check_page(page) for c in checks), return_exceptions=True)
            for check, result in zip(checks, results):
                if isinstance(result, Exception):
                    log.warning("Check %s failed on %s: %r", check.id, page.url, result)
                    ctx.diagnostics.append(f"{check.id}: {page.url}: {result!r}")
                    # Without findings the rules would read as passed, though the page was never checked.
                    _mark_error(check, sink, f"{page.url}: {result!r}")
        page.release()
        self._emit(EventType.PAGE_CRAWLED, page.url, current=len(ctx.pages), total=self.config.crawl.max_pages)

    async def _check_site(self, ctx: AuditContext, check: Check, sink: FindingSink) -> None:
        try:
            await check.check_site()
        except Exception as exc:
            log.exception("Check %s failed", check.id)
            ctx.diagnostics.append(f"{check.id}: {exc!r}")
            _mark_error(check, sink, repr(exc))

    def _emit(self, type_: EventType, message: str = "", **kwargs) -> None:
        if self._on_event:
            self._on_event(Event(type=type_, message=message, **kwargs))


def _mark_error(check: Check, sink: FindingSink, note: str) -> None:
    for spec in check.rules.values():
        if spec.enabled and spec.id not in sink.findings and spec.id not in sink.statuses:
            sink.mark(spec.id, RuleStatus.ERROR, note)


def _with_scheme(url: str) -> str:
    url = url.strip()
    if "://" not in url:
        url = f"https://{url}"
    normalized = normalize_url(url)
    if not normalized:
        raise AuditError(f"Invalid URL: {url}")
    return normalized
