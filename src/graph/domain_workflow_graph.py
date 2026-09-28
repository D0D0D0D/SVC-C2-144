"""Inner graph — the three modes and the invoke pipeline.

Topology::

    START ─route()─┬─ "invoke"   → deterministic_check → recall_perspectives
                   │                → sufficiency_draft → hitl_review
                   │                    ─route_after_hitl()─┬ confirm → evidence_validation → END
                   │                                        ├ refine  → sufficiency_draft
                   │                                        └ discard → END
                   ├─ "ingest"   → accumulate_ingest   → END
                   └─ "feedback" → accumulate_feedback → END

Routing is an **allowlist dictionary**, not a denylist: an unknown mode falls
to ``END``. The API layer already rejects unknown modes with 400, so that path
is unreachable in practice — it is the last line of defence, and a unit test
fixes the behaviour (running an unknown mode straight into the inner graph
terminates with no mode's nodes having executed, and without raising).

``route`` / ``route_after_hitl`` / ``get_output`` annotate ``state`` as the
template's own ``State``. Annotating them ``AgentState`` makes the projection
drop the custom fields and the graph mis-routes (measured on Business Travel Hotel & Plan Recommendation Agent).
"""

from __future__ import annotations

from typing import Any

from framework.errors import ConfigError
from framework.graph.base_graph import BaseGraph
from langgraph.graph import END, START

from src.nodes.dispatch_node import DispatchNode
from src.nodes.feedback_node import AccumulateFeedbackNode
from src.nodes.hitl_review import HitlReviewNode
from src.nodes.ingest_node import AccumulateIngestNode
from src.nodes.stage1_deterministic import DeterministicCheckNode
from src.nodes.stage2_recall import RecallPerspectivesNode
from src.nodes.stage3_draft import SufficiencyDraftNode
from src.nodes.stage4_validation import EvidenceValidationNode
from src.schemas.state import State
from src.services import contracts

_MODE_ENTRY = {
    "invoke": "deterministic_check",
    "ingest": "accumulate_ingest",
    "feedback": "accumulate_feedback",
}

_AFTER_HITL = {
    contracts.REFINE_CONFIRM: "evidence_validation",
    contracts.REFINE_REFINE: "sufficiency_draft",
    contracts.REFINE_DISCARD: END,
}


