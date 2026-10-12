"""Approximate rendered width of SERP snippets: Arial advance widths in font units (2048 per em)."""

from __future__ import annotations

_UNITS_PER_EM = 2048
_DEFAULT = 1139
_WIDE = 2048
_GROUPS = {
    391: "'",
    455: "ijl",
    532: "|",
    569: "ftI .,:;!/\\[]",
    682: "r()-",
    727: '"',
    745: "г",
    797: "*",
    897: "к",
    931: "з",
    942: "т",
    1024: "cksvxyzJсух",
    1038: "э",
    1067: "чь",
    1088: "в",
    1096: "я",
    1124: "нп",
    1128: "Г",
    1139: "abdeghnopqu0123456789L?$_#–«»аеёийор",
    1157: "б",
    1167: "ц",
    1196: "длК+=<>~",
    1237: "З",
    1251: "FTZТ",
    1281: "ъ",
    1300: "У",
    1352: "БЛЬ",
    1366: "ABEKPSVXYАВЕЁРХЧ&",
    1407: "жм",
    1408: "Д",
    1458: "ы",
    1479: "wCDHNRUИЙНПСЭЯ",
    1522: "Ц",
    1544: "ю",
    1556: "Ф",
    1593: "GOQО",
    1636: "шЪ",
    1665: "щ",
    1688: "ф",
    1706: "mMМ",
    1821: "%Ы",
    1898: "Ж",
    1933: "W",
    1978: "Ш",
    2025: "Щ",
    2048: "—…",
    2079: "@",
    2111: "Ю",
}
_WIDTHS = {char: units for units, chars in _GROUPS.items() for char in chars}


def text_width(text: str, font_px: float) -> int:
    units = sum(_WIDTHS.get(c, _WIDE if ord(c) >= 0x2E80 else _DEFAULT) for c in text)
    return round(units * font_px / _UNITS_PER_EM)
