from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from seoaudit.core.config import ReportConfig
from seoaudit.core.models import AuditReport
from seoaudit.reporting.data import write_csv, write_json
from seoaudit.reporting.html import write_html
from seoaudit.reporting.pdf import INSTALL_HINT as PDF_HINT, pdf_available, write_pdf

Writer = Callable[[AuditReport, Path, ReportConfig], None]

WRITERS: dict[str, tuple[str, Writer]] = {
    "json": ("json", write_json),
    "csv": ("csv", write_csv),
    "html": ("html", write_html),
    "pdf": ("pdf", write_pdf),
}

_REQUIREMENTS: dict[str, tuple[Callable[[], bool], str]] = {"pdf": (pdf_available, PDF_HINT)}


def unavailable_formats(formats: list[str]) -> dict[str, str]:
    """Formats whose optional dependencies are not installed, mapped to an install hint."""
    missing = {}
    for fmt in formats:
        if fmt in _REQUIREMENTS:
            available, hint = _REQUIREMENTS[fmt]
            if not available():
                missing[fmt] = hint
    return missing


def report_basename(report: AuditReport) -> str:
    host = urlsplit(report.final_url).hostname or "site"
    return f"{host}-{report.started_at:%Y%m%d-%H%M}"


def _basename(report: AuditReport, config: ReportConfig) -> str:
    # Both editions of one audit are often sent together, so they must not overwrite each other.
    base = report_basename(report)
    return base if config.include_recommendations else f"{base}-findings"


def render_report(report: AuditReport, out_dir: Path, config: ReportConfig, formats: list[str] | None = None) -> list[Path]:
    formats = formats or config.formats
    unknown = set(formats) - set(WRITERS)
    if unknown:
        raise ValueError(f"Unknown report formats: {', '.join(sorted(unknown))}. Available: {', '.join(WRITERS)}")
    if missing := unavailable_formats(formats):
        raise ValueError("; ".join(missing.values()))

    out_dir.mkdir(parents=True, exist_ok=True)
    # The report points readers to the CSV for full URL lists only when one is written alongside it.
    config = config.model_copy(update={"formats": formats})
    base = _basename(report, config)
    written = []
    for fmt in formats:
        extension, writer = WRITERS[fmt]
        path = out_dir / f"{base}.{extension}"
        writer(report, path, config)
        written.append(path)
    return written
