"""S-3 output gate — accept/reject pairs (AT-3 / AT-4).

Both directions are asserted on purpose. Checking only that attacks are blocked
hides over-rejection: a gate that refuses legitimate output is just as broken,
and the failure mode is quieter. So every test here starts from a baseline that
must PASS, and mutates one thing.

The probe fixtures are built from the real constants in ``src.services
.contracts``; hand-writing the expected shape is how a fixture ends up testing
the test rather than the gate.
"""

from __future__ import annotations

import pytest
from framework.errors import SecurityViolationError

from src.nodes.post_process_node import ReviewPacketPostProcessNode
from src.services import contracts


def _baseline_envelope() -> dict:
    """A valid invoke envelope: exactly INVOKE_TOP_LEVEL_KEYS, nothing else."""
    return {
        "mode": "invoke",
        "gate_kind": contracts.GATE_KIND,
        "meta": {"degraded": False, "degradation_code": None,
                 "requires_human_review": False},
        "applied_pins": {
            "criteria_version_ref": "CRIT-STD", "criteria_version": "v3",
            "criteria_content_hash": "hashcrit", "freeze_calendar_ref": None,
            "freeze_calendar_version": None, "freeze_calendar_content_hash": None,
        },
        "criteria_conditions": [
            {"condition_id": "CONDA", "criteria_version_ref": "CRIT-STD",
             "condition_ref": "2.1"},
        ],
        "evidence_sources": [{
            "source_id": "SRCAA", "doc_type": "rollback_plan",
            "quote_span": "復旧所要時間は4時間と記載", "span_kind": "quantity",
            "provenance": {"request_ref": "REQAAA", "source_id": "SRCAA",
                           "doc_ref": "DOC-RB", "span_ref": "p1-s1",
                           "criteria_version_ref": "CRIT-STD"},
        }],
        "freeze_window_facts": {
            "freeze_calendar_ref": None,
            "planned_window": {"start": "2026-09-28T22:00:00+09:00",
                               "end": "2026-09-29T02:00:00+09:00"},
            "overlapping_windows": [], "evaluated": False,
            "evaluated_at": "2026-08-20T00:00:00+09:00",
        },
        "supported_conditions": [{
            "claim_id": "C1", "description": "切戻し手順の記載がある",
            "condition_id": "CONDA", "source_ids": ["SRCAA"],
            "severity": "candidate", "confidence": 0.5,
        }],
        "evidence_gaps": [{
            "claim_id": "DGX", "description": "検証記録の記載が見当たりません",
            "condition_id": None, "source_ids": [], "gap_origin": "deterministic",
        }],
        "contradictions": [],
        "approval_scope_discrepancies": [],
        "precedent_checks": [{
            "history_ref": "HISTAA", "condition_id": "CONDA",
            "prior_issue": "検証済みの記載が無かった", "prior_outcome": "rejected",
            "status": "addressed", "source_ids": ["SRCAA"],
        }],
        "verification_questions": [],
    }


def _gate(envelope: dict) -> dict:
    node = ReviewPacketPostProcessNode(min_quote_chars=12)
    return node._extra_security_gate_output({"formatted_output": envelope})  # noqa: SLF001


# ---------------------------------------------------------------------------
# accept
# ---------------------------------------------------------------------------

def test_baseline_passes_and_is_returned_unchanged():
    """If this fails, every reject test below proves nothing."""
    envelope = _baseline_envelope()
    result = _gate(envelope)
    assert result["formatted_output"] is envelope      # validate, never transform


def test_declared_nullable_finding_values_pass_unchanged():
    envelope = _baseline_envelope()
    envelope["supported_conditions"][0]["confidence"] = None
    envelope["contradictions"] = [{
        "claim_id": "C3", "description": "申請記述と証跡が矛盾している",
        "condition_id": None, "source_ids": ["SRCAA"],
    }]
    envelope["verification_questions"] = [{
        "claim_id": "C5", "question": "緊急性を裏付ける記録はありますか",
        "condition_id": None, "source_ids": ["SRCAA"],
    }]
    # The baseline evidence gap already carries condition_id=None.
    result = _gate(envelope)
    assert result["formatted_output"] is envelope
    assert envelope["supported_conditions"][0]["confidence"] is None
    assert envelope["evidence_gaps"][0]["condition_id"] is None
    assert envelope["contradictions"][0]["condition_id"] is None
    assert envelope["verification_questions"][0]["condition_id"] is None


