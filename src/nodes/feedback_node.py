"""Feedback — record what the CAB actually decided, as a candidate.

This is the *post-review* accumulation path, not a dialogue path: it runs after
a review has concluded, to bank the judgement for next time. Correcting a draft
mid-run is HITL's job. The two have different purposes and different time
horizons, and neither substitutes for the other.

Two limits are structural:

* it appends to ``perspective_candidates`` **only**. It never writes the KB
  projection or ``review_perspectives``, so accepted feedback cannot silently
  become a recalled precedent -- promotion to the next criteria edition is
  human work outside the agent, and the agent never transitions ``status``.
* ``approved is True`` is required. Feedback that nobody signed off is not
  banked; ``decided_by`` comes from the authenticated caller, never from the
  request body.
"""

from __future__ import annotations

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.services import audit, contracts, projection, stores


class AccumulateFeedbackNode(FunctionNode):
    """Feedback path."""

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

    @audit.ensure_domain_event(audit.EVT_DOMAIN_PATH, node="accumulate_feedback")
    def execute(self, state: AgentState) -> dict[str, Any]:
        ctx = InvocationContext.from_state(state)
        scope = self._resolver.for_caller(ctx.caller_id)

        try:
            payload = _resolve_payload(scope, state, ctx)
        except stores.PayloadExpired:
            audit.emit(audit.EVT_FEEDBACK_REJECTED, {"reason": contracts.ERR_PAYLOAD_EXPIRED}, state)
            return {"status": AgentStatus.ERROR.value, "error_code": contracts.ERR_PAYLOAD_EXPIRED}

        if payload.get("approved") is not True:
            audit.emit(audit.EVT_FEEDBACK_REJECTED, {"reason": "not_approved"}, state)
            return {
                "status": "error",
                "error_code": contracts.ERR_INVALID_REQUEST,
            }

        outcome = payload.get("decision_outcome")
        action = payload.get("action")
        if outcome not in contracts.DECISION_OUTCOMES or action not in contracts.FEEDBACK_ACTIONS:
            audit.emit(audit.EVT_FEEDBACK_REJECTED, {"reason": "unknown_enum_value"}, state)
            return {"status": "error", "error_code": contracts.ERR_INVALID_REQUEST}

        if projection.text_quality_reason(str(payload.get("decision_rationale") or ""), **self._text_gate):
            audit.emit(audit.EVT_FEEDBACK_REJECTED, {"reason": "text_quality"}, state)
            return {"status": AgentStatus.ERROR.value, "error_code": contracts.ERR_INVALID_REQUEST}

        try:
            rationale_raw, rationale_audit, rationale_safe = projection.project(
                str(payload.get("decision_rationale") or ""), max_chars=self._max_rationale_chars
            )
        except projection.ProjectionRejected as exc:
            audit.emit(audit.EVT_FEEDBACK_REJECTED, {"reason": exc.reason}, state)
            return {"status": "error", "error_code": contracts.ERR_INVALID_REQUEST}
        if len(rationale_audit) > self._max_rationale_chars:
            audit.emit(audit.EVT_FEEDBACK_REJECTED, {"reason": "decision_rationale_too_long"}, state)
            return {"status": AgentStatus.ERROR.value, "error_code": contracts.ERR_INVALID_REQUEST}

        request_ref = str(payload.get("request_ref") or "")
        occurred_at = stores.utc_now_iso()
        request_row = scope.db.get_request(request_ref) or {}
        candidate_id = stores.alpha_digest(f"{request_ref}:{payload.get('claim_id')}:{occurred_at}", prefix="CAND")

        # decided_by is server-set from the authenticated caller. A client
        # cannot name somebody else as the decision maker.
        scope.db.append_candidate(
            {
                "candidate_id": candidate_id,
                "request_ref": request_ref,
                "claim_id": str(payload.get("claim_id") or ""),
                "perspective_code": str(payload.get("perspective_code") or ""),
                "condition_id": str(payload.get("condition_id") or ""),
                "criteria_version_ref": str(
                    request_row.get("criteria_version_ref") or payload.get("criteria_version_ref") or ""
                ),
                "criteria_content_hash": str(request_row.get("criteria_content_hash") or ""),
                "source_ids": list(payload.get("source_ids") or []),
                "decision_outcome": str(outcome),
                "decision_rationale_raw": rationale_raw,
                "decision_rationale_audit": rationale_audit,
                "decision_rationale_safe": rationale_safe,
                "action": str(action),
                "proposed_by": str(ctx.caller_id or ""),
                "proposed_at": occurred_at,
                # The agent never moves this forward; promotion is human work.
                "status": "pending",
            }
        )

        event = audit.build_event(
            correlation_id=str(ctx.correlation_id or ""),
            event_type=audit.EVT_FEEDBACK_APPENDED,
            target_type="request",
            target_ref=request_ref,
            occurred_at=occurred_at,
            actor_id=str(ctx.caller_id or ""),
            outcome="success",
            request_ref=request_ref,
        )
        event_id = scope.db.append_audit_event(event)

        audit.emit(
            audit.EVT_FEEDBACK_APPENDED,
            {
                "request_ref": request_ref,
                "candidate_id": candidate_id,
                "audit_event_id": event_id,
            },
            state,
        )

        summary = {"applied_count": 1, "audit_event_id": event_id}
        if set(summary) != contracts.FEEDBACK_SUMMARY_KEYS:  # pragma: no cover - constant
            raise ValueError("feedback summary drifted from the contract")
        return {"feedback_summary": summary, "status": AgentStatus.SUCCESS.value}


def _resolve_payload(scope: stores.ResourceScope, state: AgentState, ctx: InvocationContext) -> dict[str, Any]:
    """Resolve feedback locally so its rationale never enters State."""
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
    if not isinstance(raw, dict) or not isinstance(raw.get("feedback"), dict):
        raise stores.PayloadExpired("payload_not_an_object")
    return dict(raw["feedback"])


__all__ = ["AccumulateFeedbackNode"]
