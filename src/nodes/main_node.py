"""Main slot — the ``GraphNode`` wrapping the inner domain workflow.

Kept thin on purpose: the domain logic lives in the inner graph. What this
class contributes is the transport between the two hops and the merge contract
between the two state schemas.

Three inherited properties are load-bearing:

* the inner graph is built **once and cached** — a fresh instance per call
  loses the suspended HITL checkpoint and re-enters ``interrupt()`` forever;
* the inner checkpointer is a **separate instance** — sharing one saver with
  the outer graph collides on the same ``thread_id`` and makes resume restart;
* ``propagate_hitl = True`` — the inner ``interrupt()`` has to surface through
  the outer pipeline for ``/resume`` to reach it.

And one thing this class overrides deliberately: ``extract_input``. The part's
default returns ``validated_input`` (or a JSON envelope) as the inner
``user_input`` — but the inner ``BaseGraph.invoke()`` writes that straight into
inner State where the inner ``InitializeNode``'s S-2 gate rewrites
identifier-shaped values. The inner hop therefore gets a ref of its own.
"""

from __future__ import annotations

from typing import Any, ClassVar

from framework.schemas.agent_state import AgentState
from framework.schemas.trust_level import TrustLevel

from src.cat2_graph_skeleton.graph_node import DomainWorkflowGraphNode, ModeSpec
from src.graph.domain_workflow_graph import FreezeExceptionWorkflowGraph
from src.services import contracts, envelope

# Outer-state key -> sub_result key. Only keys present with a non-None value
# are merged, so "did not run" stays distinguishable from "ran and produced
# nothing".
_INVOKE_FIELDS = {
    "review_packet": "review_packet",
    "criteria_conditions": "criteria_conditions",
    "source_registry": "source_registry",
    "freeze_window_facts": "freeze_window_facts",
    "cited_source_ids": "cited_source_ids",
    "field_gaps": "field_gaps",
    "recall_refs": "recall_refs",
    "refine_action": "refine_action",
    "refine_count": "refine_count",
    "degraded": "degraded",
    "degradation_code": "degradation_code",
    "requires_human_review": "requires_human_review",
    "error_code": "error_code",
    "error_type": "error_type",
    "error_log": "error_log",
}

_COMMON_FIELDS = {
    "degraded": "degraded",
    "degradation_code": "degradation_code",
    "error_code": "error_code",
    "error_type": "error_type",
    "error_log": "error_log",
}

# What the inner hop needs. Same allowlist discipline as the outer envelope.
_INNER_KEYS = (
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


class FreezeExceptionWorkflowGraphNode(DomainWorkflowGraphNode):
    """Main-slot node for SVC-C2-144."""

    # GraphNode defaults to ANONYMOUS; leaving it there is inconsistent with
    # the inner nodes' S-1 requirement and has produced a real bug before.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    propagate_hitl: ClassVar[bool] = True
    error_strategy: ClassVar[str] = "handle"
    known_failure_codes: ClassVar[tuple[str, ...]] = contracts.INNER_FAILURE_CODES

    mode_specs: ClassVar[dict[str, ModeSpec]] = {
        "invoke": ModeSpec(gate_kind=contracts.GATE_KIND, fields=_INVOKE_FIELDS),
        "ingest": ModeSpec(
            gate_kind=contracts.GATE_KIND,
            fields={"ingest_summary": "ingest_summary", **_COMMON_FIELDS},
        ),
        "feedback": ModeSpec(
            gate_kind=contracts.GATE_KIND,
            fields={"feedback_summary": "feedback_summary", **_COMMON_FIELDS},
        ),
    }

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        super().__init__(config=config, subgraph_factory=_build_inner)
        self._envelopes = (config or {}).get("envelope_store")

    def extract_input(self, state: AgentState) -> str:
        """Hand the inner graph a ref, not a JSON envelope.

        The part's default would return ``validated_input``; this template does
        not populate that field at all, precisely because it is S-2-scanned.
        """
        payload = {key: state.get(key) for key in _INNER_KEYS if state.get(key) is not None}
        return envelope.put(self._envelopes, payload, session_id=str(state.get("session_id") or ""))


def _build_inner(config: dict[str, Any]) -> FreezeExceptionWorkflowGraph:
    """Construct the inner graph.

    Only the customer-independent resolver travels in config. Per-deployment
    connections are fetched inside each node's ``execute()``; caching one here
    would share it across every caller served by this process.
    """
    return FreezeExceptionWorkflowGraph(config=config)


__all__ = ["FreezeExceptionWorkflowGraphNode"]
