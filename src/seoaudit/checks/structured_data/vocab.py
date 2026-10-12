"""schema.org vocabulary checks: what validator.schema.org reports regardless of Google's rich result rules."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from difflib import get_close_matches
from functools import cache
from pathlib import Path
from typing import Any

_PREFIXES = ("https://schema.org/", "http://schema.org/", "https://www.schema.org/", "http://www.schema.org/", "schema:")
_ENUM_PREVIEW = 4

Report = Callable[[str, str], None]


class Vocabulary:
    def __init__(self, data: dict[str, Any]):
        self.types: dict[str, list[str]] = data["types"]
        self.properties: dict[str, list[list[str]]] = data["properties"]
        self.members: dict[str, list[str]] = data["members"]
        self._lineage: dict[str, list[str]] = {}

    def lineage(self, type_: str) -> list[str]:
        """The type and its ancestors, nearest first."""
        if type_ not in self._lineage:
            order, queue = [], [type_]
            while queue:
                current = queue.pop(0)
                if current not in order:
                    order.append(current)
                    queue.extend(self.types.get(current, []))
            self._lineage[type_] = order
        return self._lineage[type_]

    def is_a(self, type_: str, parent: str) -> bool:
        return parent in self.lineage(type_)

    def allows(self, types: list[str], prop: str) -> bool:
        domains = self.properties[prop][0]
        return any(self.is_a(t, d) for t in types for d in domains)

    def props_of(self, types: list[str]) -> list[str]:
        return [p for p in self.properties if self.allows(types, p)]

    def enum_members(self, ranges: list[str]) -> list[str]:
        return [m for m, kinds in self.members.items() if any(self.is_a(k, r) for k in kinds for r in ranges)]


@cache
def vocabulary() -> Vocabulary:
    return Vocabulary(json.loads(Path(__file__).with_name("vocab.json").read_text(encoding="utf-8")))


def schema_name(value: Any) -> str | None:
    """Local name of a schema.org term, None for other vocabularies."""
    if not isinstance(value, str):
        return None
    for prefix in _PREFIXES:
        if value.startswith(prefix):
            return value.removeprefix(prefix).strip("/") or None
    return None if ":" in value or "/" in value else value


def context_kind(value: Any) -> str:
    texts: list[str] = []
    for item in value if isinstance(value, list) else [value]:
        texts.extend(v for v in (item.values() if isinstance(item, dict) else [item]) if isinstance(v, str))
    if value is None:
        return "missing"
    # Google resolves even a scheme-less "schema.org" to the vocabulary.
    if any("schema.org" in t for t in texts):
        return "ok"
    return "obsolete" if any("data-vocabulary.org" in t for t in texts) else "foreign"


def roots(value: Any) -> Iterator[dict[str, Any]]:
    """Top-level nodes of a JSON-LD block, each carrying the @context that applies to it."""
    for node in value if isinstance(value, list) else [value]:
        if not isinstance(node, dict):
            continue
        if "@graph" in node:
            for item in _list(node["@graph"]):
                if isinstance(item, dict):
                    yield {"@context": node.get("@context"), **item}
        elif "@type" in node:
            yield node


def check_root(root: dict[str, Any], vocab: Vocabulary, report: Report) -> None:
    kind = context_kind(root.get("@context"))
    if kind == "foreign":
        return
    if kind != "ok":
        report(kind, str(root.get("@context") or root.get("@type")))
    if kind != "obsolete":
        _node(root, vocab, report)


def _node(node: dict[str, Any], vocab: Vocabulary, report: Report, via: str | None = None) -> None:
    types: list[str] = []
    for raw in _list(node.get("@type")):
        name = schema_name(raw)
        if name in vocab.types:
            types.append(name)
        elif name is not None:
            report("type", name + _suggest(name, list(vocab.types)))
    label = types[0] if types else "Thing"
    for key, value in node.items():
        prop = schema_name(key)
        # "query-input" and the like are Potential Actions annotations, not properties.
        if key.startswith("@") or prop is None or prop.endswith(("-input", "-output")):
            continue
        known = prop in vocab.properties
        # A Role carries the wrapped value under the same property it stands in for.
        wrapped = via == prop and any(vocab.is_a(t, "Role") for t in types)
        if types and not wrapped and not (known and vocab.allows(types, prop)):
            report("property", f"{label}.{key}" + _suggest(prop, vocab.props_of(types)))
            known = False
        for item in _list(value):
            if isinstance(item, dict):
                if known and "@type" in item:
                    _check_range(label, key, prop, item, vocab, report)
                elif known and set(item) == {"@id"}:
                    _check_enum(label, key, prop, item["@id"], vocab, report)
                _node(item, vocab, report, prop)
            elif known and isinstance(item, str):
                _check_enum(label, key, prop, item, vocab, report)


def _check_range(label: str, key: str, prop: str, item: dict[str, Any], vocab: Vocabulary, report: Report) -> None:
    expected = vocab.properties[prop][1]
    types = [t for t in map(schema_name, _list(item["@type"])) if t in vocab.types]
    # Like validator.schema.org: an expected type is a hint unless the property also takes a literal, and a Role
    # may wrap any value.
    if not types or not any(vocab.is_a(r, "DataType") for r in expected) or any(vocab.is_a(t, "Role") for t in types):
        return
    if not any(vocab.is_a(t, r) for t in types for r in expected):
        report("range", f"{label}.{key} = {types[0]} ({', '.join(expected)})")


def _check_enum(label: str, key: str, prop: str, value: str, vocab: Vocabulary, report: Report) -> None:
    expected = vocab.properties[prop][1]
    if not expected or not all(vocab.is_a(r, "Enumeration") for r in expected):
        return
    name = schema_name(value.strip())
    if name in vocab.members and any(vocab.is_a(k, r) for k in vocab.members[name] for r in expected):
        return
    options = vocab.enum_members(expected)
    preview = ", ".join(options[:_ENUM_PREVIEW]) + (", …" if len(options) > _ENUM_PREVIEW else "")
    report("enum", f"{label}.{key} = «{value}»" + (f" ({preview})" if options else ""))


def _suggest(name: str, candidates: list[str]) -> str:
    match = get_close_matches(name, candidates, n=1, cutoff=0.8)
    return f" → {match[0]}" if match else ""


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [] if value is None else [value]
