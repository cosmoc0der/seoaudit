from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"

    @property
    def weight(self) -> int:
        return _SEVERITY_WEIGHTS[self]

    @property
    def rank(self) -> int:
        return list(Severity).index(self)


_SEVERITY_WEIGHTS = {
    Severity.CRITICAL: 10,
    Severity.HIGH: 6,
    Severity.MEDIUM: 3,
    Severity.LOW: 1,
    Severity.INFO: 0,
}


class RuleStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_APPLICABLE = "not_applicable"
    SKIPPED = "skipped"
    ERROR = "error"


class RuleScope(StrEnum):
    PAGE = "page"
    SITE = "site"


class Finding(BaseModel):
    url: str | None = None
    key: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    message: str | None = None


class RuleResult(BaseModel):
    id: str
    check: str
    category: str
    severity: Severity
    scope: RuleScope
    status: RuleStatus
    title: str
    description: str = ""
    recommendation: str = ""
    fix: str = ""
    refs: list[str] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    note: str | None = None

    @property
    def affected(self) -> int:
        return len({f.url for f in self.findings if f.url}) or len(self.findings)

    @property
    def pages_affected(self) -> int:
        """For scoring: a finding about a target URL (broken link, image) counts the pages carrying it.

        data["sources"] holds only a sample while data["pages"] is the full count, hence the max().
        """
        pages: set[str] = set()
        largest = 0
        for finding in self.findings:
            count = finding.data.get("pages")
            if isinstance(count, int):
                pages.update(finding.data.get("sources", ()))
                largest = max(largest, count)
            elif finding.url:
                pages.add(finding.url)
        return max(len(pages), largest) or len(self.findings)


class CategoryResult(BaseModel):
    id: str
    name: str
    description: str = ""
    weight: float = 1.0
    score: int | None = None
    rules: list[RuleResult] = Field(default_factory=list)

    def count(self, status: RuleStatus) -> int:
        return sum(r.status == status for r in self.rules)

    @property
    def failed(self) -> list[RuleResult]:
        return sorted(
            (r for r in self.rules if r.status == RuleStatus.FAILED),
            key=lambda r: (r.severity.rank, -r.affected),
        )


class PageSummary(BaseModel):
    url: str
    status: int | None
    depth: int | None
    title: str | None = None
    ttfb_ms: float | None = None
    size: int | None = None
    source: str = "link"
    error: str | None = None


class CrawlStats(BaseModel):
    pages_total: int = 0
    html_pages: int = 0
    error_pages: int = 0
    redirects: int = 0
    blocked_by_robots: int = 0
    sitemap_urls: int = 0
    duration_s: float = 0.0


class ReportMeta(BaseModel):
    title: str | None = None
    author: str | None = None
    logo: str | None = None


class AuditReport(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    tool_version: str
    target: str
    final_url: str
    lang: str
    started_at: datetime
    finished_at: datetime
    score: int
    grade: str
    stats: CrawlStats
    categories: list[CategoryResult]
    pages: list[PageSummary] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)
    meta: ReportMeta = Field(default_factory=ReportMeta)

    def rules(self) -> list[RuleResult]:
        return [r for c in self.categories for r in c.rules]

    def count(self, status: RuleStatus) -> int:
        return sum(r.status == status for r in self.rules())

    def count_failed(self, *severities: Severity) -> int:
        return sum(r.status == RuleStatus.FAILED and r.severity in severities for r in self.rules())

    def top_issues(self, limit: int = 10) -> list[RuleResult]:
        failed = [r for r in self.rules() if r.status == RuleStatus.FAILED and r.severity != Severity.INFO]
        failed.sort(key=lambda r: (r.severity.rank, -r.affected))
        return failed[:limit]
