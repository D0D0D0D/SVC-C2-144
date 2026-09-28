"""Stage 2 — recall past review perspectives (no LLM).

This is the accumulation moat in operation: the issues that actually came up in
past freeze-exception reviews are recalled so the current request can be
checked against them. It is the mechanised form of a human reviewer thinking
"we bounced one of these before, for this reason".

The verdict is handled carefully in **two directions**:

* it is **not** in the search axis — the KB projection has no
  ``decision_outcome`` at all, so "approved cases rank higher" cannot happen;
* it **is** joined back from the DB by ``history_ref`` after the hit, because a
  past ruling is an organisational fact and legitimate material for the CAB.

Excluding it from retrieval and disclosing it in the output are compatible only
through this route, which is why the join lives here rather than in the search.

An empty recall is a normal, successful outcome (``kb_empty``), not an error:
on day one there is nothing accumulated yet, and the template still returns a
useful packet from Stage 1 alone.
"""

from __future__ import annotations

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.services import audit, contracts, stores


class RecallPerspectivesNode(FunctionNode):
    """Stage 2."""

    _s4_audit_exempt: ClassVar[str] = (
        "emits through src/services/audit.py::emit, a module-level helper, and @audit.ensure_domain_event guarantees one event on every return and raise path — outside this gate's same-class one-hop ceiling"
    )

    required_trust_level = TrustLevel.VERIFIED_EXTERNAL

    def __init__(
        self, *, resolver: stores.AuthorizedResourceResolver, top_k: int = 8, min_recall_hits: int = 3
    ) -> None:
        self._resolver = resolver
        self._top_k = top_k
        self._min_recall_hits = min_recall_hits

    @audit.ensure_domain_event(audit.EVT_DOMAIN_PATH, node="recall_perspectives")
    def execute(self, state: AgentState) -> dict[str, Any]:
        ctx = InvocationContext.from_state(state)
        scope = self._resolver.for_caller(ctx.caller_id)

        request_ref = str(state.get("request_ref") or "")
        query = _build_query(state)

        hits = scope.kb.search(query, top_k=self._top_k, min_hits=self._min_recall_hits)

        recall_refs: list[dict[str, Any]] = []
        for hit in hits:
            history_ref = str(hit.get("history_ref") or "")
            # Deterministic join back to the record of origin. The verdict is
            # read here, never from the projection.
            record = scope.db.get_review_perspective(history_ref)
            if record is None or record.get("status") == stores.STATUS_DELETING:
                continue
            outcome = str(record.get("decision_outcome") or "")
            if outcome not in contracts.DECISION_OUTCOMES:
                # A record whose verdict is not in the closed enum is data we do
                # not understand; skipping it is safer than surfacing it.
                continue
            recall_refs.append(
                {
                    "history_ref": history_ref,
                    "condition_id": hit.get("condition_id"),
                    "criteria_version_ref": hit.get("criteria_version_ref"),
                    # Free text is always the `_safe` projection: putting `_raw`
                    # into State would open a direct route to the output.
                    "request_summary_safe": hit.get("request_summary_safe") or "",
                    "decision_rationale_safe": hit.get("decision_rationale_safe") or "",
                    "decision_outcome": outcome,
                }
            )

        result: dict[str, Any] = {
            "recall_refs": recall_refs,
            # evidence-gate contract key. Past perspectives are deliberately NOT
            # added to `source_registry`: they are not evidence *for this
            # request* and must not be citable as such.
            "retrieved_records": [
                {"record_id": ref["history_ref"], "kind": "review_perspective"} for ref in recall_refs
            ],
        }

        if not recall_refs:
            # Normal empty result, not a failure.
            result["degraded"] = True
            result["degradation_code"] = contracts.DEGRADATION_KB_EMPTY
            audit.emit(
                audit.EVT_DEGRADED,
                {
                    "request_ref": request_ref,
                    "degradation_code": contracts.DEGRADATION_KB_EMPTY,
                },
                state,
            )

        audit.emit(
            audit.EVT_STAGE2,
            {
                "request_ref": request_ref,
                "recall_count": len(recall_refs),
                "query_length": len(query),
            },
            state,
        )
        return result


def _build_query(state: AgentState) -> str:
    """Build the retrieval query from non-sensitive, already-structured values.

    The narrative body is NOT used: it lives behind ``payload_ref`` and pulling
    it here would put it back on a path that reaches State.
    """
    parts: list[str] = []
    for condition in state.get("criteria_conditions") or []:
        if isinstance(condition, dict):
            parts.append(str(condition.get("condition_ref") or ""))
    for entry in (state.get("source_registry") or {}).values():
        if isinstance(entry, dict):
            parts.append(str(entry.get("span_text") or ""))
    return " ".join(part for part in parts if part)


__all__ = ["RecallPerspectivesNode"]
