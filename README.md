# SVC-C2-144 — ChangeFreezeExceptionReviewAgent

> **Category**: Cat 2 (domain workflow)
> **Industry**: SVC

## Overview

ChangeFreezeExceptionReviewAgent reviews a request to make a change during a change freeze. It
checks the request against an approved, version-pinned exception-criteria document and freeze
calendar, and returns a review packet: which criteria conditions the submitted evidence supports,
what is missing, what contradicts, where the customer's approval does not cover the planned change,
which issues raised in past exception reviews this request touches, and what to ask next.

**It does not decide whether the exception is approved.** That judgement belongs to the change
advisory board; the agent prepares the material the board reads. This is not merely discouraged —
the output schema has no field that could carry a verdict, and the only severity the agent may
emit is `candidate`.

The division of labour between the language model and the deterministic layer is the point of the
design. The workflow runs in four stages and **only the third calls a model**:

1. **Deterministic check.** Required fields, formats, and the freeze-window comparison. Time is
   answered arithmetically, never by the model: every timestamp is parsed to a timezone-aware
   `datetime` before comparison, because ISO-8601 strings sort wrongly across offsets
   (`2026-08-20T09:00+09:00` precedes `2026-08-20T10:00Z` lexically but is the later instant). The
   reference instant is fixed once per request by the entry adapter and travels in state, so the
   same request cannot answer differently on two hosts or twice within one run.
2. **Recall.** Past review perspectives are recalled from the customer's own knowledge base. The
   recall never keys on the past *outcome*, so "it was approved last time" cannot steer the search.
   Japanese text is matched on character bigrams, not whitespace tokens — whitespace splitting makes
   an entire Japanese sentence one token and recalls a flat zero.
3. **Sufficiency draft (the only model call).** The model matches the exception rationale, rollback
   plan and risk assessment against the pinned criteria conditions, and judges whether each recalled
   past perspective is addressed. Every finding must cite at least one source id; the prompt's JSON
   schema declares `minItems: 1`. The one exception is a gap the deterministic stage found
   ("this required field is absent"), which carries `gap_origin: "deterministic"` and is the only
   shape allowed an empty citation list.
4. **Evidence validation.** Claim-to-source correspondence, source-id existence, verbatim-quote
   fidelity, quote non-conclusiveness, and agreement between numbers in the prose and numbers in the
   cited source are all machine-checked. A mismatch fails closed or is marked for human review.

Between stages 3 and 4 the run **suspends for the reviewer** (see *Human review* below).

Past reviews accumulate: confirmed review perspectives are stored and checked against every later
request, so the agent becomes more useful the longer a customer runs it. What is deliberately *not*
accumulated is the judgement standard itself — the exception criteria and the freeze calendar are
supplied as version-pinned inputs, resolved from an approved registry and hash-verified per request,
because "which version was applied" is exactly what an audit asks.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to your
own data and policies, and run it inside your own AGENTIC STAR deployment.

## Integration

Provide:

- an AgentCore runtime;
- a `BaseLLM` implementation through `config["llm"]`. It is **optional**: a missing client is an
  explicit `llm_unavailable` degradation, not a start-up failure — the deterministic stages still
  run and the packet comes back without the drafted findings. The bundled standalone adapter builds
  an Anthropic client when an `ANTHROPIC_API_KEY` resolves through the secrets provider;
- durable database and knowledge-base implementations. The shipped ones are **in-memory** (see
  *Limitations*), and `src/services/stores.py` is the seam to replace;
- an approved control-document registry holding the exception criteria and freeze calendars, each
  with a version, a content hash, an approval record and an effective window;
- caller authentication in front of the entry adapter, plus a mapping from caller identity to a
  provisioned deployment. There is deliberately no default and no fallback: an unknown caller is an
  error, never a reason to hand out another deployment's connection.

Runtime parameters read from `config/config.yaml` (defaults in parentheses):

