from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, ClassVar

from seoaudit.core.context import AuditContext
from seoaudit.core.models import Finding, RuleStatus
from seoaudit.core.page import Page
from seoaudit.core.rules import RuleSpec


class FindingSink:
    """Collects findings and explicit rule statuses emitted by checks."""

    def __init__(self, max_findings_per_rule: int = 5000):
        self.findings: dict[str, list[Finding]] = defaultdict(list)
        self.statuses: dict[str, tuple[RuleStatus, str | None]] = {}
        self._limit = max_findings_per_rule
        self.truncated: set[str] = set()

    def add(self, rule_id: str, finding: Finding) -> None:
        bucket = self.findings[rule_id]
        if len(bucket) >= self._limit:
            self.truncated.add(rule_id)
            return
        bucket.append(finding)

    def mark(self, rule_id: str, status: RuleStatus, note: str | None = None) -> None:
        self.statuses[rule_id] = (status, note)


class Check:
    """Base class for check plugins.

    A check is a package with an ``__init__.py`` holding the subclass and a ``rules.yaml``
    describing its rules (severity, params, texts). Hooks:

    * ``setup`` - once before crawling;
    * ``check_page`` - for every successfully fetched HTML page, while crawling;
    * ``check_site`` - once after crawling, for site-wide and aggregated rules.
    """

    id: ClassVar[str]

    def __init__(self, ctx: AuditContext, rules: dict[str, RuleSpec], options: dict[str, Any], sink: FindingSink):
        self.ctx = ctx
        self.rules = rules
        self.options = options
        self._sink = sink

    @classmethod
    def rules_file(cls) -> Path:
        module_file = Path(sys.modules[cls.__module__].__file__)
        if module_file.name == "__init__.py":
            return module_file.with_name("rules.yaml")
        return module_file.with_suffix(".rules.yaml")

    async def setup(self) -> None:
        pass

    async def check_page(self, page: Page) -> None:
        pass

    async def check_site(self) -> None:
        pass

    def enabled(self, rule: str) -> bool:
        return self._rule(rule).enabled

    def params(self, rule: str) -> dict[str, Any]:
        return self._rule(rule).params

    def fail(self, rule: str, url: str | None = None, key: str | None = None, **data: Any) -> None:
        spec = self._rule(rule)
        if spec.enabled:
            self._sink.add(spec.id, Finding(url=url, key=key, data=data))

    def not_applicable(self, rule: str, note: str | None = None) -> None:
        self._sink.mark(self._rule(rule).id, RuleStatus.NOT_APPLICABLE, note)

    def skip(self, rule: str, note: str | None = None) -> None:
        self._sink.mark(self._rule(rule).id, RuleStatus.SKIPPED, note)

    def error(self, rule: str, note: str) -> None:
        self._sink.mark(self._rule(rule).id, RuleStatus.ERROR, note)

    def _rule(self, rule: str) -> RuleSpec:
        try:
            return self.rules[rule]
        except KeyError:
            raise KeyError(f"Check '{self.id}' has no rule '{rule}' in {self.rules_file().name}") from None
