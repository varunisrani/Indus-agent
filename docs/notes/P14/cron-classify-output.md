# Task: result classification and output document (P14-12) Phase: 14 Owner: wave builder C2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `cron/scheduler.py` (the silence
and failure marker helpers, the run document header and framing, failure documents with the
redacted traceback, the bounded cleanup helper), `cron/delivery_outcome.py` (status split),
`gateway/response_filters.py` (`is_autonomous_silence_response`, for the matching rule only).
Closed before writing. Evidence: none.

What it does (own words):
- Failed = the run raised, timed out, the model could not be reached, or the reply's first line is
  exactly `[JOB_FAILED]` (the lines after it are the evidence). The token anywhere else is content.
- Silent = the whole reply, or its first line alone, or its last line alone, equals `[SILENT]`,
  `SILENT`, `NO_REPLY` or `NO REPLY` (case and surrounding spaces ignored). A token inside a
  sentence is content. Silent is a success with delivery suppressed; the document is still saved.
- A failed run is always delivered, even when its text also looks silent.
- Output document `<home>/cron/output/<job_id>/<UTC ts>.md`: a short header (job, run time,
  schedule, status), then the prompt and the response in length-framed sections
  (`--- prompt: <n> chars ---` followed by exactly n characters), and for a failure a
  redacted, chained traceback section. Newest `cron.output_retention` (50) are kept per job; ≤ 0
  keeps everything. The reader (`read_document`) parses the frames back (used by P14-10).
- Cleanup steps (pruning, agent teardown) run under a bound, `cron.cleanup_timeout_seconds` (10 s);
  a hung step is abandoned (logged) so it can never wedge the in-flight guard.

Inputs → outputs: reply text / exception → `Verdict`; job, prompt, response, status → document path.

Files: `src/indus/cron/classify.py`, `src/indus/cron/output.py`, `tests/unit/cron/test_classify.py`,
`tests/unit/cron/test_output.py`. Shared: `foundation/config/schema.py` (`cron.output_retention`,
`cron.cleanup_timeout_seconds`).

Edge cases / failure modes:
- empty reply → not silent (nothing to deliver; delivery skips empty payloads, C3).
- two runs in the same second → `-2`, `-3` suffixes, never an overwrite.
- document dir not writable → the run outcome still goes to bookkeeping with the write error.
- traceback containing a key → redacted with `force=True` even when log redaction is off.

Our design differences / decisions:
- Failure token `[JOB_FAILED]` (ours). Silence matcher: whole / first line / last line only (the
  reference also accepts a reply that merely starts with `[SILENT]`; ours treats that as content
  unless the token is alone on its line).
- Our own document layout and frame lines; file names `YYYYmmddTHHMMSSZ.md`.

Tests we will write:
- the token table from the phase file; failed+silent → delivered.
- 51st document prunes the oldest; retention 0 keeps all.
- traceback chain present and redacted; frames round-trip through the reader, including a
  response that quotes a frame line.
- hung cleanup abandoned at 10 s (fake clock).

## Decisions for the lead
- none.

Compare checklist (filled after the rewrite; reopened the marker helpers in `cron/scheduler.py`,
`gateway/response_filters.py::is_autonomous_silence_response`, the run document writer and
`format_run_error`, `_run_cron_cleanup_with_timeout`):
- [x] failure token: exact first line only, evidence after it
  (`tests/unit/cron/test_classify.py::test_failure_token_on_the_first_line_fails_the_run`,
  `::test_failure_token_elsewhere_is_content`)
- [x] silence: whole / first line / last line, case-insensitive, four forms
  (`::test_silent_forms`, `::test_content_is_not_silent`)
- [x] failed + silent → delivered (`::test_failed_and_silent_is_delivered`)
- [x] length frames survive quoted frames; truncated sections rejected
  (`tests/unit/cron/test_output.py::test_document_path_and_round_trip`,
  `::test_truncated_section_is_not_trusted`)
- [x] retention 50, ≤ 0 keeps all (`::test_fifty_first_document_prunes_the_oldest`,
  `::test_retention_zero_keeps_everything`)
- [x] chained, redacted traceback (`::test_traceback_is_chained_and_redacted`)
- [x] cleanup bound 10 s, ≤ 0 inline (`::test_hung_cleanup_is_abandoned_at_ten_seconds`,
  `::test_hung_async_cleanup_is_cancelled`)
- Deliberate differences: no prefix rule (`[SILENT] no changes` is content, see the note above);
  leading spaces before the failure token are tolerated; our document layout.
- Not taken: the delivery-queue outcome write-back (`delivery_outcome.py`) — delivery status is
  C3's (P14-13) and P15's queue.

## Status

Done (2026-10-19). `src/indus/cron/classify.py` (with `RunOutcome`, the record handed to
delivery and bookkeeping) and `src/indus/cron/output.py`.
