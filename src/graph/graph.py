"""Outer graph — the fixed five-node backbone.

``AgentBaseGraph`` already wires
``START → initialize → pre_process → main → {route} → post_process → finalize → END``
and injects ``initialize``/``finalize``, so ``add_edges()`` is deliberately not
overridden: this template needs no non-standard routing at the outer level.
All branching lives in the inner graph.

Only customer-independent things are held here. The per-deployment DB/KB/payload
connections are fetched from the resolver inside each node's ``execute()``; the
graph object is built once per process and shared by every request, so anything
cached on it is cached across callers.
"""

from __future__ import annotations

from typing import Any

from framework.errors import ConfigError
from framework.graph.agent_base_graph import AgentBaseGraph

from src.nodes.main_node import FreezeExceptionWorkflowGraphNode
from src.nodes.post_process_node import ReviewPacketPostProcessNode
from src.nodes.pre_process_node import FreezeExceptionIntakePreProcessNode
from src.schemas.state import State


class ChangeFreezeExceptionReviewGraph(AgentBaseGraph):
    """SVC-C2-144 — change-freeze exception evidence review."""

    @property
    def name(self) -> str:
        return "change_freeze_exception_review"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        # AgentBaseGraph validates max_retry / memory_enabled / hitl itself.
        # What it cannot know about is the resource seam.
        config = self.config or {}
        if config.get("resource_resolver") is None:
            raise ConfigError("resource_resolver is required")
        if config.get("payload_store") is None:
            raise ConfigError("payload_store is required")
        if config.get("envelope_store") is None:
            raise ConfigError("envelope_store is required")
        super()._validate_config()

    def register_nodes(self) -> None:
        # REQUIRED first: injects initialize + finalize.
        super().register_nodes()
        config: dict[str, Any] = dict(self.config or {})
        resolver = config["resource_resolver"]

        self._nodes["pre_process"] = FreezeExceptionIntakePreProcessNode(
            resolver=resolver, envelope_store=config["envelope_store"]
        )
        self._nodes["main"] = FreezeExceptionWorkflowGraphNode(config=config)
        self._nodes["post_process"] = ReviewPacketPostProcessNode(
            min_quote_chars=int(config.get("min_quote_chars", 12)),
        )

    def get_output(self, state: State) -> dict[str, Any]:
        """Return the caller-visible result.

        Two deliberate departures from the base implementation:

        * **no ``result`` fallback.** The base returns
          ``formatted_output or result``. When the S-3 output gate raises, the
          offending node's return dict is discarded -- and the fallback then
          serves whatever an *earlier* node left in ``result``, routing around
          the gate that just rejected the output. Only ``formatted_output`` is
          ever returned here.
        * **error metadata is carried out.** The base drops ``error_code``, so
          a node that fail-closes with a precise reason reaches the caller as a
          bare "error" and the operator cannot tell a version-pin failure from
          an expired payload. Only stable, self-authored codes are surfaced --
          never ``error_log`` entries, which can contain provider-authored text.
        """
        out = {
            "output": state.get("formatted_output"),
            "status": state.get("status"),
            "trace_id": state.get("trace_id"),
            "correlation_id": state.get("correlation_id"),
            "node_history": state.get("node_history", []),
        }
        for key in ("error_code", "error_type"):
            value = state.get(key)
            if value:
                out[key] = value
        # HITL suspension handles, so the adapter can hand the client something
        # to echo back to /resume.
        for key in ("thread_id", "hitl_metadata", "hitl_status"):
            value = state.get(key)
            if value is not None:
                out[key] = value
        return out


# The scaffold entry point imports ``Graph``; keep the alias so the manifest and
# the standalone server agree on one name.
Graph = ChangeFreezeExceptionReviewGraph

__all__ = ["ChangeFreezeExceptionReviewGraph", "Graph"]
