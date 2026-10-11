from __future__ import annotations

import asyncio
import os
from typing import Any

from seoaudit.core.check import Check

from .runners import (
    LighthouseError,
    LighthouseUnavailable,
    Measurement,
    PsiRunner,
    QuotaError,
    Runner,
    create_local_runner,
    node_available,
)

_FIELD_METRICS = {
    "LARGEST_CONTENTFUL_PAINT_MS": "LCP",
    "INTERACTION_TO_NEXT_PAINT": "INP",
    "CUMULATIVE_LAYOUT_SHIFT_SCORE": "CLS",
}


class PageSpeedCheck(Check):
    """Lighthouse lab data via the PageSpeed Insights API or a local Lighthouse, plus CrUX field data."""

    id = "pagespeed"

    async def check_site(self) -> None:
        try:
            runner = await self._runner()
        except LighthouseUnavailable as exc:
            self.ctx.diagnostics.append(f"pagespeed: {exc}")
            self._mark_all(self.skip, "lighthouse_unavailable")
            return

        jobs = [(url, strategy) for url in self._sample_urls() for strategy in self.options["strategies"]]
        semaphore = asyncio.Semaphore(runner.concurrency)

        async def run(url: str, strategy: str) -> Measurement | Exception:
            async with semaphore:
                try:
                    measurement = await runner.run(url, strategy)
                    if error := measurement.lighthouse.get("runtimeError"):
                        raise LighthouseError(f"{error.get('code')}: {error.get('message')}")
                    return measurement
                except Exception as exc:
                    return exc

        results = await asyncio.gather(*(run(u, s) for u, s in jobs))
        errors = [r for r in results if isinstance(r, Exception)]
        for (url, strategy), error in zip(jobs, results):
            if isinstance(error, Exception):
                self.ctx.diagnostics.append(f"pagespeed: {runner.name} {strategy} {url}: {error}")
        if len(errors) == len(results):
            if any(isinstance(e, QuotaError) for e in errors):
                self._mark_all(self.skip, "pagespeed_quota")
            else:
                self._mark_all(self.error, f"{runner.name}: {errors[0]}")
            return

        has_field = False
        for (url, strategy), data in zip(jobs, results):
            if not isinstance(data, Exception):
                self._evaluate(url, strategy, data.lighthouse)
                has_field |= self._evaluate_field(url, strategy, data.field)
        if not has_field:
            self.not_applicable("field_cwv", "no_field_data" if runner.has_field_data else "field_data_local")

    async def _runner(self) -> Runner:
        provider = self.options["provider"]
        key = self.options.get("api_key") or os.environ.get("PAGESPEED_API_KEY")
        if provider == "auto":
            provider = "api" if key or not node_available() else "local"
        if provider == "local":
            return await create_local_runner(self.options, self.ctx.config.lang)
        if provider == "api":
            return PsiRunner(self.ctx.client, key, self.ctx.config.lang, self.options["timeout"], self.options["concurrency"])
        raise LighthouseUnavailable(f"Unknown provider '{provider}', expected auto, api or local")

    def _mark_all(self, mark, note: str) -> None:
        for rule in self.rules:
            mark(rule, note)

    def _sample_urls(self) -> list[str]:
        home = self.ctx.site.base_url
        others = sorted(
            (p for p in self.ctx.html_pages() if p.url != home),
            key=lambda p: len(self.ctx.inlinks.get(p.url, [])),
            reverse=True,
        )
        return [home, *(p.url for p in others[: self.options["max_pages"] - 1])]

    def _evaluate(self, url: str, strategy: str, lighthouse: dict[str, Any]) -> None:
        audits = lighthouse.get("audits", {})
        categories = lighthouse.get("categories", {})
        base = {"strategy": strategy}

        score = round((categories.get("performance", {}).get("score") or 0) * 100)
        if score < self.params("performance_score")["min_score"]:
            self.fail("performance_score", url, score=score, opportunities=_opportunities(audits), **base)

        lab = {
            "lcp_slow": ("largest-contentful-paint", "max_ms", lambda v: round(v)),
            "cls_high": ("cumulative-layout-shift", "max_value", lambda v: round(v, 3)),
            "tbt_high": ("total-blocking-time", "max_ms", lambda v: round(v)),
        }
        for rule, (audit_id, param, fmt) in lab.items():
            value = audits.get(audit_id, {}).get("numericValue")
            if value is not None and value > self.params(rule)[param]:
                self.fail(rule, url, value=fmt(value), **base)

        failed_seo = [
            audits[ref["id"]]["title"]
            for ref in categories.get("seo", {}).get("auditRefs", [])
            if ref.get("weight") and audits.get(ref["id"], {}).get("score") == 0
        ]
        if failed_seo:
            self.fail("lighthouse_seo", url, audits=failed_seo, **base)

    def _evaluate_field(self, url: str, strategy: str, field: dict[str, Any]) -> bool:
        metrics = field.get("metrics")
        if not metrics:
            return False
        failing = [
            f"{label}: {metric['category']}"
            for key, label in _FIELD_METRICS.items()
            if (metric := metrics.get(key)) and metric.get("category") not in (None, "FAST")
        ]
        if failing:
            self.fail("field_cwv", url, metrics=failing, strategy=strategy)
        return True


def _opportunities(audits: dict[str, Any], limit: int = 5) -> list[str]:
    # Lighthouse 13 moved most opportunities into insight audits that report metricSavings instead.
    savings = {}
    for audit in audits.values():
        if audit.get("score") is None or audit["score"] >= 0.9:
            continue
        metric = audit.get("metricSavings") or {}
        ms = (audit.get("details") or {}).get("overallSavingsMs") or max(metric.get("LCP", 0), metric.get("FCP", 0))
        if ms > 100:
            savings[audit["title"]] = max(ms, savings.get(audit["title"], 0))
    top = sorted(savings.items(), key=lambda item: item[1], reverse=True)[:limit]
    return [f"{title} (−{ms / 1000:.1f} с)" for title, ms in top]
