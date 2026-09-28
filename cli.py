"""AGENTIC STAR Marketplace entry adapter for SVC-C2-144."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import replace
from datetime import datetime
import json
import os
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from framework.schemas.invocation_context import InvocationContext
from framework.secrets.base import SecretProvider
from framework.secrets.context import current_secrets
from shared.services.llm.base_llm import BaseLLM

# AgentCore 1.0.1 does not ship the Marketplace bootstrap, EnvProvider, Azure
# client, or load_agent_config.  Keep this module importable on that wheel;
# provider-only integrations still fail loudly when they are actually used.
try:  # pragma: no cover - exercised on the Marketplace 1.0.2 image
    from shared.bootstrap import run_agent_marketplace
except ImportError:  # pragma: no cover - the local 1.0.1 wheel takes this arm

    def run_agent_marketplace(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("AGENTIC STAR Marketplace bootstrap is unavailable in this AgentCore build")


try:  # pragma: no cover - exercised on the Marketplace 1.0.2 image
    from shared.secrets.env_provider import EnvProvider as _SdkEnvProvider
except ImportError:

    class _SdkEnvProvider(SecretProvider):
        """AgentCore 1.0.1-compatible process environment provider."""

        def __init__(self, *, namespace: str = "", agent_name: str = "") -> None:
            self._namespace = namespace
            self._agent_name = agent_name

        def get(self, key: str, default: str | None = None) -> str | None:
            return os.environ.get(key, default)


try:  # pragma: no cover - exercised on AgentCore builds that provide it
    from framework.utils.config_loader import load_agent_config
except ImportError:
    from framework.utils.config_loader import load_config

    def load_agent_config(root: str | Path) -> dict[str, Any]:
        return cast(dict[str, Any], load_config(str(Path(root) / "config" / "config.yaml")))


from src.graph.graph import ChangeFreezeExceptionReviewGraph
from src.payload_store.payload_store import PayloadStore, PayloadStoreError
from src.services import contracts, envelope, freeze_window, projection, stores

_NAMESPACE = "svc"
_AGENT_NAME = "SVC-C2-144"
# server.py uses the graph class name, which is a placeholder-shaped provider
# path.  Marketplace deployments use the stable template identifier instead.
_PROVIDER_AGENT_NAME = "svc_c2_144"

_ROOT = Path(__file__).resolve().parent
_CONTROL_SAMPLE_PATH = _ROOT / "config" / "control_documents.sample.json"
_PERSPECTIVE_SAMPLE_PATH = _ROOT / "config" / "review_perspectives.sample.json"

_AZURE_KEYS = (
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_DEPLOYMENT",
)
_NARRATIVE_FIELDS = (
    "change_description",
    "exception_rationale",
    "rollback_plan",
    "risk_assessment",
)
_SAMPLE_CRITERIA = {
    "ref": "CRIT-STD",
    "version": "v3",
    "content_hash": "hashcrit",
}
_SAMPLE_CALENDAR = {
    "ref": "CAL-FY26",
    "version": "v1",
    "content_hash": "hashcal",
}
_MARKETPLACE_INPUT_EXAMPLE = {
    "request_ref": "SAMPLE-REQUEST-AA",
    "planned_window": {
        "start": "2026-09-28T22:00:00+09:00",
        "end": "2026-09-29T02:00:00+09:00",
    },
    "change_description": "変更内容を記載",
    "exception_rationale": "凍結期間中に実施する理由を記載",
    "rollback_plan": "切戻し手順を記載",
    "risk_assessment": "リスク評価を記載",
    "evidence_declarations": [],
}


class _AllowlistedEnvironmentProvider(_SdkEnvProvider):  # type: ignore[misc]
    """Expose only this adapter's three declared Azure settings."""

    def __init__(self) -> None:
        try:
            super().__init__(namespace=_NAMESPACE, agent_name=_PROVIDER_AGENT_NAME)
        except TypeError:  # pragma: no cover - compatibility with older ctor
            super().__init__()

    def get(self, key: str, default: str | None = None) -> str | None:
        if key not in _AZURE_KEYS:
            return default
        value = super().get(key)
        return value if value is not None else default


def _validated_azure_values(provider: SecretProvider) -> dict[str, str]:
    """Read and validate clean Azure values without repairing them."""

    values: dict[str, str] = {}
    for key in _AZURE_KEYS:
        value = provider.get(key)
        if not isinstance(value, str) or not value or value != value.strip() or "\n" in value or "\r" in value:
            raise ValueError(f"{key} must be a non-empty clean value")
        values[key] = value

    _require_azure_endpoint(provider)
    return values


