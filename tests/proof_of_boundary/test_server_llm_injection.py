"""PB — the entry adapter's wiring, exercised through a real startup.

``run-tests`` only runs ``pytest tests/``; it never boots the app. Production
start-up blockers (a missing config load, a secret made mandatory, a service
that is never wired) therefore pass every gate and fail on deployment. Entering
the ``TestClient`` context manager runs the startup path, which is the point of
the first test here.

The LLM is checked at the seam, not by naming a provider: this template must
not contain a model id or a concrete client class anywhere under ``src/``.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


class TestStartupIsActuallyExercised:
    def test_app_starts_and_health_responds(self):
        import src.api.server as server

        # The context manager form runs startup/shutdown; a bare TestClient
        # would not, and the blockers this test exists for would slip past.
        with TestClient(server.app) as client:
            assert client.get("/health").json() == {"status": "ok"}

    def test_empty_auth_runbook_blocks_real_startup(self, monkeypatch):
        import src.api.server as server

        monkeypatch.setitem(server._config, "auth_runbook_url", "")  # noqa: SLF001
        with pytest.raises(RuntimeError, match="auth_runbook_url must be non-empty"):
            with TestClient(server.app):
                pass

    def test_nonempty_auth_runbook_allows_real_startup(self, monkeypatch):
        import src.api.server as server

        monkeypatch.setitem(
            server._config, "auth_runbook_url", "https://runbook.example/auth")  # noqa: SLF001
        with TestClient(server.app) as client:
            assert client.get("/health").status_code == 200

    def test_config_values_reach_the_services(self):
        """A config key nothing reads is decoration.

        `payload_ttl_s` has to arrive at the payload store, or the documented
        retention is not the retention in force.
        """
        import src.api.server as server

        configured = int(server._config.get("payload_ttl_s", 0))    # noqa: SLF001
        assert configured > 0
        assert server._PAYLOADS._ttl_seconds == configured          # noqa: SLF001
        assert "retention_days" not in server._config               # noqa: SLF001
        assert "audit_retention_days" not in server._config         # noqa: SLF001

    def test_the_resource_seam_is_wired(self):
        import src.api.server as server

        for key in ("resource_resolver", "payload_store", "envelope_store"):
            assert server._config.get(key) is not None, key         # noqa: SLF001


class TestLlmInjectionSeam:
    def test_agent_boots_without_a_key(self):
        """No key must degrade, not crash.

        `deploy-stg` has no provider key, and `requires.secrets` is empty
        precisely so a key-less boot succeeds. Making the key mandatory here
        would contradict the documented `llm_unavailable` degradation.
        """
        import src.api.server as server

        assert server.agent is not None
        # In this environment there is no key, so the seam holds None.
        assert server._config["llm"] is None                        # noqa: SLF001

    def test_the_llm_reaches_the_drafting_node_through_config(self):
        """Injection is by config, not by construction inside the node."""
        from src.nodes.stage3_draft import SufficiencyDraftNode

        sentinel = object()
        node = SufficiencyDraftNode(resolver=None, config={"llm": sentinel})
        assert node._config["llm"] is sentinel                      # noqa: SLF001

    def test_no_model_id_or_concrete_client_in_the_domain_code(self):
        """Model choice is deployment configuration, not template content.

        The one permitted mention is the entry adapter's own construction of a
        client from a resolved secret; everything under ``src/nodes`` and
        ``src/graph`` must go through ``config["llm"]``.
        """
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[2] / "src"
        offenders = []
        for path in list((root / "nodes").rglob("*.py")) + list((root / "graph").rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for marker in ("claude-", "gpt-4", "gpt-5", "AnthropicClient",
                           "OpenAIClient", "BedrockClient"):
                if marker in text:
                    offenders.append(f"{path.name}: {marker}")
        assert not offenders, offenders


class TestEntryAuthContract:
    def test_write_operations_require_a_middleware_identity(self):
        """A standalone entry token must not be able to write business state.

        The part enforces this; the test fixes the template's dependence on it,
        so replacing the adapter cannot quietly open the write endpoints.
        """
        from src.entry_adapter.auth import Operation

        import src.entry_adapter.auth as auth

        assert Operation.WRITE_INGEST in auth._MIDDLEWARE_ONLY_OPERATIONS   # noqa: SLF001
        assert Operation.WRITE_FEEDBACK in auth._MIDDLEWARE_ONLY_OPERATIONS  # noqa: SLF001
        assert Operation.RESUME in auth._MIDDLEWARE_ONLY_OPERATIONS         # noqa: SLF001

    def test_this_template_declares_no_partition_keys(self):
        """One deployment holds one company's data; isolation is the connection.

        A non-empty value here would mean the template had silently adopted the
        part's example vocabulary instead of adapting it.
        """
        import src.entry_adapter.auth as auth

        assert auth.SCOPE_KEYS == ()

    def test_invoke_is_not_a_read_only_operation(self):
        """`/invoke` persists a request record and a claim snapshot.

        Declaring it READ_INVOKE would let a standalone token write business
        state, so the adapter must declare a write operation for it.
        """
        source = importlib.import_module("src.api.server")
        path = source.__file__
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        invoke_block = text.split('@app.post("/invoke")')[1].split("@app.post")[0]
        assert "Operation.WRITE_INGEST" in invoke_block
        assert "Operation.READ_INVOKE" not in invoke_block
