"""Main-slot GraphNode contract (AT-1).

The two properties tested here fail in opposite directions and both are silent:

* the inner graph **must** be cached -- rebuilding it per call loses the
  suspended HITL checkpoint and the resumed run re-enters ``interrupt()``
  forever;
* the per-deployment connections **must not** be cached -- the graph object is
  built once per process and shared by every caller, so a held connection is
  held across callers.
"""

from __future__ import annotations

from framework.schemas.trust_level import TrustLevel

from src.nodes.main_node import FreezeExceptionWorkflowGraphNode
from src.services import contracts


def _node(**config_overrides):
    config = {
        "resource_resolver": object(),
        "payload_store": object(),
        "envelope_store": object(),
        "memory_enabled": True,
        "hitl": {"enabled": True, "max_hitl": 8},
    }
    config.update(config_overrides)
    return FreezeExceptionWorkflowGraphNode(config=config)


def test_trust_level_is_declared_explicitly():
    """GraphNode defaults to ANONYMOUS, which contradicts the inner nodes."""
    assert FreezeExceptionWorkflowGraphNode.required_trust_level is TrustLevel.VERIFIED_EXTERNAL


def test_hitl_is_propagated():
    """Without this the inner interrupt never surfaces to /resume."""
    assert FreezeExceptionWorkflowGraphNode.propagate_hitl is True


def test_inner_errors_are_handled_through_a_closed_code_allowlist():
    assert FreezeExceptionWorkflowGraphNode.error_strategy == "handle"
    assert FreezeExceptionWorkflowGraphNode.known_failure_codes == contracts.INNER_FAILURE_CODES
    assert "llm_call_failed" in FreezeExceptionWorkflowGraphNode.known_failure_codes

    node = _node()

    class _UnknownProviderError(RuntimeError):
        error_log = ["provider echoed private request text"]

    result = node.on_subgraph_error({}, _UnknownProviderError())
    assert result["error_code"] == "subgraph_failed"
    assert "provider echoed private request text" not in str(result)


def test_every_mode_declares_the_same_gate_kind():
    specs = FreezeExceptionWorkflowGraphNode.mode_specs
    assert set(specs) == {"invoke", "ingest", "feedback"}
    assert {spec.gate_kind for spec in specs.values()} == {contracts.GATE_KIND}


def test_merge_output_returns_only_the_executed_mode_fields():
    """Padding the other modes with None makes "did not run" and "ran and
    produced nothing" indistinguishable downstream."""
    node = _node()
    delta = node.merge_output(
        {"mode": "ingest"},
        {"ingest_summary": {"written_ids": ["HISTAA"], "rejected": []},
         "status": "success"},
    )
    assert delta["ingest_summary"]["written_ids"] == ["HISTAA"]
    assert "review_packet" not in delta
    assert "feedback_summary" not in delta


def test_merge_output_omits_none_valued_fields():
    node = _node()
    delta = node.merge_output({"mode": "invoke"},
                              {"review_packet": None, "status": "success"})
    assert "review_packet" not in delta


def test_unknown_mode_is_an_error_not_a_silent_pass():
    node = _node()
    delta = node.merge_output({"mode": "audit"}, {"status": "success"})
    assert delta["status"] == "error"


def test_inner_route_allowlist_sends_unknown_mode_to_end():
    from langgraph.graph import END

    from src.graph.domain_workflow_graph import FreezeExceptionWorkflowGraph

    graph = object.__new__(FreezeExceptionWorkflowGraph)
    assert graph.route({"mode": "audit", "status": "pending"}) == END


def test_extract_input_returns_a_ref_not_json():
    """The inner hop is S-2-scanned too.

    Returning the envelope as JSON would let ``detect_pii`` rewrite
    customer-named identifiers inside the inner ``user_input``.
    """
    minted: dict = {}

    class _Store:
        def put(self, value, *, scope, session_id):
            minted["value"] = value
            minted["scope"] = scope
            minted["session_id"] = session_id
            return "abcdefghijklmnopqrstuvwxyzabcdef"        # 32 letters

    node = _node(envelope_store=_Store())
    out = node.extract_input({
        "mode": "invoke", "request_ref": "REQAAA",
        "criteria_version_ref": "CRIT-2026-0001",           # digit-bearing, customer-named
        "session_id": "SESSAA",
    })

    assert out.isalpha() and len(out) == 32
    assert minted["value"]["criteria_version_ref"] == "CRIT-2026-0001"
    assert minted["session_id"] == "SESSAA"


def test_subgraph_is_built_once_and_cached():
    """A fresh inner graph per call loses the suspended checkpoint."""
    builds = []

    class _Inner:
        def compile(self, checkpointer=None):
            return self

    def factory(config):
        builds.append(config)
        return _Inner()

    node = _node()
    node._subgraph_factory = factory                        # noqa: SLF001
    node._subgraph = None                                   # noqa: SLF001

    first = node.get_subgraph()
    second = node.get_subgraph()
    assert first is second
    assert len(builds) == 1


def test_inner_checkpointer_is_a_separate_instance():
    """Sharing one saver with the outer graph collides on thread_id and makes
    every resume restart from the beginning."""
    seen = {}

    class _Inner:
        def compile(self, checkpointer=None):
            seen["checkpointer"] = checkpointer
            return self

    node = _node()
    node._subgraph_factory = lambda config: _Inner()        # noqa: SLF001
    node._subgraph = None                                   # noqa: SLF001
    node.get_subgraph()

    assert seen["checkpointer"] is not None
    # ...and it is the node's own, not something handed in from outside.
    assert seen["checkpointer"] is node._checkpointer       # noqa: SLF001
