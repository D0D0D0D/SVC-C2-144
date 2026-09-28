"""Exact-key contracts for SVC-C2-144.

This module is the **single source** for every closed key set and enum in the
output envelope. Prose in ``docs/02_design.md`` describes these constants; it
never re-lists them, and neither does any other module. Two definition sites is
how ``hitl_limit_reached`` ended up accepted by the state machine while the S-3
gate still rejected it.

The prompt contract in ``src/services/response_contract.py`` and the type
projection applied to the model's reply are both generated from
``FINDING_FIELDS_BY_SECTION`` here, so a field can never be added to one side
only.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# top-level envelope keys, per mode
# ---------------------------------------------------------------------------
INVOKE_TOP_LEVEL_KEYS = frozenset(
    {
        "mode",
        "gate_kind",
        "meta",
        "applied_pins",
        "criteria_conditions",
        "evidence_sources",
        "freeze_window_facts",
        "supported_conditions",
        "evidence_gaps",
        "contradictions",
        "approval_scope_discrepancies",
        "precedent_checks",
        "verification_questions",
    }
)
INGEST_TOP_LEVEL_KEYS = frozenset({"mode", "gate_kind", "meta", "ingest_summary"})
FEEDBACK_TOP_LEVEL_KEYS = frozenset({"mode", "gate_kind", "meta", "feedback_summary"})
DELETE_TOP_LEVEL_KEYS = frozenset({"status", "deleted", "audit_event_id"})

GATE_KIND = "freeze_exception_evidence"

# ---------------------------------------------------------------------------
# finding sections -- prompt contract and type projection both derive from this
# ---------------------------------------------------------------------------
FINDING_FIELDS_BY_SECTION: dict[str, tuple[str, ...]] = {
    "supported_conditions": ("claim_id", "description", "condition_id", "source_ids", "severity", "confidence"),
    "evidence_gaps": ("claim_id", "description", "condition_id", "source_ids", "gap_origin"),
    "contradictions": ("claim_id", "description", "condition_id", "source_ids"),
    "approval_scope_discrepancies": ("claim_id", "description", "source_ids"),
    "verification_questions": ("claim_id", "question", "condition_id", "source_ids"),
}

# Nullable values are still required keys. Keep this section-qualified:
# support is defined against a criterion, but a gap, contradiction or follow-up
# question may concern the request as a whole. Confidence may be unknown
# without turning a supported finding into a fabricated numeric estimate.
NULLABLE_FINDING_FIELDS = frozenset(
    {
        ("evidence_gaps", "condition_id"),
        ("contradictions", "condition_id"),
        ("verification_questions", "condition_id"),
        ("supported_conditions", "confidence"),
    }
)

# ---------------------------------------------------------------------------
# nested exact-key sets
# ---------------------------------------------------------------------------
META_KEYS = frozenset({"degraded", "degradation_code", "requires_human_review"})
EVIDENCE_SOURCE_KEYS = frozenset({"source_id", "doc_type", "quote_span", "span_kind", "provenance"})
PROVENANCE_KEYS = frozenset({"request_ref", "source_id", "doc_ref", "span_ref", "criteria_version_ref"})
FREEZE_FACT_KEYS = frozenset(
    {"freeze_calendar_ref", "planned_window", "overlapping_windows", "evaluated", "evaluated_at"}
)
# When no freeze calendar is pinned, all three calendar values are null.
APPLIED_PIN_KEYS = frozenset(
    {
        "criteria_version_ref",
        "criteria_version",
        "criteria_content_hash",
        "freeze_calendar_ref",
        "freeze_calendar_version",
        "freeze_calendar_content_hash",
    }
)
CRITERIA_CONDITION_KEYS = frozenset({"condition_id", "criteria_version_ref", "condition_ref"})
# Output shape of a precedent check.
PRECEDENT_CHECK_KEYS = frozenset(
    {"history_ref", "condition_id", "prior_issue", "prior_outcome", "status", "source_ids"}
)
# The subset the model is allowed to return: `prior_issue` and `prior_outcome`
# are copied deterministically from the KB, never authored by the model.
PRECEDENT_DRAFT_KEYS = frozenset({"history_ref", "status", "source_ids"})
PRECEDENT_STATUSES = frozenset({"addressed", "not_addressed", "unclear"})
PLANNED_WINDOW_KEYS = frozenset({"start", "end"})
OVERLAPPING_WINDOW_KEYS = frozenset({"window_id", "window_ref", "overlap_start", "overlap_end"})
INGEST_SUMMARY_KEYS = frozenset({"written_ids", "rejected"})
REJECTED_ITEM_KEYS = frozenset({"index", "record_type", "reason"})
FEEDBACK_SUMMARY_KEYS = frozenset({"applied_count", "audit_event_id"})
AUDIT_EVENT_KEYS = frozenset(
    {
        "event_id",
        "correlation_id",
        "event_type",
        "request_ref",
        "occurred_at",
        "actor_id",
        "approval_ref",
        "outcome",
        "deleted_counts",
        "error_code",
        "target_type",
        "target_ref",
    }
)
DELETED_KEYS = frozenset({"db_rows", "kb_docs", "payloads"})

# ---------------------------------------------------------------------------
# span_kind -- quotability is an allowlist, the default is NOT quotable
# ---------------------------------------------------------------------------
SPAN_KIND_ADJUDICATIVE = "adjudicative"
SPAN_KIND_OPINION = "opinion"
SPAN_KIND_QUANTITY = "quantity"
SPAN_KIND_DATE = "date"
SPAN_KIND_IDENTIFIER = "identifier"
SPAN_KIND_OBSERVED_CONDITION = "observed_condition"
SPAN_KIND_PROCEDURAL_RECORD = "procedural_record"
SPAN_KIND_UNCLASSIFIED = "unclassified"

QUOTABLE_SPAN_KINDS = frozenset(
    {
        SPAN_KIND_QUANTITY,
        SPAN_KIND_DATE,
        SPAN_KIND_IDENTIFIER,
        SPAN_KIND_OBSERVED_CONDITION,
        SPAN_KIND_PROCEDURAL_RECORD,
    }
)

# ---------------------------------------------------------------------------
# enums shared by /ingest and /feedback -- defined once so the two entry points
# cannot drift
# ---------------------------------------------------------------------------
DECISION_OUTCOMES = frozenset({"approved", "rejected", "withdrawn", "deferred"})
FEEDBACK_ACTIONS = frozenset({"confirm", "amend", "discard"})
DOC_TYPES = frozenset(
    {
        "change_request",
        "approval_record",
        "rollback_plan",
        "risk_assessment",
        "test_evidence",
        "monitoring_evidence",
        "vendor_notice",
        "other",
    }
)
GAP_ORIGINS = frozenset({"deterministic", "llm"})
SEVERITIES = frozenset({"candidate"})  # the agent never emits a confirmed severity
RECORD_TYPES = frozenset({"case_evidence", "review_perspective"})

# ---------------------------------------------------------------------------
# degradation codes -- MUST stay identical to the set the S-3 gate accepts.
# Adding a value on one side only makes the run fail reject-condition #11.
# ---------------------------------------------------------------------------
DEGRADATION_KB_EMPTY = "kb_empty"
DEGRADATION_LLM_UNAVAILABLE = "llm_unavailable"
DEGRADATION_DISCARDED = "discarded_by_reviewer"
DEGRADATION_HITL_LIMIT = "hitl_limit_reached"

DEGRADATION_CODES = frozenset(
    {
        DEGRADATION_KB_EMPTY,
        DEGRADATION_LLM_UNAVAILABLE,
        DEGRADATION_DISCARDED,
        DEGRADATION_HITL_LIMIT,
    }
)

# ---------------------------------------------------------------------------
# HITL routing
# ---------------------------------------------------------------------------
REFINE_CONFIRM = "confirm"
REFINE_REFINE = "refine"
REFINE_DISCARD = "discard"
REFINE_ACTIONS = frozenset({REFINE_CONFIRM, REFINE_REFINE, REFINE_DISCARD})

# ---------------------------------------------------------------------------
# error codes (fail-closed made visible)
# ---------------------------------------------------------------------------
ERR_INVALID_REQUEST = "invalid_request"
ERR_CUSTOMER_RESOLUTION = "customer_resolution_failed"
ERR_CRITERIA_VERSION_MISSING = "criteria_version_missing"
ERR_CRITERIA_VERSION_UNKNOWN = "criteria_version_unknown"
ERR_CRITERIA_HASH_MISMATCH = "criteria_content_hash_mismatch"
ERR_CRITERIA_NOT_EFFECTIVE = "criteria_version_not_effective"
ERR_CALENDAR_UNKNOWN = "freeze_calendar_unknown"
ERR_CALENDAR_HASH_MISMATCH = "freeze_calendar_content_hash_mismatch"
ERR_CALENDAR_NOT_EFFECTIVE = "freeze_calendar_not_effective"
ERR_CRITERIA_SEAL_UNRESOLVED = "criteria_seal_unresolved"
ERR_CALENDAR_SEAL_UNRESOLVED = "freeze_calendar_seal_unresolved"
ERR_EVALUATED_AT_MISSING = "evaluated_at_missing"
ERR_PAYLOAD_EXPIRED = "payload_expired"
ERR_LLM_CALL_FAILED = "llm_call_failed"
ERR_OUTPUT_CONTRACT = "output_contract_violation"
ERR_REQUEST_NOT_FOUND = "request_not_found"
ERR_PERSPECTIVE_NOT_FOUND = "perspective_not_found"
ERR_DELETE_INCOMPLETE = "delete_incomplete"
ERR_APPROVAL_REF_MISSING = "approval_ref_missing"
ERR_HITL_EXPIRED = "hitl_expired"

# Only these inner-graph error codes may cross the GraphNode boundary.  The
# protected Cat2 skeleton recognises codes from ``error_log`` when operating in
# ``handle`` mode; arbitrary inner/provider strings are never returned.
INNER_FAILURE_CODES = (
    ERR_INVALID_REQUEST,
    ERR_CRITERIA_SEAL_UNRESOLVED,
    ERR_CALENDAR_SEAL_UNRESOLVED,
    ERR_EVALUATED_AT_MISSING,
    ERR_PAYLOAD_EXPIRED,
    ERR_LLM_CALL_FAILED,
    ERR_OUTPUT_CONTRACT,
)
