from __future__ import annotations

from pathlib import Path
from string import Template
from typing import Any

import yaml
from pydantic import BaseModel, Field

from seoaudit.core.config import AuditConfig, ConfigError
from seoaudit.core.i18n import pick
from seoaudit.core.models import RuleScope, Severity


class RuleText(BaseModel):
    title: str
    description: str = ""
    recommendation: str = ""
    fix: str = ""
    message: str | dict[str, str] | None = None

    def render(self, params: dict[str, Any]) -> RuleText:
        """Substitutes params into static texts; messages are formatted per finding later."""
        values = {k: _fmt(v) for k, v in params.items()}
        sub = lambda s: Template(s).safe_substitute(values)  # noqa: E731
        return RuleText(
            title=sub(self.title),
            description=sub(self.description),
            recommendation=sub(self.recommendation),
            fix=sub(self.fix),
            message=self.message,
        )

    def format_message(self, key: str | None, data: dict[str, Any]) -> str | None:
        template = self.message
        if isinstance(template, dict):
            template = template.get(key or "default") or template.get("default")
        if not template:
            return None
        return Template(template).safe_substitute({k: _fmt(v) for k, v in data.items()})


class RuleSpec(BaseModel):
    id: str
    check: str
    category: str
    severity: Severity = Severity.MEDIUM
    scope: RuleScope = RuleScope.PAGE
    enabled: bool = True
    params: dict[str, Any] = Field(default_factory=dict)
    refs: list[str] = Field(default_factory=list)
    text: dict[str, RuleText] = Field(default_factory=dict)

    @property
    def local_id(self) -> str:
        return self.id.split(".", 1)[1]

    def localized(self, lang: str) -> RuleText:
        text = pick(self.text, lang) or RuleText(title=self.id)
        return text.render(self.params)


class CheckDefinition(BaseModel):
    """Contents of a check's rules.yaml."""

    category: str
    categories: dict[str, dict[str, Any]] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)
    rules: dict[str, dict[str, Any]] = Field(default_factory=dict)


def load_definition(path: Path) -> CheckDefinition:
    if not path.is_file():
        raise ConfigError(f"Rules file not found: {path}")
    return CheckDefinition.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def build_rules(check_id: str, definition: CheckDefinition, config: AuditConfig) -> dict[str, RuleSpec]:
    """Merges rule defaults from the plugin with user overrides from the config."""
    rules: dict[str, RuleSpec] = {}
    for local_id, raw in definition.rules.items():
        rule_id = f"{check_id}.{local_id}"
        spec = RuleSpec.model_validate({"category": definition.category, **raw, "id": rule_id, "check": check_id})
        override = config.rules.get(rule_id)
        if override:
            unknown = set(override.params) - set(spec.params)
            if unknown:
                raise ConfigError(f"Unknown params for rule {rule_id}: {', '.join(sorted(unknown))}")
            spec = spec.model_copy(
                update={
                    "enabled": spec.enabled if override.enabled is None else override.enabled,
                    "severity": override.severity or spec.severity,
                    "category": override.category or spec.category,
                    "params": {**spec.params, **override.params},
                }
            )
        rules[local_id] = spec
    return rules


def validate_overrides(config: AuditConfig, known_rule_ids: set[str]) -> None:
    unknown = set(config.rules) - known_rule_ids
    if unknown:
        raise ConfigError(f"Config references unknown rules: {', '.join(sorted(unknown))}")


def _fmt(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (list, tuple, set)):
        return ", ".join(map(str, value))
    return str(value)
