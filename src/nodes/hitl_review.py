"""Stage 3.5 — human review of the draft (D6 ``interrupt()``).

The reviewer preparing the CAB packet works on the draft in the same session,
so the run is suspended rather than restarted: re-invoking would make the model
produce a different draft, and the correction would no longer be a correction
*of that draft*.

Four things are enforced here, each because skipping one is a known failure:

1. **``hitl_allowed`` guard.** An unguarded ``interrupt()`` deadlocks any
   automated pipeline that composes this agent through a ``GraphNode``.
2. **``max_hitl`` is enforced by this template.** Cat 2 has no framework
   circuit breaker (``hitl_count`` is checked only in Cat 3's ``ToolActNode``),
   so declaring the key without enforcing it means "unlimited" in practice.
   The check runs **before** the counter is incremented, so ``max_hitl: 2``
   means two re-drafts and a stop on the third request.
3. **The resume value is sanitised at the call site.** The S-2 input gate runs
   *before* ``execute()``, while ``interrupt()`` returns *inside* it -- the
   operator's value never passes through a framework gate.
4. **Rejection clears ``hitl_feedback``.** ``route()`` evaluates the HITL branch
   before reading ``status``; leaving a stale value there would route a
   rejected resume onward as though it had been accepted.

Only ``hitl_draft`` is written by this template. ``hitl_status``,
``hitl_feedback``, ``hitl_count`` and ``hitl_allowed`` are framework-owned.
"""

from __future__ import annotations

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from langgraph.types import interrupt

from src.services import audit, contracts, projection, stores

# Closed key space for the operator's reply. Keys are *rejected*, never masked:
# masking would rewrite `action` and break routing, and two distinct keys can
# mask to the same string and collide.
_ALLOWED_KEYS = frozenset({"action", "note"})
_MAX_NOTE_CHARS = 2000
_MAX_FEEDBACK_KEYS = 8
# How many times a malformed reply may be re-asked before failing closed.
_MAX_FEEDBACK_ATTEMPTS = 3


