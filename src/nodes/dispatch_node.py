"""Inner entry — resolve the inner envelope and expand it into inner State.

The inner hop has the same problem as the outer one: ``GraphNode.execute()``
calls ``subgraph.invoke(extract_input(state), ...)``, so whatever
``extract_input`` returns becomes the inner ``user_input`` and is written to
inner State before any inner node runs — where the inner ``InitializeNode``'s
S-2 gate scans it.

So the inner graph receives a ref too, and resolves it here, first thing, into
custom typed keys. Mode routing happens *after* this node, because the mode
itself is inside the envelope.
"""

from __future__ import annotations

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.services import audit, contracts, envelope

_ENVELOPE_KEYS = (
    "mode",
    "request_ref",
    "payload_ref",
    "criteria_version_ref",
    "criteria_version",
    "criteria_content_hash",
    "freeze_calendar_ref",
    "freeze_calendar_version",
    "freeze_calendar_content_hash",
    "sealed_refs",
    "planned_window",
    "evidence_declarations",
    "ingress_evidence_gaps",
    "evaluated_at",
    "fields_present",
    "refine_count",
)


class DispatchNode(FunctionNode):
    """Inner graph entry point."""

    _s4_audit_exempt: ClassVar[str] = (
        "emits through src/services/audit.py::emit, a module-level helper, and @audit.ensure_domain_event guarantees one event on every return and raise path — outside this gate's same-class one-hop ceiling"
    )

    required_trust_level = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, envelope_store: Any) -> None:
        self._envelopes = envelope_store

    @audit.ensure_domain_event(audit.EVT_DOMAIN_PATH, node="dispatch")
    def execute(self, state: AgentState) -> dict[str, Any]:
        ctx = InvocationContext.from_state(state)

        # On a resumed run the envelope has already been expanded into State
        # and `user_input` is no longer a fresh ref; keep what is there.
        if state.get("mode"):
            audit.emit(
                audit.EVT_INTAKE,
                {
                    "mode": str(state.get("mode") or ""),
                    "phase": "inner_dispatch_already_expanded",
                },
                state,
            )
            return {}

        ref = state.get("user_input")
        if not envelope.looks_like_ref(ref):
            audit.emit(audit.EVT_INPUT_REJECTED, {"reason": "inner_envelope_ref_malformed"}, state)
            return {"status": "error", "error_code": contracts.ERR_INVALID_REQUEST}

        try:
            payload = envelope.take(self._envelopes, str(ref), session_id=str(ctx.session_id or ""))
        except envelope.EnvelopeUnavailable as exc:
            audit.emit(audit.EVT_INPUT_REJECTED, {"reason": f"inner_envelope_unavailable:{exc}"}, state)
            return {"status": "error", "error_code": contracts.ERR_INVALID_REQUEST}

        expanded: dict[str, Any] = {key: payload[key] for key in _ENVELOPE_KEYS if payload.get(key) is not None}
        expanded.setdefault("refine_count", 0)
        audit.emit(
            audit.EVT_INTAKE,
            {
                "mode": str(expanded.get("mode") or ""),
                "phase": "inner_dispatch_resolved",
            },
            state,
        )
        return expanded


__all__ = ["DispatchNode"]
