"""The file a browser actually downloads for a responsive image."""

from __future__ import annotations

import re

# A wide desktop at DPR 1: the biggest file a typical visitor gets, without assuming retina everywhere.
VIEWPORT = 1920
_EM = 16
_CONDITION = re.compile(r"\(\s*(min|max)-width\s*:\s*([\d.]+)(px|em)\s*\)")
_LENGTH = re.compile(r"^([\d.]+)(px|vw|em)$")
_SUPPORTED_TYPES = {"", "image/avif", "image/webp", "image/jpeg", "image/png", "image/gif", "image/svg+xml"}


def pick_candidate(srcset: str | None, sizes: str | None) -> str | None:
    candidates = _parse_srcset(srcset)
    widths = [(int(d[:-1]), url) for url, d in candidates if d.endswith("w") and d[:-1].isdigit()]
    if widths:
        slot = _slot_width(sizes)
        fitting = [w for w in widths if w[0] >= slot]
        return (min(fitting) if fitting else max(widths))[1]
    for url, descriptor in candidates:
        if descriptor in ("", "1x"):
            return url
    return candidates[0][0] if candidates else None


def candidate_urls(srcset: str | None) -> list[str]:
    return [url for url, _ in _parse_srcset(srcset)]


def source_applies(type_: str | None, media: str | None) -> bool:
    return (type_ or "").strip().lower() in _SUPPORTED_TYPES and (not media or _media_matches(media))


def _media_matches(media: str) -> bool:
    conditions = _CONDITION.findall(media)
    return bool(conditions) and all(_condition_holds(kind, value, unit) for kind, value, unit in conditions)


def _parse_srcset(srcset: str | None) -> list[tuple[str, str]]:
    """URLs may contain commas (image CDNs), so the URL runs to whitespace, as in the HTML spec."""
    text = srcset or ""
    result = []
    pos = 0
    while pos < len(text):
        while pos < len(text) and (text[pos].isspace() or text[pos] == ","):
            pos += 1
        start = pos
        while pos < len(text) and not text[pos].isspace():
            pos += 1
        url, descriptor = text[start:pos], ""
        if url.endswith(","):
            url = url.rstrip(",")
        else:
            end = text.find(",", pos)
            end = len(text) if end == -1 else end
            descriptor = text[pos:end].strip().split(" ")[0].lower()
            pos = end + 1
        if url:
            result.append((url, descriptor))
    return result


def _slot_width(sizes: str | None) -> float:
    for entry in (sizes or "").split(","):
        entry = entry.strip()
        media, length = "", entry
        if entry.startswith("("):
            depth = 0
            for index, char in enumerate(entry):
                depth += {"(": 1, ")": -1}.get(char, 0)
                if depth == 0:
                    media, length = entry[: index + 1], entry[index + 1 :].strip()
                    break
        if media and not _media_matches(media):
            continue
        match = _LENGTH.match(length)
        if match:
            value, unit = float(match[1]), match[2]
            return value * VIEWPORT / 100 if unit == "vw" else value * (_EM if unit == "em" else 1)
        if length != "auto":
            break
    return VIEWPORT


def _condition_holds(kind: str, value: str, unit: str) -> bool:
    limit = float(value) * (_EM if unit == "em" else 1)
    return VIEWPORT >= limit if kind == "min" else VIEWPORT <= limit
