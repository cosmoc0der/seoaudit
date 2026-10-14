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
    # Codes that checks put into finding data (device, TLS error kind) and their wording.
    terms: dict[str, str] = Field(default_factory=dict)

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
            terms=self.terms,
        )

    def format_message(self, key: str | None, data: dict[str, Any]) -> str | None:
        template = self.message
        if isinstance(template, dict):
            template = template.get(key or "default") or template.get("default")
        if not template:
            return None
        return Template(template).safe_substitute({k: _fmt(self._term(v)) for k, v in data.items()})

    def _term(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.terms.get(value, value)
        if isinstance(value, (list, tuple)):
            return [self.terms.get(v, v) if isinstance(v, str) else v for v in value]
        return value


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
    terms: dict[str, dict[str, str]] = Field(default_factory=dict)

    @property
    def local_id(self) -> str:
        return self.id.split(".", 1)[1]

    def localized(self, lang: str) -> RuleText:
        text = pick(self.text, lang) or RuleText(title=self.id)
        terms = pick(self.terms, lang) or {}
        return text.model_copy(update={"terms": {**terms, **text.terms}}).render(self.params)


class CheckDefinition(BaseModel):
    """Contents of a check's rules.yaml."""

    category: str
    categories: dict[str, dict[str, Any]] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)
    rules: dict[str, dict[str, Any]] = Field(default_factory=dict)
    terms: dict[str, dict[str, str]] = Field(default_factory=dict)


def load_definition(path: Path) -> CheckDefinition:
    """rules.yaml holds the Russian texts; rules.<lang>.yaml next to it adds a translation."""
    if not path.is_file():
        raise ConfigError(f"Rules file not found: {path}")
    definition = CheckDefinition.model_validate(_read_yaml(path))
    for translation in sorted(path.parent.glob(f"{path.stem}.*{path.suffix}")):
        lang = translation.name.removeprefix(f"{path.stem}.").removesuffix(path.suffix)
        data = _read_yaml(translation)
        if unknown := set(data.get("rules", {})) - set(definition.rules):
            raise ConfigError(f"{translation.name} translates unknown rules: {', '.join(sorted(unknown))}")
        for rule_id, text in data.get("rules", {}).items():
            definition.rules[rule_id].setdefault("text", {})[lang] = text
        if terms := data.get("terms"):
            definition.terms[lang] = terms
    return definition


def _read_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def build_rules(check_id: str, definition: CheckDefinition, config: AuditConfig) -> dict[str, RuleSpec]:
    """Merges rule defaults from the plugin with user overrides from the config."""
    rules: dict[str, RuleSpec] = {}
    for local_id, raw in definition.rules.items():
        rule_id = f"{check_id}.{local_id}"
        spec = RuleSpec.model_validate(
            {"category": definition.category, "terms": definition.terms, **raw, "id": rule_id, "check": check_id}
        )
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
