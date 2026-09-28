"""Ingest — take in case evidence and past review perspectives.

Two record types only. Anything else — including ``exception_criteria`` and
``freeze_calendar`` — is rejected by row, because **the agent has no write
access to control documents**. Letting feedback flow back into the criteria
table would mean the agent is authoring the approval criteria, which does not
work as a change-management control.

Rows are validated and rejected individually (the request as a whole still
returns 200 with a ``rejected[]`` list), but the two stores are kept
consistent: if the KB projection write fails after the DB row landed, that
row is compensated back out so a half-written perspective cannot be recalled.
"""

from __future__ import annotations

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.services import audit, contracts, freeze_window, projection, service, stores

_CASE_EVIDENCE_REQUIRED = ("request_ref", "source_id", "doc_type", "doc_ref", "span_ref", "span_text", "submitted_at")
_PERSPECTIVE_REQUIRED = (
    "history_ref",
    "criteria_version_ref",
    "criteria_content_hash",
    "condition_id",
    "request_summary",
    "decision_outcome",
    "decision_rationale",
    "approved_by",
    "approved_at",
)


class AccumulateIngestNode(FunctionNode):
    """Ingest path."""

    _s4_audit_exempt: ClassVar[str] = (
        "emits through src/services/audit.py::emit, a module-level helper, and @audit.ensure_domain_event guarantees one event on every return and raise path — outside this gate's same-class one-hop ceiling"
    )

    required_trust_level = TrustLevel.VERIFIED_EXTERNAL

    def __init__(
        self,
        *,
        resolver: stores.AuthorizedResourceResolver,
        max_rationale_chars: int = 1000,
        text_gate_config: dict[str, Any] | None = None,
    ) -> None:
        self._resolver = resolver
        self._max_rationale_chars = max_rationale_chars
        config = text_gate_config or {}
        self._text_gate: projection.TextQualityConfig = {
            "max_replacement_ratio": float(config.get("text_gate_max_replacement_ratio", 0.02)),
            "max_control_ratio": float(config.get("text_gate_max_control_ratio", 0.01)),
            "min_readable_ratio": float(config.get("text_gate_min_readable_ratio", 0.80)),
            "max_chars": int(config.get("text_gate_max_chars", 20000)),
        }

    @audit.ensure_domain_event(audit.EVT_DOMAIN_PATH, node="accumulate_ingest")
    def execute(self, state: AgentState) -> dict[str, Any]:
        ctx = InvocationContext.from_state(state)
        scope = self._resolver.for_caller(ctx.caller_id)

        try:
            payload = _resolve_payload(scope, state, ctx)
        except stores.PayloadExpired:
            audit.emit(audit.EVT_INPUT_REJECTED, {"reason": contracts.ERR_PAYLOAD_EXPIRED}, state)
            return {"status": AgentStatus.ERROR.value, "error_code": contracts.ERR_PAYLOAD_EXPIRED}
        rows = payload.get("records")
        rows = rows if isinstance(rows, list) else []

        written: list[str] = []
        rejected: list[dict[str, Any]] = []

        for index, row in enumerate(rows):
            record_type = row.get("record_type") if isinstance(row, dict) else None
            reason = _row_reject_reason(row, record_type, text_gate=self._text_gate)
            if reason is not None:
                rejected.append({"index": index, "record_type": str(record_type or ""), "reason": reason})
                audit.emit(
                    audit.EVT_ROW_REJECTED,
                    {"index": index, "record_type": str(record_type or ""), "reason": reason},
                    state,
                )
                continue

            try:
                if record_type == "case_evidence":
                    written.append(_write_case_evidence(scope, row))
                else:
                    written.append(_write_perspective(scope, row, max_chars=self._max_rationale_chars))
            except projection.ProjectionRejected as exc:
                rejected.append({"index": index, "record_type": str(record_type), "reason": exc.reason})
                audit.emit(
                    audit.EVT_ROW_REJECTED,
                    {"index": index, "record_type": str(record_type), "reason": exc.reason},
                    state,
                )
            except Exception as exc:  # noqa: BLE001
                rejected.append({"index": index, "record_type": str(record_type), "reason": type(exc).__name__})
                audit.emit(
                    audit.EVT_ROW_REJECTED,
                    {"index": index, "record_type": str(record_type), "reason": type(exc).__name__},
                    state,
                )

        summary = {"written_ids": written, "rejected": rejected}
        if set(summary) != contracts.INGEST_SUMMARY_KEYS:  # pragma: no cover - constant
            raise ValueError("ingest summary drifted from the contract")

        audit.emit(
            audit.EVT_INTAKE,
            {
                "mode": "ingest",
                "written": len(written),
                "rejected": len(rejected),
            },
            state,
        )
        # Terminal node of this mode: set the status explicitly. Leaving it
        # `pending` makes the OUTER route() treat the run as "not success"
        # and skip post_process entirely -- silently, with no error.
        return {"ingest_summary": summary, "status": AgentStatus.SUCCESS.value}


