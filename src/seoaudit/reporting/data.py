from __future__ import annotations

import csv
from pathlib import Path

from seoaudit.core.config import ReportConfig
from seoaudit.core.models import AuditReport, RuleStatus
from seoaudit.core.urls import pretty_url


def write_json(report: AuditReport, path: Path, config: ReportConfig) -> None:
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")


def write_csv(report: AuditReport, path: Path, config: ReportConfig) -> None:
    # utf-8-sig so Excel opens Cyrillic correctly
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh, delimiter=";")
        writer.writerow(["category", "rule_id", "severity", "rule", "url", "details"])
        for category in report.categories:
            for rule in category.rules:
                if rule.status != RuleStatus.FAILED:
                    continue
                for finding in rule.findings:
                    writer.writerow(
                        [
                            category.name,
                            rule.id,
                            rule.severity.value,
                            rule.title,
                            pretty_url(finding.url) if finding.url else "",
                            finding.message or "",
                        ]
                    )


def load_report(path: Path) -> AuditReport:
    return AuditReport.model_validate_json(path.read_text(encoding="utf-8"))
