"""Stage 3 — the one LLM step.

The model maps the free-text exception rationale onto the pinned criteria
conditions and judges whether the current request touches each recalled past
issue. Everything factual around it is deterministic.

Three boundaries are enforced here rather than requested politely:

* **The narrative body never returns to State.** It is resolved from the
  payload store into a local variable, handed to the model, and dropped. The
  reply is type-projected so only declared finding fields survive -- otherwise
  the model itself becomes a route from the body back into State.
* **No arithmetic, no date comparison.** The model may *quote* a stated
  rollback duration as a claim; whether that number exists in the source and
  whether the window overlaps are decided by Stages 1 and 4.
* **The past verdict is not in the prompt.** ``prior_outcome`` is copied
  deterministically after the fact. Showing it to the model creates the
  "it was approved before, so approve it now" path.

LLM absent -> ``llm_unavailable`` degradation with ``status: success``.
LLM present but failing -> ``llm_call_failed`` **error**; a runtime failure is
not silently downgraded into a partial answer.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.errors import SecurityViolationError
from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.evidence_gate.projection import FindingField, project_typed_findings
from src.services import audit, contracts, projection as safe_projection, response_contract, stores

# `gap_origin` is deliberately absent from the projection: it is Stage 1's
# marker for "the code is certain this field is missing", and it is the only
# thing that lets a gap carry an empty `source_ids` past the S-3 gate. If the
# model could emit it, it could self-declare a gap with no evidence at all.
# post_process stamps "llm" on the model-authored gaps instead.
_MODEL_AUTHORED_EXCLUSIONS = {"gap_origin"}

_NARRATIVE_FIELDS = (
    "change_description",
    "exception_rationale",
    "rollback_plan",
    "risk_assessment",
)
_WITHHELD_NARRATIVE = "[INSTRUCTION WITHHELD]"
_NARRATIVE_CLAIM_IDS = {
    "change_description": "DGNARRATIVECHANGE",
    "exception_rationale": "DGNARRATIVEEXCEPTION",
    "rollback_plan": "DGNARRATIVEROLLBACK",
    "risk_assessment": "DGNARRATIVERISK",
}


def _finding_fields(section: str) -> tuple[FindingField, ...]:
    """Build the projection spec for one section from the single contract."""
    spec: list[FindingField] = []
    for name in contracts.FINDING_FIELDS_BY_SECTION[section]:
        if name in _MODEL_AUTHORED_EXCLUSIONS:
            continue
        if name == "source_ids":
            spec.append(FindingField(name=name, expected_type=list, required=True, item_type=str))
        elif name == "confidence":
            expected_types: tuple[type, ...] = (int, float)
            if (section, name) in contracts.NULLABLE_FINDING_FIELDS:
                expected_types += (type(None),)
            spec.append(FindingField(name=name, expected_type=expected_types, required=True))
        elif (section, name) in contracts.NULLABLE_FINDING_FIELDS:
            spec.append(FindingField(name=name, expected_type=(str, type(None)), required=True))
        elif name == "severity":
            # The agent only ever produces candidate-grade findings; a
            # confirmed severity is not representable.
            spec.append(
                FindingField(
                    name=name, expected_type=str, required=True, allowed_values=tuple(sorted(contracts.SEVERITIES))
                )
            )
        else:
            spec.append(FindingField(name=name, expected_type=str, required=True))
    return tuple(spec)


_PRECEDENT_FIELDS = (
    FindingField(name="history_ref", expected_type=str, required=True),
    FindingField(
        name="status", expected_type=str, required=True, allowed_values=tuple(sorted(contracts.PRECEDENT_STATUSES))
    ),
    FindingField(name="source_ids", expected_type=list, required=True, item_type=str),
)


class SufficiencyDraftNode(FunctionNode):
    """Stage 3."""

    _s4_audit_exempt: ClassVar[str] = (
        "emits through src/services/audit.py::emit, a module-level helper, and @audit.ensure_domain_event guarantees one event on every return and raise path — outside this gate's same-class one-hop ceiling"
    )

    required_trust_level = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, resolver: stores.AuthorizedResourceResolver, config: dict[str, Any] | None = None) -> None:
        self._resolver = resolver
        # Injection seam only. No model id, no concrete client in src/.
        self._config = config or {}

    @audit.ensure_domain_event(audit.EVT_DOMAIN_PATH, node="sufficiency_draft")
    def execute(self, state: AgentState) -> dict[str, Any]:
        ctx = InvocationContext.from_state(state)
        scope = self._resolver.for_caller(ctx.caller_id)
        request_ref = str(state.get("request_ref") or "")

        llm = self._config.get("llm")
        if llm is None:
            audit.emit(
                audit.EVT_DEGRADED,
                {
                    "request_ref": request_ref,
                    "degradation_code": contracts.DEGRADATION_LLM_UNAVAILABLE,
                },
                state,
            )
            return {
                "review_packet": _empty_packet(),
                "evidence_map": [],
                "degraded": True,
                "degradation_code": contracts.DEGRADATION_LLM_UNAVAILABLE,
            }

        # -- layer 2: resolve the body for this call only ---------------------
        body: dict[str, str] | None
        try:
            body, narrative_gaps = _resolve_body(scope, state, ctx)
        except stores.PayloadExpired:
            audit.emit(
                audit.EVT_INPUT_REJECTED,
                {
                    "request_ref": request_ref,
                    "reason": contracts.ERR_PAYLOAD_EXPIRED,
                },
                state,
            )
            return {"status": "error", "error_code": contracts.ERR_PAYLOAD_EXPIRED}

        try:
            field_gaps = _merge_gaps(list(state.get("field_gaps") or []), narrative_gaps)
            prompt = response_contract.build_prompt(
                criteria_conditions=list(state.get("criteria_conditions") or []),
                source_registry=dict(state.get("source_registry") or {}),
                recall_refs=list(state.get("recall_refs") or []),
                freeze_window_facts=dict(state.get("freeze_window_facts") or {}),
                field_gaps=field_gaps,
                reviewer_note=_reviewer_note(state),
            )
            # The body is passed as a JSON value in its own message, never
            # concatenated into the instruction prose.
            messages = [
                {"role": "user", "content": prompt},
                {"role": "user", "content": json.dumps({"request_body": body}, ensure_ascii=False)},
            ]
            reply = llm.complete(messages)
        except Exception as exc:  # noqa: BLE001 - normalised below
            # Never persist ``str(exc)``: provider messages carry request
            # fragments. Only the exception class is recorded.
            audit.emit(
                audit.EVT_LLM_FAILED,
                {
                    "request_ref": request_ref,
                    "error_type": type(exc).__name__,
                },
                state,
            )
            return {
                "status": "error",
                "error_code": contracts.ERR_LLM_CALL_FAILED,
                "error_type": type(exc).__name__,
            }
        finally:
            body = None  # drop the resolved narrative from this frame

        try:
            packet, evidence_map, dropped = _project_reply(reply)
        except SecurityViolationError:
            audit.emit(
                audit.EVT_LLM_FAILED,
                {
                    "request_ref": request_ref,
                    "error_type": "OutputContractViolation",
                },
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_code": contracts.ERR_OUTPUT_CONTRACT,
                "error_type": "OutputContractViolation",
            }

        audit.emit(
            audit.EVT_STAGE3,
            {
                "request_ref": request_ref,
                "section_counts": {name: len(packet.get(name) or []) for name in packet},
            },
            state,
        )

        result: dict[str, Any] = {
            "review_packet": packet,
            "evidence_map": evidence_map,
            "hitl_draft": packet,  # idempotency guard for resume
        }
        if narrative_gaps:
            # Stage 4 deterministically merges these into evidence_gaps.  The
            # raw narrative never enters this delta; only fixed descriptions
            # containing the declared field name are checkpointed.
            result["field_gaps"] = field_gaps
        if dropped or narrative_gaps:
            result["requires_human_review"] = True
        return result


def _reviewer_note(state: AgentState) -> str | None:
    """The reviewer's note from a previous ``refine``, if any.

    Already sanitised by the HITL node before it reached State.
    """
    draft = state.get("hitl_draft")
    if isinstance(draft, dict):
        note = draft.get("reviewer_note")
        if isinstance(note, str) and note:
            return note
    return None


def _resolve_body(
    scope: stores.ResourceScope, state: AgentState, ctx: InvocationContext
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Resolve and sanitise the narrative for this model call only.

    The payload store is the sole home of the raw values.  The object returned
    to ``execute`` contains only the four declared fields after NFKC and PII
    masking.  An instruction-shaped field is replaced as a whole: partially
    retaining it would still give the model attacker-controlled instructions.
    """
    ref = state.get("payload_ref")
    if not ref:
        return {}, []
    # Both halves must match what the entry adapter used at `put` time:
    #   scope      -> the request this body belongs to
    #   session_id -> the SERVER-decided handle, echoed back by the framework
    #                 as ctx.session_id. NOT ctx.thread_id: that is a fresh
    #                 random UUID per InvocationContext, so the adapter could
    #                 not have bound the payload to it.
    # `consume` stays False so a later `refine` can resolve the same body
    # again; the store still fail-closes on replay after a consuming read.
    try:
        raw = scope.payloads.resolve(
            ref,
            scope={"request_ref": str(state.get("request_ref") or "")},
            session_id=str(ctx.session_id or ""),
        )
    except Exception as exc:  # noqa: BLE001
        raise stores.PayloadExpired(type(exc).__name__) from exc
    if raw is None:
        raise stores.PayloadExpired("payload_absent")
    if not isinstance(raw, dict):
        raise stores.PayloadExpired("payload_not_an_object")

    safe: dict[str, str] = {}
    gaps: list[dict[str, Any]] = []
    for field in _NARRATIVE_FIELDS:
        value = safe_projection.normalise_and_mask(str(raw.get(field) or ""))
        if safe_projection.contains_instruction(value):
            safe[field] = _WITHHELD_NARRATIVE
            gaps.append(
                {
                    "claim_id": _NARRATIVE_CLAIM_IDS[field],
                    "description": f"申請本文「{field}」は指示形を含むため除外しました。",
                    "condition_id": None,
                    "source_ids": [],
                    "gap_origin": "deterministic",
                }
            )
        else:
            safe[field] = value
    return safe, gaps


