from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path

import yaml

from seoaudit.core.i18n import pick


@dataclass(frozen=True)
class Category:
    id: str
    name: str
    description: str
    weight: float
    order: int


@cache
def _raw() -> dict:
    return yaml.safe_load(Path(__file__).with_name("categories.yaml").read_text(encoding="utf-8"))


def get_category(category_id: str, lang: str, extra: dict | None = None) -> Category:
    """`extra` holds categories declared by plugins in their rules.yaml."""
    raw = {**_raw(), **(extra or {})}
    item = raw.get(category_id)
    if item is None:
        return Category(category_id, category_id.replace("_", " ").capitalize(), "", 1.0, len(raw))
    text = pick(item.get("text", {}), lang) or {}
    return Category(
        id=category_id,
        name=text.get("name", category_id),
        description=text.get("description", ""),
        weight=float(item.get("weight", 1.0)),
        order=list(raw).index(category_id),
    )
