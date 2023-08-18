from __future__ import annotations

from typing import TypeVar

T = TypeVar("T")

FALLBACK_LANGS = ("en", "ru")


def pick(variants: dict[str, T], lang: str) -> T | None:
    for candidate in (lang, *FALLBACK_LANGS):
        if candidate in variants:
            return variants[candidate]
    return next(iter(variants.values()), None)
