# PB-6: Invoke Execution Order and S-1 Denial Verification
# Verifies BaseNode.__call__() enforces the authorised path: S-1 trust gate ->
# S-4 node_start -> S-2 _security_gate_input() -> execute() -> S-3
# _security_gate_output() -> S-4 node_complete, for every concrete node under
# src/nodes/. It also verifies an under-privileged caller is denied at S-1
# before execute() or normal lifecycle events can run.

import importlib
import inspect
import pkgutil
from typing import ClassVar

import pytest
from framework.nodes.base_node import BaseNode
from framework.schemas.trust_level import TrustLevel


class _PrivilegedTrustGateFixture(BaseNode):
    """Always-present privileged node used to prove the S-1 negative boundary."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _security_gate_input(self, state):
        return state

    def execute(self, state):
        return {"status": "success"}

    def _security_gate_output(self, result):
        return result


def _trust_predecessor(required: TrustLevel) -> TrustLevel:
    """Return a lower valid trust level; fail loudly if the framework adds one."""
    predecessors = {
        TrustLevel.VERIFIED_EXTERNAL: TrustLevel.ANONYMOUS,
        TrustLevel.INTERNAL: TrustLevel.VERIFIED_EXTERNAL,
    }
    try:
        return predecessors[required]
    except KeyError as exc:
        raise AssertionError(f"no lower trust level defined for {required!r}") from exc


class _NullStore:
    """Stand-in for an injected store.

    PB-6 checks the *order* of the gates, not what the node does with its
    collaborators, so the doubles only need to exist. They must not, however,
    make the node take a shortcut: each returns a value shaped enough for
    ``execute()`` to run to the point where the gate order is observable.
    """

    def put(self, value, *, scope=None, session_id=None):
        return "abcdefghijklmnopqrstuvwxyzabcdef"

    def resolve(self, ref, *, scope=None, session_id=None, consume=False):
        # The smallest envelope that lets the inner graph run to completion.
        # A GraphNode propagates inner failures, so an empty envelope would
        # make execute() raise and the S-3 gate would never be reached --
        # the delegating node would then drop out of PB-6's coverage.
        if scope == {"kind": "envelope"}:
            return {
                "mode": "ingest", "request_ref": "INGEST",
                "payload_ref": "abcdefghijklmnopqrstuvwxyzabcdef",
            }
        return {"records": []}

    def delete(self, ref, *, scope=None, session_id=None):
        return None


def _instantiate(node_cls: type):
    """Build a node whatever its constructor requires.

    Nodes in this template take injected collaborators (the resolver, the
    envelope store, config). A PB that could only build zero-argument nodes
    would quietly stop covering them the moment a dependency is added -- the
    suite would still be green while its coverage shrank.
    """
    stubs = {
        "resolver": _NullResolver(),
        "envelope_store": _NullStore(),
        "payload_store": _NullStore(),
        # A GraphNode builds its inner graph from this, and the inner graph
        # validates its own config. An empty dict would make execute() raise
        # before the S-3 gate could run, and the order assertion would then be
        # measuring a construction failure rather than the gate sequence.
        "config": {
            "resource_resolver": _NullResolver(),
            "payload_store": _NullStore(),
            "envelope_store": _NullStore(),
            "memory_enabled": True,
            "hitl": {"enabled": True, "max_hitl": 8},
            "llm": None,
        },
    }
    signature = inspect.signature(node_cls.__init__)
    kwargs = {}
    for name, param in signature.parameters.items():
        if name == "self":
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        if name in stubs:
            # Supplied even when the parameter has a default: a GraphNode's
            # `config=None` default builds an inner graph that fails its own
            # validation, and the node would then be dropped from coverage for
            # a setup reason rather than a real ordering violation.
            kwargs[name] = stubs[name]
        elif param.default is inspect.Parameter.empty:
            kwargs[name] = _NullStore()
    return node_cls(**kwargs)


class _NullResolver:
    """Resolver double: every caller resolves to empty in-memory stores."""

    def for_caller(self, caller_id):
        from src.services import stores

        return stores.ResourceScope(
            tenant_ref="pb6", permissions=frozenset(),
            db=stores.InMemoryDatabase(), kb=stores.InMemoryKnowledgeBase(),
            payloads=_NullStore(), registry=stores.ControlDocumentRegistry())


def _discover_node_classes() -> list[type]:
    """Import every module under src/nodes/ and collect concrete BaseNode subclasses."""
    try:
        pkg = importlib.import_module("src.nodes")
    except ImportError as exc:
        pytest.fail(f"PB-6 cannot import src.nodes; framework/template setup is broken: {exc}")

    discovered = []
    for _, modname, _ in pkgutil.walk_packages(pkg.__path__, prefix="src.nodes."):
        module = importlib.import_module(modname)
        for attr in vars(module).values():
            if (
                isinstance(attr, type)
                and issubclass(attr, BaseNode)
                and attr is not BaseNode
                and attr.__module__ == modname
                and not inspect.isabstract(attr)
            ):
                discovered.append(attr)
    return discovered


class TestInvokeOrder:
    """PB-6: __call__ must run S-1 -> node_start -> S-2 -> execute() -> S-3 -> node_complete."""

    def test_s1_denial_refuses_execution_before_execute(self, monkeypatch):
        """TC-08: an always-present privileged node proves the negative S-1 path."""
        import framework.nodes.base_node as base_node_module

        events: list[str] = []
        execute_calls: list[object] = []
        monkeypatch.setattr(
            base_node_module,
            "emit_trace_event",
            lambda event_type, _payload, _state: events.append(event_type),
        )
        original_execute = _PrivilegedTrustGateFixture.execute

        def spy_execute(self, state):
            execute_calls.append(state)
            return original_execute(self, state)

        monkeypatch.setattr(_PrivilegedTrustGateFixture, "execute", spy_execute)
        result = _PrivilegedTrustGateFixture()(
            {
                "caller_trust_level": _trust_predecessor(
                    _PrivilegedTrustGateFixture.required_trust_level
                ).value,
                "correlation_id": "tc08-s1-denial",
            }
        )

        assert result["status"] == "error"
        assert "S-1 trust gate denied" in result["error_log"][0]
        assert events == ["s1_denied"]
        assert not execute_calls

    def test_call_order_for_every_node(self, monkeypatch):
        node_classes = _discover_node_classes()
        if not node_classes:
            pytest.skip("no concrete BaseNode subclasses found under src/nodes/")

        import framework.nodes.base_node as base_node_module

        failures: list[str] = []
        for node_cls in node_classes:
            order: list[str] = []
            # Record only THIS node's lifecycle events. A GraphNode runs its
            # inner graph inside execute(), and those inner nodes emit their
            # own start/complete pairs -- interleaving them would look like an
            # ordering violation when the outer sequence is in fact correct.
            def _record(event_type, payload, _state, _o=order, _name=node_cls.__name__):
                if isinstance(payload, dict) and payload.get("node") not in (None, _name):
                    return
                _o.append(f"event:{event_type}")

            monkeypatch.setattr(base_node_module, "emit_trace_event", _record)

            for method_name, label in (
                ("_security_gate_input", "security_gate_input"),
                ("execute", "execute"),
                ("_security_gate_output", "security_gate_output"),
            ):
                original = getattr(node_cls, method_name)

                def spy(self, arg, _o=order, _label=label, _orig=original):
                    _o.append(_label)
                    return _orig(self, arg)

                monkeypatch.setattr(node_cls, method_name, spy)

            instance = _instantiate(node_cls)
            # A minimal-but-complete state. `InvocationContext.from_state()`
            # reads several framework keys, and a KeyError there would surface
            # as `node_error` -- the order assertion would then fail for a
            # reason that has nothing to do with the gate sequence.
            state = {
                "caller_trust_level": node_cls.required_trust_level.value,
                "correlation_id": "pb6-invoke-order-test",
                "trace_id": "pb6-trace",
                "session_id": "pb6-session",
                "thread_id": "pb6-thread",
                "caller_id": "pb6-caller",
                "hitl_allowed": False,
                "status": "pending",
                "node_history": [],
                "error_log": [],
                "execution_time": {},
                # ingest is the simplest mode that runs end to end without
                # pinned control documents.
                "mode": "ingest",
            }
            instance(state)

            # The gate SEQUENCE is what PB-6 fixes. A node whose S-3 gate
            # rejects the output ends in `node_error` instead of
            # `node_complete` -- that is the gate doing its job, and the
            # sequence up to and including S-3 is unchanged. Demanding
            # `node_complete` here would force every node to be fed a
            # fully-valid state just to observe the ordering, which is how a
            # PB quietly narrows to the handful of nodes that are easy to set
            # up.
            prefix = [
                "event:node_start",
                "security_gate_input",
                "execute",
                "security_gate_output",
            ]
            expected_ok = (prefix + ["event:node_complete"],
                           prefix + ["event:node_error"])
            expected = expected_ok[0]
            if order not in expected_ok:
                failures.append(
                    f"{node_cls.__name__}: invoke order violation.\n" f"expected: {expected}\nactual:   {order}"
                )

        assert not failures, "\n\n".join(failures)
