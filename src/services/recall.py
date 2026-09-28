"""Search-term construction for Japanese business text.

🔴 **This module is an interim backend contract, not a permanent design.**
The SDK persistence layer is entirely STUB today, so retrieval is a
substring-matching stand-in. The one durable requirement is: *recall must not
return zero hits on real Japanese input*. Everything below is the known-good
way to satisfy that with a substring backend, and is expected to be thrown away
when a real (vector) backend lands.

Why it is written down at all: splitting on whitespace and punctuation makes an
entire Japanese sentence a single token, which then fails to substring-match
any stored summary — measured as a flat zero in practice. The defect is
invisible if the test input has spaces inserted by hand, so an implementer has
no reliable way to discover it independently.

The port contract test for this is deliberately ``xfail``: when a real backend
is wired it will ``XPASS`` and force a review of this module's removal.
"""

from __future__ import annotations

import re
import unicodedata

_INVISIBLE = dict.fromkeys([0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x00AD])

# Below this length a token is already discriminating enough to use whole.
_NGRAM_THRESHOLD = 7
_NGRAM_SIZE = 2

_SPLIT_RE = re.compile(r"[\s、。,.　・／/()（）「」『』\[\]【】:：;；]+")


def normalise(text: str) -> str:
    folded = unicodedata.normalize("NFKC", str(text or "")).translate(_INVISIBLE)
    return folded.strip()


def _bigrams(token: str) -> list[str]:
    return [token[i : i + _NGRAM_SIZE] for i in range(len(token) - _NGRAM_SIZE + 1)]


def _sample_evenly(items: list[str], limit: int) -> list[str]:
    """Take ``limit`` items spread across the whole list.

    Truncating to the head loses the tail, and in business prose the
    discriminating part is usually at the end ("...の切戻し手順が未検証").
    """
    if limit <= 0 or len(items) <= limit:
        return items
    step = len(items) / float(limit)
    return [items[int(i * step)] for i in range(limit)]


def build_terms(text: str, *, max_terms: int = 64) -> list[str]:
    """Return the search terms for one query string.

    Whitespace splitting is kept (it works for identifiers and ASCII), and
    n-gram expansion is added for the long unsegmented tokens that Japanese
    input actually produces.
    """
    normalised = normalise(text)
    if not normalised:
        return []

    terms: list[str] = []
    seen: set[str] = set()

    def _add(term: str) -> None:
        if term and term not in seen:
            seen.add(term)
            terms.append(term)

    for token in _SPLIT_RE.split(normalised):
        if not token:
            continue
        _add(token)
        if len(token) >= _NGRAM_THRESHOLD:
            for gram in _bigrams(token):
                _add(gram)

    return _sample_evenly(terms, max_terms)


def escape_like(term: str, escape: str = "\\") -> str:
    """Escape LIKE wildcards.

    A ``%`` inside the request body would otherwise match every row in the
    connection, silently turning a targeted lookup into a full scan.
    """
    out = term.replace(escape, escape + escape)
    for wildcard in ("%", "_"):
        out = out.replace(wildcard, escape + wildcard)
    return out


def match_score(terms: list[str], haystack: str) -> int:
    """Count how many distinct terms occur in ``haystack``."""
    text = normalise(haystack)
    if not text:
        return 0
    return sum(1 for term in terms if term and term in text)


def meets_threshold(score: int, min_hits: int) -> bool:
    """Absolute threshold, never a ratio.

    n-grams share single tokens easily, so ``score > 0`` returns unrelated rows.
    A ratio is worse: the longer the query, the higher the bar, until nothing
    can reach it.
    """
    return score >= max(1, int(min_hits))


__all__ = ["normalise", "build_terms", "escape_like", "match_score", "meets_threshold"]
