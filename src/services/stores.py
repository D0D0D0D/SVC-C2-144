"""Per-deployment persistence seam, control-document registry and resolver.

**Unwired seam.** The SDK persistence layer is STUB in 1.0.1, so the database
and knowledge base here are in-memory implementations behind the interfaces the
nodes use. What to resolve when a real backend is wired is listed in
the operation guide.

Three properties are *not* placeholders and must survive the swap:

* **No default connection.** ``AuthorizedResourceResolver.for_caller`` refuses
  an unregistered caller instead of falling back, so a misconfiguration fails
  closed rather than serving another deployment's data.
* **No handle caching.** Resolution happens per invocation. A connection cached
  on a node instance is shared across every request served from the registry's
  LRU cache -- and the graph object itself is built once per process.
* **The KB projection never holds the verdict.** Keeping ``decision_outcome``
  out of the search axis is what stops "approved cases rank higher" bias; the
  verdict is joined back from the DB by ``history_ref`` after the hit.

There are no partition keys (``SCOPE_KEYS = ()``): one deployment holds one
company's data and isolation is the connection itself.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

from src.services import contracts, freeze_window, recall

# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


class AuthorizationError(RuntimeError):
    """Caller could not be resolved to an authorised deployment."""


class MissingPermission(RuntimeError):
    """Caller is authorised but lacks the operation permission."""

    def __init__(self, permission: str) -> None:
        super().__init__(permission)
        self.permission = permission


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def alpha_digest(seed: str, *, prefix: str, length: int = 16) -> str:
    """Alphabetic-only handle.

    Digits in a handle get mis-detected as PII by the S-2 gate and destructively
    rewritten (a measured defect), so every identifier this template mints
    is letters only.
    """
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return prefix + "".join(_ALPHABET[b % 26] for b in digest[:length])


def hash_thread_id(thread_id: str) -> str:
    """Store a digest of a thread id, never the raw value.

    A leaked ledger must not hand anyone a resumable handle to someone else's
    suspended session.
    """
    return hashlib.sha256(str(thread_id).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# control document registry (version-pinned inputs)
# ---------------------------------------------------------------------------

KIND_CRITERIA = "criteria"
KIND_CALENDAR = "freeze_calendar"

_PIN_ERRORS = {
    KIND_CRITERIA: {
        "unknown": contracts.ERR_CRITERIA_VERSION_UNKNOWN,
        "hash": contracts.ERR_CRITERIA_HASH_MISMATCH,
        "effective": contracts.ERR_CRITERIA_NOT_EFFECTIVE,
        "seal": contracts.ERR_CRITERIA_SEAL_UNRESOLVED,
    },
    KIND_CALENDAR: {
        "unknown": contracts.ERR_CALENDAR_UNKNOWN,
        "hash": contracts.ERR_CALENDAR_HASH_MISMATCH,
        "effective": contracts.ERR_CALENDAR_NOT_EFFECTIVE,
        "seal": contracts.ERR_CALENDAR_SEAL_UNRESOLVED,
    },
}


class PinError(RuntimeError):
    """A pinned control document failed one of the four checks."""

    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


@dataclass
class ControlDocumentRegistry:
    """The customer's system of record for approved control documents.

    The agent never writes here. ``/ingest`` rejects ``exception_criteria`` and
    ``freeze_calendar`` record types precisely so this stays true.
    """

    # (kind, ref, version) -> {content_hash, approved, effective_from,
    #                          effective_to, body}
    documents: dict[tuple[str, str, str], dict[str, Any]] = field(default_factory=dict)
    _sealed: dict[str, dict[str, Any]] = field(default_factory=dict, init=False)

    def register(self, kind: str, ref: str, version: str, document: dict[str, Any]) -> None:
        self.documents[(kind, ref, version)] = document

    def resolve_pin(
        self,
        kind: str,
        *,
        ref: str,
        version: str,
        content_hash: str,
        evaluated_at: str,
    ) -> str:
        """Run the four checks and mint a request-lifetime seal reference.

        Raises :class:`PinError` with the code specific to the failing check --
        the four failures are distinguishable on purpose so an operator can tell
        "not registered" from "registered but superseded".
        """
        codes = _PIN_ERRORS[kind]
        document = self.documents.get((kind, ref, version))
        if document is None:
            raise PinError(codes["unknown"])
        if str(document.get("content_hash") or "") != str(content_hash or ""):
            raise PinError(codes["hash"])
        if not document.get("approved"):
            raise PinError(codes["effective"])
        if not _is_effective(document, evaluated_at):
            raise PinError(codes["effective"])

        sealed_ref = alpha_digest(f"{kind}:{ref}:{version}:{evaluated_at}", prefix="SEAL")
        self._sealed[sealed_ref] = document
        return sealed_ref

    def resolve_sealed(self, sealed_ref: str | None) -> dict[str, Any] | None:
        """Return the sealed document body, or ``None`` if unresolvable."""
        if not sealed_ref:
            return None
        return self._sealed.get(sealed_ref)


def _is_effective(document: dict[str, Any], evaluated_at: str) -> bool:
    try:
        now = freeze_window.parse_aware(evaluated_at, field="evaluated_at")
    except freeze_window.WindowFormatError:
        return False
    for key, comparator in (("effective_from", "ge"), ("effective_to", "lt")):
        raw = document.get(key)
        if not raw:
            continue
        try:
            bound = freeze_window.parse_aware(raw, field=key)
        except freeze_window.WindowFormatError:
            return False
        if comparator == "ge" and now < bound:
            return False
        if comparator == "lt" and now >= bound:
            return False
    return True


# ---------------------------------------------------------------------------
# database
# ---------------------------------------------------------------------------

STATUS_ACTIVE = "active"
STATUS_DELETING = "deleting"
HITL_NONE = "none"
HITL_SUSPENDED = "suspended"


@dataclass
class InMemoryDatabase:
    """The five tables of ``docs/02_design.md`` §DB schema."""

    freeze_exception_requests: dict[str, dict[str, Any]] = field(default_factory=dict)
    case_evidence: dict[str, dict[str, Any]] = field(default_factory=dict)
    review_perspectives: dict[str, dict[str, Any]] = field(default_factory=dict)
    perspective_candidates: dict[str, dict[str, Any]] = field(default_factory=dict)
    audit_events: dict[str, dict[str, Any]] = field(default_factory=dict)

    # -- requests ---------------------------------------------------------
    def upsert_request(self, row: dict[str, Any]) -> None:
        ref = row["request_ref"]
        current = self.freeze_exception_requests.setdefault(ref, {"status": STATUS_ACTIVE, "hitl_state": HITL_NONE})
        current.update(row)

    def get_request(self, request_ref: str) -> dict[str, Any] | None:
        return self.freeze_exception_requests.get(request_ref)

    def mark_hitl_suspended(self, request_ref: str, *, thread_id: str, at: str) -> None:
        row = self.freeze_exception_requests.get(request_ref)
        if row is None:
            return
        row["hitl_state"] = HITL_SUSPENDED
        row["hitl_thread_id_hash"] = hash_thread_id(thread_id)
        row["hitl_suspended_at"] = at

    def clear_hitl(self, request_ref: str) -> None:
        row = self.freeze_exception_requests.get(request_ref)
        if row is None:
            return
        row["hitl_state"] = HITL_NONE
        row.pop("hitl_thread_id_hash", None)
        row.pop("hitl_suspended_at", None)

    # -- evidence ---------------------------------------------------------
    def add_case_evidence(self, row: dict[str, Any]) -> None:
        self.case_evidence[row["source_id"]] = row

    def evidence_for_request(self, request_ref: str) -> list[dict[str, Any]]:
        return [r for r in self.case_evidence.values() if r.get("request_ref") == request_ref]

    # -- review perspectives (the accumulated moat) -----------------------
    def add_review_perspective(self, row: dict[str, Any]) -> None:
        row.setdefault("status", STATUS_ACTIVE)
        self.review_perspectives[row["history_ref"]] = row

    def get_review_perspective(self, history_ref: str) -> dict[str, Any] | None:
        return self.review_perspectives.get(history_ref)

    # -- feedback candidates (append-only) --------------------------------
    def append_candidate(self, row: dict[str, Any]) -> None:
        # The agent never transitions `status`; promoting a candidate into the
        # next criteria edition is human work outside the agent.
        self.perspective_candidates[row["candidate_id"]] = row

    # -- audit ------------------------------------------------------------
    def append_audit_event(self, row: dict[str, Any]) -> str:
        unexpected = set(row) - contracts.AUDIT_EVENT_KEYS
        if unexpected:
            raise ValueError(f"audit row has undeclared keys: {sorted(unexpected)}")
        self.audit_events[row["event_id"]] = row
        return cast(str, row["event_id"])

    def get_audit_event(self, event_id: str) -> dict[str, Any] | None:
        return self.audit_events.get(event_id)


# ---------------------------------------------------------------------------
# knowledge base (projection only)
# ---------------------------------------------------------------------------


@dataclass
class InMemoryKnowledgeBase:
    """Retrieval-safe projection of past review perspectives."""

    documents: dict[str, dict[str, Any]] = field(default_factory=dict)

    _FORBIDDEN = frozenset(
        {
            "decision_outcome",
            "request_summary_raw",
            "decision_rationale_raw",
            "request_summary_audit",
            "decision_rationale_audit",
        }
    )

    def upsert(self, doc: dict[str, Any]) -> None:
        leaked = self._FORBIDDEN & set(doc)
        if leaked:
            raise ValueError(f"KB projection must not carry {sorted(leaked)}")
        self.documents[doc["history_ref"]] = doc

    def delete(self, history_ref: str) -> int:
        return 1 if self.documents.pop(history_ref, None) is not None else 0

    def search(self, query: str, *, top_k: int, min_hits: int) -> list[dict[str, Any]]:
        """Substring recall over the safe projection (interim backend)."""
        terms = recall.build_terms(query)
        if not terms:
            return []
        scored: list[tuple[int, str, dict[str, Any]]] = []
        for doc in self.documents.values():
            haystack = " ".join(
                str(doc.get(key) or "")
                for key in ("request_summary_safe", "decision_rationale_safe", "perspective_code")
            )
            score = recall.match_score(terms, haystack)
            if recall.meets_threshold(score, min_hits):
                scored.append((score, str(doc.get("history_ref") or ""), doc))
        # Deterministic order: score desc, then handle asc.
        scored.sort(key=lambda row: (-row[0], row[1]))
        return [doc for _, _, doc in scored[:top_k]]


# ---------------------------------------------------------------------------
# payload store protocol (the concrete one comes from the payload-store part)
# ---------------------------------------------------------------------------


class PayloadExpired(RuntimeError):
    """The transient request body is no longer resolvable."""


# ---------------------------------------------------------------------------
# resolver
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResourceScope:
    """Per-invocation handles. Never stored on ``self`` or in State."""

    # Identifies the provisioned connection. NOT an isolation key: this
    # template is one deployment per company (docs/02 §データ層設計), so there
    # is no in-deployment partition for it to select.
    tenant_ref: str
    permissions: frozenset[str]
    db: InMemoryDatabase
    kb: InMemoryKnowledgeBase
    payloads: Any
    registry: ControlDocumentRegistry

    def require_permission(self, name: str) -> None:
        if name not in self.permissions:
            raise MissingPermission(name)


class AuthorizedResourceResolver:
    """caller_id -> provisioned connections. Customer-independent; may be injected.

    This is the only resource seam the graph is allowed to hold. Everything it
    returns is per-invocation.
    """

    def __init__(self) -> None:
        self._by_caller: dict[str, ResourceScope] = {}

    def register(self, caller_id: str, scope: ResourceScope) -> None:
        if not isinstance(caller_id, str) or not caller_id.strip():
            raise ValueError("caller_id must be a non-empty string")
        self._by_caller[caller_id] = scope

    def for_caller(self, caller_id: str | None) -> ResourceScope:
        """Return the caller's scope, or raise.

        There is deliberately no default and no fallback: an unknown caller is
        an error, not a reason to hand out some other deployment's connection.
        """
        if not isinstance(caller_id, str) or not caller_id.strip():
            raise AuthorizationError(contracts.ERR_CUSTOMER_RESOLUTION)
        scope = self._by_caller.get(caller_id)
        if scope is None:
            raise AuthorizationError(contracts.ERR_CUSTOMER_RESOLUTION)
        return scope


def utc_now_iso() -> str:
    """Only for audit/bookkeeping timestamps, never for freeze-window logic.

    Window comparison uses ``state["evaluated_at"]``, fixed once by the entry
    adapter, so that the same request cannot produce different answers on
    different hosts or at different points inside one request.
    """
    return datetime.now().astimezone().isoformat()


__all__ = [
    "AuthorizationError",
    "MissingPermission",
    "PinError",
    "PayloadExpired",
    "ControlDocumentRegistry",
    "InMemoryDatabase",
    "InMemoryKnowledgeBase",
    "ResourceScope",
    "AuthorizedResourceResolver",
    "alpha_digest",
    "hash_thread_id",
    "utc_now_iso",
    "KIND_CRITERIA",
    "KIND_CALENDAR",
    "STATUS_ACTIVE",
    "STATUS_DELETING",
    "HITL_NONE",
    "HITL_SUSPENDED",
]
