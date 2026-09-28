"""Post-process — assemble the envelope and enforce the S-3 output contract.

Verification is deliberately **two-staged**, because the framework hook does
not get the state:

* ``execute(state)`` — everything that has to be checked against the
  deterministic tables (source registry, condition table, request scope,
  recalled precedents). A finding that fails is dropped and
  ``requires_human_review`` is raised. This is where reject conditions
  #4 / #5 / #6 / #7 / #10 / #10-b / #10-c live.
* ``_extra_security_gate_output(result)`` — everything decidable from the
  result alone: exact-key closure at every declared path, severity and type
  discipline, precedent status rules, residual PII. Conditions
  #1 / #2 / #8 / #9 / #10-d / #11 / #12.

🔴 ``_extra_security_gate_output`` receives **no ``state``**. Mode branching
uses the declared ``result["mode"]`` value, never key presence — branching on
"does this key exist" is how a gate ends up failing open.

There is **no denylist of conclusive phrasings or key names.** Phrases can be
paraphrased, while undeclared keys are already rejected by the closed schemas;
either list would manufacture a false assurance of coverage.
Non-conclusiveness is structural instead: verdict fields cannot be
represented, ``severity`` is pinned to ``candidate``, every claim carries a
verified source, and quotes are restricted to non-adjudicative spans. The
residue is handled by human review, and ``docs/02`` says so plainly.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any, ClassVar

from framework.errors import SecurityViolationError
from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.security import detect_pii

from src.services import audit, contracts, quantities, service

_FREE_TEXT_FIELDS = ("description", "question")

_NESTED_KEY_SETS = (
    contracts.META_KEYS,
    contracts.APPLIED_PIN_KEYS,
    contracts.CRITERIA_CONDITION_KEYS,
    contracts.EVIDENCE_SOURCE_KEYS,
    contracts.PROVENANCE_KEYS,
    contracts.FREEZE_FACT_KEYS,
    contracts.PRECEDENT_CHECK_KEYS,
    contracts.PLANNED_WINDOW_KEYS,
    contracts.OVERLAPPING_WINDOW_KEYS,
    contracts.INGEST_SUMMARY_KEYS,
    contracts.REJECTED_ITEM_KEYS,
    contracts.FEEDBACK_SUMMARY_KEYS,
    contracts.AUDIT_EVENT_KEYS,
    contracts.DELETED_KEYS,
) + tuple(frozenset(fields) for fields in contracts.FINDING_FIELDS_BY_SECTION.values())

_TOP_LEVEL_BY_MODE = {
    "invoke": contracts.INVOKE_TOP_LEVEL_KEYS,
    "ingest": contracts.INGEST_TOP_LEVEL_KEYS,
    "feedback": contracts.FEEDBACK_TOP_LEVEL_KEYS,
}


class ReviewPacketPostProcessNode(FunctionNode):
    """Outer ``post_process`` slot."""

    _s4_audit_exempt: ClassVar[str] = (
        "emits through src/services/audit.py::emit, a module-level helper, and @audit.ensure_domain_event guarantees one event on every return and raise path — outside this gate's same-class one-hop ceiling"
    )

    required_trust_level = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, min_quote_chars: int = 12) -> None:
        self._min_quote_chars = max(1, int(min_quote_chars))

    # -- stage 1: state-dependent verification + assembly -------------------
    @audit.ensure_domain_event(audit.EVT_DOMAIN_PATH, node="post_process")
    def execute(self, state: AgentState) -> dict[str, Any]:
        mode = str(state.get("mode") or "")
        if state.get("status") == AgentStatus.ERROR.value:
            # Pass an upstream error through untouched; assembling a packet on
            # top of a failed run would present it as a result.
            return {}

        needs_review = bool(state.get("requires_human_review"))
        meta_degraded = bool(state.get("degraded"))
        degradation_code = state.get("degradation_code")

        if mode == "ingest":
            envelope = {
                "mode": "ingest",
                "gate_kind": contracts.GATE_KIND,
                "meta": _meta(meta_degraded, degradation_code, False),
                "ingest_summary": state.get("ingest_summary") or {"written_ids": [], "rejected": []},
            }
        elif mode == "feedback":
            envelope = {
                "mode": "feedback",
                "gate_kind": contracts.GATE_KIND,
                "meta": _meta(meta_degraded, degradation_code, False),
                "feedback_summary": state.get("feedback_summary") or {"applied_count": 0, "audit_event_id": ""},
            }
        else:
            envelope, needs_review = self._build_invoke_envelope(state, needs_review)
            envelope["meta"] = _meta(
                meta_degraded or bool(envelope.pop("_degraded", False)), degradation_code, needs_review
            )

        audit.emit(
            audit.EVT_STAGE4,
            {
                "mode": mode,
                "request_ref": str(state.get("request_ref") or ""),
                "requires_human_review": needs_review,
                "phase": "envelope_assembled",
            },
            state,
        )

        result: dict[str, Any] = {
            "formatted_output": envelope,
            "status": AgentStatus.SUCCESS.value,
        }
        if needs_review:
            result["requires_human_review"] = True
        return result

    # ------------------------------------------------------------------
    def _build_invoke_envelope(self, state: AgentState, needs_review: bool) -> tuple[dict[str, Any], bool]:
        registry: dict[str, dict[str, Any]] = dict(state.get("source_registry") or {})
        packet: dict[str, Any] = dict(state.get("review_packet") or {})
        request_ref = str(state.get("request_ref") or "")
        criteria_version_ref = str(state.get("criteria_version_ref") or "")
        conditions = {
            str(row.get("condition_id")) for row in (state.get("criteria_conditions") or []) if isinstance(row, dict)
        }
        recall_by_ref = {str(r.get("history_ref")): r for r in (state.get("recall_refs") or []) if isinstance(r, dict)}
        freeze_facts = dict(state.get("freeze_window_facts") or {})

        # Deterministic values the free text must not contradict (#10-b).
        deterministic_numbers = _deterministic_numbers(freeze_facts, state)

        sections: dict[str, list[dict[str, Any]]] = {}
        cited: list[str] = []

        for section, fields in contracts.FINDING_FIELDS_BY_SECTION.items():
            kept: list[dict[str, Any]] = []
            for finding in packet.get(section) or []:
                if not isinstance(finding, dict):
                    continue
                gap_origin = finding.get("gap_origin")
                source_ids = [s for s in (finding.get("source_ids") or []) if isinstance(s, str)]

                if gap_origin != "deterministic":
                    reason = self._reject_finding(
                        finding,
                        source_ids,
                        registry,
                        conditions,
                        request_ref,
                        criteria_version_ref,
                        deterministic_numbers,
                    )
                    if reason is not None:
                        needs_review = True
                        continue

                row = {name: finding.get(name) for name in fields if name in finding}
                row.setdefault("source_ids", source_ids)
                if section == "evidence_gaps":
                    row.setdefault("gap_origin", gap_origin or "llm")
                kept.append(_mask_free_text(row))
                for source_id in source_ids:
                    if source_id not in cited:
                        cited.append(source_id)
            sections[section] = kept

        precedents = []
        for row in packet.get("precedent_checks") or []:
            if not isinstance(row, dict):
                continue
            history_ref = str(row.get("history_ref") or "")
            recalled = recall_by_ref.get(history_ref)
            if recalled is None:
                needs_review = True
                continue
            # #10-c: the past facts are copied, never authored. Any drift means
            # the model rewrote history, which is quieter and more dangerous
            # than getting the current judgement wrong.
            if row.get("prior_issue") != (recalled.get("decision_rationale_safe") or ""):
                needs_review = True
                continue
            if row.get("prior_outcome") != (recalled.get("decision_outcome") or ""):
                needs_review = True
                continue
            precedents.append(
                _mask_free_text({name: row.get(name) for name in contracts.PRECEDENT_CHECK_KEYS if name in row})
            )
            for source_id in row.get("source_ids") or []:
                if source_id not in cited:
                    cited.append(source_id)

        envelope: dict[str, Any] = {
            "mode": "invoke",
            "gate_kind": contracts.GATE_KIND,
            "applied_pins": _applied_pins(state),
            "criteria_conditions": [
                {k: row.get(k) for k in contracts.CRITERIA_CONDITION_KEYS}
                for row in (state.get("criteria_conditions") or [])
                if isinstance(row, dict)
            ],
            "evidence_sources": _evidence_sources(cited, registry, self._min_quote_chars),
            "freeze_window_facts": {k: freeze_facts.get(k) for k in contracts.FREEZE_FACT_KEYS},
            "supported_conditions": sections.get("supported_conditions", []),
            "evidence_gaps": sections.get("evidence_gaps", []),
            "contradictions": sections.get("contradictions", []),
            "approval_scope_discrepancies": sections.get("approval_scope_discrepancies", []),
            "precedent_checks": precedents,
            "verification_questions": sections.get("verification_questions", []),
        }
        return envelope, needs_review

    def _reject_finding(
        self,
        finding: dict[str, Any],
        source_ids: list[str],
        registry: dict[str, dict[str, Any]],
        conditions: set[str],
        request_ref: str,
        criteria_version_ref: str,
        deterministic_numbers: set[tuple[str, float]],
    ) -> str | None:
        if not source_ids:
            return "no_source"
        for source_id in source_ids:
            entry = registry.get(source_id)
            if entry is None:
                return "unknown_source"  # #6
            if not service.is_quotable(str(entry.get("span_kind") or "")):
                return "non_quotable_span"  # #5
            if len(str(entry.get("span_text") or "")) < self._min_quote_chars:
                # The span is too short to be quoted, so `_evidence_sources`
                # will not offer it. Keeping the claim would leave it citing a
                # source the reader cannot see -- drop the claim, not just the
                # source, or the packet contradicts itself.
                return "span_too_short"
            provenance = entry.get("provenance") or {}
            if provenance.get("request_ref") != request_ref:
                return "foreign_request_ref"  # #7
            if provenance.get("criteria_version_ref") != criteria_version_ref:
                return "foreign_criteria_version"  # #7

        condition_id = finding.get("condition_id")
        if condition_id and str(condition_id) not in conditions:
            return "unknown_condition"  # #6

        source_texts = [str(registry[s].get("span_text") or "") for s in source_ids]
        for field in _FREE_TEXT_FIELDS:
            text = finding.get(field)
            if not isinstance(text, str) or not text:
                continue
            supported, _ = quantities.claim_numbers_supported(text, source_texts)
            if not supported:
                return "number_not_in_source"  # #10
            written, _ = quantities.extract(text)
            for kind, value in written:
                # #10-b: only contradict-checking. Saying nothing about a
                # deterministic value is fine; stating a different one is not.
                same_kind = {v for k, v in deterministic_numbers if k == kind}
                if same_kind and value not in same_kind and not _appears_in(value, kind, source_texts):
                    return "contradicts_deterministic_value"
        return None

    # -- stage 2: result-only verification ---------------------------------
    def _extra_security_gate_output(self, result: dict[str, Any]) -> dict[str, Any]:
        """Validate only; return the result unchanged.

        The hook's contract is ``-> dict`` and ``__call__`` uses the RETURN
        value as the node's result -- returning ``None`` silently turns every
        run into an error. It also must not *transform* anything: a gate that
        rewrites values makes the returned envelope differ from what was
        already validated and rendered upstream.
        """
        envelope = result.get("formatted_output")
        if envelope is None:
            return result
        if not isinstance(envelope, dict):
            raise SecurityViolationError("S-3: formatted_output must be an object")

        declared_mode = envelope.get("mode")
        expected = _TOP_LEVEL_BY_MODE.get(str(declared_mode) or "")
        if expected is None:
            raise SecurityViolationError(f"S-3: unknown declared mode {declared_mode!r}")

        # #1 -- exact match, so a MISSING key fails too, not only an extra one.
        if set(envelope) != set(expected):
            missing = sorted(set(expected) - set(envelope))
            extra = sorted(set(envelope) - set(expected))
            raise SecurityViolationError(f"S-3: top-level keys not exact (missing={missing}, extra={extra})")

        _assert_nested_exact_keys(envelope)  # #2
        _assert_types(envelope)  # #11
        if str(envelope.get("gate_kind")) != contracts.GATE_KIND:
            # An arbitrary gate_kind lets a packet claim it came from a gate
            # that never ran; exact-key alone only proves the field is present.
            raise SecurityViolationError(f"S-3: gate_kind {envelope.get('gate_kind')!r} is not {contracts.GATE_KIND!r}")
        if declared_mode == "invoke":
            _assert_finding_discipline(envelope)  # #8 / #9 / #10-d
            _assert_citation_integrity(envelope, self._min_quote_chars)
        _assert_no_residual_pii(envelope)  # #12
        return result


# ---------------------------------------------------------------------------
# assembly helpers
# ---------------------------------------------------------------------------


def _meta(degraded: bool, degradation_code: Any, needs_review: bool) -> dict[str, Any]:
    return {
        "degraded": bool(degraded),
        "degradation_code": degradation_code if degradation_code else None,
        "requires_human_review": bool(needs_review),
    }


def _applied_pins(state: AgentState) -> dict[str, Any]:
    return {
        "criteria_version_ref": state.get("criteria_version_ref"),
        "criteria_version": state.get("criteria_version"),
        "criteria_content_hash": state.get("criteria_content_hash"),
        "freeze_calendar_ref": state.get("freeze_calendar_ref"),
        "freeze_calendar_version": state.get("freeze_calendar_version"),
        "freeze_calendar_content_hash": state.get("freeze_calendar_content_hash"),
    }


def _evidence_sources(
    cited: list[str], registry: dict[str, dict[str, Any]], min_quote_chars: int
) -> list[dict[str, Any]]:
    """Build ``evidence_sources`` from the registry, not from the model.

    Every field is copied from the trusted table, so a fabricated source with a
    self-consistent provenance cannot appear here at all.
    """
    out: list[dict[str, Any]] = []
    for source_id in cited:
        entry = registry.get(source_id)
        if entry is None:
            continue
        span_text = str(entry.get("span_text") or "")
        if len(span_text) < min_quote_chars:
            # Too short to be a meaningful quote: `quote in text` would be
            # vacuous, so it is not offered as one.
            continue
        out.append(
            {
                "source_id": source_id,
                "doc_type": entry.get("doc_type"),
                "quote_span": span_text,
                "span_kind": entry.get("span_kind"),
                "provenance": {k: (entry.get("provenance") or {}).get(k) for k in contracts.PROVENANCE_KEYS},
            }
        )
    return out


def _deterministic_numbers(freeze_facts: dict[str, Any], state: AgentState) -> set[tuple[str, float]]:
    numbers: set[tuple[str, float]] = set()
    overlaps = freeze_facts.get("overlapping_windows") or []
    numbers.add(("count", float(len(overlaps))))
    gaps = state.get("field_gaps") or []
    numbers.add(("count", float(len(gaps))))
    return numbers


def _appears_in(value: float, kind: str, texts: list[str]) -> bool:
    for text in texts:
        found, _ = quantities.extract(text)
        if (kind, value) in found:
            return True
    return False


def _mask_free_text(row: dict[str, Any]) -> dict[str, Any]:
    """Mask PII in the free-text fields on the way out.

    The framework performs **no** PII masking on output (S-3's default check is
    credentials only), so a value masked on input is not masked on return.
    """
    masked = dict(row)
    for field in _FREE_TEXT_FIELDS:
        value = masked.get(field)
        if isinstance(value, str) and value:
            masked[field] = _mask(value)
    for field in ("prior_issue",):
        value = masked.get(field)
        if isinstance(value, str) and value:
            masked[field] = _mask(value)
    return masked


def _mask(text: str) -> str:
    findings = detect_pii(text)
    if not findings:
        return text
    spans = [
        (f["start"], f["end"])
        for f in findings
        if isinstance(f, dict) and isinstance(f.get("start"), int) and isinstance(f.get("end"), int)
    ]
    out = text
    for start, end in sorted(spans, key=lambda s: s[0], reverse=True):
        out = out[:start] + "[MASKED]" + out[end:]
    return out


# ---------------------------------------------------------------------------
# gate helpers (result-only)
# ---------------------------------------------------------------------------


def _walk(node: Any, path: str = "$") -> Iterator[tuple[str, Any]]:
    yield path, node
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _walk(value, f"{path}.{key}")
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            yield from _walk(value, f"{path}[{index}]")


def _normalise_path(path: str) -> str:
    """Collapse list indices so a path maps to one schema."""
    return re.sub(r"\[\d+\]", "[]", path)


# Path -> the EXACT key set that path must carry. Matching by path rather than
# by "is a subset of any declared set" is the point: a subset test accepts a
# `provenance` that is missing `source_id`, and accepts an object whose keys
# happen to be a subset of some *other* schema entirely.
_PATH_SCHEMAS: dict[str, frozenset[str]] = {
    "$.meta": contracts.META_KEYS,
    "$.applied_pins": contracts.APPLIED_PIN_KEYS,
    "$.criteria_conditions[]": contracts.CRITERIA_CONDITION_KEYS,
    "$.evidence_sources[]": contracts.EVIDENCE_SOURCE_KEYS,
    "$.evidence_sources[].provenance": contracts.PROVENANCE_KEYS,
    "$.freeze_window_facts": contracts.FREEZE_FACT_KEYS,
    "$.freeze_window_facts.planned_window": contracts.PLANNED_WINDOW_KEYS,
    "$.freeze_window_facts.overlapping_windows[]": contracts.OVERLAPPING_WINDOW_KEYS,
    "$.precedent_checks[]": contracts.PRECEDENT_CHECK_KEYS,
    "$.ingest_summary": contracts.INGEST_SUMMARY_KEYS,
    "$.ingest_summary.rejected[]": contracts.REJECTED_ITEM_KEYS,
    "$.feedback_summary": contracts.FEEDBACK_SUMMARY_KEYS,
    **{f"$.{section}[]": frozenset(fields) for section, fields in contracts.FINDING_FIELDS_BY_SECTION.items()},
}


def _assert_nested_exact_keys(envelope: dict[str, Any]) -> None:
    for path, node in _walk(envelope):
        if path == "$" or not isinstance(node, dict):
            continue
        normalised = _normalise_path(path)
        expected = _PATH_SCHEMAS.get(normalised)
        if expected is None:
            # An object at a path the contract never declared. There is no
            # schema to judge it by, so it must not exist.
            raise SecurityViolationError(f"S-3: undeclared nested object at {path}")
        keys = set(node)
        if keys != set(expected):
            missing = sorted(set(expected) - keys)
            extra = sorted(keys - set(expected))
            raise SecurityViolationError(
                f"S-3: nested object at {path} is not exact-key " f"(missing={missing}, extra={extra})"
            )


def _assert_types(envelope: dict[str, Any]) -> None:
    if not isinstance(envelope.get("mode"), str):
        raise SecurityViolationError("S-3: mode must be a string")
    if not isinstance(envelope.get("gate_kind"), str):
        raise SecurityViolationError("S-3: gate_kind must be a string")
    meta = envelope.get("meta")
    if not isinstance(meta, dict):
        raise SecurityViolationError("S-3: meta must be an object")
    if not isinstance(meta.get("degraded"), bool):
        raise SecurityViolationError("S-3: meta.degraded must be bool")
    if not isinstance(meta.get("requires_human_review"), bool):
        raise SecurityViolationError("S-3: meta.requires_human_review must be bool")
    code = meta.get("degradation_code")
    if code is not None and code not in contracts.DEGRADATION_CODES:
        raise SecurityViolationError(f"S-3: unknown degradation_code {code!r}")

    if envelope.get("mode") == "invoke":
        pins = envelope.get("applied_pins")
        if not isinstance(pins, dict):
            raise SecurityViolationError("S-3: applied_pins must be an object")
        for key in ("criteria_version_ref", "criteria_version", "criteria_content_hash"):
            if not isinstance(pins.get(key), str) or not pins.get(key):
                raise SecurityViolationError(f"S-3: applied_pins.{key} must be a string")
        for key in ("freeze_calendar_ref", "freeze_calendar_version", "freeze_calendar_content_hash"):
            if pins.get(key) is not None and not isinstance(pins.get(key), str):
                raise SecurityViolationError(f"S-3: applied_pins.{key} must be string or null")

        facts = envelope.get("freeze_window_facts")
        if not isinstance(facts, dict) or not isinstance(facts.get("evaluated"), bool):
            raise SecurityViolationError("S-3: freeze_window_facts.evaluated must be bool")
        for key in (
            "criteria_conditions",
            "evidence_sources",
            "supported_conditions",
            "evidence_gaps",
            "contradictions",
            "approval_scope_discrepancies",
            "precedent_checks",
            "verification_questions",
        ):
            if not isinstance(envelope.get(key), list):
                raise SecurityViolationError(f"S-3: {key} must be a list")

        for row in envelope["criteria_conditions"]:
            _assert_string_fields(row, ("condition_id", "criteria_version_ref", "condition_ref"), "criteria_conditions")
        for row in envelope["evidence_sources"]:
            _assert_string_fields(row, ("source_id", "doc_type", "quote_span", "span_kind"), "evidence_sources")
            provenance = row.get("provenance")
            _assert_string_fields(
                provenance,
                ("request_ref", "source_id", "doc_ref", "span_ref", "criteria_version_ref"),
                "evidence_sources.provenance",
            )
            if row.get("doc_type") not in contracts.DOC_TYPES:
                raise SecurityViolationError("S-3: evidence_sources.doc_type is outside enum")
            if row.get("span_kind") not in contracts.QUOTABLE_SPAN_KINDS:
                raise SecurityViolationError("S-3: evidence_sources.span_kind is not quotable")

        planned = facts.get("planned_window")
        _assert_string_fields(planned, ("start", "end"), "freeze_window_facts.planned_window")
        overlaps = facts.get("overlapping_windows")
        if not isinstance(overlaps, list):
            raise SecurityViolationError("S-3: freeze_window_facts.overlapping_windows must be a list")
        for row in overlaps:
            _assert_string_fields(
                row,
                ("window_id", "window_ref", "overlap_start", "overlap_end"),
                "freeze_window_facts.overlapping_windows",
            )
        if not isinstance(facts.get("evaluated_at"), str):
            raise SecurityViolationError("S-3: freeze_window_facts.evaluated_at must be string")
        if facts.get("freeze_calendar_ref") is not None and not isinstance(facts.get("freeze_calendar_ref"), str):
            raise SecurityViolationError("S-3: freeze_window_facts.freeze_calendar_ref must be string or null")

        _assert_finding_types(envelope)
    elif envelope.get("mode") == "ingest":
        summary = envelope.get("ingest_summary")
        if not isinstance(summary, dict) or not isinstance(summary.get("written_ids"), list):
            raise SecurityViolationError("S-3: ingest_summary has invalid types")
        if any(not isinstance(item, str) for item in summary["written_ids"]):
            raise SecurityViolationError("S-3: ingest_summary.written_ids must contain strings")
        if not isinstance(summary.get("rejected"), list):
            raise SecurityViolationError("S-3: ingest_summary.rejected must be a list")
        for row in summary["rejected"]:
            if not isinstance(row, dict):
                raise SecurityViolationError("S-3: ingest_summary.rejected element must be an object")
            if (
                not isinstance(row.get("index"), int)
                or isinstance(row.get("index"), bool)
                or not isinstance(row.get("record_type"), str)
                or not isinstance(row.get("reason"), str)
            ):
                raise SecurityViolationError("S-3: ingest_summary.rejected element has invalid types")
    else:
        summary = envelope.get("feedback_summary")
        if (
            not isinstance(summary, dict)
            or not isinstance(summary.get("applied_count"), int)
            or isinstance(summary.get("applied_count"), bool)
            or not isinstance(summary.get("audit_event_id"), str)
        ):
            raise SecurityViolationError("S-3: feedback_summary has invalid types")


def _assert_string_fields(row: Any, fields: tuple[str, ...], label: str) -> None:
    if not isinstance(row, dict):
        raise SecurityViolationError(f"S-3: {label} element must be an object")
    for field in fields:
        if not isinstance(row.get(field), str) or not row.get(field):
            raise SecurityViolationError(f"S-3: {label}.{field} must be a non-empty string")


def _assert_finding_types(envelope: dict[str, Any]) -> None:
    for section, fields in contracts.FINDING_FIELDS_BY_SECTION.items():
        for row in envelope[section]:
            if not isinstance(row, dict):
                raise SecurityViolationError(f"S-3: {section} element must be an object")
            for field in fields:
                value = row.get(field)
                if value is None and (section, field) in contracts.NULLABLE_FINDING_FIELDS:
                    continue
                if field == "source_ids":
                    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
                        raise SecurityViolationError(f"S-3: {section}.source_ids must contain non-empty strings")
                elif field == "confidence":
                    if not isinstance(value, (int, float)) or isinstance(value, bool):
                        raise SecurityViolationError(f"S-3: {section}.confidence must be numeric")
                elif not isinstance(value, str):
                    raise SecurityViolationError(f"S-3: {section}.{field} must be a string")
            if section == "evidence_gaps" and row.get("gap_origin") not in contracts.GAP_ORIGINS:
                raise SecurityViolationError("S-3: evidence_gaps.gap_origin is outside enum")

    for row in envelope["precedent_checks"]:
        if not isinstance(row, dict):
            raise SecurityViolationError("S-3: precedent_checks element must be an object")
        _assert_string_fields(
            row, ("history_ref", "condition_id", "prior_issue", "prior_outcome", "status"), "precedent_checks"
        )
        source_ids = row.get("source_ids")
        if not isinstance(source_ids, list) or any(not isinstance(item, str) or not item for item in source_ids):
            raise SecurityViolationError("S-3: precedent_checks.source_ids must contain non-empty strings")


def _assert_finding_discipline(envelope: dict[str, Any]) -> None:
    for finding in envelope.get("supported_conditions") or []:
        if finding.get("severity") not in contracts.SEVERITIES:  # #8
            raise SecurityViolationError(f"S-3: severity {finding.get('severity')!r} is not candidate-grade")
        confidence = finding.get("confidence")
        if confidence is not None:
            if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
                raise SecurityViolationError("S-3: confidence must be numeric")
            if not 0.0 <= float(confidence) <= 1.0:
                raise SecurityViolationError("S-3: confidence out of range")

    for finding in envelope.get("approval_scope_discrepancies") or []:  # #9
        if set(finding) != {"claim_id", "description", "source_ids"}:
            raise SecurityViolationError("S-3: approval_scope_discrepancies element is not exact-key")

    for row in envelope.get("precedent_checks") or []:  # #10-d
        status = row.get("status")
        if status not in contracts.PRECEDENT_STATUSES:
            raise SecurityViolationError(f"S-3: unknown precedent status {status!r}")
        if status in ("addressed", "unclear") and not (row.get("source_ids") or []):
            raise SecurityViolationError(f"S-3: precedent status {status!r} requires at least one source")


def _assert_citation_integrity(envelope: dict[str, Any], min_quote_chars: int) -> None:
    """Every cited source must be present, unique and actually quotable.

    Assembly builds ``evidence_sources`` from the registry and the findings
    from the model, so the two can drift apart: a source dropped for being too
    short leaves the finding citing an id the reader cannot resolve. That is a
    *dangling citation* -- the packet looks sourced and is not. Checking it
    here, on the result only, means the property holds however assembly is
    later rewritten.
    """
    source_ids: list[str] = []
    for entry in envelope.get("evidence_sources") or []:
        if not isinstance(entry, dict):
            raise SecurityViolationError("S-3: evidence_sources element must be an object")
        source_id = entry.get("source_id")
        if not isinstance(source_id, str) or not source_id:
            raise SecurityViolationError("S-3: evidence_sources element has no source_id")
        if source_id in source_ids:
            raise SecurityViolationError(f"S-3: duplicate source_id {source_id!r}")
        source_ids.append(source_id)

        if entry.get("span_kind") not in contracts.QUOTABLE_SPAN_KINDS:
            raise SecurityViolationError(f"S-3: evidence source {source_id!r} is not quotable")
        provenance = entry.get("provenance")
        if not isinstance(provenance, dict) or provenance.get("source_id") != source_id:
            raise SecurityViolationError(f"S-3: provenance source_id does not match {source_id!r}")

        quote = entry.get("quote_span")
        if not isinstance(quote, str) or len(quote) < min_quote_chars:
            # A one-character "quote" makes every verbatim check vacuous.
            raise SecurityViolationError(f"S-3: quote_span for {source_id!r} is shorter than {min_quote_chars}")

    known = set(source_ids)
    known_conditions = {row["condition_id"] for row in envelope.get("criteria_conditions") or []}
    pinned_criteria = envelope["applied_pins"]["criteria_version_ref"]
    for entry in envelope.get("evidence_sources") or []:
        if entry["provenance"]["criteria_version_ref"] != pinned_criteria:
            raise SecurityViolationError("S-3: evidence source criteria_version_ref does not match applied pin")
    sections = list(contracts.FINDING_FIELDS_BY_SECTION) + ["precedent_checks"]
    for section in sections:
        for finding in envelope.get(section) or []:
            cited_ids = finding.get("source_ids") or []
            source_optional = (section == "evidence_gaps" and finding.get("gap_origin") == "deterministic") or (
                section == "precedent_checks" and finding.get("status") == "not_addressed"
            )
            if not source_optional and not cited_ids:
                raise SecurityViolationError(f"S-3: {section} requires at least one source")
            if len(cited_ids) != len(set(cited_ids)):
                raise SecurityViolationError(f"S-3: {section} contains duplicate source ids")
            condition_id = finding.get("condition_id")
            if condition_id is not None and condition_id not in known_conditions:
                raise SecurityViolationError(f"S-3: {section} cites unknown condition {condition_id!r}")
            for cited in cited_ids:
                if cited not in known:
                    raise SecurityViolationError(f"S-3: {section} cites unknown source {cited!r}")


def _assert_no_residual_pii(envelope: dict[str, Any]) -> None:
    for path, node in _walk(envelope):
        # Customer-defined references are identifiers, not prose.  AgentCore's
        # phone detector intentionally over-matches values such as
        # ``EVT-04-12-2024``; scanning every string would reject a legitimate
        # packet.  Scan every model/human-visible prose field instead.
        field = path.rsplit(".", 1)[-1]
        if (
            field in {"description", "question", "quote_span", "prior_issue", "condition_ref", "window_ref"}
            and isinstance(node, str)
            and node
            and detect_pii(node)
        ):
            raise SecurityViolationError(f"S-3: residual PII at {path}")


__all__ = ["ReviewPacketPostProcessNode"]
