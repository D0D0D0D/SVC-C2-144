"""State schema for SVC-C2-144 (change-freeze exception evidence review).

Contract: ``docs/02_design.md`` -> "State Definition".

Hard constraints repeated here because breaking them is silent:

* flat ``TypedDict`` only -- primitives plus JSON-serialisable containers.
  Pydantic models, dataclasses and arbitrary objects are msgpack-incompatible
  and break checkpointing.
* no credential material, no ``InvocationContext``: State can cross a
  checkpoint boundary (``memory_enabled``/``hitl.enabled`` are both true here).
* **no free-text request bodies.** The four narrative fields live in the
  payload store; State carries only ``payload_ref``.
* ``hitl_count`` and ``hitl_allowed`` are framework-owned -- never written by
  this template. ``refine_count`` is the template-owned counter.
* nodes return **only changed keys**; ``node_history`` / ``error_log`` are
  ``operator.add`` reducers and a full-state return duplicates the audit trail.

Reference identifiers are **alphabetic only**. The framework S-2 input gate
scans ``user_input``/``validated_input``/``llm_response`` with ``detect_pii``
and destructively rewrites matches to ``[MASKED]``; a ``uuid4()``-shaped ref
containing digits gets mis-detected (phone-number shape) and corrupted. This is
a measured defect, not a hypothetical.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NotRequired, TypedDict

if TYPE_CHECKING:
    # AgentCore 1.0.1 does not publish ``py.typed``, so mypy otherwise sees
    # AgentState as ``Any`` and loses the TypedDict semantics of this schema.
    # Keep a local description of only the framework fields this template
    # reads.  The runtime branch still subclasses the real SDK class.
    class _AgentStateBase(TypedDict, total=False):
        user_input: str
        session_id: str
        result: Any
        status: str
        formatted_output: Any
        node_history: list[str]
        error_log: list[str]
        hitl_allowed: bool
        hitl_metadata: dict[str, Any]
        hitl_status: str | None
        hitl_count: int
        correlation_id: str
        trace_id: str

else:
    from framework.schemas.agent_state import AgentState as _AgentStateBase


class State(_AgentStateBase, total=False):
    """Flat state for the three modes (``ingest`` / ``invoke`` / ``feedback``)."""

    # ---- routing / identity -------------------------------------------------
    mode: NotRequired[str]
    request_ref: NotRequired[str]
    validated_input: NotRequired[str]

    # ---- sensitive body isolation (layer 1) ---------------------------------
    # Opaque handle minted by the server. A client-supplied value is discarded
    # unconditionally so a forged ref cannot bypass the isolation.
    payload_ref: NotRequired[str]

    # ---- version-pinned control documents -----------------------------------
    criteria_version_ref: NotRequired[str]
    criteria_version: NotRequired[str]
    criteria_content_hash: NotRequired[str]
    freeze_calendar_ref: NotRequired[str | None]
    freeze_calendar_version: NotRequired[str | None]
    freeze_calendar_content_hash: NotRequired[str | None]
    # {criteria_sealed_ref, freeze_calendar_sealed_ref} -- request-lifetime seal
    # references. The resolved documents themselves never enter State.
    sealed_refs: NotRequired[dict[str, Any]]

    # ---- request payload (structured part only) -----------------------------
    planned_window: NotRequired[dict[str, Any]]
    evidence_declarations: NotRequired[list[dict[str, Any]]]
    ingress_evidence_gaps: NotRequired[list[dict[str, Any]]]
    # TZ-aware ISO-8601 reference instant fixed once by the entry adapter. The
    # graph is constructed once per process, so this cannot travel via config.
    evaluated_at: NotRequired[str]

    # ---- Stage 1 (deterministic) --------------------------------------------
    fields_present: NotRequired[dict[str, Any]]
    field_gaps: NotRequired[list[dict[str, Any]]]
    format_check: NotRequired[dict[str, Any]]
    freeze_window_facts: NotRequired[dict[str, Any]]
    criteria_conditions: NotRequired[list[dict[str, Any]]]
    # source_id -> {doc_type, doc_ref, span_ref, span_text, span_kind, provenance}
    # Immutable after Stage 1. This is the S-3 matching basis; the LLM selects
    # from it and never mints identifiers.
    source_registry: NotRequired[dict[str, Any]]

    # ---- Stage 2 (precedent recall) -----------------------------------------
    # Free text here is always the ``_safe`` projection. Putting ``_raw`` into
    # State would open a direct route from the KB to the output.
    recall_refs: NotRequired[list[dict[str, Any]]]
    retrieved_records: NotRequired[list[dict[str, Any]]]

    # ---- Stage 3 (LLM draft) ------------------------------------------------
    review_packet: NotRequired[dict[str, Any]]
    evidence_map: NotRequired[list[dict[str, Any]]]

    # ---- Stage 4 / evidence gate -------------------------------------------
    # Recomputed by post_process from the surviving claims' source_ids; the
    # LLM's self-reported value is not trusted.
    cited_source_ids: NotRequired[list[str]]

    # ---- HITL ---------------------------------------------------------------
    refine_action: NotRequired[str]
    # Template-owned. Initialised to 0 on /invoke -- an implicit "absent means
    # zero" dependency is not relied upon anywhere.
    refine_count: NotRequired[int]
    hitl_draft: NotRequired[dict[str, Any]]

    # ---- outcome ------------------------------------------------------------
    requires_human_review: NotRequired[bool]
    degraded: NotRequired[bool]
    degradation_code: NotRequired[str]
    ingest_summary: NotRequired[dict[str, Any]]
    feedback_summary: NotRequired[dict[str, Any]]
    error_code: NotRequired[str]
    error_type: NotRequired[str]


__all__ = ["State"]
