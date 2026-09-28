"""Standalone HTTP entry point.

Adapter only -- no business logic. What lives here is the work that has to
happen *before* the graph runs, or that the graph structurally cannot do:

* **caller authentication and operation permission.** The trust level says
  "authenticated"; it does not say "may delete". Permissions come from the
  resolver and are required per endpoint.
* **body isolation.** The four narrative fields move into the payload store and
  are replaced by a handle **before** ``agent.invoke()`` is called.
  ``BaseGraph.invoke()`` puts its raw input into ``user_input``, and with a
  checkpointer attached that lands in the first checkpoint -- removing the body
  afterwards is too late.
* **the version pin checks.** Resolve the pinned criteria/calendar, verify
  hash, approval and effective window, mint the request-lifetime seal.
* **the reference instant.** ``evaluated_at`` is fixed once, timezone-aware, so
  the same request cannot answer differently on two hosts or twice within one
  request. It cannot travel via config: the graph is built once per process.

For platform deployments AgentGateway calls ``agent.invoke()`` directly and the
middleware supplies the verified identity. This module is the STG/development
path; do not expose it publicly without the gateway in front, because the
resume ownership check below is the only thing standing in for it here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
import logging
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from framework.schemas.invocation_context import InvocationContext
from framework.secrets.context import bound_secrets
from framework.utils.config_loader import load_config
from langgraph.checkpoint.memory import MemorySaver
from pydantic import BaseModel, ConfigDict, Field, field_validator
from shared.secrets import factory as secrets_factory
from starlette.responses import Response

from src.entry_adapter.auth import Operation, authenticate
from src.graph.graph import Graph
from src.nodes.hitl_review import sanitise_feedback
from src.payload_store.payload_store import PayloadStore, PayloadStoreError
from src.services import audit, contracts, envelope, projection, stores

log = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"
_config: dict[str, Any] = load_config(str(_CONFIG_PATH)) if _CONFIG_PATH.exists() else {}

# .get() not .require(): deploy-stg has no key yet, and a missing key must
# degrade to config["llm"] = None rather than crash at import.
_secrets_provider = secrets_factory(namespace="svc", agent_name="ChangeFreezeExceptionReviewAgent")
_anthropic_key = _secrets_provider.get("ANTHROPIC_API_KEY")
_llm = None
if _anthropic_key:
    from shared.services.llm.anthropic_client import AnthropicClient

    _llm = AnthropicClient(config={"api_key": _anthropic_key})
else:
    log.warning("ANTHROPIC_API_KEY not set - booting with no LLM configured.")
_config["llm"] = _llm


def wire_services(config: dict[str, Any]) -> dict[str, Any]:
    """Build the resource seam.

    Unwired: DB and KB are in-memory (the SDK persistence layer is STUB). The
    properties that must survive a real backend are documented in
    ``src/services/stores.py`` and the operation guide.
    """
    config["resource_resolver"] = stores.AuthorizedResourceResolver()
    # `scope_keys=("request_ref",)`, NOT `()`. Two different partitions are in
    # play and they are not the same thing:
    #   * entry-adapter's SCOPE_KEYS = ()  -> no KB/DB partition inside the
    #     deployment (one company per deployment; isolation is the connection).
    #   * payload-store's scope_keys      -> which payloads may be resolved
    #     together. A narrative body belongs to exactly one request, and one
    #     request must never resolve another's body.
    # The part refuses an empty tuple on purpose ("if this domain has no
    # partition, do not use scope-bound storage"), and it is right to: an
    # unpartitioned body store is precisely what we do not want here.
    config["payload_store"] = PayloadStore(
        scope_keys=("request_ref",),
        ttl_seconds=int(config.get("payload_ttl_s", 3600)),
    )
    config["control_document_registry"] = stores.ControlDocumentRegistry()
    # Separate store for the internal envelope. It never mixes with the
    # narrative payloads, and its TTL only has to cover one request's hops.
    config["envelope_store"] = PayloadStore(
        scope_keys=envelope.ENVELOPE_SCOPE_KEYS,
        ttl_seconds=int(config.get("timeout_s", 60)) * 10,
    )
    return config


_config = wire_services(_config)

agent = Graph(config=_config)
_hitl_enabled = bool((agent.config.get("hitl") or {}).get("enabled", False))
_needs_checkpointer = bool(agent.config.get("memory_enabled")) or _hitl_enabled
agent.compile(checkpointer=MemorySaver() if _needs_checkpointer else None)
agent.provision_secrets(_secrets_provider)

_RESOLVER: stores.AuthorizedResourceResolver = _config["resource_resolver"]
_PAYLOADS: PayloadStore = _config["payload_store"]
_ENVELOPES: PayloadStore = _config["envelope_store"]


def _validate_startup_config(config: dict[str, Any]) -> None:
    """Reject a deployment whose credential operations have no runbook."""
    runbook = config.get("auth_runbook_url")
    if not isinstance(runbook, str) or not runbook.strip():
        raise RuntimeError("auth_runbook_url must be non-empty")


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    _validate_startup_config(_config)
    yield


app = FastAPI(title="ChangeFreezeExceptionReviewAgent", lifespan=_lifespan)

# The four narrative fields -- the reason the payload store exists.
_NARRATIVE_FIELDS = ("change_description", "exception_rationale", "rollback_plan", "risk_assessment")

_PERM_INVOKE = "freeze.invoke.write_snapshot"
_PERM_INGEST = "freeze.ingest.write_case_data"
_PERM_FEEDBACK = "freeze.feedback.append_candidate"
_PERM_DELETE_REQUEST = "freeze.request.delete"
_PERM_DELETE_PERSPECTIVE = "freeze.perspective.delete"
_PERM_AUDIT_READ = "freeze.audit.read"


@app.middleware("http")
async def _json_only(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """The four POST entry points accept JSON bodies and no parser fallback."""
    if request.method == "POST" and (
        request.url.path in {"/invoke", "/ingest", "/feedback"} or request.url.path.startswith("/resume/")
    ):
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            return JSONResponse(status_code=415, content={"detail": "unsupported_media_type"})
    return await call_next(request)


@app.exception_handler(RequestValidationError)
async def _request_validation_error(_request: Request, _exc: RequestValidationError) -> JSONResponse:
    """Expose the documented stable 400 without echoing rejected input."""
    return JSONResponse(status_code=400, content={"detail": contracts.ERR_INVALID_REQUEST})


# ---------------------------------------------------------------------------
# request models -- closed schemas. An undeclared key is a 400, never a silent
# drop: dropping it means a constraint the caller believed they sent vanished.
# ---------------------------------------------------------------------------


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlannedWindow(_Closed):
    start: str
    end: str


class EvidenceSpan(_Closed):
    span_ref: str = Field(min_length=1)
    span_text: str = Field(min_length=1)


class EvidenceDeclaration(_Closed):
    doc_type: str
    doc_ref: str = Field(min_length=1)
    spans: list[EvidenceSpan] = Field(min_length=1)

    @field_validator("doc_type")
    @classmethod
    def _known_doc_type(cls, value: str) -> str:
        if value not in contracts.DOC_TYPES:
            raise ValueError("unknown doc_type")
        return value


class InvokeRequest(_Closed):
    request_ref: str
    criteria_version_ref: str
    criteria_version: str
    criteria_content_hash: str
    planned_window: PlannedWindow
    change_description: str = ""
    exception_rationale: str = ""
    rollback_plan: str = ""
    risk_assessment: str = ""
    freeze_calendar_ref: str | None = None
    freeze_calendar_version: str | None = None
    freeze_calendar_content_hash: str | None = None
    evidence_declarations: list[EvidenceDeclaration] = Field(default_factory=list)


class IngestRequest(_Closed):
    records: list[dict[str, Any]] = Field(default_factory=list)


class FeedbackRequest(_Closed):
    request_ref: str
    claim_id: str
    perspective_code: str = ""
    condition_id: str = ""
    criteria_version_ref: str = ""
    source_ids: list[str] = Field(default_factory=list)
    decision_outcome: str
    decision_rationale: str = ""
    action: str
    approved: bool = False


class ResumeRequest(_Closed):
    action: str
    note: str | None = None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """Timezone-aware reference instant, fixed once per request."""
    return datetime.now().astimezone().isoformat()


def _session_id(principal: str, request_ref: str) -> str:
    """Server-decided session handle.

    Deliberately NOT client-supplied. The payload store binds a body to
    ``session_id``, so letting a caller name it would let them name someone
    else's and resolve a body that is not theirs. It is also not
    ``ctx.thread_id``: that is a fresh random UUID per InvocationContext, so
    the adapter cannot know it at ``put`` time and the two would never match.
    """
    return stores.alpha_digest(f"{principal}:{request_ref}", prefix="SESS")


def _scope_for(request: Request, operation: Operation) -> tuple[Any, stores.ResourceScope]:
    """Authenticate, then resolve to a provisioned deployment.

    Two distinct failures: 401 for "who are you", 403 for "you are not wired to
    a deployment". Collapsing them would tell an unauthenticated caller whether
    a deployment exists.
    """
    auth = authenticate(request, operation=operation)
    # AuthContext.principal is a dict ({"caller_id": ...}), not a string.
    caller_id = (auth.principal or {}).get("caller_id", "")
    try:
        scope = _RESOLVER.for_caller(caller_id)
    except stores.AuthorizationError:
        raise HTTPException(status_code=403, detail=contracts.ERR_CUSTOMER_RESOLUTION) from None
    return auth, scope


def _caller_id(auth: Any) -> str:
    return cast(str, (auth.principal or {}).get("caller_id", ""))


def _require(scope: stores.ResourceScope, permission: str) -> None:
    try:
        scope.require_permission(permission)
    except stores.MissingPermission:
        # 403 before any write happens. Returning 403 *after* a partial write
        # is the worst failure mode this check exists to prevent.
        raise HTTPException(status_code=403, detail="permission_denied") from None


def _approval_ref(request: Request) -> str:
    """Deletion requires a recorded approval reference.

    Clean-value contract: no surrounding whitespace, no newline, non-empty.
    A value that passes here but is rejected downstream produces a deployment
    that looks healthy and 400s every request, so it is rejected at the door.
    """
    raw = request.headers.get("X-Approval-Ref")
    if raw is None:
        raise HTTPException(status_code=400, detail=contracts.ERR_APPROVAL_REF_MISSING)
    value = unicodedata.normalize("NFKC", raw)
    if not value.strip() or value != value.strip() or "\n" in value or "\r" in value:
        raise HTTPException(status_code=400, detail=contracts.ERR_APPROVAL_REF_MISSING)
    return value


def _text_gate(value: str, *, field: str) -> None:
    """Reject unusable text before it is indexed or put into a prompt.

    Normalisation first (NFKC, zero-width removal), then four ratios, all
    expressed as "reject at or beyond the threshold" so the boundary behaves
    the same way in every direction.
    """
    reason = projection.text_quality_reason(
        value,
        max_replacement_ratio=float(_config.get("text_gate_max_replacement_ratio", 0.02)),
        max_control_ratio=float(_config.get("text_gate_max_control_ratio", 0.01)),
        min_readable_ratio=float(_config.get("text_gate_min_readable_ratio", 0.80)),
        max_chars=int(_config.get("text_gate_max_chars", 20000)),
    )
    if reason:
        raise HTTPException(status_code=400, detail=f"{field}: {reason}")


def _resolve_pins(scope: stores.ResourceScope, req: InvokeRequest, evaluated_at: str) -> dict[str, str | None]:
    """Run the four pin checks and mint the request-lifetime seals."""
    try:
        criteria_seal = scope.registry.resolve_pin(
            stores.KIND_CRITERIA,
            ref=req.criteria_version_ref,
            version=req.criteria_version,
            content_hash=req.criteria_content_hash,
            evaluated_at=evaluated_at,
        )
    except stores.PinError as exc:
        raise HTTPException(status_code=400, detail=exc.error_code) from None

    sealed = {"criteria_sealed_ref": criteria_seal, "freeze_calendar_sealed_ref": None}

    if req.freeze_calendar_ref:
        # Omission is legitimate; a supplied-but-unresolvable pin is not, and
        # the two must not share a shape.
        if not req.freeze_calendar_version or not req.freeze_calendar_content_hash:
            raise HTTPException(status_code=400, detail=contracts.ERR_CALENDAR_UNKNOWN)
        try:
            sealed["freeze_calendar_sealed_ref"] = scope.registry.resolve_pin(
                stores.KIND_CALENDAR,
                ref=req.freeze_calendar_ref,
                version=req.freeze_calendar_version,
                content_hash=req.freeze_calendar_content_hash,
                evaluated_at=evaluated_at,
            )
        except stores.PinError as exc:
            raise HTTPException(status_code=400, detail=exc.error_code) from None
    return sealed


def _run(payload: dict[str, Any], *, session_id: str, auth: Any) -> dict[str, Any]:
    """Invoke the agent with the envelope passed by reference.

    ``BaseGraph.invoke`` takes ``user_input: str`` and writes it into State
    before any node runs, where the S-2 gate scans it. Passing the envelope as
    JSON would let ``detect_pii`` rewrite customer-named identifiers such as
    ``criteria_version_ref``; a 32-letter handle matches no PII pattern.

    🔴 The ``ctx`` argument is not optional here. Without it ``invoke()``
    constructs a default ``InvocationContext`` whose trust level is
    ``ANONYMOUS``, and every node declaring ``VERIFIED_EXTERNAL`` is refused by
    the S-1 gate -- the run fails with a trust denial that looks nothing like
    an authentication problem, because authentication already succeeded at the
    HTTP layer. Constructing the context is the *adapter's* job; nodes must
    always use ``InvocationContext.from_state(state)`` instead.
    """
    ref = envelope.put(_ENVELOPES, payload, session_id=session_id)
    ctx = InvocationContext(
        correlation_id=str(uuid4()),
        session_id=session_id,
        caller_id=_caller_id(auth),
        caller_trust_level=auth.trust,
    )
    with bound_secrets(agent._secrets_provider):  # noqa: SLF001 - scaffold contract
        return cast(dict[str, Any], agent.invoke(ref, session_id=session_id, ctx=ctx))


def _envelope(result: dict[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], result.get("formatted_output") or result.get("output") or {})


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> dict[str, Any]:
    # /invoke persists the request registry, so it is a write: a standalone
    # entry token alone must not reach it.
    auth, scope = _scope_for(request, Operation.WRITE_INGEST)
    _require(scope, _PERM_INVOKE)

    for field in _NARRATIVE_FIELDS:
        _text_gate(getattr(req, field), field=field)
    for doc_index, declaration in enumerate(req.evidence_declarations):
        for span_index, span in enumerate(declaration.spans):
            _text_gate(
                span.span_text,
                field=f"evidence_declarations[{doc_index}].spans[{span_index}].span_text",
            )

    evaluated_at = _now_iso()
    sealed = _resolve_pins(scope, req, evaluated_at)

    session_id = _session_id(_caller_id(auth), req.request_ref)

    # Move the narrative out of the request BEFORE the graph sees it.
    body = {field: getattr(req, field) for field in _NARRATIVE_FIELDS}
    payload_ref = _PAYLOADS.put(body, scope={"request_ref": req.request_ref}, session_id=session_id)

    projected = {
        "mode": "invoke",
        "request_ref": req.request_ref,
        "payload_ref": payload_ref,
        "criteria_version_ref": req.criteria_version_ref,
        "criteria_version": req.criteria_version,
        "criteria_content_hash": req.criteria_content_hash,
        "freeze_calendar_ref": req.freeze_calendar_ref,
        "freeze_calendar_version": req.freeze_calendar_version,
        "freeze_calendar_content_hash": req.freeze_calendar_content_hash,
        "sealed_refs": sealed,
        "planned_window": req.planned_window.model_dump(),
        "evidence_declarations": [d.model_dump() for d in req.evidence_declarations],
        "evaluated_at": evaluated_at,
        "fields_present": {f: bool(getattr(req, f)) for f in _NARRATIVE_FIELDS},
        "refine_count": 0,
    }

    scope.db.upsert_request(
        {
            "request_ref": req.request_ref,
            "criteria_version_ref": req.criteria_version_ref,
            "criteria_version": req.criteria_version,
            "criteria_content_hash": req.criteria_content_hash,
            "freeze_calendar_ref": req.freeze_calendar_ref,
            "planned_window_start": req.planned_window.start,
            "planned_window_end": req.planned_window.end,
            "evaluated_at": evaluated_at,
            "submitted_at": evaluated_at,
            "payload_ref": payload_ref,
            "session_id": session_id,
        }
    )

    try:
        result = _run(projected, session_id=session_id, auth=auth)
    except Exception:
        # Framework normally projects node failures into a result, but an
        # adapter/graph failure before that boundary must not leave the raw
        # narrative resident until TTL.
        _drop_payload(scope, req.request_ref)
        raise
    return _shape_response(result, scope, req.request_ref, session_id)


def _delete_checkpoints(thread_id_hash: str) -> int:
    """Physically remove the suspended checkpoint from BOTH savers.

    Clearing the ledger only makes the session unreachable through this API;
    the derived data stays in the checkpointer. The saver does expose
    ``delete_thread``, so "unreachable" is not good enough here.

    Only the hash of the thread id is stored (a raw id in a leaked ledger is a
    resumable handle to someone else's session), so the id is recovered by
    scanning the saver's own threads and matching the digest.
    """
    savers = []
    outer = getattr(getattr(agent, "_compiled", None), "checkpointer", None)
    if outer is not None:
        savers.append(outer)
    inner = getattr(agent._nodes.get("main"), "_checkpointer", None)  # noqa: SLF001
    if inner is not None:
        savers.append(inner)

    removed = 0
    for saver in savers:
        thread_ids: list[str] = []
        storage = getattr(saver, "storage", None)
        if isinstance(storage, dict):
            thread_ids = [str(key) for key in storage]
        else:  # pragma: no cover - durable saver
            try:
                thread_ids = [str(item.config["configurable"]["thread_id"]) for item in saver.list(None)]
            except Exception:  # noqa: BLE001
                thread_ids = []
        for thread_id in set(thread_ids):
            if stores.hash_thread_id(thread_id) != thread_id_hash:
                continue
            try:
                saver.delete_thread(thread_id)
                removed += 1
            except Exception:  # noqa: BLE001
                # Surfaced by the caller as delete_incomplete; never counted
                # as a success.
                raise
    return removed


def _drop_payload(scope: stores.ResourceScope, request_ref: str) -> None:
    """Delete the request body, using the session the adapter actually bound it to.

    The binding session is read back from the request row: a caller id would not
    match what `put` used, and the delete would silently do nothing.
    """
    row = scope.db.get_request(request_ref)
    if row is None:
        return
    ref = row.get("payload_ref")
    if not ref:
        return
    try:
        _PAYLOADS.delete(ref, scope={"request_ref": request_ref}, session_id=str(row.get("session_id") or ""))
    except PayloadStoreError:
        # Already gone (TTL, prior terminal) -- nothing to do.
        pass
    else:
        row.pop("payload_ref", None)


def _shape_response(
    result: dict[str, Any], scope: stores.ResourceScope, request_ref: str, session_id: str
) -> dict[str, Any]:
    """Return either the finished envelope or the suspension handle."""
    status = result.get("status")
    thread_id = result.get("thread_id") or result.get("__interrupt_thread_id__")

    if status == "awaiting_human" or result.get("__interrupt__"):
        # The client must echo this back: InvocationContext.thread_id is a
        # random UUID per instance, so the server cannot re-derive it later.
        if thread_id:
            scope.db.mark_hitl_suspended(request_ref, thread_id=str(thread_id), at=_now_iso())
        return {
            "status": "awaiting_human",
            "thread_id": thread_id,
            "hitl_metadata": result.get("hitl_metadata") or {},
            "correlation_id": result.get("correlation_id"),
        }

    # Terminal: the narrative body has served its purpose and must not outlive
    # the run. Deleting it here is the only place it happens on the happy path
    # -- the TTL is a backstop, not the policy.
    row = scope.db.get_request(request_ref)
    thread_id_hash = str((row or {}).get("hitl_thread_id_hash") or "")
    try:
        if thread_id_hash:
            _delete_checkpoints(thread_id_hash)
    except Exception:
        # A saver failure must not extend retention of the more sensitive raw
        # payload. Keep the HITL ledger for an operator retry, but remove the
        # body even when checkpoint deletion could not finish.
        _drop_payload(scope, request_ref)
        raise
    scope.db.clear_hitl(request_ref)
    _drop_payload(scope, request_ref)
    if status == "error":
        return {
            "status": "error",
            "error_code": result.get("error_code") or contracts.ERR_INVALID_REQUEST,
            "correlation_id": result.get("correlation_id"),
        }
    return {
        "status": "success",
        "output": _envelope(result),
        "correlation_id": result.get("correlation_id"),
    }


@app.post("/ingest")
async def ingest(req: IngestRequest, request: Request) -> dict[str, Any]:
    auth, scope = _scope_for(request, Operation.WRITE_INGEST)
    _require(scope, _PERM_INGEST)
    session_id = _session_id(_caller_id(auth), "ingest")
    request_ref = "INGEST"
    payload_ref = _PAYLOADS.put({"records": req.records}, scope={"request_ref": request_ref}, session_id=session_id)
    try:
        result = _run(
            {"mode": "ingest", "request_ref": request_ref, "payload_ref": payload_ref}, session_id=session_id, auth=auth
        )
    finally:
        _PAYLOADS.delete(payload_ref, scope={"request_ref": request_ref}, session_id=session_id)
    return {"status": result.get("status"), "output": _envelope(result)}


@app.post("/feedback")
async def feedback(req: FeedbackRequest, request: Request) -> dict[str, Any]:
    auth, scope = _scope_for(request, Operation.WRITE_FEEDBACK)
    _require(scope, _PERM_FEEDBACK)
    if req.approved is not True:
        raise HTTPException(status_code=400, detail=contracts.ERR_INVALID_REQUEST)
    session_id = _session_id(_caller_id(auth), req.request_ref)
    payload_ref = _PAYLOADS.put(
        {"feedback": req.model_dump()}, scope={"request_ref": req.request_ref}, session_id=session_id
    )
    try:
        result = _run(
            {"mode": "feedback", "request_ref": req.request_ref, "payload_ref": payload_ref},
            session_id=session_id,
            auth=auth,
        )
    finally:
        _PAYLOADS.delete(payload_ref, scope={"request_ref": req.request_ref}, session_id=session_id)
    if result.get("status") == "error":
        raise HTTPException(status_code=400, detail=result.get("error_code") or contracts.ERR_INVALID_REQUEST)
    return {"status": result.get("status"), "output": _envelope(result)}


@app.post("/resume/{thread_id}")
async def resume(thread_id: str, req: ResumeRequest, request: Request) -> dict[str, Any]:
    """Resume a suspended session.

    ``/resume`` is an intervention in an *existing* session, so it needs the
    same entry authentication as ``/invoke`` **plus** ownership of the
    checkpoint. Without the ownership check, anyone authenticated could pass an
    arbitrary ``thread_id`` and steer somebody else's pending draft.

    Unknown and foreign threads return the same 403 so the endpoint does not
    disclose whether a checkpoint exists.
    """
    if not thread_id or not thread_id.strip():
        raise HTTPException(status_code=422, detail="thread_id_required")

    auth, scope = _scope_for(request, Operation.RESUME)
    _require(scope, _PERM_INVOKE)

    owner_row = _find_by_thread(scope, thread_id)
    if owner_row is None:
        raise HTTPException(status_code=404, detail="unknown_thread")

    # Ownership, not mere existence. Foreign and unreadable are the same 403 so
    # the endpoint does not disclose whether a checkpoint is there.
    if not _checkpoint_owned_by(thread_id, _caller_id(auth), str(owner_row.get("session_id") or "")):
        raise HTTPException(status_code=403, detail="resume_not_authorised")

    session_ttl = int(_config.get("hitl_session_ttl_s", 3600))
    if _expired(owner_row.get("hitl_suspended_at"), session_ttl):
        _expire_session(scope, owner_row, thread_id)
        raise HTTPException(status_code=410, detail=contracts.ERR_HITL_EXPIRED)

    # Validate and project feedback before calling ``agent.resume``.  Passing
    # the raw value into LangGraph first would persist it in the checkpoint
    # before HitlReviewNode gets a chance to inspect it.
    action, note, reject_reason = sanitise_feedback(req.model_dump(exclude_none=True))
    if reject_reason is not None:
        raise HTTPException(status_code=400, detail=contracts.ERR_INVALID_REQUEST)
    feedback = {"action": action}
    if note:
        feedback["note"] = note

    try:
        with bound_secrets(agent._secrets_provider):  # noqa: SLF001 - scaffold contract
            result = agent.resume(thread_id, feedback)
    except Exception as exc:  # noqa: BLE001
        log.warning("resume failed: %s", type(exc).__name__)
        raise HTTPException(status_code=404, detail="unknown_thread") from None

    return _shape_response(result, scope, str(owner_row.get("request_ref") or ""), _caller_id(auth))


def _checkpoint_owned_by(thread_id: str, caller_id: str, session_id: str) -> bool:
    """Verify the persisted checkpoint belongs to this caller before replaying it.

    The ledger lookup below only proves *a* request suspended on this thread --
    not that the caller asking to resume is the one who suspended it. Without
    this check any authenticated caller in the deployment can pass someone
    else's thread_id and confirm, re-draft or discard their pending packet.

    Follows the canonical implementation (Financial Regulatory Consultation Response Evidence Brief Agent): read the compiled
    graph's own snapshot and compare the identity the framework recorded.
    Missing or unreadable state is deliberately indistinguishable from a
    foreign checkpoint, so the endpoint never discloses whether a thread
    exists.
    """
    compiled = getattr(agent, "_compiled", None)
    if compiled is None:
        return False
    try:
        snapshot = compiled.get_state({"configurable": {"thread_id": thread_id}})
        values = getattr(snapshot, "values", {}) if snapshot is not None else {}
    except Exception:  # noqa: BLE001
        return False
    return values.get("caller_id") == caller_id and values.get("session_id") == session_id


def _find_by_thread(scope: stores.ResourceScope, thread_id: str) -> dict[str, Any] | None:
    digest = stores.hash_thread_id(thread_id)
    for row in scope.db.freeze_exception_requests.values():
        if row.get("hitl_thread_id_hash") == digest:
            return row
    return None


def _expired(suspended_at: Any, ttl_seconds: int) -> bool:
    if not suspended_at:
        return False
    try:
        started = datetime.fromisoformat(str(suspended_at))
    except ValueError:
        return False
    return (datetime.now().astimezone() - started).total_seconds() >= ttl_seconds


def _expire_session(scope: stores.ResourceScope, row: dict[str, Any], thread_id: str) -> None:
    """Drop the checkpoint's payload and clear the ledger entry."""
    ref = row.get("payload_ref")
    if ref:
        try:
            _PAYLOADS.delete(
                ref,
                scope={"request_ref": str(row.get("request_ref") or "")},
                session_id=str(row.get("session_id") or ""),
            )
        except PayloadStoreError:
            pass
    # Same as the delete path: physically remove the checkpoint rather than
    # only refusing future resumes.
    _delete_checkpoints(str(row.get("hitl_thread_id_hash") or ""))
    scope.db.clear_hitl(str(row.get("request_ref") or ""))
    audit.emit(
        audit.EVT_HITL_EXPIRED,
        {
            "request_ref": str(row.get("request_ref") or ""),
            "thread_id_hash": stores.hash_thread_id(thread_id),
            "occurred_at": _now_iso(),
            "action": "expire",
            "outcome": "expired",
        },
        {"correlation_id": "", "session_id": str(row.get("session_id") or "")},
    )


@app.delete("/requests/{request_ref}")
async def delete_request(request_ref: str, request: Request) -> dict[str, Any]:
    auth, scope = _scope_for(request, Operation.WRITE_INGEST)
    _require(scope, _PERM_DELETE_REQUEST)
    approval = _approval_ref(request)

    row = scope.db.get_request(request_ref)
    if row is None:
        # 404 means "never existed here", not "partially deleted": a tombstone
        # would still be present in the latter case.
        raise HTTPException(status_code=404, detail=contracts.ERR_REQUEST_NOT_FOUND)

    occurred_at = _now_iso()
    row["status"] = stores.STATUS_DELETING
    row.setdefault("delete_started_at", occurred_at)
    row["delete_updated_at"] = occurred_at
    row.setdefault(
        "delete_plan",
        {
            "case_evidence": [e["source_id"] for e in scope.db.evidence_for_request(request_ref)],
            "candidates": [
                candidate_id
                for candidate_id, candidate in scope.db.perspective_candidates.items()
                if candidate.get("request_ref") == request_ref
            ],
            "payload_ref": row.get("payload_ref"),
            "hitl_state": row.get("hitl_state"),
            "hitl_thread_id_hash": row.get("hitl_thread_id_hash"),
        },
    )
    plan = dict(row["delete_plan"])
    progress = dict(row.get("delete_progress") or {})

    deleted = dict(progress.get("deleted_counts") or {"db_rows": 0, "kb_docs": 0, "payloads": 0})
    try:
        if plan.get("hitl_state") == stores.HITL_SUSPENDED and progress.get("hitl") != "done":
            # Physical removal from both savers, not just a ledger clear: the
            # saver exposes delete_thread, so leaving derived data behind would
            # be a choice, not a limitation.
            _delete_checkpoints(str(plan.get("hitl_thread_id_hash") or ""))
            scope.db.clear_hitl(request_ref)
            progress["hitl"] = "done"
        if progress.get("case_evidence") != "done":
            for source_id in plan.get("case_evidence") or []:
                if scope.db.case_evidence.pop(source_id, None) is not None:
                    deleted["db_rows"] += 1
            progress["case_evidence"] = "done"

        if progress.get("candidates") != "done":
            for candidate_id in plan.get("candidates") or []:
                if scope.db.perspective_candidates.pop(candidate_id, None) is not None:
                    deleted["db_rows"] += 1
            progress["candidates"] = "done"

        ref = plan.get("payload_ref")
        if ref and progress.get("payload") != "done":
            try:
                _PAYLOADS.delete(ref, scope={"request_ref": request_ref}, session_id=str(row.get("session_id") or ""))
            except PayloadStoreError as exc:
                # Swallowing this and counting +1 reports a body as deleted
                # while it is still resident -- the one outcome a delete API
                # must never produce.
                if str(exc) not in ("payload_ref_unknown", "payload_ref_expired"):
                    raise
            else:
                deleted["payloads"] += 1
            progress["payload"] = "done"

        scope.db.freeze_exception_requests.pop(request_ref, None)
        deleted["db_rows"] += 1
    except Exception as exc:  # noqa: BLE001
        progress["deleted_counts"] = deleted
        row["delete_progress"] = progress
        row["delete_updated_at"] = _now_iso()
        event = audit.build_event(
            correlation_id="",
            event_type=audit.EVT_DELETE_INCOMPLETE,
            target_type="request",
            target_ref=request_ref,
            occurred_at=_now_iso(),
            actor_id=_caller_id(auth),
            approval_ref=approval,
            outcome="incomplete",
            deleted_counts=deleted,
            error_code=type(exc).__name__,
        )
        scope.db.append_audit_event(event)
        raise HTTPException(status_code=500, detail=contracts.ERR_DELETE_INCOMPLETE) from None

    event = audit.build_event(
        correlation_id="",
        event_type=audit.EVT_DELETE_COMPLETED,
        target_type="request",
        target_ref=request_ref,
        occurred_at=occurred_at,
        actor_id=_caller_id(auth),
        approval_ref=approval,
        outcome="success",
        deleted_counts=deleted,
    )
    event_id = scope.db.append_audit_event(event)
    # kb_docs is always 0 here: past perspectives are a separate lineage.
    return {"status": "success", "deleted": deleted, "audit_event_id": event_id}


@app.delete("/perspectives/{history_ref}")
async def delete_perspective(history_ref: str, request: Request) -> dict[str, Any]:
    auth, scope = _scope_for(request, Operation.WRITE_INGEST)
    # A dedicated permission: the ordinary request-delete right must not be
    # able to erase the organisation's accumulated review perspectives.
    _require(scope, _PERM_DELETE_PERSPECTIVE)
    approval = _approval_ref(request)

    row = scope.db.get_review_perspective(history_ref)
    if row is None:
        raise HTTPException(status_code=404, detail=contracts.ERR_PERSPECTIVE_NOT_FOUND)

    occurred_at = _now_iso()
    row["status"] = stores.STATUS_DELETING
    row.setdefault("delete_started_at", occurred_at)
    row["delete_updated_at"] = occurred_at
    row.setdefault("delete_plan", {"kb": history_ref, "db": history_ref})
    progress = dict(row.get("delete_progress") or {})

    deleted = dict(progress.get("deleted_counts") or {"db_rows": 0, "kb_docs": 0, "payloads": 0})
    try:
        if progress.get("kb") != "done":
            deleted["kb_docs"] += scope.kb.delete(history_ref)
            progress["kb"] = "done"
        if progress.get("db") != "done":
            if scope.db.review_perspectives.pop(history_ref, None) is not None:
                deleted["db_rows"] += 1
            progress["db"] = "done"
    except Exception as exc:  # noqa: BLE001
        progress["deleted_counts"] = deleted
        row["delete_progress"] = progress
        row["delete_updated_at"] = _now_iso()
        event = audit.build_event(
            correlation_id="",
            event_type=audit.EVT_DELETE_INCOMPLETE,
            target_type="perspective",
            target_ref=history_ref,
            occurred_at=_now_iso(),
            actor_id=_caller_id(auth),
            approval_ref=approval,
            outcome="incomplete",
            deleted_counts=deleted,
            error_code=type(exc).__name__,
        )
        scope.db.append_audit_event(event)
        raise HTTPException(status_code=500, detail=contracts.ERR_DELETE_INCOMPLETE) from None

    event = audit.build_event(
        correlation_id="",
        event_type=audit.EVT_DELETE_COMPLETED,
        target_type="perspective",
        target_ref=history_ref,
        occurred_at=occurred_at,
        actor_id=_caller_id(auth),
        approval_ref=approval,
        outcome="success",
        deleted_counts=deleted,
    )
    event_id = scope.db.append_audit_event(event)
    return {"status": "success", "deleted": deleted, "audit_event_id": event_id}


@app.get("/audit-events/{event_id}")
async def read_audit_event(event_id: str, request: Request) -> dict[str, Any]:
    """Read-only audit lookup.

    Separate permission from the delete rights on purpose: the role that checks
    the trail should not be the role that can erase what it checks.
    """
    _auth, scope = _scope_for(request, Operation.READ_INVOKE)
    _require(scope, _PERM_AUDIT_READ)
    row = scope.db.get_audit_event(event_id)
    if row is None:
        raise HTTPException(status_code=404, detail="audit_event_not_found")
    return {k: row.get(k) for k in contracts.AUDIT_EVENT_KEYS}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
