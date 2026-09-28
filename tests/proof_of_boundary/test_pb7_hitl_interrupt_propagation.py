# tests/proof_of_boundary/test_pb7_hitl_interrupt_propagation.py
#
# CONDITIONAL: Required only when config/config.yaml has hitl.enabled: true
# Templates that do not use HITL will see this file produce SKIPPED results.
#
# Two test cases verify the PB-7 boundary behavior:
#
#   test_pb7_hitl_interrupt_propagates()
#       Verifies that interrupt() raises GraphInterrupt and the signal propagates
#       through BaseNode.__call__() to the LangGraph engine — NOT caught by the
#       application error boundary.
#
#   test_pb7_hitl_allowed_false_skips_interrupt()
#       Verifies that the hitl_allowed=False guard prevents interrupt() from
#       firing — no GraphInterrupt raised, no deadlock.
#

from __future__ import annotations

import pathlib
import warnings

import pytest

# ---------------------------------------------------------------------------
# Conditional skip — only runs when config/config.yaml has hitl.enabled: true
# ---------------------------------------------------------------------------

_CONFIG_PATH = pathlib.Path(__file__).parents[2] / "config" / "config.yaml"


def _hitl_enabled() -> bool:
    """Return True when config/config.yaml declares hitl.enabled: true.

    An absent or unreadable config.yaml warns instead of skipping silently —
    either case is ambiguous (never shipped / broken
    vs. genuinely non-HITL) and should not look like a clean auto-waiver.
    """
    if not _CONFIG_PATH.exists():
        warnings.warn(
            f"{_CONFIG_PATH} not found — PB-7 skipped without verifying hitl.enabled. "
            "If this template calls interrupt(), ship config/config.yaml before release.",
            stacklevel=2,
        )
        return False
    try:
        import yaml  # pyyaml==6.0.1 — pinned in scaffold/pyproject.toml dependencies

        data = yaml.safe_load(_CONFIG_PATH.read_text())
    except Exception as exc:
        warnings.warn(
            f"{_CONFIG_PATH} could not be read as YAML ({exc}) — PB-7 skipped without "
            "verifying hitl.enabled. If this template calls interrupt(), fix config/config.yaml "
            "before release.",
            stacklevel=2,
        )
        return False
    hitl = (data or {}).get("hitl", {}) if isinstance(data, dict) else None
    if not isinstance(hitl, dict):
        warnings.warn(
            f"{_CONFIG_PATH} does not have the expected 'hitl:' mapping shape — PB-7 skipped "
            "without verifying hitl.enabled. If this template calls interrupt(), fix "
            "config/config.yaml before release.",
            stacklevel=2,
        )
        return False
    return bool(hitl.get("enabled", False))


pytestmark = pytest.mark.skipif(
    not _hitl_enabled(),
    reason="config/config.yaml does not set hitl.enabled: true — PB-7 not applicable",
)

# ---------------------------------------------------------------------------
# Helper — build a minimal base state for the node under test
# ---------------------------------------------------------------------------

def _base_state(**overrides) -> dict:
    """Return a minimal state dict for the HITL guard test."""
    state = {
        # Framework-managed fields
        "caller_trust_level": "anonymous",
        "correlation_id": "pb7-test",
        "node_history": [],
        "error_log": [],
        # HITL fields
        "hitl_allowed": True,           # overridden per test case
        "hitl_count": 0,
    }
    state.update(overrides)
    return state


# ---------------------------------------------------------------------------
# PB-7-A: interrupt() raises GraphInterrupt and propagates
# ---------------------------------------------------------------------------


