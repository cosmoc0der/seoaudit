# seoaudit

Modular website SEO auditor: a crawler, check plugins and a PDF report with recommendations
and developer instructions.

- **140 rules in 12 modules**: indexability, meta tags, content, links, images, performance,
  PageSpeed/Core Web Vitals, mobile, security, HTML validity, structured data, URLs, hreflang.
- **Rules are data.** Thresholds, severities and texts live in YAML next to each module and
  can be overridden in the config without touching code.
- **Reports:** PDF, HTML, JSON (the full model, for an API/SaaS), CSV (for developers and Excel).
  The PDF opens with an executive summary and a work plan; recommendations can be turned off (`--no-recommendations`).

## Installation

```bash
python -m venv .venv
.venv\Scripts\activate            # Linux/macOS: source .venv/bin/activate
pip install -e ".[browser,dev]"
playwright install --only-shell chromium   # headless Chromium: PDF and local Lighthouse
```

The `browser` extra (Playwright) is only needed for PDF. Without it the audit still runs and writes HTML, JSON and CSV.
If PDF is among the default formats, HTML is written instead. If PDF is requested explicitly
(`-f pdf`), the CLI tells you what to install right away, before crawling.

## Usage

```bash
seoaudit audit https://example.com                         # PDF + JSON in ./reports
seoaudit audit example.com -c config.yaml -f pdf,html,csv --max-pages 500
seoaudit audit example.com -d pagespeed,html               # disable modules
seoaudit render reports/example.com-20250905-1200.json -f pdf   # rebuild a report without recrawling
seoaudit render reports/example.com-20250905-1200.json -f pdf --no-recommendations   # findings only, no recommendations (file gets a -findings suffix)
seoaudit rules                                              # all rules with current thresholds
seoaudit checks                                             # loaded modules
```

### Performance: PageSpeed API or local Lighthouse

The `pagespeed` module takes Lighthouse data from one of two sources (`checks.pagespeed.options.provider`):

| | `api` — PageSpeed Insights | `local` — Lighthouse on this machine |
|---|---|---|
| Requires | a free `PAGESPEED_API_KEY` | Node.js 22+ |
| Real-user data (CrUX) | yes | no, the `field_cwv` rule is marked "not applicable" |
| Private sites (staging, localhost, VPN) | no | yes |
| Speed | several pages in parallel | ~25 s per page, strictly one at a time |
| Score stability | same Google servers every time | depends on machine load |

The default is `provider: auto`: the API when a key is set, local Lighthouse otherwise.
Lighthouse is looked up in the global install (`npm i -g lighthouse`). If it is not there, it is installed
into `~/.cache/seoaudit/lighthouse` on first run; the `SEOAUDIT_CACHE` variable sets another folder.
Chromium comes from Playwright, `CHROME_PATH` or the system Chrome. When running in Docker as root,
add `chrome_flags: "--no-sandbox"`.

### HTML validation: local or via W3C

The `html` module checks markup with the Nu Html Checker (vnu), the same engine that runs
validator.w3.org. By default (`validator: auto`) vnu runs locally: on first run a build with bundled
Java (~65 MB, checksum verified) is downloaded into `~/.cache/seoaudit/vnu`.
Locally, all crawled pages are checked in a single run, with no network and no limits.
If vnu is unavailable, pages are sent to `validator_url` (W3C by default, 5 pages with a pause between them).
A custom build or `vnu.jar` can be set in `vnu`, and `validator: online` turns local mode off.

### Structured data: Schema.org vocabulary and Google requirements

The `structured_data` module parses JSON-LD and Microdata and checks them offline the way validator.schema.org does:
unknown types and properties (with a suggestion for typos), objects of the wrong type, invalid enumeration
values (`availability`, `eventStatus` and so on), a missing `@context`. On top of that it checks
the required and recommended properties of Google rich results (as Rich Results Test errors and warnings)
and flags markup for features Google has switched off (FAQ, HowTo).
The vocabulary lives in `checks/structured_data/vocab.json`; to update it to the latest Schema.org release, run
`python scripts/update_schema_vocab.py`.

From Python:

```python
from pathlib import Path

from seoaudit import Auditor, load_config
from seoaudit.reporting import render_report

config = load_config("config.yaml")
report = Auditor(config, on_event=print).run_sync("https://example.com")
render_report(report, Path("reports"), config.report, ["pdf", "json"])
```

## Architecture