def _merge_gaps(existing: list[Any], additions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge deterministic gaps idempotently across HITL refine loops."""
    merged = [dict(gap) for gap in existing if isinstance(gap, dict)]
    known = {str(gap.get("claim_id") or "") for gap in merged}
    for gap in additions:
        if str(gap.get("claim_id") or "") not in known:
            merged.append(dict(gap))
    return merged


def _empty_packet() -> dict[str, list[dict[str, Any]]]:
    packet: dict[str, list[dict[str, Any]]] = {section: [] for section in contracts.FINDING_FIELDS_BY_SECTION}
    packet["precedent_checks"] = []
    return packet


def _project_reply(
    reply: Any,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]], int]:
    """Type-project and sanitise the model reply before it reaches State.

    Stage 4 remains a second validation layer, but it is already too late for
    checkpoint safety: LangGraph checkpoints the Stage 3 delta first.  PII and
    over-limit text therefore have to be removed here.
    """
    payload = _reply_object(reply)
    expected_sections = set(contracts.FINDING_FIELDS_BY_SECTION) | {"precedent_checks"}
    if set(payload) != expected_sections:
        raise SecurityViolationError("S-3 draft: response sections are not exact-key")

    packet: dict[str, list[dict[str, Any]]] = {}
    dropped = 0
    for section in contracts.FINDING_FIELDS_BY_SECTION:
        items = payload.get(section)
        items = items if isinstance(items, list) else []
        projected = list(project_typed_findings(items, _finding_fields(section)))
        safe_rows: list[dict[str, Any]] = []
        for row in projected:
            safe = dict(row)
            rejected = False
            for field in ("description", "question"):
                value = safe.get(field)
                if not isinstance(value, str):
                    continue
                limit = response_contract.limit_for(field)
                if limit is not None and len(value) > limit:
                    rejected = True
                    break
                safe[field] = safe_projection.normalise_and_mask(value)
            if rejected:
                dropped += 1
            else:
                safe_rows.append(safe)
        packet[section] = safe_rows

    precedents = payload.get("precedent_checks")
    precedents = precedents if isinstance(precedents, list) else []
    packet["precedent_checks"] = list(project_typed_findings(precedents, _PRECEDENT_FIELDS))

    evidence_map = [
        {"claim_id": item.get("claim_id"), "source_ids": item.get("source_ids") or []}
        for section, items in packet.items()
        if section != "precedent_checks"
        for item in items
        if isinstance(item, dict)
    ]
    return packet, evidence_map, dropped


def _reply_object(reply: Any) -> dict[str, Any]:
    """Extract the JSON object from a ``BaseLLM.complete`` return value.

    The canonical shape is ``{"content": str, ...}``; a bare string is accepted
    too so a minimal test double does not need to mimic the whole envelope.
    """
    if isinstance(reply, dict):
        content = reply.get("content")
    else:
        content = reply
    if isinstance(content, dict):
        return content
    if not isinstance(content, str):
        raise SecurityViolationError("S-3 draft: provider content must be JSON text")
    from src.llm_draft_stage.draft_node import parse_json_object

    try:
        parsed = parse_json_object(content)
    except Exception as exc:  # noqa: BLE001
        raise SecurityViolationError("S-3 draft: invalid JSON object") from exc
    if not isinstance(parsed, dict):
        raise SecurityViolationError("S-3 draft: response must be an object")
    return parsed


__all__ = ["SufficiencyDraftNode"]
