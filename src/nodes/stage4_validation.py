"""Stage 4 — machine verification of the draft (no LLM).

Every claim the model wrote is checked against the deterministic tables built
in Stage 1. A claim that fails is **dropped**, and dropping anything raises
``requires_human_review`` so the packet is not handed to the CAB as if it were
complete.

What is checked, and why each one exists:

* **source_id exists** — the model may only select from ``source_registry``; a
  minted identifier is a fabricated citation.
* **at least one source** — except for Stage 1's own deterministic gaps, which
  carry ``gap_origin: "deterministic"``. The model cannot emit that marker.
* **the quote is verbatim** — a paraphrase severs the link to the evidence.
* **the quoted span is not adjudicative** — this is the one that is easy to
  miss: quoting an evidence document's *own ruling* perfectly correctly still
  leaks a conclusion into the packet. Quotability is an allowlist over
  deterministic ``span_kind``, defaulting to not-quotable.
* **numbers appear in the cited source** — a stated rollback duration that
  exists nowhere in the evidence is not a finding, it is an invention.
* **condition_id belongs to the pinned edition** — a condition from another
  version means the packet is not a review against the declared criteria.
"""

from __future__ import annotations

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.services import audit, contracts, quantities, response_contract, service

_TEXT_FIELDS = ("description", "question")


class EvidenceValidationNode(FunctionNode):
    """Stage 4."""

    _s4_audit_exempt: ClassVar[str] = (
        "emits through src/services/audit.py::emit, a module-level helper, and @audit.ensure_domain_event guarantees one event on every return and raise path — outside this gate's same-class one-hop ceiling"
    )

    required_trust_level = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, min_quote_chars: int = 12) -> None:
        self._min_quote_chars = max(1, int(min_quote_chars))

    @audit.ensure_domain_event(audit.EVT_DOMAIN_PATH, node="evidence_validation")
    def execute(self, state: AgentState) -> dict[str, Any]:
        request_ref = str(state.get("request_ref") or "")
        registry: dict[str, dict[str, Any]] = dict(state.get("source_registry") or {})
        packet: dict[str, Any] = dict(state.get("review_packet") or {})
        conditions = {
            str(row.get("condition_id")) for row in (state.get("criteria_conditions") or []) if isinstance(row, dict)
        }
        recall_by_ref = {
            str(ref.get("history_ref")): ref for ref in (state.get("recall_refs") or []) if isinstance(ref, dict)
        }

        needs_review = bool(state.get("requires_human_review"))
        dropped: list[dict[str, str]] = []
        validated: dict[str, list[dict[str, Any]]] = {}

        for section, fields in contracts.FINDING_FIELDS_BY_SECTION.items():
            kept: list[dict[str, Any]] = []
            for finding in packet.get(section) or []:
                if not isinstance(finding, dict):
                    continue
                reason = _reject_reason(
                    finding,
                    section=section,
                    registry=registry,
                    conditions=conditions,
                    min_quote_chars=self._min_quote_chars,
                )
                if reason is not None:
                    dropped.append(
                        {"section": section, "claim_id": str(finding.get("claim_id") or ""), "reason": reason}
                    )
                    needs_review = True
                    continue
                row = {name: finding.get(name) for name in fields if name in finding}
                if section == "evidence_gaps":
                    # Stamp the origin here: the model's projection has no
                    # `gap_origin` field, so a model-authored gap can never
                    # claim to be a deterministic one.
                    row["gap_origin"] = "llm"
                kept.append(row)
            validated[section] = kept

        # Stage 1's deterministic gaps are merged in unchanged.
        deterministic_gaps = [gap for gap in (state.get("field_gaps") or []) if isinstance(gap, dict)]
        validated["evidence_gaps"] = deterministic_gaps + validated.get("evidence_gaps", [])

        precedents, precedent_dropped = _validate_precedents(
            packet.get("precedent_checks") or [], recall_by_ref, registry, self._min_quote_chars
        )
        if precedent_dropped:
            dropped.extend(precedent_dropped)
            needs_review = True

        cited: list[str] = []
        for section, items in validated.items():
            for item in items:
                for source_id in item.get("source_ids") or []:
                    if source_id not in cited:
                        cited.append(source_id)
        for item in precedents:
            for source_id in item.get("source_ids") or []:
                if source_id not in cited:
                    cited.append(source_id)

        for entry in dropped:
            audit.emit(
                audit.EVT_CLAIM_DROPPED,
                {
                    "request_ref": request_ref,
                    "section": entry["section"],
                    "claim_id": entry["claim_id"],
                    "reason": entry["reason"],
                },
                state,
            )

        audit.emit(
            audit.EVT_STAGE4,
            {
                "request_ref": request_ref,
                "dropped": len(dropped),
                "cited_sources": len(cited),
                "requires_human_review": needs_review,
            },
            state,
        )

        result: dict[str, Any] = {
            "review_packet": {**validated, "precedent_checks": precedents},
            "cited_source_ids": cited,
            # Terminal node of the invoke path.
            "status": AgentStatus.SUCCESS.value,
        }
        if needs_review:
            result["requires_human_review"] = True
        return result


