"""Domain audit events (S-4) and the persisted audit record.

Two distinct things live here:

* ``emit(...)`` — the S-4 trace event every template-owned ``execute()`` must
  raise at least once. ``BaseNode.__call__`` already emits
  ``node_start`` / ``node_complete`` / ``node_error``; duplicating those here
  would only produce double log noise.
* ``build_event(...)`` — a row for the ``audit_events`` table, used by the
  delete and purge paths where "who, under which approval, how many rows" has
  to survive the request.

Events are raised on the failure paths too. A reject, an upstream error
short-circuit, a degradation and a delete failure all emit; if only the happy
path did, an operator reading the trail would conclude nothing happened.
"""

from __future__ import annotations

from contextvars import ContextVar
from functools import wraps
from typing import Any, Callable, TypeVar, cast

from shared.utils.audit_logger import emit_trace_event

from src.services import contracts

_F = TypeVar("_F", bound=Callable[..., Any])
_DOMAIN_EVENT_SEEN: ContextVar[list[bool] | None] = ContextVar("freeze_exception_domain_event_seen", default=None)

# Event names are fixed here so operations can count them. Free-form strings at
# each call site make "how many suspensions are outstanding?" unanswerable.
EVT_INTAKE = "freeze_exception_intake"
EVT_STAGE1 = "freeze_exception_stage1_deterministic"
EVT_STAGE2 = "freeze_exception_stage2_recall"
EVT_STAGE3 = "freeze_exception_stage3_draft"
EVT_STAGE4 = "freeze_exception_stage4_validation"
EVT_INPUT_REJECTED = "freeze_exception_input_rejected"
EVT_ROW_REJECTED = "freeze_exception_ingest_row_rejected"
EVT_AUTHZ_FAILED = "freeze_exception_authorization_failed"
EVT_LLM_FAILED = "freeze_exception_llm_call_failed"
EVT_DEGRADED = "freeze_exception_degraded"
EVT_CLAIM_DROPPED = "freeze_exception_claim_dropped"
EVT_FEEDBACK_REJECTED = "freeze_exception_feedback_rejected"
EVT_FEEDBACK_APPENDED = "freeze_exception_feedback_appended"
# Fallback used only when an execute path has no more specific domain outcome
# (for example a no-op resume dispatch or an unexpected exception before the
# node reaches its ordinary event). It is not emitted alongside a specific
# event and therefore does not double-count successful stages.
EVT_DOMAIN_PATH = "freeze_exception_domain_path"

# HITL events, fixed shape: {request_ref, thread_id_hash, occurred_at, action, outcome}
EVT_HITL_SUSPENDED = "freeze_exception_hitl_suspended"
EVT_HITL_RESUMED = "freeze_exception_hitl_resumed"
EVT_HITL_DISCARDED = "freeze_exception_hitl_discarded"
EVT_HITL_EXPIRED = "freeze_exception_hitl_expired"
EVT_HITL_LIMIT_REACHED = "freeze_exception_hitl_limit_reached"
EVT_HITL_FEEDBACK_REJECTED = "freeze_exception_hitl_feedback_rejected"
EVT_HITL_BYPASSED = "freeze_exception_hitl_bypassed"

# delete / purge
EVT_DELETE_STARTED = "freeze_exception_delete_started"
EVT_DELETE_COMPLETED = "freeze_exception_delete_completed"
EVT_DELETE_INCOMPLETE = "freeze_exception_delete_incomplete"
EVT_PURGE_RUN = "freeze_exception_purge_run"


def emit(event_type: str, payload: dict[str, Any], state: dict[str, Any]) -> None:
    """Emit one domain trace event.

    Never pass raw request bodies, credentials or ``*_raw`` rationale values;
    the trace payload is not a safe place for them.
    """
    marker = _DOMAIN_EVENT_SEEN.get()
    if marker is not None:
        marker[0] = True
    emit_trace_event(event_type, payload, state)


def ensure_domain_event(event_type: str, *, node: str) -> Callable[[_F], _F]:
    """Guarantee one domain event for every return and exception path.

    Most paths emit a more useful outcome-specific event themselves.  The
    fallback fires only when a path returned or raised before doing so.  A
    ``ContextVar`` keeps the marker invocation-local; graph node instances are
    shared, so mutable instance flags would race across requests.
    """

    def decorate(func: _F) -> _F:
        @wraps(func)
        def guarded(self: Any, state: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
            marker = [False]
            token = _DOMAIN_EVENT_SEEN.set(marker)
            outcome = "returned"
            try:
                return func(self, state, *args, **kwargs)
            except BaseException:  # GraphInterrupt must also preserve the trace contract.
                outcome = "exception"
                raise
            finally:
                try:
                    if not marker[0]:
                        emit(event_type, {"node": node, "outcome": outcome}, state)
                finally:
                    _DOMAIN_EVENT_SEEN.reset(token)

        setattr(guarded, "__domain_event_guard__", True)
        return cast(_F, guarded)

    return decorate


def _alpha_id(seed: str) -> str:
    """Alphabetic-only identifier.

    Digits in an identifier get mis-detected as PII by the S-2 gate and
    rewritten, which corrupts the handle (a measured defect).
    """
    import hashlib

    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return "EVT" + "".join(alphabet[b % 26] for b in digest[:16])


def build_event(
    *,
    correlation_id: str,
    event_type: str,
    target_type: str,
    target_ref: str,
    occurred_at: str,
    actor_id: str,
    approval_ref: str | None = None,
    outcome: str = "success",
    deleted_counts: dict[str, int] | None = None,
    error_code: str | None = None,
    request_ref: str | None = None,
) -> dict[str, Any]:
    """Build one ``audit_events`` row.

    ``target_type``/``target_ref`` exist because ``request_ref`` alone cannot
    identify a perspective deletion — the two delete lineages are separate.
    """
    if target_type not in ("request", "perspective"):
        raise ValueError("target_type must be 'request' or 'perspective'")
    row = {
        "event_id": _alpha_id(f"{correlation_id}:{event_type}:{target_ref}:{occurred_at}"),
        "correlation_id": correlation_id,
        "event_type": event_type,
        "request_ref": request_ref if request_ref is not None else (target_ref if target_type == "request" else ""),
        "occurred_at": occurred_at,
        "actor_id": actor_id,
        "approval_ref": approval_ref or "",
        "outcome": outcome,
        "deleted_counts": deleted_counts or {},
        "error_code": error_code or "",
        "target_type": target_type,
        "target_ref": target_ref,
    }
    unexpected = set(row) - contracts.AUDIT_EVENT_KEYS
    if unexpected:  # pragma: no cover - guarded above
        raise ValueError(f"audit row has undeclared keys: {sorted(unexpected)}")
    return row


__all__ = [name for name in dir() if name.startswith("EVT_")] + ["emit", "build_event"]
