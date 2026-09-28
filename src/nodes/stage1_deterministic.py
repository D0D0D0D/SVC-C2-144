"""Stage 1 — deterministic checks (no LLM).

Produces the facts every later stage is measured against:

* ``criteria_conditions`` — the condition table of the *pinned* criteria
  edition, resolved from the request-lifetime seal.
* ``source_registry`` — the immutable table of quotable spans. The model may
  only *select* from it; it never mints a ``source_id``, which is what stops
  fabricated citations at the root.
* ``freeze_window_facts`` — the time reconciliation, computed here rather than
  asked of the model.
* ``field_gaps`` — required fields that are simply absent. These carry
  ``gap_origin: "deterministic"`` and are the *only* gaps allowed to have an
  empty ``source_ids``.

The seal is resolved here and nowhere else: Stage 2/3/4 see the structured
results only, so the pinned document body never has to travel in State (and
therefore never reaches a checkpoint).
"""

from __future__ import annotations

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.services import audit, contracts, freeze_window, service, stores

# Required narrative fields. Their presence is checked here; their *content*
# is only ever seen by Stage 3, resolved from the payload store.
_REQUIRED_NARRATIVES = (
    "change_description",
    "exception_rationale",
    "rollback_plan",
    "risk_assessment",
)


class DeterministicCheckNode(FunctionNode):
    """Stage 1."""

    _s4_audit_exempt: ClassVar[str] = (
        "emits through src/services/audit.py::emit, a module-level helper, and @audit.ensure_domain_event guarantees one event on every return and raise path — outside this gate's same-class one-hop ceiling"
    )

    required_trust_level = TrustLevel.VERIFIED_EXTERNAL

    def __init__(
        self,
        *,
        resolver: stores.AuthorizedResourceResolver,
        max_quotable_chars: int = service.DEFAULT_MAX_QUOTABLE_CHARS,
    ) -> None:
        # Only customer-independent things may be held on the node: the graph
        # object is built once per process and shared by every invocation.
        self._resolver = resolver
        self._max_quotable_chars = max_quotable_chars

    @audit.ensure_domain_event(audit.EVT_DOMAIN_PATH, node="deterministic_check")
    def execute(self, state: AgentState) -> dict[str, Any]:
        ctx = InvocationContext.from_state(state)
        scope = self._resolver.for_caller(ctx.caller_id)

        request_ref = str(state.get("request_ref") or "")
        evaluated_at = state.get("evaluated_at")
        if not evaluated_at:
            # Fail-closed: without a fixed reference instant the window answer
            # is not reproducible. Never fall back to "now" here.
            audit.emit(
                audit.EVT_INPUT_REJECTED,
                {"request_ref": request_ref, "reason": contracts.ERR_EVALUATED_AT_MISSING},
                state,
            )
            return {
                "status": "error",
                "error_code": contracts.ERR_EVALUATED_AT_MISSING,
            }

        sealed = state.get("sealed_refs") or {}
        criteria_doc = scope.registry.resolve_sealed(sealed.get("criteria_sealed_ref"))
        if criteria_doc is None:
            audit.emit(
                audit.EVT_INPUT_REJECTED,
                {"request_ref": request_ref, "reason": contracts.ERR_CRITERIA_SEAL_UNRESOLVED},
                state,
            )
            return {"status": "error", "error_code": contracts.ERR_CRITERIA_SEAL_UNRESOLVED}

        criteria_version_ref = str(state.get("criteria_version_ref") or "")
        conditions = _project_conditions(criteria_doc, criteria_version_ref)

        # -- freeze windows ------------------------------------------------
        calendar_ref = state.get("freeze_calendar_ref")
        calendar_windows: list[dict[str, Any]] | None = None
        if calendar_ref:
            calendar_doc = scope.registry.resolve_sealed(sealed.get("freeze_calendar_sealed_ref"))
            if calendar_doc is None:
                # A supplied pin that cannot be resolved is an error, not a
                # degradation: "omitted" and "resolution failed" must not share
                # a shape.
                audit.emit(
                    audit.EVT_INPUT_REJECTED,
                    {"request_ref": request_ref, "reason": contracts.ERR_CALENDAR_SEAL_UNRESOLVED},
                    state,
                )
                return {"status": "error", "error_code": contracts.ERR_CALENDAR_SEAL_UNRESOLVED}
            calendar_windows = list(calendar_doc.get("windows") or [])

        facts, window_errors = freeze_window.evaluate(
            planned_window=state.get("planned_window"),
            calendar_windows=calendar_windows,
            freeze_calendar_ref=calendar_ref,
            evaluated_at=str(evaluated_at),
        )

        # -- required fields ------------------------------------------------
        fields_present = {name: bool((state.get("fields_present") or {}).get(name)) for name in _REQUIRED_NARRATIVES}
        gaps: list[dict[str, Any]] = [
            dict(gap) for gap in (state.get("ingress_evidence_gaps") or []) if isinstance(gap, dict)
        ]
        for name, present in fields_present.items():
            if not present:
                gaps.append(
                    _deterministic_gap(
                        claim_id=f"DG{_alpha(name)}",
                        description=f"申請に必須項目「{name}」の記載が見当たりません。",
                        condition_id=None,
                    )
                )

        if not calendar_ref:
            gaps.append(
                _deterministic_gap(
                    claim_id="DGCALENDAR",
                    description="凍結カレンダーが指定されていないため、実施予定時間窓と"
                    "凍結期間の重なりを評価していません。",
                    condition_id=None,
                )
            )

        format_check = {"errors": window_errors}
        for reason in window_errors:
            gaps.append(
                _deterministic_gap(
                    claim_id="DGWINDOW",
                    description=f"実施予定時間窓を評価できません: {reason}",
                    condition_id=None,
                )
            )

        # -- source registry -------------------------------------------------
        registry = service.build_source_registry(
            list(state.get("evidence_declarations") or []),
            request_ref=request_ref,
            criteria_version_ref=criteria_version_ref,
            max_quotable_chars=self._max_quotable_chars,
        )
        if not registry:
            gaps.append(
                _deterministic_gap(
                    claim_id="DGEVIDENCE",
                    description="証跡が 1 件も提出されていません。",
                    condition_id=None,
                )
            )

        audit.emit(
            audit.EVT_STAGE1,
            {
                "request_ref": request_ref,
                "condition_count": len(conditions),
                "source_count": len(registry),
                "gap_count": len(gaps),
                "window_evaluated": facts.get("evaluated"),
                "overlap_count": len(facts.get("overlapping_windows") or []),
            },
            state,
        )

        result: dict[str, Any] = {
            "criteria_conditions": conditions,
            "source_registry": registry,
            "freeze_window_facts": facts,
            "fields_present": fields_present,
            "field_gaps": gaps,
            "format_check": format_check,
        }
        if state.get("ingress_evidence_gaps"):
            result["requires_human_review"] = True
        return result