def test_condition_id_nullability_is_not_broadened_to_other_sections():
    envelope = _baseline_envelope()
    envelope["supported_conditions"][0]["condition_id"] = None
    with pytest.raises(SecurityViolationError):
        _gate(envelope)


def test_every_model_authored_field_has_the_declared_nullability():
    """Exercise every model field, not only the two nullable examples."""
    rows = {
        "supported_conditions": {
            "claim_id": "C1", "description": "切戻し手順の記載がある",
            "condition_id": "CONDA", "source_ids": ["SRCAA"],
            "severity": "candidate", "confidence": 0.5,
        },
        "evidence_gaps": {
            "claim_id": "C2", "description": "顧客承認の記録が不足している",
            "condition_id": "CONDA", "source_ids": ["SRCAA"], "gap_origin": "llm",
        },
        "contradictions": {
            "claim_id": "C3", "description": "申請記述と証跡が矛盾している",
            "condition_id": "CONDA", "source_ids": ["SRCAA"],
        },
        "approval_scope_discrepancies": {
            "claim_id": "C4", "description": "承認記録の対象範囲と申請範囲が異なる",
            "source_ids": ["SRCAA"],
        },
        "verification_questions": {
            "claim_id": "C5", "question": "顧客承認の対象範囲を確認できますか",
            "condition_id": "CONDA", "source_ids": ["SRCAA"],
        },
    }

    checked: set[tuple[str, str]] = set()
    for section, fields in contracts.FINDING_FIELDS_BY_SECTION.items():
        for field in fields:
            if field == "gap_origin":
                # Stage 4, not the model, authors this field.
                continue
            envelope = _baseline_envelope()
            envelope[section] = [dict(rows[section])]
            envelope[section][0][field] = None
            pair = (section, field)
            checked.add(pair)
            if pair in contracts.NULLABLE_FINDING_FIELDS:
                assert _gate(envelope)["formatted_output"] is envelope
            else:
                with pytest.raises(SecurityViolationError):
                    _gate(envelope)

    model_fields = {
        (section, field)
        for section, fields in contracts.FINDING_FIELDS_BY_SECTION.items()
        for field in fields
        if field != "gap_origin"
    }
    assert checked == model_fields

    # The three precedent fields authored by the model are all non-nullable;
    # condition/prior text is copied later from the trusted recall record.
    for field in contracts.PRECEDENT_DRAFT_KEYS:
        envelope = _baseline_envelope()
        envelope["precedent_checks"][0][field] = None
        with pytest.raises(SecurityViolationError):
            _gate(envelope)


def test_ingest_and_feedback_envelopes_pass():
    for envelope in (
        {"mode": "ingest", "gate_kind": contracts.GATE_KIND,
         "meta": {"degraded": False, "degradation_code": None,
                  "requires_human_review": False},
         "ingest_summary": {"written_ids": [], "rejected": []}},
        {"mode": "feedback", "gate_kind": contracts.GATE_KIND,
         "meta": {"degraded": False, "degradation_code": None,
                  "requires_human_review": False},
         "feedback_summary": {"applied_count": 1, "audit_event_id": "EVTAA"}},
    ):
        assert _gate(envelope)["formatted_output"] is envelope


# ---------------------------------------------------------------------------
# reject — one mutation each
# ---------------------------------------------------------------------------

def test_reject_extra_top_level_key():
    envelope = _baseline_envelope()
    envelope["approval_verdict"] = "approved"          # #1 exact top-level schema
    with pytest.raises(SecurityViolationError):
        _gate(envelope)


