from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from seoaudit.core.config import ReportConfig
from seoaudit.core.models import AuditReport
from seoaudit.reporting.data import write_csv, write_json

Writer = Callable[[AuditReport, Path, ReportConfig], None]

WRITERS: dict[str, tuple[str, Writer]] = {
    "json": ("json", write_json),
    "csv": ("csv", write_csv),
}


def report_basename(report: AuditReport) -> str:
    host = urlsplit(report.final_url).hostname or "site"
    return f"{host}-{report.started_at:%Y%m%d-%H%M}"


def render_report(report: AuditReport, out_dir: Path, config: ReportConfig, formats: list[str] | None = None) -> list[Path]:
    formats = formats or config.formats
    unknown = set(formats) - set(WRITERS)
    if unknown:
        raise ValueError(f"Unknown report formats: {', '.join(sorted(unknown))}. Available: {', '.join(WRITERS)}")

    out_dir.mkdir(parents=True, exist_ok=True)
    base = report_basename(report)
    written = []
    for fmt in formats:
        extension, writer = WRITERS[fmt]
        path = out_dir / f"{base}.{extension}"
        writer(report, path, config)
        written.append(path)
    return written
