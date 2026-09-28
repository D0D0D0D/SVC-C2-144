"""PB-1 — template nodes emit their own outcome-specific domain events.

The guard's ``freeze_exception_domain_path`` event proves only that the
fallback works.  It cannot satisfy the normal-path assertion below: otherwise
deleting every explicit node event would leave this PB green.
"""

from __future__ import annotations

import importlib
import inspect
import json
import pkgutil
from typing import Any

import pytest
from framework.nodes.base_node import BaseNode

from src.services import audit, stores
from tests.proof_of_boundary.test_pb_invoke_order import _instantiate


_EXPECTED_EVENT_BY_NODE = {
    "DispatchNode": audit.EVT_INTAKE,
    "FreezeExceptionIntakePreProcessNode": audit.EVT_INTAKE,
    "DeterministicCheckNode": audit.EVT_STAGE1,
    "RecallPerspectivesNode": audit.EVT_STAGE2,
    "SufficiencyDraftNode": audit.EVT_STAGE3,
    "HitlReviewNode": audit.EVT_HITL_BYPASSED,
    "EvidenceValidationNode": audit.EVT_STAGE4,
    "AccumulateIngestNode": audit.EVT_INTAKE,
    "AccumulateFeedbackNode": audit.EVT_FEEDBACK_REJECTED,
    "ReviewPacketPostProcessNode": audit.EVT_STAGE4,
}


class _ExplodingState(dict):
    def get(self, *_args, **_kwargs):
        raise RuntimeError("forced_before_normal_domain_emit")


class _Resolver:
    def __init__(self, scope: stores.ResourceScope) -> None:
        self._scope = scope

    def for_caller(self, _caller_id: str) -> stores.ResourceScope:
        return self._scope


def _node_classes() -> list[type]:
    package = importlib.import_module("src.nodes")
    classes: list[type] = []
    for _, module_name, _ in pkgutil.walk_packages(
            package.__path__, prefix="src.nodes."):
        module = importlib.import_module(module_name)
        for value in vars(module).values():
            if (isinstance(value, type)
                    and issubclass(value, BaseNode)
                    and value is not BaseNode
                    and value.__module__ == module_name
                    and "execute" in value.__dict__
                    and not inspect.isabstract(value)):
                classes.append(value)
    return classes


def _state(node_cls: type) -> dict[str, Any]:
    return {
        "caller_trust_level": node_cls.required_trust_level.value,
        "correlation_id": "pb1-correlation",
        "trace_id": "pb1-trace",
        "session_id": "pb1-session",
        "thread_id": "pb1-thread",
        "caller_id": "pb1-caller",
        "hitl_allowed": False,
        "status": "pending",
        "mode": "ingest",
    }


def _normal_case(node_cls: type) -> tuple[Any, dict[str, Any]]:
    state = _state(node_cls)

    if node_cls.__name__ in {
            "DispatchNode", "FreezeExceptionIntakePreProcessNode"}:
        state.pop("mode")
        state["user_input"] = "abcdefghijklmnopqrstuvwxyzabcdef"

    if node_cls.__name__ == "AccumulateIngestNode":
        state.update({
            "request_ref": "INGEST",
            "payload_ref": "abcdefghijklmnopqrstuvwxyzabcdef",
        })

    if node_cls.__name__ == "DeterministicCheckNode":
        registry = stores.ControlDocumentRegistry()
        document = {
            "content_hash": "hashpb",
            "approved": True,
            "effective_from": "2020-01-01T00:00:00+09:00",
            "conditions": [{"condition_id": "CONDA", "condition_ref": "1.1"}],
        }
        registry.register(stores.KIND_CRITERIA, "CRIT-PB", "v1", document)
        sealed_ref = registry.resolve_pin(
            stores.KIND_CRITERIA,
            ref="CRIT-PB",
            version="v1",
            content_hash="hashpb",
            evaluated_at="2026-08-20T00:00:00+09:00",
        )
        scope = stores.ResourceScope(
            tenant_ref="pb1",
            permissions=frozenset(),
            db=stores.InMemoryDatabase(),
            kb=stores.InMemoryKnowledgeBase(),
            payloads=None,
            registry=registry,
        )
        state.update({
            "mode": "invoke",
            "request_ref": "REQPB",
            "criteria_version_ref": "CRIT-PB",
            "evaluated_at": "2026-08-20T00:00:00+09:00",
            "sealed_refs": {
                "criteria_sealed_ref": sealed_ref,
                "freeze_calendar_sealed_ref": None,
            },
            "planned_window": {
                "start": "2026-09-01T00:00:00+09:00",
                "end": "2026-09-01T04:00:00+09:00",
            },
            "fields_present": {
                "change_description": True,
                "exception_rationale": True,
                "rollback_plan": True,
                "risk_assessment": True,
            },
            "evidence_declarations": [],
        })
        return node_cls(resolver=_Resolver(scope)), state

    if node_cls.__name__ == "SufficiencyDraftNode":
        class _Llm:
            def complete(self, _messages):
                return {"content": json.dumps({
                    "supported_conditions": [],
                    "evidence_gaps": [],
                    "contradictions": [],
                    "approval_scope_discrepancies": [],
                    "verification_questions": [],
                    "precedent_checks": [],
                })}

        scope = stores.ResourceScope(
            tenant_ref="pb1",
            permissions=frozenset(),
            db=stores.InMemoryDatabase(),
            kb=stores.InMemoryKnowledgeBase(),
            payloads=None,
            registry=stores.ControlDocumentRegistry(),
        )
        state.update({
            "mode": "invoke",
            "request_ref": "REQPB",
            "criteria_conditions": [],
            "source_registry": {},
            "recall_refs": [],
            "freeze_window_facts": {},
            "field_gaps": [],
        })
        return node_cls(resolver=_Resolver(scope), config={"llm": _Llm()}), state

    return _instantiate(node_cls), state