def test_reject_missing_top_level_key():
    """Exact match, so a MISSING key fails too -- a set-difference check
    that only looks for extras would let `mode` disappear."""
    envelope = _baseline_envelope()
    del envelope["gate_kind"]                          # #1
    with pytest.raises(SecurityViolationError):
        _gate(envelope)


def test_reject_nested_undeclared_key():
    envelope = _baseline_envelope()
    envelope["supported_conditions"][0]["recommendation"] = "承認すべき"   # #2
    with pytest.raises(SecurityViolationError):
        _gate(envelope)


def test_reject_coverage_verdict_in_discrepancies():
    envelope = _baseline_envelope()
    envelope["approval_scope_discrepancies"] = [
        {"claim_id": "C9", "description": "範囲外", "source_ids": ["SRCAA"],
         "covered": False},                            # #9
    ]
    with pytest.raises(SecurityViolationError):
        _gate(envelope)


def test_reject_arbitrary_provenance_key():
    envelope = _baseline_envelope()
    envelope["evidence_sources"][0]["provenance"]["note"] = "x"   # #2
    with pytest.raises(SecurityViolationError):
        _gate(envelope)


def test_reject_confirmed_severity():
    envelope = _baseline_envelope()
    envelope["supported_conditions"][0]["severity"] = "confirmed"  # #8
    with pytest.raises(SecurityViolationError):
        _gate(envelope)


def test_reject_out_of_range_confidence():
    envelope = _baseline_envelope()
    envelope["supported_conditions"][0]["confidence"] = 1.5        # #8
    with pytest.raises(SecurityViolationError):
        _gate(envelope)


def test_reject_unknown_degradation_code():
    envelope = _baseline_envelope()
    envelope["meta"]["degradation_code"] = "made_up"               # #11
    with pytest.raises(SecurityViolationError):
        _gate(envelope)


def test_accept_every_declared_degradation_code():
    """The state enum and the gate enum must be the same set.

    ``hitl_limit_reached`` was added to one side only during development; the
    run then produced a success envelope the gate rejected.
    """
    for code in contracts.DEGRADATION_CODES:
        envelope = _baseline_envelope()
        envelope["meta"]["degraded"] = True
        envelope["meta"]["degradation_code"] = code
        assert _gate(envelope)["formatted_output"]["meta"]["degradation_code"] == code


def test_reject_non_bool_evaluated():
    envelope = _baseline_envelope()
    envelope["freeze_window_facts"]["evaluated"] = "false"         # #11
    with pytest.raises(SecurityViolationError):
        _gate(envelope)


def test_reject_precedent_status_without_source():
    envelope = _baseline_envelope()
    envelope["precedent_checks"][0]["source_ids"] = []             # #10-d
    with pytest.raises(SecurityViolationError):
        _gate(envelope)


def test_not_addressed_may_have_no_source():
    """You cannot point at something that is absent."""
    envelope = _baseline_envelope()
    envelope["precedent_checks"][0]["status"] = "not_addressed"
    envelope["precedent_checks"][0]["source_ids"] = []
    assert _gate(envelope)


def test_reject_unknown_precedent_status():
    envelope = _baseline_envelope()
    envelope["precedent_checks"][0]["status"] = "approved"         # #10-d
    with pytest.raises(SecurityViolationError):
        _gate(envelope)


def test_reject_residual_pii_in_free_text():
    envelope = _baseline_envelope()
    envelope["supported_conditions"][0]["description"] = "担当者: 山田太郎 が確認"   # #12
    with pytest.raises(SecurityViolationError):
        _gate(envelope)


def test_reject_japanese_boundary_key_in_full_width():
    envelope = _baseline_envelope()
    envelope["承認可否"] = "可"                                     # #1
    with pytest.raises(SecurityViolationError):
        _gate(envelope)


def test_reject_unknown_mode():
    envelope = _baseline_envelope()
    envelope["mode"] = "audit"
    with pytest.raises(SecurityViolationError):
        _gate(envelope)


def test_reject_arbitrary_gate_kind():
    envelope = _baseline_envelope()
    envelope["gate_kind"] = "some_other_gate"
    with pytest.raises(SecurityViolationError):
        _gate(envelope)


