"""Internal envelope transport between hops.

``BaseGraph.invoke()`` writes ``user_input`` into State **before any node
runs**, and ``InitializeNode`` is itself a ``FunctionNode`` — so the S-2 PII
gate scans it ahead of ``pre_process``. Anything identifier-shaped inside a
JSON envelope passed that way gets rewritten to ``[MASKED]``.

That is not hypothetical and not limited to handles we mint ourselves: this
template carries ``criteria_version_ref``, ``doc_ref`` and ``span_ref``, all
**named by the customer**, so their shape is outside our control. Measured
against agenticstar-agentcore 1.0.1, ``EVT-04-12-2024`` is detected as
``phone_jp`` and a 12-digit sequence as ``my_number_jp``.

So the envelope travels by reference:

1. the entry adapter puts the envelope in a transient store and gets a
   32-character all-lowercase-letter handle (no digits -> matches no PII
   pattern -> passes S-2 byte-identical);
2. ``invoke(user_input=<ref>)``;
3. ``pre_process`` resolves it and expands it into **custom typed State keys**
   — never into ``validated_input``, which is itself a scanned field and would
   be re-masked by the next node's gate;
4. the same thing happens on the inner hop: ``GraphNode.extract_input()``'s
   return value becomes the inner ``BaseGraph.invoke()``'s ``user_input``, so
   the inner graph gets its own ref and its dispatch node resolves it.

An armoured (digit-free) encoding of the whole envelope would also survive the
gate, but a ref is better: it does not double in size, it is independent of the
S-1 input size limit, and request-size limits do not have to be derived from
the encoded length.
"""

from __future__ import annotations

from typing import Any, cast

# One fixed partition. The envelope store holds exactly one kind of thing and
# is never mixed with the narrative payload store.
ENVELOPE_SCOPE = {"kind": "envelope"}
ENVELOPE_SCOPE_KEYS = ("kind",)


class EnvelopeUnavailable(RuntimeError):
    """The internal envelope could not be resolved (expired, replayed, foreign)."""


def put(store: Any, envelope: dict[str, Any], *, session_id: str) -> str:
    """Store one envelope and return its digit-free handle."""
    return cast(str, store.put(envelope, scope=dict(ENVELOPE_SCOPE), session_id=session_id))


def take(store: Any, ref: str, *, session_id: str) -> dict[str, Any]:
    """Resolve an envelope.

    ``consume`` stays False: a resumed run re-enters ``pre_process`` and has to
    resolve the same envelope again. The store still fail-closes on scope or
    session mismatch, which is the property that matters here.
    """
    try:
        value = store.resolve(ref, scope=dict(ENVELOPE_SCOPE), session_id=session_id)
    except Exception as exc:  # noqa: BLE001
        raise EnvelopeUnavailable(type(exc).__name__) from exc
    if not isinstance(value, dict):
        raise EnvelopeUnavailable("envelope_not_an_object")
    return value


def looks_like_ref(value: Any) -> bool:
    """True for a minted handle (32 lowercase letters, no digits)."""
    return isinstance(value, str) and len(value) == 32 and value.isalpha() and value.islower()


__all__ = ["ENVELOPE_SCOPE", "ENVELOPE_SCOPE_KEYS", "EnvelopeUnavailable", "put", "take", "looks_like_ref"]