def _resolve_payload(scope: stores.ResourceScope, state: AgentState, ctx: InvocationContext) -> dict[str, Any]:
    """Resolve raw ingest rows locally; never return them into graph State."""
    ref = state.get("payload_ref")
    if not ref:
        raise stores.PayloadExpired("payload_absent")
    try:
        raw = scope.payloads.resolve(
            ref,
            scope={"request_ref": str(state.get("request_ref") or "")},
            session_id=str(ctx.session_id or ""),
        )
    except Exception as exc:  # noqa: BLE001 - stable error below
        raise stores.PayloadExpired(type(exc).__name__) from exc
    if not isinstance(raw, dict):
        raise stores.PayloadExpired("payload_not_an_object")
    return raw


def _row_reject_reason(row: Any, record_type: Any, *, text_gate: projection.TextQualityConfig) -> str | None:
    if not isinstance(row, dict):
        return "not_an_object"
    if record_type not in contracts.RECORD_TYPES:
        # `exception_criteria` / `freeze_calendar` land here by design.
        return "unknown_record_type"

    required = _CASE_EVIDENCE_REQUIRED if record_type == "case_evidence" else _PERSPECTIVE_REQUIRED
    declared = set(required) | {"record_type", "perspective_code"}
    undeclared = set(row) - declared
    if undeclared:
        return f"undeclared_keys:{','.join(sorted(undeclared))}"
    missing = [name for name in required if not row.get(name)]
    if missing:
        return f"missing:{','.join(missing)}"

    for field in ("submitted_at", "approved_at"):
        if field in row:
            try:
                freeze_window.parse_aware(row[field], field=field)
            except freeze_window.WindowFormatError as exc:
                return str(exc)

    if record_type == "review_perspective":
        if row.get("decision_outcome") not in contracts.DECISION_OUTCOMES:
            return "unknown_decision_outcome"
        text_fields: tuple[str, ...] = ("request_summary", "decision_rationale")
    else:
        if row.get("doc_type") not in contracts.DOC_TYPES:
            return "unknown_doc_type"
        text_fields = ("span_text",)
    for field in text_fields:
        reason = projection.text_quality_reason(str(row.get(field) or ""), **text_gate)
        if reason:
            return f"{field}:{reason}"
    return None


def _write_case_evidence(scope: stores.ResourceScope, row: dict[str, Any]) -> str:
    source_id = str(row["source_id"])
    scope.db.add_case_evidence(
        {
            "source_id": source_id,
            "request_ref": str(row["request_ref"]),
            "doc_type": str(row["doc_type"]),
            "doc_ref": str(row["doc_ref"]),
            "span_ref": str(row["span_ref"]),
            "span_text": str(row["span_text"]),
            "span_kind": service.classify_span_kind(str(row["span_text"])),
            "submitted_at": str(row["submitted_at"]),
        }
    )
    return source_id


def _write_perspective(scope: stores.ResourceScope, row: dict[str, Any], *, max_chars: int) -> str:
    """Write the record of origin and its projection, or neither."""
    history_ref = str(row["history_ref"])

    summary_raw, summary_audit, summary_safe = projection.project(str(row["request_summary"]), max_chars=max_chars)
    rationale_raw, rationale_audit, rationale_safe = projection.project(
        str(row["decision_rationale"]), max_chars=max_chars
    )

    scope.db.add_review_perspective(
        {
            "history_ref": history_ref,
            "condition_id": str(row["condition_id"]),
            "criteria_version_ref": str(row["criteria_version_ref"]),
            "criteria_content_hash": str(row["criteria_content_hash"]),
            "request_summary_raw": summary_raw,
            "request_summary_audit": summary_audit,
            "request_summary_safe": summary_safe,
            "decision_outcome": str(row["decision_outcome"]),
            "decision_rationale_raw": rationale_raw,
            "decision_rationale_audit": rationale_audit,
            "decision_rationale_safe": rationale_safe,
            "approved_by": str(row["approved_by"]),
            "approved_at": str(row["approved_at"]),
        }
    )

    try:
        scope.kb.upsert(
            {
                "history_ref": history_ref,
                "condition_id": str(row["condition_id"]),
                "criteria_version_ref": str(row["criteria_version_ref"]),
                "criteria_content_hash": str(row["criteria_content_hash"]),
                "request_summary_safe": summary_safe,
                "decision_rationale_safe": rationale_safe,
                "perspective_code": str(row.get("perspective_code") or ""),
            }
        )
    except Exception:
        # Compensate: a DB row whose projection is missing would be invisible
        # to recall while still counting as written.
        scope.db.review_perspectives.pop(history_ref, None)
        raise

    return history_ref


__all__ = ["AccumulateIngestNode"]