def test_reject_dangling_finding_source_id():
    envelope = _baseline_envelope()
    envelope["supported_conditions"][0]["source_ids"] = ["SRCUNKNOWN"]
    with pytest.raises(SecurityViolationError):
        _gate(envelope)


def test_reject_cited_source_when_evidence_sources_is_empty():
    envelope = _baseline_envelope()
    envelope["evidence_sources"] = []
    with pytest.raises(SecurityViolationError):
        _gate(envelope)


def test_reject_short_quote_span():
    envelope = _baseline_envelope()
    envelope["evidence_sources"][0]["quote_span"] = "4時間"
    with pytest.raises(SecurityViolationError):
        _gate(envelope)


def test_reject_source_less_model_finding():
    envelope = _baseline_envelope()
    envelope["supported_conditions"][0]["source_ids"] = []
    with pytest.raises(SecurityViolationError):
        _gate(envelope)


def test_customer_reference_false_positive_is_not_treated_as_prose_pii():
    """A customer ref can look like a Japanese telephone number to the SDK."""
    envelope = _baseline_envelope()
    envelope["evidence_sources"][0]["provenance"]["doc_ref"] = "EVT-04-12-2024"
    assert _gate(envelope)["formatted_output"] is envelope


def test_reject_provenance_source_id_mismatch():
    envelope = _baseline_envelope()
    envelope["evidence_sources"][0]["provenance"]["source_id"] = "SRCBB"
    with pytest.raises(SecurityViolationError):
        _gate(envelope)


def test_reject_non_object_finding_in_a_less_privileged_section():
    envelope = _baseline_envelope()
    envelope["contradictions"] = ["not-an-object"]
    with pytest.raises(SecurityViolationError):
        _gate(envelope)


# ---------------------------------------------------------------------------
# absence test (AT-4) — the verdict fields are not representable
# ---------------------------------------------------------------------------

def test_no_verdict_field_exists_anywhere_in_the_contract():
    """Structural non-conclusiveness: the packet has no place to put a verdict.

    This is the property the design leans on instead of a phrase denylist --
    paraphrase defeats a denylist, but it cannot invent a field.
    """
    declared = set(contracts.INVOKE_TOP_LEVEL_KEYS)
    for fields in contracts.FINDING_FIELDS_BY_SECTION.values():
        declared |= set(fields)
    declared |= set(contracts.PRECEDENT_CHECK_KEYS) | set(contracts.META_KEYS)
    verdict_fields = {"approval_verdict", "exception_approved", "risk_acceptance",
                      "change_permissible", "recommendation", "next_action", "verdict"}
    assert not (declared & verdict_fields)
    # And severity can only ever be candidate-grade.
    assert contracts.SEVERITIES == frozenset({"candidate"})


# ---------------------------------------------------------------------------
# exact-key regression: a subset test is not an allowlist
# ---------------------------------------------------------------------------

def test_reject_nested_object_missing_a_required_key():
    """`keys <= allowed` accepts a truncated object; `keys == allowed` does not.

    An `applied_pins` without `criteria_version` still looks like a valid
    subset, and the review record then cannot say which edition it was judged
    under -- which is the whole point of pinning.
    """
    envelope = _baseline_envelope()
    del envelope["applied_pins"]["criteria_version"]
    with pytest.raises(SecurityViolationError):
        _gate(envelope)


def test_reject_object_whose_keys_belong_to_another_schema():
    """A subset test lets one schema's object masquerade as another's.

    `{"description": ...}` is a subset of the finding schema, so a subset check
    accepts it as a `provenance` -- losing every provenance field at once.
    """
    envelope = _baseline_envelope()
    envelope["evidence_sources"][0]["provenance"] = {"description": "not provenance"}
    with pytest.raises(SecurityViolationError):
        _gate(envelope)


def test_reject_object_at_an_undeclared_path():
    envelope = _baseline_envelope()
    envelope["meta"] = dict(envelope["meta"], extra={"smuggled": True})
    with pytest.raises(SecurityViolationError):
        _gate(envelope)