class FreezeExceptionWorkflowGraph(BaseGraph):
    """Inner domain workflow for SVC-C2-144."""

    @property
    def name(self) -> str:
        return "freeze_exception_workflow"

    @property
    def state_schema(self) -> type:
        return State

    # ------------------------------------------------------------------
    def _validate_config(self) -> None:
        """Validate the customer-independent injections only.

        Deliberately **not** validated here:

        * ``db_client`` / ``kb_client`` — the per-deployment connections are
          fetched from the resolver on every invocation, never held.
        * ``criteria_version_ref`` — the pinned edition is per-request input,
          not configuration.
        * ``clock_now`` — the graph is constructed once per process, so a
          per-request instant cannot travel through config at all; it arrives
          in State as ``evaluated_at``.
        """
        config = self.config or {}
        if config.get("resource_resolver") is None:
            raise ConfigError("resource_resolver is required")
        if config.get("payload_store") is None:
            raise ConfigError("payload_store is required")
        if config.get("envelope_store") is None:
            raise ConfigError("envelope_store is required")
        top_k = config.get("top_k", 8)
        if not isinstance(top_k, int) or top_k < 1:
            raise ConfigError("top_k must be a positive integer")
        # HITL needs a checkpointer on the INNER graph too: without these
        # declarations `compile()` attaches none and `interrupt()` raises.
        if not config.get("memory_enabled"):
            raise ConfigError("memory_enabled must be true (HITL requires a checkpointer)")
        hitl = config.get("hitl") or {}
        if not hitl.get("enabled"):
            raise ConfigError("hitl.enabled must be true on the inner graph")

    # ------------------------------------------------------------------
    def register_nodes(self) -> None:
        # No super() call: BaseGraph.register_nodes is abstract, and
        # initialize/finalize are the outer backbone's concern.
        config = self.config or {}
        resolver = config["resource_resolver"]
        hitl = config.get("hitl") or {}

        # Entry: resolves the inner envelope ref. Mode routing happens after
        # it, because the mode itself is inside the envelope.
        self._nodes["dispatch"] = DispatchNode(envelope_store=config["envelope_store"])
        self._nodes["deterministic_check"] = DeterministicCheckNode(
            resolver=resolver,
            max_quotable_chars=int(config.get("max_quotable_chars", 120)),
        )
        self._nodes["recall_perspectives"] = RecallPerspectivesNode(
            resolver=resolver,
            top_k=int(config.get("top_k", 8)),
            min_recall_hits=int(config.get("min_recall_hits", 3)),
        )
        self._nodes["sufficiency_draft"] = SufficiencyDraftNode(
            resolver=resolver,
            config=config,
        )
        self._nodes["hitl_review"] = HitlReviewNode(
            max_hitl=int(hitl.get("max_hitl", 8)),
        )
        self._nodes["evidence_validation"] = EvidenceValidationNode(
            min_quote_chars=int(config.get("min_quote_chars", 12)),
        )
        self._nodes["accumulate_ingest"] = AccumulateIngestNode(
            resolver=resolver,
            max_rationale_chars=int(config.get("max_rationale_chars", 1000)),
            text_gate_config=config,
        )
        self._nodes["accumulate_feedback"] = AccumulateFeedbackNode(
            resolver=resolver,
            max_rationale_chars=int(config.get("max_rationale_chars", 1000)),
            text_gate_config=config,
        )

    # ------------------------------------------------------------------
    def add_edges(self) -> None:
        self._sg.add_edge(START, "dispatch")
        self._sg.add_conditional_edges("dispatch", self.route)

        self._sg.add_edge("deterministic_check", "recall_perspectives")
        self._sg.add_edge("recall_perspectives", "sufficiency_draft")
        self._sg.add_edge("sufficiency_draft", "hitl_review")
        self._sg.add_conditional_edges("hitl_review", self.route_after_hitl)
        self._sg.add_edge("evidence_validation", END)

        self._sg.add_edge("accumulate_ingest", END)
        self._sg.add_edge("accumulate_feedback", END)

    # ------------------------------------------------------------------
    def route(self, state: State) -> str:
        """Mode dispatch (allowlist)."""
        if state.get("status") == "error":
            return END
        return _MODE_ENTRY.get(str(state.get("mode") or ""), END)

    def route_after_hitl(self, state: State) -> str:
        """Branch on the single deterministic field the HITL node writes.

        Unset or unknown routes to ``END``: never hand a free-text reply to the
        model to interpret, and never loop back into re-drafting by default.
        """
        if state.get("status") == "error":
            return END
        return _AFTER_HITL.get(str(state.get("refine_action") or ""), END)

    # ------------------------------------------------------------------
    def get_output(self, state: State) -> dict[str, Any]:
        """Return only the fields the executed mode actually produced.

        Padding the other modes' keys with ``None`` makes the outer
        ``merge_output`` unable to tell "did not run" from "ran and produced
        nothing".
        """
        mode = str(state.get("mode") or "")
        out: dict[str, Any] = {
            "mode": mode,
            "status": state.get("status"),
            "correlation_id": state.get("correlation_id"),
        }
        for key in ("degraded", "degradation_code", "requires_human_review", "error_code", "error_type"):
            if state.get(key) is not None:
                out[key] = state.get(key)

        # The protected GraphNode's handle strategy can only inspect a
        # SubgraphError.error_log. Export the stable code through that narrow
        # channel, never the inner/provider error text.
        if state.get("status") == "error":
            error_code = state.get("error_code")
            out["error_log"] = [error_code] if error_code in contracts.INNER_FAILURE_CODES else []

        if mode == "invoke":
            for key in (
                "review_packet",
                "criteria_conditions",
                "source_registry",
                "freeze_window_facts",
                "cited_source_ids",
                "field_gaps",
                "recall_refs",
                "refine_action",
                "refine_count",
            ):
                if state.get(key) is not None:
                    out[key] = state.get(key)
        elif mode == "ingest":
            if state.get("ingest_summary") is not None:
                out["ingest_summary"] = state.get("ingest_summary")
        elif mode == "feedback":
            if state.get("feedback_summary") is not None:
                out["feedback_summary"] = state.get("feedback_summary")
        return out


__all__ = ["FreezeExceptionWorkflowGraph"]
