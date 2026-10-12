from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from seoaudit.core.models import ReportMeta, Severity


class ConfigError(ValueError):
    pass


_USER_AGENTS = {
    "mobile": (
        "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Mobile Safari/537.36 (compatible; SEOAuditBot/0.1)"
    ),
    "desktop": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36 (compatible; SEOAuditBot/0.1)"
    ),
}


class CrawlConfig(BaseModel):
    max_pages: int = 300
    max_depth: int = 10
    concurrency: int = 8
    timeout: float = 20.0
    delay: float = 0.0
    device: Literal["mobile", "desktop"] = "mobile"
    user_agent: str | None = None
    robots_agent: str = "Googlebot"
    respect_robots: bool = True
    use_sitemap: bool = True
    follow_nofollow: bool = True
    include_subdomains: bool = False
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    strip_params: list[str] = Field(
        # Not "from": sites use it as a real pagination offset or date filter.
        default_factory=lambda: ["utm_*", "gclid", "yclid", "fbclid", "_openstat"]
    )
    max_body_mb: float = 10.0
    max_sitemap_urls: int = 50_000

    @property
    def effective_user_agent(self) -> str:
        # Sites with dynamic serving return different HTML per device; mobile mirrors mobile-first indexing
        return self.user_agent or _USER_AGENTS[self.device]


class CheckConfig(BaseModel):
    enabled: bool = True
    options: dict[str, Any] = Field(default_factory=dict)


class RuleOverride(BaseModel):
    enabled: bool | None = None
    severity: Severity | None = None
    category: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class ReportConfig(BaseModel):
    formats: list[str] = Field(default_factory=lambda: ["pdf", "json"])
    max_urls_per_rule: int = 10
    include_passed: bool = True
    include_recommendations: bool = True
    meta: ReportMeta = Field(default_factory=ReportMeta)


class AuditConfig(BaseModel):
    lang: str = "ru"
    crawl: CrawlConfig = Field(default_factory=CrawlConfig)
    checks: dict[str, CheckConfig] = Field(default_factory=dict)
    rules: dict[str, RuleOverride] = Field(default_factory=dict)
    report: ReportConfig = Field(default_factory=ReportConfig)
    plugin_paths: list[Path] = Field(default_factory=list)

    def check(self, check_id: str) -> CheckConfig:
        return self.checks.get(check_id) or CheckConfig()


def load_config(path: str | Path | None = None, **overrides: Any) -> AuditConfig:
    data: dict[str, Any] = {}
    if path:
        path = Path(path)
        if not path.is_file():
            raise ConfigError(f"Config file not found: {path}")
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        data["plugin_paths"] = [
            (path.parent / p).resolve() for p in data.get("plugin_paths", [])
        ]
    return AuditConfig.model_validate(_deep_merge(data, overrides))


def _deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result
