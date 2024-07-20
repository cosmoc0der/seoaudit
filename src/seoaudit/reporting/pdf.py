from __future__ import annotations

from html import escape
from pathlib import Path
from urllib.parse import urlsplit

from seoaudit.core.config import ReportConfig
from seoaudit.core.models import AuditReport
from seoaudit.reporting.html import render_html

_FOOTER = """
<div style="width:100%;font-size:8px;color:#94a3b8;padding:0 14mm;display:flex;justify-content:space-between;
            font-family:'Segoe UI',Roboto,Arial,sans-serif;">
  <span>{label}</span>
  <span><span class="pageNumber"></span> / <span class="totalPages"></span></span>
</div>
"""


def write_pdf(report: AuditReport, path: Path, config: ReportConfig) -> None:
    from playwright.sync_api import sync_playwright

    html = render_html(report, config)
    label = urlsplit(report.final_url).hostname or report.final_url
    if report.meta.author:
        label = f"{report.meta.author} · {label}"

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content(html, wait_until="load")
            page.emulate_media(media="print")
            page.pdf(
                path=str(path),
                format="A4",
                print_background=True,
                display_header_footer=True,
                header_template="<span></span>",
                footer_template=_FOOTER.format(label=escape(label)),
                margin={"top": "14mm", "bottom": "16mm", "left": "0", "right": "0"},
            )
        finally:
            browser.close()