def test_pb7_hitl_interrupt_propagates() -> None:
    """PB-7: interrupt() raises GraphInterrupt and propagates (not caught by app boundary).

    Covers the PB-7 boundary (first assertion):
      GraphInterrupt reaches LangGraph engine; status is NOT set to error.

    """
    # 🔴 `interrupt()` cannot be exercised on a bare node. It only behaves
    # correctly inside a compiled graph that has a checkpointer attached, so a
    # node-level probe here would assert nothing (it does not raise) and PB-7
    # would be green while proving nothing. The propagation is therefore
    # verified on a REAL graph: the suspension has to survive BaseNode's error
    # boundary, the GraphNode boundary and the outer pipeline to arrive as
    # `awaiting_human` rather than as an error.
    import json

    from framework.schemas.invocation_context import InvocationContext
    from framework.schemas.trust_level import TrustLevel
    from framework.secrets.context import bound_secrets

    import src.api.server as server
    from src.services import envelope, stores

    caller = "pb7-caller"
    registry = stores.ControlDocumentRegistry()
    registry.register(stores.KIND_CRITERIA, "CRIT-PB", "v1", {
        "content_hash": "hashpb", "approved": True,
        "effective_from": "2020-01-01T00:00:00+09:00",
        "conditions": [{"condition_id": "CONDA", "condition_ref": "1.1"}],
    })
    server._RESOLVER.register(caller, stores.ResourceScope(   # noqa: SLF001
        tenant_ref="pb7", permissions=frozenset({"freeze.invoke.write_snapshot"}),
        db=stores.InMemoryDatabase(), kb=stores.InMemoryKnowledgeBase(),
        payloads=server._PAYLOADS, registry=registry))       # noqa: SLF001

    class _Llm:
        def complete(self, messages):
            return {"content": json.dumps({"supported_conditions": [],
                                           "evidence_gaps": [],
                                           "contradictions": [],
                                           "approval_scope_discrepancies": [],
                                           "verification_questions": [],
                                           "precedent_checks": []})}

    main = server.agent._nodes["main"]                        # noqa: SLF001
    previous_llm = main._config.get("llm")                    # noqa: SLF001
    main._config["llm"] = _Llm()                              # noqa: SLF001
    main._subgraph = None                                     # noqa: SLF001

    try:
        session_id = "SESSPBSEVEN"
        evaluated_at = "2026-08-20T00:00:00+09:00"
        payload = {
            "mode": "invoke", "request_ref": "REQPB",
            "criteria_version_ref": "CRIT-PB", "criteria_version": "v1",
            "criteria_content_hash": "hashpb",
            "planned_window": {"start": "2026-09-01T00:00:00+09:00",
                               "end": "2026-09-01T04:00:00+09:00"},
            "evidence_declarations": [], "evaluated_at": evaluated_at,
            "fields_present": {}, "refine_count": 0,
            "sealed_refs": {
                "criteria_sealed_ref": registry.resolve_pin(
                    stores.KIND_CRITERIA, ref="CRIT-PB", version="v1",
                    content_hash="hashpb", evaluated_at=evaluated_at),
                "freeze_calendar_sealed_ref": None,
            },
        }
        ref = envelope.put(server._ENVELOPES, payload, session_id=session_id)  # noqa: SLF001
        ctx = InvocationContext(
            correlation_id="pb7-cid", session_id=session_id, caller_id=caller,
            caller_trust_level=TrustLevel.VERIFIED_EXTERNAL, hitl_allowed=True)
        with bound_secrets(server.agent._secrets_provider):   # noqa: SLF001
            result = server.agent.invoke(ref, session_id=session_id, ctx=ctx)

        # The signal reached the engine and was turned into a suspension...
        assert result["status"] == "awaiting_human", result
        # ...and was NOT swallowed into an error by any boundary on the way.
        assert result["status"] != "error"
        assert result.get("thread_id")
    finally:
        main._config["llm"] = previous_llm                    # noqa: SLF001
        main._subgraph = None                                 # noqa: SLF001


# ---------------------------------------------------------------------------
# PB-7-B: hitl_allowed=False guard prevents deadlock
# ---------------------------------------------------------------------------


def test_pb7_hitl_allowed_false_skips_interrupt() -> None:
    """PB-7 guard: hitl_allowed=False must NOT raise GraphInterrupt (no deadlock).

    Covers the HITL compliance contract — the hitl_allowed guard:
      When hitl_allowed=False, the node must check the flag before calling
      interrupt() and skip the HITL path entirely.

    """
    from langgraph.errors import GraphInterrupt

    from src.nodes.hitl_review import HitlReviewNode

    node = HitlReviewNode(max_hitl=8)
    state = _base_state(hitl_allowed=False)
    state["caller_trust_level"] = "VERIFIED_EXTERNAL"
    state["review_packet"] = {"supported_conditions": []}
    state["refine_count"] = 0

    try:
        result = node(state)
    except GraphInterrupt:                      # pragma: no cover - the failure
        pytest.fail("hitl_allowed=False must not suspend: an automated "
                    "pipeline composing this agent would deadlock")

    # The guard is a deadlock preventer, not a bypass: the run continues down
    # the ordinary confirm path rather than skipping the work.
    assert result.get("refine_action") == "confirm"