class HitlReviewNode(FunctionNode):
    """Stage 3.5."""

    _s4_audit_exempt: ClassVar[str] = (
        "emits through src/services/audit.py::emit, a module-level helper, and @audit.ensure_domain_event guarantees one event on every return and raise path — outside this gate's same-class one-hop ceiling"
    )

    required_trust_level = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, max_hitl: int = 8) -> None:
        self._max_hitl = max(1, int(max_hitl))

    @audit.ensure_domain_event(audit.EVT_DOMAIN_PATH, node="hitl_review")
    def execute(self, state: AgentState) -> dict[str, Any]:
        request_ref = str(state.get("request_ref") or "")

        # (1) Automated pipeline: proceed without human review rather than
        # suspending a graph nobody is watching.
        if not state.get("hitl_allowed", True):
            audit.emit(
                audit.EVT_HITL_BYPASSED,
                {
                    "request_ref": request_ref,
                    "reason": "hitl_not_allowed",
                },
                state,
            )
            return {"refine_action": contracts.REFINE_CONFIRM}

        # Degraded runs have no draft to review; suspending would show the
        # reviewer an empty packet and wait for them to approve nothing.
        if state.get("degradation_code") == contracts.DEGRADATION_LLM_UNAVAILABLE:
            audit.emit(
                audit.EVT_HITL_BYPASSED,
                {
                    "request_ref": request_ref,
                    "reason": contracts.DEGRADATION_LLM_UNAVAILABLE,
                },
                state,
            )
            return {"refine_action": contracts.REFINE_CONFIRM}

        refine_count = state.get("refine_count")
        refine_count = refine_count if isinstance(refine_count, int) else 0

        draft = state.get("hitl_draft") or state.get("review_packet") or {}

        audit.emit(
            audit.EVT_HITL_SUSPENDED,
            {
                "request_ref": request_ref,
                "thread_id_hash": _thread_hash(state),
                "occurred_at": stores.utc_now_iso(),
                "action": "suspend",
                "outcome": "awaiting_human",
            },
            state,
        )

        # A rejected reply must not fall through to a route decision, so the
        # node asks again rather than returning. The retry count is bounded:
        # an automated client that keeps sending malformed feedback would
        # otherwise spin here forever.
        action = note = ""
        reject_reason: str | None = None
        for _ in range(_MAX_FEEDBACK_ATTEMPTS):
            feedback = interrupt(
                {
                    "draft": draft,
                    "reason": "reviewer confirmation",
                    "refine_count": refine_count,
                    "max_hitl": self._max_hitl,
                    "rejected_previous": reject_reason,
                }
            )

            # (3) Sanitise before first use and before the state write.
            action, note, reject_reason = _sanitise(feedback)
            if reject_reason is None:
                break

            audit.emit(
                audit.EVT_HITL_FEEDBACK_REJECTED,
                {
                    "request_ref": request_ref,
                    "thread_id_hash": _thread_hash(state),
                    "occurred_at": stores.utc_now_iso(),
                    "action": "reject",
                    "outcome": reject_reason,
                },
                state,
            )
        else:
            # (4) Out of attempts: fail closed rather than accepting a value
            # that never passed sanitisation. `hitl_feedback` is cleared so a
            # stale value cannot make route() take the HITL branch.
            return {
                "hitl_feedback": None,
                "hitl_draft": draft,
                "status": AgentStatus.ERROR.value,
                "error_code": contracts.ERR_INVALID_REQUEST,
            }

        # (2) Ceiling check on the RESUME, before the counter is incremented.
        # It belongs here rather than before `interrupt()`: checking on entry
        # would cut the loop one round early and the reviewer would never see
        # the last re-draft they asked for. `max_hitl` counts re-drafts, so
        # `max_hitl: 2` means two of them and a stop on the third request.
        if action == contracts.REFINE_REFINE and refine_count >= self._max_hitl:
            audit.emit(
                audit.EVT_HITL_LIMIT_REACHED,
                {
                    "request_ref": request_ref,
                    "thread_id_hash": _thread_hash(state),
                    "occurred_at": stores.utc_now_iso(),
                    "action": contracts.REFINE_REFINE,
                    "outcome": "limit_reached",
                },
                state,
            )
            # Not a discard: what has been produced is validated and returned.
            return {
                "refine_action": contracts.REFINE_CONFIRM,
                "degraded": True,
                "degradation_code": contracts.DEGRADATION_HITL_LIMIT,
                "requires_human_review": True,
            }

        updates: dict[str, Any] = {"refine_action": action}
        if action == contracts.REFINE_DISCARD:
            # Discard routes straight to END, so this node is terminal on that
            # path and owns the status. A discard is a normal outcome, not an
            # error: the reviewer decided the packet should not go forward.
            updates["status"] = AgentStatus.SUCCESS.value
            updates["degraded"] = True
            updates["degradation_code"] = contracts.DEGRADATION_DISCARDED
            empty_packet = _empty_review_packet()
            updates["review_packet"] = empty_packet
            updates["hitl_draft"] = empty_packet
            updates["field_gaps"] = []
            updates["cited_source_ids"] = []
            updates["requires_human_review"] = False
        if action == contracts.REFINE_REFINE:
            updates["refine_count"] = refine_count + 1
            if note:
                merged = dict(draft) if isinstance(draft, dict) else {}
                merged["reviewer_note"] = note
                updates["hitl_draft"] = merged

        event = audit.EVT_HITL_DISCARDED if action == contracts.REFINE_DISCARD else audit.EVT_HITL_RESUMED
        audit.emit(
            event,
            {
                "request_ref": request_ref,
                "thread_id_hash": _thread_hash(state),
                "occurred_at": stores.utc_now_iso(),
                "action": action,
                "outcome": "accepted",
            },
            state,
        )
        return updates


def _thread_hash(state: AgentState) -> str:
    thread_id = state.get("thread_id") or state.get("session_id") or ""
    return stores.hash_thread_id(str(thread_id)) if thread_id else ""


def sanitise_feedback(feedback: Any) -> tuple[str, str, str | None]:
    """Return ``(action, note, reject_reason)``.

    Four steps, in order: key space, enum, PII, injection. ``detect_pii``
    raises ``TypeError`` on a non-``str``, so the type check comes first.
    """
    if not isinstance(feedback, dict):
        return "", "", "not_an_object"
    if len(feedback) > _MAX_FEEDBACK_KEYS:
        return "", "", "too_many_keys"

    for key in feedback:
        if not isinstance(key, str):
            return "", "", "non_string_key"
        if key not in _ALLOWED_KEYS:
            return "", "", "undeclared_key"

    action = feedback.get("action")
    if not isinstance(action, str) or action not in contracts.REFINE_ACTIONS:
        return "", "", "unknown_action"

    note = feedback.get("note", "")
    if note is None:
        note = ""
    if not isinstance(note, str):
        return "", "", "note_not_a_string"
    if len(note) > _MAX_NOTE_CHARS:
        return "", "", "note_too_long"

    if note:
        # The order is part of the boundary contract.  In particular, the PII
        # detector is ASCII-oriented, so NFKC and masking must happen before
        # the instruction classifier sees or persists the operator text.
        note = projection.normalise_and_mask(note)
        if projection.contains_instruction(note):
            return "", "", "instruction_shape_detected"

    return action, note, None


def _empty_review_packet() -> dict[str, list[Any]]:
    packet: dict[str, list[Any]] = {section: [] for section in contracts.FINDING_FIELDS_BY_SECTION}
    packet["precedent_checks"] = []
    return packet


_sanitise = sanitise_feedback


__all__ = ["HitlReviewNode", "sanitise_feedback"]
