# Task: output schema validation (P10-04) Phase: 10 Owner: P10 slice G builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/delegation_output_schema.py`
(all). Closed before writing.
Evidence: none.

What it does (own words): a task may carry `output_schema` (JSON Schema, checked with
`jsonschema`). Before any child starts, every schema is meta-checked; a bad one fails the whole
call. A child with a schema is told up front to end with one JSON value matching it. The parent
takes the last JSON object (or array) from the child's final text — a fenced block counts — and
validates it; on failure it sends the same child one correction message listing the errors (the
schema is not repeated) and checks the new answer. Still invalid → the task is `completed` with
`schema_valid: false`, at most 10 errors of at most 200 chars each, the raw text kept as summary,
and a note; output is never thrown away.

Inputs → outputs: schema + child final text → `SchemaCheck(valid, errors, value)`.

Files: `src/indus/core/subagents/output_schema.py`, `tests/unit/core/subagents/test_output_schema.py`;
shared: `pyproject.toml` (`jsonschema==4.26.0`, MIT; pulls attrs, referencing,
jsonschema-specifications, rpds-py — all MIT), `uv.lock` (regenerated here to test),
`THIRD_PARTY_NOTICES.md`.

Edge cases / failure modes:
- Several JSON objects in the text → the last top-level one wins.
- No JSON at all → one error "no JSON value found".
- Schema given as a JSON string → parsed; invalid schema → error before any child starts.

Our design differences / decisions:
- Draft picked by `$schema` when present, else 2020-12.

Tests we will write:
- valid first try; invalid then corrected; invalid twice → completed + schema_valid false + raw
  text; malformed schema rejected before spawn; fenced JSON extracted.

## Decisions for the lead
- New runtime dependency `jsonschema==4.26.0` (uploaded 2026-01-07, before exclude-newer); re-lock.

Compare checklist (reopened `tools/delegation_output_schema.py`):
- [x] meta-check before spawn (test: `tests/unit/core/subagents/test_output_schema.py::test_malformed_schema_is_rejected_before_any_child_starts`)
- [x] one correction round, schema not repeated (tests: `::test_invalid_then_corrected_after_one_retry`, `::test_contract_and_correction_texts`)
- [x] raw text kept, ≤ 10 errors ≤ 200 chars (tests: `::test_invalid_twice_keeps_the_raw_text`, `::test_check_answer_errors_are_bounded`)
- [x] fenced JSON (test: `::test_last_json_value_prefers_the_last_and_reads_fences`)
- Deliberate differences: the reference takes the outermost span; we take the last top-level
  value (the plan's "last JSON object"). The result field is `errors` (reference: `schema_errors`).

## Status

Done (2026-10-19). Shared: `pyproject.toml`, `uv.lock` (regenerated here; lead re-locks), `THIRD_PARTY_NOTICES.md`.