```
             ┌──────────── core ────────────────────────────────────────┐
URL ──► discover_site (robots.txt, sitemap) ──► Crawler (async, httpx/HTTP2)
             │                                       │ every HTML page
             │                                       ▼
             │                         Check.check_page(page)   ← all plugins in parallel
             │                                       │
             │                  after the crawl: Check.check_site() (aggregates, resource probes, APIs)
             │                                       ▼
             │      FindingSink ──► report_builder (texts, scoring) ──► AuditReport (pydantic)
             └───────────────────────────────────────────────────────────┘
                                                     │
                                  reporting: json · csv · html (Jinja2) · pdf (Chromium)
```

| Module | Purpose |
|---|---|
| `core/crawler.py` | Async crawl: robots.txt, depth, limits, redirect chains, pages from the sitemap |
| `core/page.py` | A page plus lazy extraction (title, meta, links, images, headings, JSON-LD, text) |
| `core/check.py` | Plugin base class: `setup` / `check_page` / `check_site`, `fail()` / `params()` |
| `core/rules.py` | Loads `rules.yaml`, merges config overrides, localizes texts |
| `core/probe.py` | Cached HEAD/GET resource probes (broken links, image weight) |
| `core/scoring.py` | Category scores: severity weight × share of affected pages |
| `core/events.py` | Progress events (the CLI now, a WebSocket/queue in a SaaS) |
| `checks/*` | Check plugins, each a folder with `__init__.py` and `rules.yaml` |
| `reporting/*` | Report renderers, UI strings in `reporting/i18n/*.yaml` |

A page's HTML is released once it has been checked, so memory does not grow linearly with site size.
Aggregates (duplicates, the link graph) need only compact data.

## Changing rules

Any rule's threshold, severity, category and on/off state can be changed in the config:

```yaml
rules:
  images.too_large:
    params: { max_kb: 500 }      # was 200; the report text follows: "more than 500 KB"
    severity: high
  meta.twitter_card_missing:
    enabled: false
```

A typo in a rule id or parameter name is a config error right away, not silently ignored.

## Writing a check module

A `my_checks/brand/` folder with two files:

```python
# __init__.py
from seoaudit.core.check import Check

class BrandCheck(Check):
    id = "brand"

    async def check_page(self, page):
        if self.params("brand_in_title")["brand"] not in (page.title or ""):
            self.fail("brand_in_title", page.url, title=page.title)
```

```yaml
# rules.yaml
category: meta
rules:
  brand_in_title:
    severity: low
    params: { brand: "ShopName" }
    text:
      en:
        title: The title does not contain the brand name
        description: ...
        recommendation: Add "$brand" to the end of the title.
        message: "Title: \"$title\""
        fix: |
          Markdown with code samples for the developer
```

To load it, set `plugin_paths: [./my_checks]` in the config or register a `seoaudit.checks` entry point in a pip package.
A complete example is in [examples/plugins/analytics](examples/plugins/analytics).

Texts can use the rule's `$params`; `message` can also use the finding data passed to `fail()`.
`message` can be a dict of variants: `fail(..., key="short")` picks `message.short`.
Texts are keyed by language (`ru`, `en`, …); the report uses the one matching `--lang` and falls back to English.

## Built on open source

- **robots.txt protocol:** `protego` (the Scrapy parser, following Google's spec).
- **Microdata:** `extruct`; the vocabulary is the official Schema.org release.
- **HTML parsing:** `selectolax` (Lexbor), dozens of times faster than BeautifulSoup.
- **Safe XML for sitemaps:** `defusedxml`.
- **Lighthouse and CrUX:** the PageSpeed Insights API or local Lighthouse (Node.js) on Playwright's Chromium.
- **HTML validation:** Nu Html Checker (vnu), locally or via validator.w3.org.
- **Check ideas:** SEOnaut, SiteOne Crawler, Screaming Frog, Lighthouse SEO, OWASP Secure Headers.

## Roadmap to a SaaS

The groundwork is in place:
- `Auditor` is a library with no global state.
- The result is a serializable pydantic model.
- There are progress events and report rendering from saved JSON.
- The config can be set per client.

Next steps:
1. FastAPI + a queue (arq/Celery): `POST /audits` creates a job, a worker calls `Auditor.run()`,
   events go to Redis pub/sub → WebSocket.
2. Storage: PostgreSQL (`AuditReport` in JSONB), PDFs in S3.
3. Multi-tenancy: a rule config per organization (white-label via `report.meta`).
4. Rendering JS sites (SPAs) through Playwright as an alternative crawler fetcher.
5. Comparing audits over time (diff of findings by `rule_id` + `url`), scheduled monitoring.
6. Checking structured data against visible content: product price, name and availability in JSON-LD
   must match the page text, or Google may remove rich results for misleading markup.
   Start with Product/Offer and ratings.

## Tests

```bash
pytest
```

The E2E test runs a full audit offline against a mock site (`httpx.MockTransport`).

## License

[MIT](LICENSE)
