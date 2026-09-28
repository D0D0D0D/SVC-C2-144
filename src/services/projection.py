"""Three-value split for human-written free text.

Free text that reaches the KB is loaded into the *next* run's Stage 3 prompt.
That makes the KB a persistent prompt-injection channel, not merely a PII
concern — a policy of "no free input in the KB" collapses the moment a column
like a decision rationale has to be free text to be useful.

So one input becomes three values:

===========  ==========================  =====================================
value        stored in                   content
===========  ==========================  =====================================
``_raw``     DB record of origin only    exactly as entered; the audit master
``_audit``   DB record of origin         PII-masked; for audit export
``_safe``    **KB projection**           the only model-visible form
===========  ==========================  =====================================

The ``_safe`` projection NFKC-normalises, masks PII, truncates and **rejects**
imperative/instruction shapes outright. Rejection is loud (the row is refused
with a reason) rather than a silent drop, so an operator can tell the
difference between "we filtered it" and "it never arrived".

Masking happens on write. Masking on read leaves the KB itself contaminated and
the contamination accumulates.
"""

from __future__ import annotations

import re
import unicodedata
from typing import TypedDict

from framework.security import detect_pii


class TextQualityConfig(TypedDict):
    """Typed keyword arguments accepted by :func:`text_quality_reason`."""

    max_replacement_ratio: float
    max_control_ratio: float
    min_readable_ratio: float
    max_chars: int


class ProjectionRejected(ValueError):
    """Raised when free text must not be written to the KB at all."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


_INVISIBLE = dict.fromkeys([0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x00AD])

# Instruction shapes. This list does not need to be exhaustive: it is not the
# security boundary (the boundary is that model-visible values are passed as
# JSON values, never concatenated into the prose of a prompt). It catches the
# blatant cases so they are refused at the door instead of accumulating.
_INSTRUCTION_PATTERNS = (
    re.compile(r"以後の指示"),
    re.compile(r"これまでの指示"),
    re.compile(r"前の指示"),
    re.compile(r"指示を無視"),
    re.compile(r"システムプロンプト"),
    re.compile(r"あなたは.{0,12}として(振る舞|ふるま)"),
    re.compile(r"ignore\s+(all\s+)?previous", re.IGNORECASE),
    re.compile(r"disregard\s+(all\s+)?(prior|previous)", re.IGNORECASE),
    re.compile(r"system\s+prompt", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+", re.IGNORECASE),
)


def _normalise(text: str) -> str:
    folded = unicodedata.normalize("NFKC", str(text or "")).translate(_INVISIBLE)
    return folded.strip()


def _mask_pii(text: str) -> str:
    """Apply the framework PII detector.

    Re-implementing this is prohibited: ``detect_pii`` is a
    REAL substrate capability, including the labelled Kanji/Katakana personal
    name case. Its residual gaps (unlabelled names, hiragana-only, other
    labels) are documented in the operation guide and mitigated by
    operational input discipline, not by a hand-rolled dictionary.
    """
    findings = detect_pii(text)
    if not findings:
        return text

    # `detect_pii` returns [{"type", "value", "start", "end"}, ...] (verified
    # against agenticstar-agentcore 1.0.1). Splice by offset, walking backwards
    # so each replacement cannot shift the offsets still to be applied. A plain
    # str.replace() would also rewrite identical text elsewhere in the field
    # that the detector did not flag.
    spans: list[tuple[int, int]] = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        start, end = finding.get("start"), finding.get("end")
        if isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(text):
            spans.append((start, end))
        else:  # pragma: no cover - defensive
            value = finding.get("value")
            if isinstance(value, str) and value:
                index = text.find(value)
                if index >= 0:
                    spans.append((index, index + len(value)))

    masked = text
    for start, end in sorted(spans, key=lambda s: s[0], reverse=True):
        masked = masked[:start] + "[MASKED]" + masked[end:]
    return masked


def contains_instruction(text: str) -> bool:
    normalised = _normalise(text)
    return any(pattern.search(normalised) for pattern in _INSTRUCTION_PATTERNS)


def normalise_and_mask(text: str) -> str:
    """Return the model/checkpoint-safe form of submitted free text.

    The order is security-significant.  AgentCore 1.0.1's PII detector does
    not NFKC-fold internally, so masking before normalisation lets full-width
    email addresses and telephone numbers pass through unchanged.
    """
    return _mask_pii(_normalise(str(text or "")))


def text_quality_reason(
    value: str,
    *,
    max_replacement_ratio: float = 0.02,
    max_control_ratio: float = 0.01,
    min_readable_ratio: float = 0.80,
    max_chars: int = 20000,
) -> str | None:
    """Return a stable rejection reason for unusable model-visible text."""
    if not value:
        return None
    text = unicodedata.normalize("NFKC", value).translate(_INVISIBLE)
    total = len(text)
    if total >= max_chars:
        return "too_long"
    if total == 0:
        return None
    if text.count("�") / total >= max_replacement_ratio:
        return "mojibake"
    control = sum(1 for char in text if unicodedata.category(char) == "Cc" and char not in "\t\n") / total
    if control >= max_control_ratio:
        return "control_characters"
    readable = (
        sum(1 for char in text if char.isalnum() or char.isspace() or unicodedata.category(char).startswith("P"))
        / total
    )
    if readable <= min_readable_ratio:
        return "not_readable"
    return None


def project(text: str, *, max_chars: int) -> tuple[str, str, str]:
    """Return ``(raw, audit, safe)`` for one free-text input.

    Raises :class:`ProjectionRejected` when the text carries an instruction
    shape; the caller refuses the row rather than storing a filtered version.
    """
    raw = str(text or "")
    audit = normalise_and_mask(raw)
    if contains_instruction(audit):
        raise ProjectionRejected("instruction_shape_detected")

    safe = audit
    if max_chars and len(safe) > max_chars:
        safe = safe[:max_chars]
    return raw, audit, safe


__all__ = [
    "ProjectionRejected",
    "contains_instruction",
    "normalise_and_mask",
    "text_quality_reason",
    "project",
]
