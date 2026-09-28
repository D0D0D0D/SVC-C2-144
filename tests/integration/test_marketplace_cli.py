from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

import pytest
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from langgraph.checkpoint.memory import MemorySaver
from shared.secrets.inmemory_provider import InMemoryProvider


ROOT = Path(__file__).resolve().parents[2]
CANARY = "CANARY-MARKETPLACE-HISTORY-MUST-NOT-ENTER-STATE"
AZURE_ENV = {
    "AZURE_OPENAI_ENDPOINT": "https://sample-resource.services.ai.azure.com",
    "AZURE_OPENAI_API_KEY": "sample-test-key",
    "AZURE_OPENAI_DEPLOYMENT": "sample-test-deployment",
}


class FakeLLM:
    def __init__(self) -> None:
        self.calls = 0
        self.messages: list[list[dict[str, Any]]] = []

    def complete(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        self.calls += 1
        self.messages.append(messages)
        content = {
            "supported_conditions": [],
            "evidence_gaps": [],
            "contradictions": [],
            "approval_scope_discrepancies": [],
            "verification_questions": [],
            "precedent_checks": [
                {
                    "history_ref": "SAMPLE-HIST-AA",
                    "status": "addressed",
                    "source_ids": ["SRCAA"],
                }
            ],
        }
        return {"content": json.dumps(content, ensure_ascii=False)}


def _cli():
    return importlib.import_module("cli")


def _runtime_config(cli, *, llm: Any) -> dict[str, Any]:
    config = cli.load_agent_config(ROOT)
    config["llm"] = llm
    return config


def _set_valid_azure(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in AZURE_ENV.items():
        monkeypatch.setenv(key, value)


def _invoke_body(*, mode: str = "invoke") -> str:
    return json.dumps(
        {
            "mode": mode,
            "request_ref": "SAMPLE-REQUEST-AA",
            "planned_window": {
                "start": "2026-09-28T22:00:00+09:00",
                "end": "2026-09-29T02:00:00+09:00",
            },
            "change_description": "本番DBのインデックス再構築を実施する",
            "exception_rationale": "性能劣化が業務に影響しているため",
            "rollback_plan": "ロールバック手順書に従い復旧する",
            "risk_assessment": "参照系は縮退運転で継続する",
            "evidence_declarations": [
                {
                    "doc_type": "rollback_plan",
                    "doc_ref": "SAMPLE-DOC-RB",
                    "spans": [
                        {
                            "span_ref": "SAMPLE-SPAN-AA",
                            "span_text": "ロールバック手順書に復旧所要時間は4時間と記載されている",
                        }
                    ],
                }
            ],
        },
        ensure_ascii=True,
    )


def _ctx() -> InvocationContext:
    return InvocationContext(
        caller_id="marketplace-user-aa",
        caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
    )


def test_runner_order_invoke_uses_llm_samples_and_bypasses_hitl(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _cli()
    from src.services import audit

    fake = FakeLLM()
    agent = cli.MarketplaceGraph(config=_runtime_config(cli, llm=fake))
    _set_valid_azure(monkeypatch)
    events: list[str] = []
    original_emit = audit.emit

    def capture_emit(event_type, payload, state):
        events.append(event_type)
        original_emit(event_type, payload, state)

    monkeypatch.setattr(audit, "emit", capture_emit)

    agent.provision_secrets(InMemoryProvider({}))
    agent.compile(checkpointer=MemorySaver())
    result = agent.invoke(
        _invoke_body(),
        ctx=_ctx(),
        input_context={"conversation_history": [{"role": "user", "content": CANARY}]},
    )

    rendered = json.dumps(result, ensure_ascii=False, default=str)
    assert fake.calls >= 1
    assert "SAMPLE-HIST-AA" in json.dumps(fake.messages, ensure_ascii=False)
    assert result["status"] == "success"
    assert result["output"]
    assert result["output"]["meta"]["degradation_code"] is None
    assert "llm_unavailable" not in rendered
    assert "kb_empty" not in rendered
    assert "SAMPLE-2.1 ロールバック手順の検証記録" in rendered
    assert "SAMPLE-HIST-AA" in rendered
    assert CANARY not in rendered
    assert result["status"] != "awaiting_human"
    assert audit.EVT_HITL_BYPASSED in events
    assert agent.config["payload_store"]._items == {}  # noqa: SLF001


def _validation_input(case: str) -> str:
    if case == "malformed_json":
        return '{"bad":'
    if case == "json_array":
        return "[]"

    body = json.loads(_invoke_body())
    if case == "unknown_criteria_pin":
        body["criteria_version_ref"] = "UNKNOWN-CRITERIA"
    elif case == "bad_hash":
        body["criteria_content_hash"] = "wrong-hash"
    elif case == "window_not_object":
        body["planned_window"] = "tomorrow"
    elif case == "window_missing":
        body.pop("planned_window")
    elif case == "window_bad_iso":
        body["planned_window"]["start"] = "tomorrow"
    elif case == "declarations_not_list":
        body["evidence_declarations"] = {}
    elif case == "request_ref_integer":
        body["request_ref"] = 144
    elif case != "good":
        raise AssertionError(f"unknown test case: {case}")
    return json.dumps(body, ensure_ascii=False)


@pytest.mark.parametrize(
    ("case", "expected_status", "expected_code"),
    [
        ("malformed_json", "error", "invalid_request"),
        ("json_array", "error", "invalid_request"),
        ("unknown_criteria_pin", "error", "criteria_version_unknown"),
        ("bad_hash", "error", "criteria_content_hash_mismatch"),
        ("window_not_object", "error", "invalid_request"),
        ("window_missing", "error", "invalid_request"),
        ("window_bad_iso", "error", "invalid_request"),
        ("declarations_not_list", "error", "invalid_request"),
        ("request_ref_integer", "error", "invalid_request"),
        ("good", "success", None),
    ],
)
def test_marketplace_input_outcomes_are_structured(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    expected_status: str,
    expected_code: str | None,
) -> None:
    cli = _cli()
    fake = FakeLLM()
    agent = cli.MarketplaceGraph(config=_runtime_config(cli, llm=fake))
    _set_valid_azure(monkeypatch)
    agent.provision_secrets(InMemoryProvider({}))
    agent.compile(checkpointer=MemorySaver())

    result = agent.invoke(_validation_input(case), ctx=_ctx())

    assert result["status"] == expected_status
    assert result["output"] is not None
    assert (result["status"] == "success") is (case == "good")
    if expected_code is not None:
        assert result["error_code"] == expected_code
        assert result["output"]["error_code"] == expected_code
        assert "Expecting value" not in json.dumps(result, ensure_ascii=False)
        assert fake.calls == 0
    else:
        assert fake.calls == 1
    if case == "window_missing":
        example = result["output"]["example"]
        assert example["planned_window"] == {
            "start": "2026-09-28T22:00:00+09:00",
            "end": "2026-09-29T02:00:00+09:00",
        }


def test_free_text_is_rejected_with_pasteable_json_example(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _cli()
    fake = FakeLLM()
    agent = cli.MarketplaceGraph(config=_runtime_config(cli, llm=fake))
    _set_valid_azure(monkeypatch)
    agent.provision_secrets(InMemoryProvider({}))
    agent.compile(checkpointer=MemorySaver())

    result = agent.invoke("本番DBの変更を実施したい", ctx=_ctx())

    assert result["status"] == "error"
    assert result["error_code"] == "invalid_request"
    assert result["output"]["example"]["planned_window"]["start"]
    assert fake.calls == 0


def test_one_compiled_agent_handles_three_fresh_contexts(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _cli()
    fake = FakeLLM()
    agent = cli.MarketplaceGraph(config=_runtime_config(cli, llm=fake))
    _set_valid_azure(monkeypatch)
    agent.provision_secrets(InMemoryProvider({}))
    agent.compile(checkpointer=MemorySaver())

    for expected_calls in range(1, 4):
        result = agent.invoke(_invoke_body(), ctx=_ctx())
        assert result["status"] == "success"
        assert result["output"]
        assert fake.calls == expected_calls


@pytest.mark.parametrize("requested_mode", ["ingest", "feedback"])
def test_marketplace_write_modes_are_forced_to_invoke(monkeypatch: pytest.MonkeyPatch, requested_mode: str) -> None:
    cli = _cli()
    fake = FakeLLM()
    agent = cli.MarketplaceGraph(config=_runtime_config(cli, llm=fake))
    _set_valid_azure(monkeypatch)
    agent.provision_secrets(InMemoryProvider({}))
    agent.compile()
    scope = agent.config["resource_resolver"].scope
    before = (
        len(scope.db.case_evidence),
        len(scope.db.review_perspectives),
        len(scope.kb.documents),
    )

    result = agent.invoke(_invoke_body(mode=requested_mode), ctx=_ctx())

    after = (
        len(scope.db.case_evidence),
        len(scope.db.review_perspectives),
        len(scope.kb.documents),
    )
    assert result["status"] == "success"
    assert result["output"]["mode"] == "invoke"
    assert after == before


def test_call_site_discards_runner_input_context(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _cli()
    from src.services import envelope

    agent = cli.MarketplaceGraph(config=_runtime_config(cli, llm=FakeLLM()))
    observed: dict[str, Any] = {}

    def recorder(_self, user_input, **kwargs):
        observed["user_input"] = user_input
        observed.update(kwargs)
        projected = envelope.take(
            _self.config["envelope_store"],
            user_input,
            session_id=kwargs["session_id"],
        )
        observed["mode"] = projected["mode"]
        observed["body"] = _self.config["payload_store"].resolve(
            projected["payload_ref"],
            scope={"request_ref": projected["request_ref"]},
            session_id=kwargs["session_id"],
        )
        return {"status": "success", "output": {"ok": True}}

    monkeypatch.setattr(cli.ChangeFreezeExceptionReviewGraph, "invoke", recorder)
    body = json.loads(_invoke_body(mode="feedback"))
    body["change_description"] = "連絡先080-1234-5678 変更作業"
    result = agent.invoke(
        json.dumps(body, ensure_ascii=True),
        ctx=_ctx(),
        input_context={"conversation_history": [{"content": CANARY}]},
    )

    assert result["status"] == "success"
    assert observed["input_context"] == {}
    assert observed["ctx"].hitl_allowed is False
    assert observed["ctx"].session_id == observed["session_id"]
    assert len(observed["user_input"]) == 32
    assert observed["user_input"].isalpha() and observed["user_input"].islower()
    assert observed["mode"] == "invoke"
    assert "\\u" not in json.dumps(observed["body"], ensure_ascii=False)
    assert "080-1234-5678" not in observed["body"]["change_description"]
    assert "[MASKED]" in observed["body"]["change_description"]
    assert CANARY not in json.dumps(observed, default=str)


def test_default_llm_is_injected_and_holds_no_secrets() -> None:
    cli = _cli()
    agent = cli.MarketplaceGraph(config=cli.load_agent_config(ROOT))
    assert isinstance(agent.config["llm"], cli.MarketplaceAzureLLM)
    assert vars(agent.config["llm"]) == {}


def test_existing_llm_client_is_not_overwritten() -> None:
    cli = _cli()
    fake = FakeLLM()
    agent = cli.MarketplaceGraph(config=_runtime_config(cli, llm=fake))
    assert agent.config["llm"] is fake


def test_llm_mapping_is_replaced_and_options_are_preserved() -> None:
    cli = _cli()
    config = cli.load_agent_config(ROOT)
    config["llm"] = {"temperature": 0.2, "max_tokens": 321}
    agent = cli.MarketplaceGraph(config=config)
    assert isinstance(agent.config["llm"], cli.MarketplaceAzureLLM)
    assert vars(agent.config["llm"]) == {"_temperature": 0.2, "_max_tokens": 321}


class _LangchainOpenAIBlocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        del path, target
        if fullname == "langchain_openai" or fullname.startswith("langchain_openai."):
            raise ImportError("blocked by marketplace compatibility test")
        return None


def test_cli_import_does_not_require_langchain_openai() -> None:
    blocker = _LangchainOpenAIBlocker()
    spec = importlib.util.spec_from_file_location("svc_c2_144_cli_blocked", ROOT / "cli.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.meta_path.insert(0, blocker)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.meta_path.remove(blocker)
    assert module.MarketplaceGraph is not None
    assert "langchain_openai" not in sys.modules


def test_main_passes_fully_wired_config(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _cli()
    recorded: dict[str, Any] = {}

    def recorder(graph_cls, **kwargs):
        recorded["graph_cls"] = graph_cls
        recorded.update(kwargs)

    monkeypatch.setattr(cli, "run_agent_marketplace", recorder)
    cli.main()

    assert recorded["graph_cls"] is cli.MarketplaceGraph
    assert recorded["agent_name"] == "SVC-C2-144"
    assert recorded["namespace"] == "svc"
    for key in ("llm", "resource_resolver", "payload_store", "envelope_store"):
        assert recorded["config"].get(key) is not None


def test_sample_provisioning_is_idempotent() -> None:
    cli = _cli()
    registry = cli.stores.ControlDocumentRegistry()
    db = cli.stores.InMemoryDatabase()
    kb = cli.stores.InMemoryKnowledgeBase()

    cli._provision_samples(registry, db, kb)  # noqa: SLF001
    first = (len(registry.documents), len(db.review_perspectives), len(kb.documents))
    cli._provision_samples(registry, db, kb)  # noqa: SLF001

    assert first == (2, 1, 1)
    assert (len(registry.documents), len(db.review_perspectives), len(kb.documents)) == first


@pytest.mark.parametrize("key", sorted(AZURE_ENV))
@pytest.mark.parametrize("bad_value", [None, "", "   ", " value ", "value\n"])
def test_startup_gate_rejects_missing_or_unclean_values(
    monkeypatch: pytest.MonkeyPatch, key: str, bad_value: str | None
) -> None:
    cli = _cli()
    _set_valid_azure(monkeypatch)
    if bad_value is None:
        monkeypatch.delenv(key)
    else:
        monkeypatch.setenv(key, bad_value)
    agent = cli.MarketplaceGraph(config=_runtime_config(cli, llm=FakeLLM()))

    with pytest.raises(ValueError, match=key):
        agent.provision_secrets(InMemoryProvider({}))


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://sample-resource.cognitiveservices.azure.com/openai/responses?api-version=2025-04-01-preview",
        "https://sample-resource.services.ai.azure.com/openai",
        "https://sample-resource.services.ai.azure.com/v1",
        "https://sample-resource.services.ai.azure.com?api-version=2024-10-21",
        "sample-resource.services.ai.azure.com",
    ],
    ids=["api-path", "openai-path", "base-path", "query-only", "missing-scheme"],
)
def test_unusable_azure_endpoint_shape_fails_during_startup(monkeypatch: pytest.MonkeyPatch, endpoint: str) -> None:
    """AzureOpenAIClient appends its own path, so anything but a bare endpoint breaks.

    The resulting error surfaces inside complete(); a node that swallows it
    degrades silently while the invocation still reports status: success, so the
    adapter has to reject the shape at startup instead.
    """

    cli = _cli()
    _set_valid_azure(monkeypatch)
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", endpoint)
    agent = cli.MarketplaceGraph(config=_runtime_config(cli, llm=FakeLLM()))

    with pytest.raises(ValueError, match="bare"):
        agent.provision_secrets(InMemoryProvider({}))


def test_error_output_is_never_none() -> None:
    cli = _cli()
    agent = cli.MarketplaceGraph(config=_runtime_config(cli, llm=FakeLLM()))
    output = {"status": "error", "output": None, "error_code": "invalid_request"}
    agent._enrich_output(output, {"error_code": "invalid_request"})  # noqa: SLF001
    assert output["output"] == {
        "message": "The change-freeze exception review could not be completed.",
        "error_code": "invalid_request",
    }


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("not-json", "cannot load required sample data"),
        ("[]", "root must be an object"),
        ('{"sample_origin":"SAMPLE-X","rows":[]}', "must be a non-empty list"),
    ],
)
def test_sample_loader_fails_loudly(tmp_path: Path, content: str, message: str) -> None:
    cli = _cli()
    path = tmp_path / "broken.sample.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises((RuntimeError, ValueError), match=message):
        cli._load_sample(path, "rows")  # noqa: SLF001


def test_sample_loader_rejects_missing_file(tmp_path: Path) -> None:
    cli = _cli()
    with pytest.raises(RuntimeError, match="cannot load required sample data"):
        cli._load_sample(tmp_path / "missing.sample.json", "rows")  # noqa: SLF001
