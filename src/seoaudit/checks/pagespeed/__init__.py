from __future__ import annotations

import asyncio
import os
from typing import Any

from seoaudit.core.check import Check

_API = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"

_FIELD_METRICS = {
    "LARGEST_CONTENTFUL_PAINT_MS": "LCP",
    "INTERACTION_TO_NEXT_PAINT": "INP",
    "CUMULATIVE_LAYOUT_SHIFT_SCORE": "CLS",
}


class QuotaError(RuntimeError):
    pass


class PageSpeedCheck(Check):
    """Lighthouse lab data and CrUX field data via the PageSpeed Insights API."""

    id = "pagespeed"

    async def check_site(self) -> None:
        jobs = [(url, strategy) for url in self._sample_urls() for strategy in self.options["strategies"]]
        semaphore = asyncio.Semaphore(self.options["concurrency"])

        async def run(url: str, strategy: str) -> dict[str, Any] | Exception:
            async with semaphore:
                try:
                    return await self._fetch(url, strategy)
                except Exception as exc:
                    return exc

        results = await asyncio.gather(*(run(u, s) for u, s in jobs))
        errors = [r for r in results if isinstance(r, Exception)]
        for (url, strategy), error in zip(jobs, results):
            if isinstance(error, Exception):
                self.ctx.diagnostics.append(f"pagespeed: {strategy} {url}: {error}")
        if len(errors) == len(results):
            if any(isinstance(e, QuotaError) for e in errors):
                self._mark_all(self.skip, "pagespeed_quota")
            else:
                self._mark_all(self.error, str(errors[0]))
            return

        has_field = False
        for (url, strategy), data in zip(jobs, results):
            if not isinstance(data, Exception):
                self._evaluate(url, strategy, data.get("lighthouseResult") or {})
                has_field |= self._evaluate_field(url, strategy, data.get("loadingExperience") or {})
        if not has_field:
            self.not_applicable("field_cwv")

    async def _fetch(self, url: str, strategy: str) -> dict[str, Any]:
        lang = self.ctx.config.lang
        params = [("url", url), ("strategy", strategy), ("locale", lang), ("category", "performance"), ("category", "seo")]
        if key := self.options.get("api_key") or os.environ.get("PAGESPEED_API_KEY"):
            params.append(("key", key))
        response = await self.ctx.client.get(_API, params=params, timeout=self.options["timeout"])
        if response.status_code != 200:
            try:
                message = response.json()["error"]["message"]
            except Exception:
                message = response.text[:200]
            error = QuotaError if response.status_code == 429 else RuntimeError
            raise error(f"HTTP {response.status_code}: {message}")
        return response.json()

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
    found = [
        (audit["title"], audit["details"]["overallSavingsMs"])
        for audit in audits.values()
        if (audit.get("details") or {}).get("type") == "opportunity" and audit["details"].get("overallSavingsMs", 0) > 100
    ]
    found.sort(key=lambda item: item[1], reverse=True)
    return [f"{title} (−{ms / 1000:.1f} с)" for title, ms in found[:limit]]
