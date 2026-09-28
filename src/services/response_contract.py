"""The single source for the Stage 3 prompt contract and its validator.

The prompt and the type projection applied to the model's reply are generated
from the **same** constants. Writing the JSON contract by hand in the prompt
while validating against a separate table is how a model ends up returning a
shape nothing accepts — and, worse, how a schema instruction can silently go
missing while the tests still pass.

A measured failure this guards against: a prompt said "return only the
specified JSON schema" without ever including the schema. The model returned
``{}``, a coercion helper turned that into a valid-but-empty result, and the
run reported success. Mock LLMs never expose this because they return a
finished object. Hence ``AT-12``: assert the built prompt string *contains* the
field names, the allowlist values and the limits, and mutation-test that
removing any of them fails.
"""

from __future__ import annotations

import json
from typing import Any

from src.services import contracts

# Prompt and validator read these same numbers. Copying a body verbatim into a
# finding is prevented by the limit, not by asking the model nicely.
FINDING_TEXT_LIMITS: dict[str, int] = {
    "description": 400,  # supported_conditions / evidence_gaps / contradictions /
    # approval_scope_discrepancies
    "question": 300,  # verification_questions
}


def _example_finding(section: str) -> dict[str, Any]:
    """One concrete example element per section, built from the field tuple."""
    # `gap_origin` is stamped by Stage 4.  Showing it in the model schema while
    # the Stage 3 projector forbids it makes every non-empty evidence_gaps
    # response fail its own advertised contract.
    fields = tuple(field for field in contracts.FINDING_FIELDS_BY_SECTION[section] if field != "gap_origin")
    example: dict[str, Any] = {}
    for name in fields:
        if name == "claim_id":
            example[name] = "C1"
        elif name == "description":
            example[name] = "<= %d chars, in Japanese" % FINDING_TEXT_LIMITS["description"]
        elif name == "question":
            example[name] = "<= %d chars, in Japanese" % FINDING_TEXT_LIMITS["question"]
        elif name == "condition_id":
            # A gap may concern the request as a whole rather than one pinned
            # criterion.  Showing null in the executable example prevents the
            # model from inventing a condition merely to satisfy the shape.
            example[name] = None if (section, name) in contracts.NULLABLE_FINDING_FIELDS else "COND-A"
        elif name == "source_ids":
            example[name] = ["SRCAA"]
        elif name == "severity":
            example[name] = "candidate"
        elif name == "confidence":
            example[name] = 0.5
    return example


def build_response_schema() -> dict[str, Any]:
    """The exact JSON object the model must return."""
    schema: dict[str, Any] = {section: [_example_finding(section)] for section in contracts.FINDING_FIELDS_BY_SECTION}
    # Precedent drafts carry only what the model is allowed to decide. The past
    # verdict is copied deterministically afterwards and is intentionally
    # absent from everything the model sees.
    schema["precedent_checks"] = [
        {
            "history_ref": "HISTAA",
            "status": "addressed",
            "source_ids": ["SRCAA"],
        }
    ]
    return schema


def build_prompt(
    *,
    criteria_conditions: list[dict[str, Any]],
    source_registry: dict[str, dict[str, Any]],
    recall_refs: list[dict[str, Any]],
    freeze_window_facts: dict[str, Any],
    field_gaps: list[dict[str, Any]],
    reviewer_note: str | None = None,
) -> str:
    """Build the Stage 3 prompt.

    Model-visible values are passed as JSON values, never concatenated into the
    prose — that is the structural half of the injection defence, with the
    ``_safe`` projection being the other half.
    """
    quotable = {
        source_id: {
            "doc_type": entry.get("doc_type"),
            "span_text": entry.get("span_text"),
            "span_kind": entry.get("span_kind"),
        }
        for source_id, entry in (source_registry or {}).items()
        if entry.get("span_kind") in contracts.QUOTABLE_SPAN_KINDS
    }

    # Past perspectives: the issue and its rationale only. `decision_outcome`
    # is deliberately NOT here — see docs/02 §過去判例の照合.
    past_issues = [
        {
            "history_ref": ref.get("history_ref"),
            "condition_id": ref.get("condition_id"),
            "prior_issue": ref.get("decision_rationale_safe"),
            "request_summary": ref.get("request_summary_safe"),
        }
        for ref in (recall_refs or [])
    ]

    payload = {
        "criteria_conditions": criteria_conditions or [],
        "quotable_sources": quotable,
        "past_review_issues": past_issues,
        "freeze_window_facts": freeze_window_facts or {},
        "deterministic_gaps": field_gaps or [],
    }
    if reviewer_note:
        payload["reviewer_note"] = reviewer_note

    schema = build_response_schema()

    return "\n".join(
        [
            "あなたは変更凍結の例外申請レビューを支援します。",
            "承認可否・リスク受容の判断は行いません。承認・却下・許容といった結論を書いてはいけません。",
            "",
            "## 入力",
            json.dumps(payload, ensure_ascii=False, indent=2),
            "",
            "## 出力（この JSON オブジェクトのみを返す。前後に文章を付けない）",
            json.dumps(schema, ensure_ascii=False, indent=2),
            "",
            "## 規則",
            "- `source_ids` には `quotable_sources` のキーを **必ず 1 件以上入れる**。",
            "  新しい ID を作ってはいけない。",
            "- `severity` は `%s` のみ。" % "` / `".join(sorted(contracts.SEVERITIES)),
            "- `evidence_gaps[]` / `contradictions[]` / `verification_questions[]` の `condition_id` は、"
            "特定の基準条件に紐づかない論点なら `null`。値を入れる場合は入力の `criteria_conditions` に"
            "実在する ID。`supported_conditions[].condition_id` は必ず実在する ID。",
            "- `supported_conditions[].confidence` は 0.0〜1.0 の数値。"
            "  確信度を評価できない場合は推測せず `null`。",
            "- `doc_type` は `%s` のいずれか。" % "` / `".join(sorted(contracts.DOC_TYPES)),
            "- `precedent_checks[].status` は `%s` のいずれか。" % "` / `".join(sorted(contracts.PRECEDENT_STATUSES)),
            "  `addressed` と `unclear` は `source_ids` を 1 件以上必要とする。",
            "- `description` は %d 文字以内、`question` は %d 文字以内。"
            % (FINDING_TEXT_LIMITS["description"], FINDING_TEXT_LIMITS["question"]),
            "- 引用は `quotable_sources` の `span_text` から逐語で写す（要約しない）。",
        ]
    )


def limit_for(field_name: str) -> int | None:
    return FINDING_TEXT_LIMITS.get(field_name)


__all__ = ["FINDING_TEXT_LIMITS", "build_response_schema", "build_prompt", "limit_for"]
