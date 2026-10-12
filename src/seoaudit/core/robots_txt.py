from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

# Yandex still reads Clean-param and tolerates Host/Crawl-delay; anything else is ignored by both engines.
_KNOWN = {"user-agent", "allow", "disallow", "sitemap", "crawl-delay", "clean-param", "host"}
_GROUP_MEMBERS = {"allow", "disallow", "crawl-delay"}
MAX_BYTES = 500 * 1024


@dataclass(frozen=True)
class Directive:
    line: int
    name: str
    value: str


def parse(text: str) -> tuple[list[Directive], list[int]]:
    """Returns directives and the numbers of lines that are not `name: value` at all."""
    directives, malformed = [], []
    for number, raw in enumerate(text.lstrip("﻿").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        name, sep, value = line.partition(":")
        if not sep or not name.strip():
            malformed.append(number)
            continue
        directives.append(Directive(number, name.strip().lower(), value.strip()))
    return directives, malformed


def lint(text: str) -> dict[str, list[str]]:
    directives, malformed = parse(text)
    problems: dict[str, list[str]] = defaultdict(list)
    problems["syntax"].extend(str(n) for n in malformed)
    seen_agent = False
    for d in directives:
        if d.name not in _KNOWN:
            problems["unknown"].append(f"{d.line} ({d.name})")
        elif d.name == "user-agent":
            seen_agent = True
        elif d.name in _GROUP_MEMBERS and not seen_agent:
            problems["no_agent"].append(str(d.line))
        elif d.name == "sitemap" and not d.value.lower().startswith(("http://", "https://")):
            problems["sitemap"].append(str(d.line))
        if not d.value.isascii():
            problems["non_ascii"].append(str(d.line))
    return {k: v for k, v in problems.items() if v}


def clean_params(text: str) -> list[tuple[frozenset[str], str]]:
    """Yandex Clean-param rules as (parameter names, path prefix)."""
    result = []
    for d in parse(text)[0]:
        if d.name == "clean-param" and d.value:
            names, _, prefix = d.value.partition(" ")
            result.append((frozenset(n.strip() for n in names.split("&") if n.strip()), prefix.strip().rstrip("*") or "/"))
    return result


def is_cleaned(rules: list[tuple[frozenset[str], str]], path: str, params: set[str]) -> bool:
    covered = {name for names, prefix in rules if path.startswith(prefix) for name in names}
    return bool(params) and params <= covered