def _record_events(monkeypatch, node_cls: type, instance: Any, state: dict[str, Any],
                   *, suppress: frozenset[str] = frozenset()) -> list[tuple[str, dict]]:
    module = importlib.import_module(node_cls.__module__)
    original_emit = module.audit.emit
    events: list[tuple[str, dict]] = []

    def record(event_type: str, payload: dict, event_state: dict) -> None:
        if event_type in suppress:
            return
        events.append((event_type, payload))
        original_emit(event_type, payload, event_state)

    with monkeypatch.context() as scoped:
        # Patch THIS node module's binding, not BaseNode's automatic emitter.
        scoped.setattr(module.audit, "emit", record)
        try:
            instance.execute(state)
        except BaseException:
            pass
    return events


def _assert_specific_event(node_cls: type, events: list[tuple[str, dict]]) -> None:
    expected = _EXPECTED_EVENT_BY_NODE[node_cls.__name__]
    event_types = [event_type for event_type, _payload in events]
    assert expected in event_types, (
        f"{node_cls.__name__}: expected {expected!r}; got {event_types!r}")
    assert audit.EVT_DOMAIN_PATH not in event_types, (
        f"{node_cls.__name__}: normal path used only the fallback")


def test_each_node_emits_its_own_outcome_specific_event(monkeypatch):
    classes = _node_classes()
    assert {node_cls.__name__ for node_cls in classes} == set(_EXPECTED_EVENT_BY_NODE)

    for node_cls in classes:
        execute = node_cls.__dict__["execute"]
        assert getattr(execute, "__domain_event_guard__", False)
        instance, state = _normal_case(node_cls)
        _assert_specific_event(
            node_cls, _record_events(monkeypatch, node_cls, instance, state))


def test_fallback_covers_exception_before_a_specific_event(monkeypatch):
    for node_cls in _node_classes():
        instance, _state_unused = _normal_case(node_cls)
        events = _record_events(monkeypatch, node_cls, instance, _ExplodingState())
        fallback = [payload for event_type, payload in events
                    if event_type == audit.EVT_DOMAIN_PATH]
        assert fallback == [{"node": _fallback_node_name(node_cls),
                             "outcome": "exception"}]


def test_stage1_explicit_emit_mutation_is_detected(monkeypatch):
    """Mutation proof: disabling only EVT_STAGE1 must fail PB-1's predicate."""
    from src.nodes.stage1_deterministic import DeterministicCheckNode

    instance, state = _normal_case(DeterministicCheckNode)
    events = _record_events(
        monkeypatch,
        DeterministicCheckNode,
        instance,
        state,
        suppress=frozenset({audit.EVT_STAGE1}),
    )

    assert [event_type for event_type, _payload in events] == [audit.EVT_DOMAIN_PATH]
    with pytest.raises(AssertionError, match="expected.*stage1_deterministic"):
        _assert_specific_event(DeterministicCheckNode, events)


def _fallback_node_name(node_cls: type) -> str:
    return {
        "DispatchNode": "dispatch",
        "FreezeExceptionIntakePreProcessNode": "pre_process",
        "DeterministicCheckNode": "deterministic_check",
        "RecallPerspectivesNode": "recall_perspectives",
        "SufficiencyDraftNode": "sufficiency_draft",
        "HitlReviewNode": "hitl_review",
        "EvidenceValidationNode": "evidence_validation",
        "AccumulateIngestNode": "accumulate_ingest",
        "AccumulateFeedbackNode": "accumulate_feedback",
        "ReviewPacketPostProcessNode": "post_process",
    }[node_cls.__name__]
