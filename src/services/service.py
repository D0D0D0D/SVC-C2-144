"""SVC-C2-144 — deterministic domain service.

Everything here is deterministic: span-kind classification, source-handle
minting and claim->source verification. No LLM is involved.

That split is the design: the model only *maps* free-text exception rationale
onto the approved criteria conditions, while every factual string, identifier
and quotable span is produced or checked by the code below.

Structure is copied from the released canonical implementation
(MunicipalContractVariationEvidenceBriefAgent ``classify_span_kind``); only the marker vocabulary is replaced
with change-freeze wording.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from src.services import contracts

# Zero-width and formatting characters that could break a keyword apart.
_INVISIBLE = dict.fromkeys([0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x00AD])

DEFAULT_MAX_QUOTABLE_CHARS = 120


def normalise(text: str) -> str:
    """NFKC-fold, strip invisible characters and drop whitespace.

    Keyword checks run on this form so that "許 容", "許​容" and "許容"
    are all caught. Regex checks still run on the original text, where spacing
    is meaningful (e.g. "30 分").
    """
    folded = unicodedata.normalize("NFKC", text).translate(_INVISIBLE)
    return "".join(folded.split())


# ---------------------------------------------------------------------------
# span_kind classification (deterministic — never LLM-assigned)
# ---------------------------------------------------------------------------
# Conservative by construction: anything that is not clearly a factual span
# falls through to ``unclassified``, which is NOT quotable. The allowlist plus
# this fail-closed default is what stops an evidence document's own ruling from
# being quoted verbatim into the review packet.

# Used only to WITHHOLD quoting, never to bless text as safe, so a missed
# marker degrades to `unclassified` rather than to an emitted conclusion.
_ADJUDICATIVE_MARKERS = (
    "承認",
    "却下",
    "差し支えない",
    "差支えない",
    "認められる",
    "許容",
    "不許容",
    "妥当と判断",
    "問題ない",
    "可とする",
    "不可とする",
    "例外として認める",
    "承認相当",
    "承認が相当",
    "適法",
    "違法",
)
_OPINION_MARKERS = (
    "と解される",
    "と考えられる",
    "所見",
    "見解",
    "評価する",
    "判断する",
    "助言",
    "推奨",
    "意見",
    "答申",
)

# Evaluative sentence shapes carry a judgement without using any adjudicative
# keyword — e.g. "本変更に支障はない" is a conclusion, yet 支障 also appears in
# genuinely factual text ("旧バージョンが支障となる").
#
# Chasing individual words is a losing game, so this is a STRUCTURAL guard: a
# span matching an evaluative shape is demoted to `unclassified` even when a
# factual pattern also matched. A missed phrasing costs a false `unclassified`
# (the fact is reported as a gap), never a leaked conclusion.
_EVALUATIVE_SHAPES = (
    "はない",
    "ではない",
    "ありません",
    "当たらない",
    "あたらない",
    "できる",
    "できます",
    "してよい",
    "して良い",
    "が相当",
    "が適当",
    "支障はな",
    "問題はな",
    "問題な",
    "リスクは低",
    "リスクが低",
    "範囲内",
    "許され",
    "ゆるされ",
    "正当",
    "妥当",
    "適切である",
)

_QUANTITY_RE = re.compile(
    r"[¥￥]\s*\d[\d,]*"
    r"|\d[\d,]*\s*(?:時間|分|秒|日間|日|週間|箇月|ヶ月|か月|円|万円|千円"
    r"|%|％|台|件|本|式|ノード|ユーザ|インスタンス|GB|TB|MB)"
)
_DATE_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}"  # ISO-8601
    r"|(令和|平成|昭和)\s*\d+\s*年"  # 和暦
    r"|\d{4}\s*[/年-]\s*\d{1,2}\s*[/月-]\s*\d{1,2}"  # 年月日
)
_IDENTIFIER_RE = re.compile(r"[A-Za-z]{2,}[-－][A-Za-z0-9]+(?:[-－][A-Za-z0-9]+)*")
_OBSERVED_MARKERS = ("構成", "稼働", "設置", "検出", "確認された", "判明", "計測", "現行", "バージョン", "台数", "冗長")
_PROCEDURAL_MARKERS = (
    "手順書",
    "記載されている",
    "定められている",
    "申請",
    "届出",
    "通知",
    "承認フロー",
    "起票",
    "受領",
    "登録されている",
)

# A quotable span must be one atomic fact. Sentence terminators, conjunctions
# and excessive length all indicate prose that may carry a judgement alongside
# the factual token.
_CLAUSE_BREAKS = ("。", "．", "\n", "、また", "したがって", "よって", "ゆえに", "ため、")


def _is_multi_clause(text: str, max_chars: int) -> bool:
    """True when the span is prose rather than a single atomic fact."""
    stripped = text.strip()
    if len(stripped) > max_chars:
        return True
    # A single trailing terminator is fine; an internal one means >1 clause.
    body = stripped.rstrip("。．")
    return any(marker in body for marker in _CLAUSE_BREAKS)


def classify_span_kind(text: str, *, max_quotable_chars: int = DEFAULT_MAX_QUOTABLE_CHARS) -> str:
    """Return the deterministic ``span_kind`` for a source span.

    Precedence is deliberate and fail-closed:

    1. adjudicative / opinion markers -> not quotable
    2. **evaluative sentence shape** -> ``unclassified``, even when a factual
       pattern also matches. Without this, "凍結窓内で完了できる" would be
       promoted to ``quantity`` merely because it shares a token with genuinely
       factual text.
    3. multi-clause prose or over-length -> ``unclassified``
    4. factual patterns -> quotable
    5. anything else -> ``unclassified``

    Note the date check runs **before** the quantity check: "2026年8月20日"
    also matches the quantity pattern via ``\\d+日``, so testing quantity first
    would silently classify every date as a quantity. (The ordering table in
    ``docs/02_design.md`` was corrected to match this during implementation.)
    """
    if not isinstance(text, str) or not text.strip():
        return contracts.SPAN_KIND_UNCLASSIFIED

    normalised = normalise(text)

    for marker in _ADJUDICATIVE_MARKERS:
        if marker in normalised:
            return contracts.SPAN_KIND_ADJUDICATIVE
    for marker in _OPINION_MARKERS:
        if marker in normalised:
            return contracts.SPAN_KIND_OPINION
    # Structural guard — an evaluative shape outranks any factual pattern.
    for shape in _EVALUATIVE_SHAPES:
        if shape in normalised:
            return contracts.SPAN_KIND_UNCLASSIFIED

    if _is_multi_clause(text, max_quotable_chars):
        return contracts.SPAN_KIND_UNCLASSIFIED

    if _DATE_RE.search(text):
        return contracts.SPAN_KIND_DATE
    if _QUANTITY_RE.search(text):
        return contracts.SPAN_KIND_QUANTITY
    if _IDENTIFIER_RE.search(text):
        return contracts.SPAN_KIND_IDENTIFIER
    if any(marker in normalised for marker in _OBSERVED_MARKERS):
        return contracts.SPAN_KIND_OBSERVED_CONDITION
    if any(marker in normalised for marker in _PROCEDURAL_MARKERS):
        return contracts.SPAN_KIND_PROCEDURAL_RECORD

    return contracts.SPAN_KIND_UNCLASSIFIED


def is_quotable(span_kind: str) -> bool:
    return span_kind in contracts.QUOTABLE_SPAN_KINDS


# ---------------------------------------------------------------------------
# source registry
# ---------------------------------------------------------------------------
# Handles are alphabetic only. The framework S-2 gate rewrites detected PII in
# `validated_input`, and a digit-bearing handle gets mis-detected as a phone
# number and destroyed (a measured defect).
_ALPHA = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _alpha_index(index: int) -> str:
    """Encode a 1-based ordinal as an alphabetic suffix (1->A, 27->AA)."""
    if index < 1:
        raise ValueError("index must be >= 1")
    out = ""
    while index:
        index, rem = divmod(index - 1, 26)
        out = _ALPHA[rem] + out
    return out


def mint_source_id(doc_index: int, span_index: int) -> str:
    return f"SRC{_alpha_index(doc_index)}{_alpha_index(span_index)}"


def build_source_registry(
    declarations: list[dict[str, Any]] | None,
    *,
    request_ref: str,
    criteria_version_ref: str,
    max_quotable_chars: int = DEFAULT_MAX_QUOTABLE_CHARS,
) -> dict[str, dict[str, Any]]:
    """Mint deterministic source handles from the declared evidence spans.

    The model may only *select* handles produced here; it can never mint a
    ``source_id``, which is what stops fabricated citations at the root.
    """
    registry: dict[str, dict[str, Any]] = {}
    for doc_index, document in enumerate(declarations or [], start=1):
        if not isinstance(document, dict):
            continue
        doc_ref = str(document.get("doc_ref") or "")
        doc_type = str(document.get("doc_type") or "other")
        if doc_type not in contracts.DOC_TYPES:
            doc_type = "other"
        spans = document.get("spans")
        spans = spans if isinstance(spans, list) else []
        for span_index, span in enumerate(spans, start=1):
            if not isinstance(span, dict):
                continue
            span_text = str(span.get("span_text") or "")
            source_id = mint_source_id(doc_index, span_index)
            registry[source_id] = {
                "doc_type": doc_type,
                "doc_ref": doc_ref,
                "span_ref": str(span.get("span_ref") or ""),
                "span_text": span_text,
                "span_kind": classify_span_kind(span_text, max_quotable_chars=max_quotable_chars),
                "provenance": {
                    "request_ref": request_ref,
                    "source_id": source_id,
                    "doc_ref": doc_ref,
                    "span_ref": str(span.get("span_ref") or ""),
                    "criteria_version_ref": criteria_version_ref,
                },
            }
    return registry


__all__ = [
    "normalise",
    "classify_span_kind",
    "is_quotable",
    "mint_source_id",
    "build_source_registry",
    "DEFAULT_MAX_QUOTABLE_CHARS",
]