def _reject_reason(
    finding: dict[str, Any],
    *,
    section: str,
    registry: dict[str, dict[str, Any]],
    conditions: set[str],
    min_quote_chars: int,
) -> str | None:
    source_ids = finding.get("source_ids")
    source_ids = source_ids if isinstance(source_ids, list) else []

    # Only Stage 1 may produce a source-less gap, and it does not come through
    # here -- so any model finding without a source is rejected.
    if not source_ids:
        return "no_source"

    for source_id in source_ids:
        entry = registry.get(source_id)
        if entry is None:
            return "unknown_source"
        if not service.is_quotable(str(entry.get("span_kind") or "")):
            # Quoting the evidence document's own ruling, however faithfully,
            # puts a conclusion in the packet.
            return "non_quotable_span"
        if len(str(entry.get("span_text") or "")) < min_quote_chars:
            return "span_too_short"

    condition_id = finding.get("condition_id")
    if condition_id and str(condition_id) not in conditions:
        return "unknown_condition"

    source_texts = [str(registry[s].get("span_text") or "") for s in source_ids]

    for field in _TEXT_FIELDS:
        text = finding.get(field)
        if not isinstance(text, str) or not text:
            continue

        # The limit is enforced here, not only stated in the prompt. Asking the
        # model nicely is not a control: an over-long `description` is how a
        # request body gets copied wholesale into a finding, and from there
        # into State and the checkpoint.
        limit = response_contract.limit_for(field)
        if limit is not None and len(text) > limit:
            # Drop, never truncate: truncating breaks the verbatim property the
            # quote check relies on, so the finding would fail later for a
            # different reason and the real cause would be unreadable.
            return f"{field}_over_limit"

        supported, needs_human = quantities.claim_numbers_supported(text, source_texts)
        if not supported:
            return "number_not_in_source"
        if needs_human:
            # Not a rejection: the value could not be normalised, so it is
            # reported as unverified rather than passed off as checked.
            finding.setdefault("_unverified_quantity", True)

    quote = finding.get("quote_span")
    if isinstance(quote, str) and quote:
        if len(quote) < min_quote_chars:
            # An empty or 1-character "quote" makes `quote in source` vacuous.
            return "quote_too_short"
        if not any(quote in text for text in source_texts):
            return "quote_not_verbatim"

    return None


def _validate_precedents(
    drafts: list[Any],
    recall_by_ref: dict[str, dict[str, Any]],
    registry: dict[str, dict[str, Any]],
    min_quote_chars: int,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Build ``precedent_checks`` from the model's status plus KB facts.

    ``prior_issue`` and ``prior_outcome`` are copied from what Stage 2 read out
    of the record of origin. The model never authors them: summarising a past
    fact silently changes it.
    """
    out: list[dict[str, Any]] = []
    dropped: list[dict[str, str]] = []

    for draft in drafts:
        if not isinstance(draft, dict):
            continue
        history_ref = str(draft.get("history_ref") or "")
        recalled = recall_by_ref.get(history_ref)
        if recalled is None:
            dropped.append({"section": "precedent_checks", "claim_id": history_ref, "reason": "unknown_history_ref"})
            continue

        status = str(draft.get("status") or "")
        if status not in contracts.PRECEDENT_STATUSES:
            dropped.append({"section": "precedent_checks", "claim_id": history_ref, "reason": "unknown_status"})
            continue

        source_ids = draft.get("source_ids")
        source_ids = source_ids if isinstance(source_ids, list) else []
        if any(source_id not in registry for source_id in source_ids):
            dropped.append({"section": "precedent_checks", "claim_id": history_ref, "reason": "unknown_source"})
            continue
        if status in ("addressed", "unclear") and not source_ids:
            # "It is addressed" needs to say where; "not_addressed" cannot.
            dropped.append({"section": "precedent_checks", "claim_id": history_ref, "reason": "status_requires_source"})
            continue
        if any(not service.is_quotable(str(registry[s].get("span_kind") or "")) for s in source_ids):
            dropped.append({"section": "precedent_checks", "claim_id": history_ref, "reason": "non_quotable_span"})
            continue
        if any(len(str(registry[s].get("span_text") or "")) < min_quote_chars for s in source_ids):
            dropped.append({"section": "precedent_checks", "claim_id": history_ref, "reason": "span_too_short"})
            continue

        row = {
            "history_ref": history_ref,
            "condition_id": recalled.get("condition_id"),
            # Deterministic copies -- always the `_safe` projection.
            "prior_issue": recalled.get("decision_rationale_safe") or "",
            "prior_outcome": recalled.get("decision_outcome") or "",
            "status": status,
            "source_ids": source_ids,
        }
        out.append(row)

    return out, dropped


__all__ = ["EvidenceValidationNode"]