| Key | Meaning |
|---|---|
| `top_k` (`8`) | Past review perspectives recalled in stage 2 |
| `min_quote_chars` (`12`) / `max_quotable_chars` (`120`) | Verbatim-quote bounds. Below the lower bound `quote in span_text` is vacuous; above the upper bound a span is prose, not an atomic fact, and is classified unquotable |
| `min_recall_hits` (`3`) | Absolute minimum term matches for a recall hit — absolute, not a ratio, because a ratio raises the bar for long text until nothing matches |
| `max_rationale_chars` (`1000`) | Cap on the model-visible projection of a decision rationale |
| `text_gate_max_replacement_ratio` (`0.02`) / `text_gate_max_control_ratio` (`0.01`) / `text_gate_min_readable_ratio` (`0.80`) / `text_gate_max_chars` (`20000`) | Input quality gate, applied after NFKC normalisation and zero-width removal |
| `payload_ttl_s` (`3600`) | Lifetime of the request-scoped store holding the narrative fields. It must exceed the expected review dwell time — `refine` re-runs stage 3 and re-reads the body |
| `hitl_session_ttl_s` (`3600`) | Lifetime of a suspended review session. On expiry the checkpoint and the payload are deleted and a later resume returns 410 |
| `max_retry` (`3`) / `timeout_s` (`60`) | Execution limits |
| `memory_enabled` (`true`) / `hitl.enabled` (`true`) / `hitl.max_hitl` (`8`) | A checkpointer is required for suspend/resume; `max_hitl` bounds re-drafts |
| `auth_runbook_url` | Where the credential issue/rotate/revoke procedure lives. Empty is a deployment blocker — start-up refuses |

The template does not fetch documents, parse PDF or spreadsheet binaries, perform OCR, schedule
changes, or approve anything. Input is text and declared evidence spans.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | Required for production deployment and durable service integrations. Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Supplied by the platform's own package registry at build time. It is not published on public PyPI, so `pip install -e .` alone does not fetch it. The Anthropic client used by the standalone adapter comes from the framework's `anthropic` extra |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework package must be installed — `src/api/server.py` imports `framework.*` and `shared.*`
at module load, so without it the server does not start at all. With the framework present, the
standalone adapter does start without a platform connection, and three things then differ from a
production deployment:

- **The database, knowledge base and checkpoint saver are in-memory.** Nothing survives a process
  restart, and nothing is shared between processes. This is for local validation and staging wiring
  checks, not for running a review that anyone depends on.
- **No language model is configured** unless an `ANTHROPIC_API_KEY` resolves through the secrets
  provider. That is an `llm_unavailable` degradation reported in the response, not a crash.
- **No caller is admitted until authentication is configured.** Identity comes from upstream
  middleware or from an `INVOKE_AUTH_TOKEN` bearer; an unauthenticated caller gets 401, and an
  authenticated caller who is not mapped to a provisioned deployment gets 403. The two are
  deliberately distinct codes, but the 403 never reveals whether a deployment exists.

`/health` is unauthenticated and returns `{"status": "ok"}`. The test suite runs without a platform
connection and without a language model.

## Input

All request bodies are **closed schemas**: an undeclared key is a 400, never a silent drop —
dropping it would make a constraint the caller believed they sent disappear.

| Endpoint | Purpose | Body |
|---|---|---|
| `POST /invoke` | Review one exception request | `request_ref`, the criteria pin (`criteria_version_ref`, `criteria_version`, `criteria_content_hash`), `planned_window` (`start`, `end`), the four narrative fields, an optional freeze-calendar pin, and `evidence_declarations[]` |
| `POST /ingest` | Accumulate case evidence and confirmed past review perspectives | `records[]` |
| `POST /feedback` | Queue a reviewer-confirmed perspective as a candidate for the next criteria version | `request_ref`, `claim_id`, `decision_outcome`, `action`, `approved` (must be `true`), and optional `perspective_code`, `condition_id`, `criteria_version_ref`, `source_ids[]`, `decision_rationale` |
| `POST /resume/{thread_id}` | Resume a suspended review | `action` (`confirm` / `refine` / `discard`), optional `note` |
| `DELETE /requests/{request_ref}` · `DELETE /perspectives/{history_ref}` | Erasure, in two separate lineages | Requires an `X-Approval-Ref` header |
| `GET /audit-events/{event_id}` · `GET /health` | Audit record lookup · liveness | — |

