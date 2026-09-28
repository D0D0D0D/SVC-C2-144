"""Deterministic numeric guard for claims about durations and counts.

Stage 3 may *quote* a rollback duration from the request; Stage 4 verifies that
the number it wrote actually appears in the cited source span. That check is
only as good as the extractor, and exactly how it fails has been measured:

🔴 **Never require a ``\\w`` boundary.** In Japanese the unit and particle sit
flush against the digits ("30分以内", "約2時間半"). A boundary-anchored regex
returns the empty set on such text, and an empty set means the guard passes
unconditionally — a green test suite over a guard that never fires.

Scope is deliberately narrow (durations and counts only). Money, identifier
fragments and version numbers are out of scope: they are matched verbatim by
the quote check instead, where an exact-string comparison is the right tool.

Unparsed forms are **not** silently accepted. A range ("2〜4時間") or a relative
expression ("前回より短い") is not extracted, and the caller raises
``requires_human_review`` — "not verified" is reported as such rather than
being folded into a pass.
"""

from __future__ import annotations

import re
import unicodedata

_INVISIBLE = dict.fromkeys([0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x00AD])

# --- kanji numerals -------------------------------------------------------
_KANJI_DIGITS = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_KANJI_UNITS = {"十": 10, "百": 100, "千": 1000}
_KANJI_CHARS = set(_KANJI_DIGITS) | set(_KANJI_UNITS) | {"万"}

# --- unit normal forms ----------------------------------------------------
# Durations normalise to MINUTES; counts stay dimensionless.
_DURATION_UNITS = {
    "時間": 60,
    "h": 60,
    "hr": 60,
    "hrs": 60,
    "hour": 60,
    "hours": 60,
    "分": 1,
    "min": 1,
    "mins": 1,
    "minute": 1,
    "minutes": 1,
    "日": 1440,
    "day": 1440,
    "days": 1440,
}
_COUNT_UNITS = ("件", "台", "本", "ユーザ", "ノード", "インスタンス")

# Hedges are stripped, not interpreted. "約 2 時間" and "2 時間" compare equal;
# treating the hedge as a range would let any nearby value pass.
_HEDGES = ("約", "およそ", "概ね", "おおむね", "程度", "以内", "以下", "以上", "超", "弱", "強")

# Ranges and relative comparisons are recognised only so they can be EXCLUDED.
_RANGE_RE = re.compile(r"\d\s*[〜~～\-–—]\s*\d")
_RELATIVE_RE = re.compile(r"(前回|従来|通常|平常)より")

_UNIT_ALTERNATION = "|".join(
    sorted((re.escape(u) for u in list(_DURATION_UNITS) + list(_COUNT_UNITS)), key=len, reverse=True)
)
# No \b anywhere: the unit is glued to the digits and often followed by a
# particle. `半` is captured so "2時間半" resolves to 150 minutes.
_QUANTITY_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(" + _UNIT_ALTERNATION + r")\s*(半)?",
    re.IGNORECASE,
)


def _kanji_to_int(chunk: str) -> int | None:
    """Convert a pure-kanji numeral to an int (supports 〜万)."""
    if not chunk or any(ch not in _KANJI_CHARS for ch in chunk):
        return None
    total = 0
    section = 0
    current = 0
    for ch in chunk:
        if ch in _KANJI_DIGITS:
            current = _KANJI_DIGITS[ch]
        elif ch in _KANJI_UNITS:
            section += (current or 1) * _KANJI_UNITS[ch]
            current = 0
        elif ch == "万":
            total += (section + current) * 10000
            section = current = 0
    return total + section + current


_KANJI_RUN_RE = re.compile("[" + "".join(sorted(_KANJI_CHARS)) + "]+")


def normalise(text: str) -> str:
    """NFKC-fold, strip invisible characters, drop hedges, arabicise kanji."""
    if not isinstance(text, str):
        return ""
    folded = unicodedata.normalize("NFKC", text).translate(_INVISIBLE)
    for hedge in _HEDGES:
        folded = folded.replace(hedge, "")

    def _sub(match: re.Match[str]) -> str:
        value = _kanji_to_int(match.group(0))
        return str(value) if value is not None else match.group(0)

    return _KANJI_RUN_RE.sub(_sub, folded)


def extract(text: str) -> tuple[set[tuple[str, float]], bool]:
    """Return ``(quantities, has_unsupported_form)``.

    ``quantities`` holds ``("duration_min" | "count", value)`` pairs in normal
    form. ``has_unsupported_form`` is True when the text contains a range or a
    relative expression, which the caller must surface as "not verified"
    instead of treating as a pass.
    """
    normalised = normalise(text)
    unsupported = bool(_RANGE_RE.search(normalised) or _RELATIVE_RE.search(normalised))

    found: set[tuple[str, float]] = set()
    for match in _QUANTITY_RE.finditer(normalised):
        raw, unit, half = match.group(1), match.group(2), match.group(3)
        try:
            value = float(raw)
        except ValueError:  # pragma: no cover - regex guarantees a number
            continue
        unit_key = unit.lower()
        if unit_key in _DURATION_UNITS:
            factor = _DURATION_UNITS[unit_key]
            if half:
                value += 0.5
            found.add(("duration_min", value * factor))
        else:
            if half:
                # "3件半" is not a real form; treat it as unsupported rather
                # than inventing a rounding rule.
                unsupported = True
                continue
            found.add(("count", value))
    return found, unsupported


def claim_numbers_supported(claim_text: str, source_texts: list[str]) -> tuple[bool, bool]:
    """Check every quantity in ``claim_text`` appears in one of the sources.

    Returns ``(all_supported, needs_human_review)``. Comparison is exact on the
    normal form; no tolerance is allowed, because a tolerance would let "4 時間"
    be written as "4.5 時間" and still pass.
    """
    claim_qty, claim_unsupported = extract(claim_text)
    if not claim_qty:
        # Nothing numeric to verify. An unsupported form still needs a human.
        return True, claim_unsupported

    source_qty: set[tuple[str, float]] = set()
    source_unsupported = False
    for text in source_texts:
        qty, unsupported = extract(text)
        source_qty |= qty
        source_unsupported = source_unsupported or unsupported

    missing = claim_qty - source_qty
    return (not missing), (claim_unsupported or source_unsupported)


__all__ = ["normalise", "extract", "claim_numbers_supported"]