def _alpha(name: str) -> str:
    """Alphabetic suffix for a deterministic claim id (no digits: S-2 rewrites them)."""
    return "".join(ch for ch in name.upper() if ch.isalpha())[:12] or "X"


def _deterministic_gap(*, claim_id: str, description: str, condition_id: str | None) -> dict[str, Any]:
    """A gap the code is certain about.

    ``gap_origin: "deterministic"`` is the *only* thing that lets a gap carry an
    empty ``source_ids`` through the S-3 gate, and the model cannot emit that
    value: the type projection applied to its reply has no ``gap_origin`` field.
    """
    return {
        "claim_id": claim_id,
        "description": description,
        "condition_id": condition_id,
        "source_ids": [],
        "gap_origin": "deterministic",
    }


def _project_conditions(document: dict[str, Any], criteria_version_ref: str) -> list[dict[str, Any]]:
    """Project the pinned criteria edition to its exact-key condition table."""
    out: list[dict[str, Any]] = []
    for condition in document.get("conditions") or []:
        if not isinstance(condition, dict):
            continue
        row = {
            "condition_id": str(condition.get("condition_id") or ""),
            "criteria_version_ref": criteria_version_ref,
            "condition_ref": str(condition.get("condition_ref") or ""),
        }
        if set(row) != contracts.CRITERIA_CONDITION_KEYS:  # pragma: no cover - constant
            raise ValueError("criteria condition projection drifted from the contract")
        out.append(row)
    return out


__all__ = ["DeterministicCheckNode"]
