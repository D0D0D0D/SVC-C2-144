"""Shared fixtures.

The provisioned deployment is built here rather than in each test so that the
"no default connection" property is exercised the same way everywhere: a test
that forgets to register its caller gets the same fail-closed 403 production
would give it.
"""

from __future__ import annotations

from typing import Any

import pytest

CALLER = "caller-msp-ops"
OTHER_CALLER = "caller-other"

ALL_PERMISSIONS = frozenset({
    "freeze.invoke.write_snapshot",
    "freeze.ingest.write_case_data",
    "freeze.feedback.append_candidate",
    "freeze.request.delete",
    "freeze.perspective.delete",
    "freeze.audit.read",
})


@pytest.fixture()
def server_module():
    """The entry adapter, with per-test stores.

    ``src.api.server`` builds its agent at import time (that is the scaffold
    contract), so the module is imported once and its resolver registry is
    reset between tests instead.
    """
    import src.api.server as server

    server._RESOLVER._by_caller.clear()               # noqa: SLF001 - test setup
    return server


@pytest.fixture()
def registry():
    from src.services import stores

    reg = stores.ControlDocumentRegistry()
    reg.register(stores.KIND_CRITERIA, "CRIT-STD", "v3", {
        "content_hash": "hashcrit",
        "approved": True,
        "effective_from": "2026-01-01T00:00:00+09:00",
        "conditions": [
            {"condition_id": "CONDA", "condition_ref": "2.1 ロールバック手順の検証記録"},
            {"condition_id": "CONDB", "condition_ref": "2.2 顧客承認の範囲"},
        ],
    })
    reg.register(stores.KIND_CALENDAR, "CAL-FY26", "v1", {
        "content_hash": "hashcal",
        "approved": True,
        "effective_from": "2026-01-01T00:00:00+09:00",
        "windows": [
            {"window_id": "WINQ", "window_ref": "四半期決算凍結",
             "start": "2026-09-25T00:00:00+09:00", "end": "2026-10-05T00:00:00+09:00"},
        ],
    })
    return reg


@pytest.fixture()
def deployment(server_module, registry):
    """One provisioned deployment, registered for ``CALLER``."""
    from src.services import stores

    db = stores.InMemoryDatabase()
    kb = stores.InMemoryKnowledgeBase()
    scope = stores.ResourceScope(
        tenant_ref="deployment-a",
        permissions=ALL_PERMISSIONS,
        db=db, kb=kb,
        payloads=server_module._PAYLOADS,             # noqa: SLF001 - test setup
        registry=registry,
    )
    server_module._RESOLVER.register(CALLER, scope)   # noqa: SLF001 - test setup
    return scope


def make_client(server_module, *, caller: str = CALLER,
                trust: str = "VERIFIED_EXTERNAL"):
    """A TestClient with a middleware that stamps a verified identity.

    This stands in for AgentGateway. Without it every request is ANONYMOUS and
    the S-1 gate refuses each node -- which is the correct production default,
    not something to work around in the adapter.
    """
    from fastapi.testclient import TestClient

    class _Middleware:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http":
                scope.setdefault("state", {})
                if trust is not None:
                    scope["state"]["trust_level"] = trust
                if caller is not None:
                    scope["state"]["caller_id"] = caller
            await self.app(scope, receive, send)

    return TestClient(_Middleware(server_module.app))


def invoke_payload(**overrides: Any) -> dict[str, Any]:
    """A well-formed /invoke body; override single fields per test."""
    body: dict[str, Any] = {
        "request_ref": "REQAAA",
        "criteria_version_ref": "CRIT-STD",
        "criteria_version": "v3",
        "criteria_content_hash": "hashcrit",
        "freeze_calendar_ref": "CAL-FY26",
        "freeze_calendar_version": "v1",
        "freeze_calendar_content_hash": "hashcal",
        "planned_window": {"start": "2026-09-28T22:00:00+09:00",
                           "end": "2026-09-29T02:00:00+09:00"},
        "change_description": "本番DBのインデックス再構築を実施する",
        "exception_rationale": "性能劣化が顧客業務に影響しているため",
        "rollback_plan": "スナップショットから4時間で復旧する",
        "risk_assessment": "参照系は縮退運転で継続可能",
        "evidence_declarations": [{
            "doc_type": "rollback_plan", "doc_ref": "DOC-RB",
            "spans": [
                {"span_ref": "p1-s1", "span_text": "復旧所要時間は4時間と記載"},
                {"span_ref": "p1-s2", "span_text": "本番DBは3ノード構成"},
            ],
        }],
    }
    body.update(overrides)
    return body


def perspective_record(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "record_type": "review_perspective",
        "history_ref": "HISTAA",
        "criteria_version_ref": "CRIT-STD",
        "criteria_content_hash": "hashcrit",
        "condition_id": "CONDA",
        "request_summary": "本番DBのスキーマ変更を凍結期間中に実施する申請",
        "decision_outcome": "rejected",
        "decision_rationale": "切戻し手順は記載されていたが、検証済みである記載が無かった",
        "approved_by": "change-manager",
        "approved_at": "2026-06-01T10:00:00+09:00",
        "perspective_code": "ROLLBACK-UNVERIFIED",
    }
    row.update(overrides)
    return row