The four narrative fields — `change_description`, `exception_rationale`, `rollback_plan`,
`risk_assessment` — are the sensitive part of a request. They are moved into a request-scoped
payload store and replaced by a handle **before** the graph is invoked, because the framework puts
its raw input into state and, with a checkpointer attached, that lands in the first checkpoint.
Removing the body afterwards would be too late. On a terminal response the body is deleted
outright; the TTL is a backstop, not the policy.

Each endpoint requires both a verified caller and an explicit per-operation permission, checked
before any write happens. Writes additionally require a verified middleware identity — a standalone
entry token is not enough to reach `/invoke`, `/ingest` or `/feedback`.

Requests fail closed with stable error codes, including `invalid_request`,
`customer_resolution_failed`, `criteria_version_missing`, `criteria_version_unknown`,
`criteria_content_hash_mismatch`, `criteria_version_not_effective`, `freeze_calendar_unknown`,
`freeze_calendar_content_hash_mismatch`, `freeze_calendar_not_effective`, `evaluated_at_missing`,
`payload_expired`, `llm_call_failed`, `output_contract_violation`, `approval_ref_missing` and
`hitl_expired`.

### Human review

`/invoke` does not always return a finished packet. When the draft is ready for the reviewer the run
suspends and the response is the suspension handle:

```json
{"status": "awaiting_human", "thread_id": "...", "hitl_metadata": {}, "correlation_id": "..."}
```

The client must keep `thread_id` and echo it back to `POST /resume/{thread_id}`. `confirm` sends the
draft to stage 4, `refine` re-runs the drafting stage, and `discard` ends the run with every finding
section empty and `discarded_by_reviewer` recorded — the pinned versions, conditions and freeze
facts are kept, the drafted content is not.

Resume is an intervention in an existing session, so it requires the same entry authentication as
`/invoke` **plus** ownership of the checkpoint. An unknown thread and someone else's thread return
the same status, so the endpoint does not disclose whether a checkpoint exists. A session older than
`hitl_session_ttl_s` is deleted and returns 410 `hitl_expired`.

## Output

A successful response carries `status`, `correlation_id` and `output`. `output` always contains
`mode`, `gate_kind` and `meta` (`degraded`, `degradation_code`, `requires_human_review`), plus the
fields for the mode:

| Mode | Additional fields |
|---|---|
| `invoke` | `applied_pins`, `criteria_conditions`, `evidence_sources`, `freeze_window_facts`, `supported_conditions`, `evidence_gaps`, `contradictions`, `approval_scope_discrepancies`, `precedent_checks`, `verification_questions` |
| `ingest` | `ingest_summary` (`written_ids`, `rejected`) |
| `feedback` | `feedback_summary` |

`applied_pins` records exactly which criteria version and freeze calendar the review was judged
under — the six pin values, with the calendar entries null when none was pinned. Each finding
section has a fixed field set:

| Section | Fields |
|---|---|
| `supported_conditions` | `claim_id`, `description`, `condition_id`, `source_ids`, `severity`, `confidence` |
| `evidence_gaps` | `claim_id`, `description`, `condition_id`, `source_ids`, `gap_origin` |
| `contradictions` | `claim_id`, `description`, `condition_id`, `source_ids` |
| `approval_scope_discrepancies` | `claim_id`, `description`, `source_ids` |
| `verification_questions` | `claim_id`, `question`, `condition_id`, `source_ids` |

