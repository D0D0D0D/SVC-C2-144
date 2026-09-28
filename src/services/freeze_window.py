"""Deterministic freeze-window reconciliation.

The time question ("does this change fall inside a freeze period?") is the core
of this template and is answered here, never by the model.

Two measured bugs are closed by construction:

* **No ISO-8601 string comparison.** ``"2026-08-20T09:00+09:00"`` sorts before
  ``"2026-08-20T10:00Z"`` lexicographically even though it is the later
  instant. Everything is parsed to a timezone-aware ``datetime`` first, and an
  input without an offset is rejected rather than guessed.
* **"no overlap" and "not evaluated" are different answers.** Returning an
  empty ``overlapping_windows`` for both lets a reader conclude "outside the
  freeze period" when in fact nothing was checked. ``evaluated`` is the key
  that separates them.

The output is facts only. Whether an overlap justifies an exception is a human
decision; emitting that judgement would break the S-3 output contract.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


class WindowFormatError(ValueError):
    """Raised when a timestamp is absent, unparsable or offset-naive."""


def parse_aware(value: Any, *, field: str) -> datetime:
    """Parse an ISO-8601 timestamp, requiring an explicit UTC offset.

    An offset-naive value is a hard error: the server's local timezone is not a
    safe default, and silently applying it makes the same request produce
    different answers on different hosts.
    """
    if not isinstance(value, str) or not value.strip():
        raise WindowFormatError(f"{field}: missing")
    text = value.strip()
    # `fromisoformat` accepts "Z" only from Python 3.11 onward; normalise anyway
    # so the contract does not depend on the interpreter patch level.
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise WindowFormatError(f"{field}: not ISO-8601") from exc
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise WindowFormatError(f"{field}: UTC offset required")
    return parsed


def _overlap(
    a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime
) -> tuple[datetime, datetime] | None:
    """Return the intersection of two half-open intervals, or None.

    Half-open ``[start, end)`` means a window ending exactly when another
    begins does NOT overlap — adjacent maintenance slots are a normal pattern
    and reporting them as conflicts would be noise.
    """
    start = max(a_start, b_start)
    end = min(a_end, b_end)
    return (start, end) if start < end else None


def evaluate(
    *,
    planned_window: dict[str, Any] | None,
    calendar_windows: list[dict[str, Any]] | None,
    freeze_calendar_ref: str | None,
    evaluated_at: str,
) -> tuple[dict[str, Any], list[str]]:
    """Return ``(freeze_window_facts, format_errors)``.

    ``format_errors`` is a list of human-readable reasons; the caller turns
    them into deterministic gaps. When the calendar is absent the facts carry
    ``evaluated: False`` and the caller adds the "calendar not supplied" gap.
    """
    errors: list[str] = []

    facts: dict[str, Any] = {
        "freeze_calendar_ref": freeze_calendar_ref,
        "planned_window": None,
        "overlapping_windows": [],
        "evaluated": False,
        "evaluated_at": evaluated_at,
    }

    planned = planned_window if isinstance(planned_window, dict) else {}
    try:
        start = parse_aware(planned.get("start"), field="planned_window.start")
        end = parse_aware(planned.get("end"), field="planned_window.end")
    except WindowFormatError as exc:
        errors.append(str(exc))
        return facts, errors

    if end <= start:
        errors.append("planned_window: end must be after start")
        return facts, errors

    facts["planned_window"] = {
        "start": planned.get("start"),
        "end": planned.get("end"),
    }

    if not freeze_calendar_ref or calendar_windows is None:
        # Not an error — the calendar is optional. `evaluated` stays False so
        # the reader can tell "not checked" from "checked, no overlap".
        return facts, errors

    overlaps: list[dict[str, Any]] = []
    for window in calendar_windows:
        if not isinstance(window, dict):
            continue
        try:
            w_start = parse_aware(window.get("start"), field="freeze_window.start")
            w_end = parse_aware(window.get("end"), field="freeze_window.end")
        except WindowFormatError as exc:
            # A malformed calendar entry is a data problem on the pinned
            # document, not on the request. Record it and keep evaluating the
            # rest rather than failing the whole reconciliation.
            errors.append(f"{window.get('window_id') or '?'}: {exc}")
            continue
        hit = _overlap(start, end, w_start, w_end)
        if hit is None:
            continue
        overlaps.append(
            {
                "window_id": str(window.get("window_id") or ""),
                "window_ref": str(window.get("window_ref") or ""),
                "overlap_start": hit[0].isoformat(),
                "overlap_end": hit[1].isoformat(),
            }
        )

    facts["overlapping_windows"] = overlaps
    facts["evaluated"] = True
    return facts, errors


__all__ = ["WindowFormatError", "parse_aware", "evaluate"]