class MarketplaceAzureLLM(BaseLLM):  # type: ignore[misc]
    """Resolve Azure credentials at call time; never retain them on ``self``."""

    def __init__(
        self,
        *,
        temperature: Any | None = None,
        max_tokens: Any | None = None,
        tools: tuple[Any, ...] = (),
    ) -> None:
        if temperature is not None:
            self._temperature = temperature
        if max_tokens is not None:
            self._max_tokens = max_tokens
        if tools:
            self._tools = tools

    def _client(self) -> BaseLLM:
        # Validate first.  A bad deployment setting must not be disguised as an
        # optional-dependency ImportError on the 1.0.1 compatibility image.
        values = _validated_azure_values(current_secrets())
        try:
            from shared.services.llm.azure_openai_client import AzureOpenAIClient
        except ImportError as exc:  # pragma: no cover - 1.0.1 has no Azure client
            raise RuntimeError("AzureOpenAIClient is unavailable in this AgentCore build") from exc

        config: dict[str, Any] = {
            "api_key": values["AZURE_OPENAI_API_KEY"],
            "azure_endpoint": values["AZURE_OPENAI_ENDPOINT"],
            "azure_deployment": values["AZURE_OPENAI_DEPLOYMENT"],
        }
        for attribute, key in (("_temperature", "temperature"), ("_max_tokens", "max_tokens")):
            if hasattr(self, attribute):
                config[key] = getattr(self, attribute)
        client = AzureOpenAIClient(config)
        tools = getattr(self, "_tools", ())
        return cast(BaseLLM, client.bind_tools(list(tools)) if tools else client)

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._client().complete(messages))

    def stream(self, messages: list[Any]) -> Iterator[str]:
        yield from self._client().stream(messages)

    def bind_tools(self, tools: list[Any]) -> "MarketplaceAzureLLM":
        return type(self)(
            temperature=getattr(self, "_temperature", None),
            max_tokens=getattr(self, "_max_tokens", None),
            tools=tuple(tools),
        )


def _is_llm_client(value: Any) -> bool:
    return callable(getattr(value, "complete", None))


class MarketplaceResourceResolver(stores.AuthorizedResourceResolver):
    """Map every authenticated Marketplace caller to this deployment's scope."""

    def __init__(self, scope: stores.ResourceScope) -> None:
        super().__init__()
        self.scope = scope

    def for_caller(self, caller_id: str | None) -> stores.ResourceScope:
        if not isinstance(caller_id, str) or not caller_id.strip():
            raise stores.AuthorizationError("customer_resolution_failed")
        return self.scope