The key sets are closed on purpose. `approval_scope_discrepancies` in particular may not grow a
field, because a coverage-percentage or verdict key added there is precisely how a "here is what the
approval does not cover" list turns into "the approval is sufficient".

`meta.degradation_code` is one of `kb_empty`, `llm_unavailable`, `discarded_by_reviewer` or
`hitl_limit_reached`. An empty result and a failed one are never allowed to look alike.

An error response carries `status: "error"`, an `error_code` from the list above, and
`correlation_id` — never a provider message or a stack trace.

## Security and Limitations

Every node requires verified-external trust, so an unverified caller never reaches a node body.
Authentication and permission both complete before anything is written. Erasure is split into three
lineages — request data, past perspectives, audit — because past perspectives are not owned by any
one request and must not vanish when a request is deleted; deletion requires a recorded approval
reference and returns the audit event id that records it.

Free text that is stored is split three ways: a raw value that stays in the customer's database, an
audit value, and the only projection the model may ever see. Knowledge-base free text is a
persistent prompt-injection path — it rides into the next request's prompt — and masking alone
leaves the instruction sentence intact, so the model-visible projection is a separate, bounded
value rather than a filtered copy of the original.

Verbatim quotes are checked for fidelity and for non-conclusiveness, and the numbers written in the
prose are checked against the numbers in the cited span. The numeric extractor deliberately does
**not** require a word boundary: in Japanese the unit and particle sit flush against the digits
("30分以内", "約2時間半"), and a boundary-anchored pattern returns the empty set on such text — which
makes the guard pass on everything.

Known limits, recorded rather than hidden:

- **The shipped persistence is in-memory.** The framework's durable storage services are not yet
  available, so the database, knowledge base and checkpoint saver in this repository do not survive
  a restart and are not shared across processes. The properties a real backend must preserve are
  documented at the seam in `src/services/stores.py`, and the port contract tests for them are
  marked expected-to-fail until one is connected.
- **There is no partition key inside a deployment.** Isolation is the connection: one customer per
  deployment. A row-level tenant filter is deliberately not used.
- **Personal-name detection in Japanese is incomplete.** The framework detector covers Latin-script
  names and labelled Kanji/Katakana names; unlabelled names, Hiragana-only values and other label
  forms are not detected. The template does not paper over this with a hand-rolled dictionary; the
  mitigation is operational input discipline, recorded in the operation guide.
- **A recalled precedent is a fact, not an argument.** Past outcomes are shown to the board because
  they are legitimate material, but they are never given to the model as a reason: the recall does
  not key on outcome, and the outcome is copied into the output deterministically rather than
  written by the model.
- **Retention scheduling is out of scope.** This runtime does not enumerate expired rows; the
  retention periods belong to the surrounding data-lifecycle controller.

Output is machine-generated and assistive. It does not substitute for expert review, and the
approval decision belongs to the change advisory board.

## Release

- Version: 1.0.0
- Category: fixed multi-step domain workflow (Cat 2) with an inner custom-topology workflow graph
- Generation mode: `llm` — one model call, in the drafting stage only
- HITL: enabled — the run suspends for reviewer confirm / refine / discard, with an ownership-checked resume
- Persistence: in-memory database, knowledge base and checkpoint saver; the swap point is `src/services/stores.py`
- External API clients: an Anthropic client is constructed by the standalone adapter when a key is available; otherwise none

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest -q
ruff check src tests
```

Tests run without a platform connection and without a language model, using fakes. Running the agent
itself needs authentication configured — see *Behaviour without the platform* above.

## Documentation

`docs/02_design.md` carries the node flow, the state schema, the version-pinning and provenance
contracts, the data-governance rules and the security design. `docs/03_test_spec.md` records the
test specification and the verification results, including a live-model run and the contract defect
it exposed.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and test specifications
```

See `docs/02_design.md` for the design and `docs/03_test_spec.md` for the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.

---
