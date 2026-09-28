"""Pre-process — resolve the internal envelope into typed State keys.

The entry adapter has already rejected undeclared keys, moved the four
narrative fields into the payload store, fixed ``evaluated_at``, and put the
whole request envelope into the envelope store. What arrives here as
``user_input`` is a single 32-character handle.

🔴 The expansion goes into **custom typed State keys, never into
``validated_input``.** ``validated_input`` is one of the fields the S-2 gate
scans, so writing the envelope there would have the next node's gate rewrite
customer-named identifiers to ``[MASKED]`` — the exact failure the ref
indirection exists to avoid (``src/services/envelope.py``).
"""

from __future__ import annotations

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.services import audit, contracts, envelope, projection, service, stores

_MODES = frozenset({"invoke", "ingest", "feedback"})

# Keys the envelope may carry into State. An allowlist, so a malformed or
# tampered envelope cannot introduce a field the graph never declared.
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

_WITHHELD_SPAN = "[INSTRUCTION WITHHELD]"


class FreezeExceptionIntakePreProcessNode(FunctionNode):
    """Outer ``pre_process`` slot."""

    _s4_audit_exempt: ClassVar[str] = (
        "emits through src/services/audit.py::emit, a module-level helper, and @audit.ensure_domain_event guarantees one event on every return and raise path — outside this gate's same-class one-hop ceiling"
    )

    required_trust_level = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, resolver: stores.AuthorizedResourceResolver, envelope_store: Any) -> None:
        self._resolver = resolver
        self._envelopes = envelope_store

    @audit.ensure_domain_event(audit.EVT_DOMAIN_PATH, node="pre_process")
    def execute(self, state: AgentState) -> dict[str, Any]:
        ctx = InvocationContext.from_state(state)

        try:
            # Resolve early: an unauthorised caller fails before any node
            # touches a store, and there is no default connection to fall to.
            self._resolver.for_caller(ctx.caller_id)
        except stores.AuthorizationError:
            audit.emit(audit.EVT_AUTHZ_FAILED, {"reason": contracts.ERR_CUSTOMER_RESOLUTION}, state)
            return {"status": "error", "error_code": contracts.ERR_CUSTOMER_RESOLUTION}

        ref = state.get("user_input")
        if not envelope.looks_like_ref(ref):
            # Either the caller bypassed the adapter, or S-2 rewrote the handle
            # — both are fail-closed, and neither should be guessed at.
            audit.emit(audit.EVT_INPUT_REJECTED, {"reason": "envelope_ref_malformed"}, state)
            return {"status": "error", "error_code": contracts.ERR_INVALID_REQUEST}

        try:
            payload = envelope.take(self._envelopes, str(ref), session_id=str(ctx.session_id or ""))
        except envelope.EnvelopeUnavailable as exc:
            audit.emit(audit.EVT_INPUT_REJECTED, {"reason": f"envelope_unavailable:{exc}"}, state)
            return {"status": "error", "error_code": contracts.ERR_INVALID_REQUEST}

        mode = str(payload.get("mode") or "")
        if mode not in _MODES:
            audit.emit(audit.EVT_INPUT_REJECTED, {"mode": mode, "reason": contracts.ERR_INVALID_REQUEST}, state)
            return {"status": "error", "error_code": contracts.ERR_INVALID_REQUEST}

        expanded: dict[str, Any] = {key: payload[key] for key in _ENVELOPE_KEYS if payload.get(key) is not None}
        if mode == "invoke":
            declarations, gaps = _sanitise_evidence_declarations(expanded.get("evidence_declarations") or [])
            expanded["evidence_declarations"] = declarations
            if gaps:
                expanded["ingress_evidence_gaps"] = gaps
                expanded["requires_human_review"] = True
        # Always present downstream, so nothing has to treat "absent" as zero.
        expanded.setdefault("refine_count", 0)

        audit.emit(
            audit.EVT_INTAKE,
            {
                "mode": mode,
                "request_ref": str(expanded.get("request_ref") or ""),
                "has_payload_ref": bool(expanded.get("payload_ref")),
                "evidence_count": len(expanded.get("evidence_declarations") or []),
            },
            state,
        )

        return expanded


def _sanitise_evidence_declarations(
    declarations: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Project submitted spans before they can enter State/checkpoints.

    Document and span ordinals are preserved even for withheld spans so the
    source handle mentioned by the deterministic gap stays stable.  Customer
    supplied ``doc_ref``/``span_ref`` values are deliberately not copied into
    the gap text: identifier-shaped refs can be false positives for the output
    PII scanner.
    """
    safe_documents: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    rows = declarations if isinstance(declarations, list) else []
    for doc_index, document in enumerate(rows, start=1):
        if not isinstance(document, dict):
            continue
        safe_spans: list[dict[str, str]] = []
        spans = document.get("spans")
        for span_index, span in enumerate(spans if isinstance(spans, list) else [], start=1):
            if not isinstance(span, dict):
                continue
            safe_text = projection.normalise_and_mask(str(span.get("span_text") or ""))
            source_id = service.mint_source_id(doc_index, span_index)
            if projection.contains_instruction(safe_text):
                safe_text = _WITHHELD_SPAN
                gaps.append(
                    {
                        "claim_id": f"DGINJECTION{source_id}",
                        "description": (f"証跡 {source_id} は指示形を含むため引用候補から除外しました。"),
                        "condition_id": None,
                        "source_ids": [],
                        "gap_origin": "deterministic",
                    }
                )
            safe_spans.append(
                {
                    "span_ref": str(span.get("span_ref") or ""),
                    "span_text": safe_text,
                }
            )
        safe_documents.append(
            {
                "doc_type": str(document.get("doc_type") or ""),
                "doc_ref": str(document.get("doc_ref") or ""),
                "spans": safe_spans,
            }
        )
    return safe_documents, gaps


__all__ = ["FreezeExceptionIntakePreProcessNode"]