def _load_sample(path: Path, collection_key: str) -> list[dict[str, Any]]:
    """Load one non-empty, explicitly labelled sample collection."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot load required sample data: {path.name}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name}: root must be an object")
    origin = payload.get("sample_origin")
    if not isinstance(origin, str) or not origin.startswith("SAMPLE-"):
        raise ValueError(f"{path.name}: sample_origin must start with SAMPLE-")
    records = payload.get(collection_key)
    if not isinstance(records, list) or not records:
        raise ValueError(f"{path.name}: {collection_key} must be a non-empty list")
    if any(not isinstance(record, dict) for record in records):
        raise ValueError(f"{path.name}: every {collection_key} item must be an object")
    return cast(list[dict[str, Any]], records)


def _require_keys(record: Mapping[str, Any], required: frozenset[str], *, label: str) -> None:
    missing = required - set(record)
    if missing:
        raise ValueError(f"{label}: missing required keys {sorted(missing)}")


def _provision_samples(
    registry: stores.ControlDocumentRegistry,
    db: stores.InMemoryDatabase,
    kb: stores.InMemoryKnowledgeBase,
) -> None:
    controls = _load_sample(_CONTROL_SAMPLE_PATH, "documents")
    for index, row in enumerate(controls):
        _require_keys(row, frozenset({"kind", "ref", "version", "document"}), label=f"control[{index}]")
        kind = row["kind"]
        document = row["document"]
        if kind not in {stores.KIND_CRITERIA, stores.KIND_CALENDAR} or not isinstance(document, dict):
            raise ValueError(f"control[{index}]: invalid kind or document")
        _require_keys(
            document,
            frozenset({"content_hash", "approved", "effective_from"}),
            label=f"control[{index}].document",
        )
        body_key = "conditions" if kind == stores.KIND_CRITERIA else "windows"
        body = document.get(body_key)
        if not isinstance(body, list) or not body:
            raise ValueError(f"control[{index}].document.{body_key} must be non-empty")
        item_keys = (
            frozenset({"condition_id", "condition_ref"})
            if kind == stores.KIND_CRITERIA
            else frozenset({"window_id", "window_ref", "start", "end"})
        )
        for item_index, item in enumerate(body):
            if not isinstance(item, dict):
                raise ValueError(f"control[{index}].document.{body_key}[{item_index}] must be an object")
            _require_keys(
                item,
                item_keys,
                label=f"control[{index}].document.{body_key}[{item_index}]",
            )
        registry.register(str(kind), str(row["ref"]), str(row["version"]), dict(document))

    perspectives = _load_sample(_PERSPECTIVE_SAMPLE_PATH, "perspectives")
    db_required = frozenset({"history_ref", "decision_outcome", "condition_id"})
    kb_required = frozenset(
        {
            "history_ref",
            "condition_id",
            "criteria_version_ref",
            "criteria_content_hash",
            "request_summary_safe",
            "decision_rationale_safe",
            "perspective_code",
        }
    )
    for index, row in enumerate(perspectives):
        _require_keys(row, frozenset({"db_record", "kb_document"}), label=f"perspective[{index}]")
        db_record, kb_document = row["db_record"], row["kb_document"]
        if not isinstance(db_record, dict) or not isinstance(kb_document, dict):
            raise ValueError(f"perspective[{index}]: db_record and kb_document must be objects")
        _require_keys(db_record, db_required, label=f"perspective[{index}].db_record")
        _require_keys(kb_document, kb_required, label=f"perspective[{index}].kb_document")
        if db_record["history_ref"] != kb_document["history_ref"]:
            raise ValueError(f"perspective[{index}]: history_ref mismatch")
        db.add_review_perspective(dict(db_record))
        kb.upsert(dict(kb_document))


def _marketplace_config(config: dict[str, Any] | None) -> dict[str, Any]:
    """Merge the live LLM seam and one-deployment sample backend."""

    merged = dict(config or {})
    existing = merged.get("llm")
    if not _is_llm_client(existing):
        options = existing if isinstance(existing, dict) else {}
        merged["llm"] = MarketplaceAzureLLM(
            **{key: options[key] for key in ("temperature", "max_tokens") if key in options}
        )

    if not isinstance(merged.get("resource_resolver"), MarketplaceResourceResolver):
        payloads = PayloadStore(
            scope_keys=("request_ref",),
            ttl_seconds=int(merged.get("payload_ttl_s", 3600)),
        )
        envelopes = PayloadStore(
            scope_keys=envelope.ENVELOPE_SCOPE_KEYS,
            ttl_seconds=int(merged.get("timeout_s", 60)) * 10,
        )
        registry = stores.ControlDocumentRegistry()
        db = stores.InMemoryDatabase()
        kb = stores.InMemoryKnowledgeBase()
        _provision_samples(registry, db, kb)
        scope = stores.ResourceScope(
            tenant_ref="SAMPLE-DEPLOYMENT-SVC-C2-144",
            permissions=frozenset(),
            db=db,
            kb=kb,
            payloads=payloads,
            registry=registry,
        )
        merged.update(
            {
                "resource_resolver": MarketplaceResourceResolver(scope),
                "payload_store": payloads,
                "envelope_store": envelopes,
                "control_document_registry": registry,
            }
        )
    return merged


class _MarketplaceInputError(ValueError):
    """A caller-authored Marketplace request failed adapter validation."""


def _clean_string(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\n" in value or "\r" in value:
        raise _MarketplaceInputError(f"{field} must be a non-empty clean string")
    return value


def _parse_marketplace_input(user_input: str, *, caller_id: str) -> dict[str, Any]:
    if not isinstance(user_input, str):
        raise _MarketplaceInputError("Marketplace input must be a string")
    if user_input.lstrip().startswith("{"):
        parsed = json.loads(user_input)
        if not isinstance(parsed, dict):
            raise _MarketplaceInputError("Marketplace JSON input must be an object")
        request = dict(parsed)
    else:
        request = {"change_description": user_input}

    request["mode"] = "invoke"
    if request.get("request_ref") is None:
        request["request_ref"] = stores.alpha_digest(f"{caller_id}:{user_input}", prefix="REQ")
    request["request_ref"] = _clean_string(request["request_ref"], field="request_ref")
    return request


def _pin_value(request: Mapping[str, Any], key: str, default: str) -> str:
    value = request.get(key, default)
    return _clean_string(value, field=key)


def _validate_planned_window(value: Any) -> dict[str, str]:
    """Require a caller-supplied, timezone-aware execution interval."""

    if not isinstance(value, dict):
        raise _MarketplaceInputError("planned_window must be an object")
    start = _clean_string(value.get("start"), field="planned_window.start")
    end = _clean_string(value.get("end"), field="planned_window.end")
    try:
        parsed_start = freeze_window.parse_aware(start, field="planned_window.start")
        parsed_end = freeze_window.parse_aware(end, field="planned_window.end")
    except freeze_window.WindowFormatError as exc:
        raise _MarketplaceInputError("planned_window must contain timezone-aware ISO-8601 values") from exc
    if parsed_end <= parsed_start:
        raise _MarketplaceInputError("planned_window.end must be after planned_window.start")
    return {"start": start, "end": end}


def _input_error(error_code: str, *, ctx: InvocationContext) -> dict[str, Any]:
    """Return a runner-safe rejection without reflecting caller text."""

    return {
        "status": "error",
        "error_code": error_code,
        "output": {
            "message": "The Marketplace request was rejected before review.",
            "error_code": error_code,
            "required_fields": ["planned_window.start", "planned_window.end"],
            "example": dict(_MARKETPLACE_INPUT_EXAMPLE),
        },
        "trace_id": None,
        "correlation_id": ctx.correlation_id,
        "node_history": [],
    }


def _require_azure_endpoint(provider: SecretProvider) -> str:
    """Reject ``AZURE_OPENAI_ENDPOINT`` values that are not a bare Azure resource endpoint.

    ``AzureOpenAIClient`` appends its own ``/openai/v1/`` path, so anything
    beyond a scheme and host silently produces a broken request URL, and the
    failure surfaces inside ``complete()``. A node that swallows that exception
    degrades quietly while the invocation still reports ``status: success``,
    which is why this has to be a startup check. The value is reported here,
    never repaired.
    """

    endpoint = provider.require("AZURE_OPENAI_ENDPOINT")
    parts = urlsplit(endpoint)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port == -1
        or parts.path.strip("/")
        or parts.query
        or parts.fragment
        or any(character.isspace() for character in endpoint)
    ):
        raise ValueError(
            "Marketplace environment variable 'AZURE_OPENAI_ENDPOINT' must be a bare Azure "
            "resource endpoint -- scheme and host only, with no path (such as "
            "'/openai'), query or fragment"
        )
    return endpoint


class MarketplaceGraph(ChangeFreezeExceptionReviewGraph):
    """Marketplace boundary, envelope construction, and dependency wiring."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        merged = _marketplace_config(config)
        # register_nodes() consumes these injections during compile(), so all
        # wiring is complete before the base constructor receives the config.
        super().__init__(config=merged)

    def provision_secrets(self, provider: SecretProvider) -> None:
        # The 1.0.2 runner may supply a chained provider with dotenv fallback.
        # This single-agent image intentionally accepts only its three process
        # environment settings.  Validate before any Azure client import.
        del provider
        environment_provider = _AllowlistedEnvironmentProvider()
        _validated_azure_values(environment_provider)
        _require_azure_endpoint(environment_provider)
        super().provision_secrets(environment_provider)

    def invoke(
        self,
        user_input: str,
        session_id: str = "",
        ctx: InvocationContext | None = None,
        input_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del session_id
        del input_context
        if ctx is None or not isinstance(ctx.caller_id, str) or not ctx.caller_id.strip():
            raise ValueError("Marketplace invocation requires a non-empty caller_id")

        resolver = cast(MarketplaceResourceResolver, self.config["resource_resolver"])
        scope = resolver.for_caller(ctx.caller_id)
        evaluated_at = datetime.now().astimezone().isoformat()
        try:
            request = _parse_marketplace_input(user_input, caller_id=ctx.caller_id)
            request_ref = request["request_ref"]
            planned_window = _validate_planned_window(request.get("planned_window"))
            declarations = request.get("evidence_declarations", [])
            if not isinstance(declarations, list):
                raise _MarketplaceInputError("evidence_declarations must be a list")

            criteria_ref = _pin_value(request, "criteria_version_ref", _SAMPLE_CRITERIA["ref"])
            criteria_version = _pin_value(request, "criteria_version", _SAMPLE_CRITERIA["version"])
            criteria_hash = _pin_value(request, "criteria_content_hash", _SAMPLE_CRITERIA["content_hash"])
            calendar_ref = _pin_value(request, "freeze_calendar_ref", _SAMPLE_CALENDAR["ref"])
            calendar_version = _pin_value(request, "freeze_calendar_version", _SAMPLE_CALENDAR["version"])
            calendar_hash = _pin_value(request, "freeze_calendar_content_hash", _SAMPLE_CALENDAR["content_hash"])
            criteria_seal = scope.registry.resolve_pin(
                stores.KIND_CRITERIA,
                ref=criteria_ref,
                version=criteria_version,
                content_hash=criteria_hash,
                evaluated_at=evaluated_at,
            )
            calendar_seal = scope.registry.resolve_pin(
                stores.KIND_CALENDAR,
                ref=calendar_ref,
                version=calendar_version,
                content_hash=calendar_hash,
                evaluated_at=evaluated_at,
            )
        except json.JSONDecodeError:
            return _input_error(contracts.ERR_INVALID_REQUEST, ctx=ctx)
        except stores.PinError as exc:
            return _input_error(exc.error_code, ctx=ctx)
        except _MarketplaceInputError:
            return _input_error(contracts.ERR_INVALID_REQUEST, ctx=ctx)

        # json.loads has already decoded ASCII escapes.  Apply the repository's
        # established NFKC/PII projection before the body enters transient
        # storage, then round-trip with ensure_ascii=False to preserve that fact.
        body = {field: projection.normalise_and_mask(str(request.get(field) or "")) for field in _NARRATIVE_FIELDS}
        body = cast(dict[str, str], json.loads(json.dumps(body, ensure_ascii=False)))
        invocation_session = stores.alpha_digest(f"{ctx.caller_id}:{request_ref}", prefix="SESS")
        payloads = cast(PayloadStore, self.config["payload_store"])
        payload_ref = payloads.put(
            body,
            scope={"request_ref": request_ref},
            session_id=invocation_session,
        )
        try:
            projected = {
                "mode": "invoke",
                "request_ref": request_ref,
                "payload_ref": payload_ref,
                "criteria_version_ref": criteria_ref,
                "criteria_version": criteria_version,
                "criteria_content_hash": criteria_hash,
                "freeze_calendar_ref": calendar_ref,
                "freeze_calendar_version": calendar_version,
                "freeze_calendar_content_hash": calendar_hash,
                "sealed_refs": {
                    "criteria_sealed_ref": criteria_seal,
                    "freeze_calendar_sealed_ref": calendar_seal,
                },
                "planned_window": planned_window,
                "evidence_declarations": declarations,
                "evaluated_at": evaluated_at,
                "fields_present": {field: bool(body[field]) for field in _NARRATIVE_FIELDS},
                "refine_count": 0,
            }
            ref = envelope.put(
                self.config["envelope_store"],
                projected,
                session_id=invocation_session,
            )
            safe_ctx = replace(ctx, hitl_allowed=False, session_id=invocation_session)
            return cast(
                dict[str, Any],
                super().invoke(
                    ref,
                    session_id=invocation_session,
                    ctx=safe_ctx,
                    input_context={},
                ),
            )
        finally:
            try:
                payloads.delete(
                    payload_ref,
                    scope={"request_ref": request_ref},
                    session_id=invocation_session,
                )
            except PayloadStoreError:
                pass

    def _enrich_output(self, output: dict[str, Any], result: dict[str, Any]) -> None:
        super()._enrich_output(output, result)
        if output.get("output") is None:
            error_code = output.get("error_code") or result.get("error_code") or "request_failed"
            output["output"] = {
                "message": "The change-freeze exception review could not be completed.",
                "error_code": error_code,
            }


def main() -> None:
    config = _marketplace_config(load_agent_config(_ROOT))
    run_agent_marketplace(
        MarketplaceGraph,
        agent_name=_AGENT_NAME,
        namespace=_NAMESPACE,
        config=config,
    )


if __name__ == "__main__":
    main()
