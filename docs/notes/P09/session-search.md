# Task: `session_search` tool (P09-25, F31) Phase: 09 Owner: P09 slice A builder

Read: research `02-tools.md` §3.5 (tool row: discover/scroll/read/browse) and `03-state.md` §3.13
(search behaviour). No reference source file is named for this task; none opened.
Evidence: none.

What it does (own words): the agent looks up this profile's earlier conversations instead of asking
the user to repeat themselves. One tool, four actions:
- `discover {query, sort: rank|newest, limit}` — full-text search over stored messages through the
  P02-15 search (`state.search.search`), 10 hits by default (25 at most), each with session id,
  message id, role, time, the session's title and the marked snippet;
- `scroll` — the next page of the last `discover` in this session (or of an explicit `query` +
  `offset`);
- `read {session_id, message_id?, window?}` — the messages around a hit (or from the start), at
  most 40, tool outputs clipped to 1,000 characters each;
- `browse {limit}` — recent conversations with titles and previews from the P02-09 listing.
The current conversation (its whole compression lineage) and hidden sources (`cron`, `subagent`)
are left out unless the call asks (`include_current`, `include_hidden`). Every text that goes out
passes the redactor (`safety.redaction.redact_output`, forced). One answer is at most 12,000
characters; past that, items are dropped from the end and the answer says how to continue
(`next_offset` / `continue_from`). With `session_search.summarize` on and an auxiliary client
present, a `read` window that does not fit is condensed by the side-task model (task
`session_search`) instead of cut; on any aux failure the cut answer is returned.

Inputs → outputs: tool arguments + the session's store handle and id → JSON text.

Files: `src/indus/learning/session_search.py`, `tests/unit/learning/test_session_search.py`.
Shared: `src/indus/tools/toolsets.py` (toolset + core list, with P09-02), config row
`session_search.summarize`.

Edge cases / failure modes:
- empty or blank query → refused with a hint; a query of only specials → no hits (sanitiser);
- unknown session id, or a session that is hidden / part of the current lineage without the flag
  → refused with a hint, nothing else read;
- `message_id` not in that session → refused;
- no store (in-memory sessions) → the tool says search is unavailable here;
- a store error → reported as a tool error, never raised into the loop;
- lineage lookup ambiguous (two live continuations) → only the current id is excluded.

Our design differences / decisions:
- the scroll state is per session in the wiring (the last discover); an explicit `offset` works too;
- `read` without `message_id` starts at the first message (`offset` to move on);
- limits: discover 10 default / 25 max, browse 10 default / 25 max, read window 20 default / 40 max.

Tests we will write:
- discover hits exclude the current lineage (root and continuation) and include it on request;
- cron and subagent sessions hidden unless `include_hidden`;
- scroll returns the next page without repeats;
- `read` window bounded at 40 around a hit; tool output clipped to 1,000 chars;
- a secret in an old message comes back redacted (snippet and read);
- 12,000-char cap with a continuation hint;
- browse lists recent sessions with titles and previews, current lineage left out;
- summarize on → aux called with task `session_search`; aux failure → cut answer.

## Decisions for the lead
- Hidden sources are `cron` and `subagent` (the same pair the trigram index leaves out); `kanban`,
  `tool` and `oneshot` sessions are also kept out of `browse` by the P02 listing's internal-source
  rule unless `include_hidden` is set.

Compare checklist (research rows only; no reference file named):
- [x] four actions with the documented shapes (tests: `test_session_search.py`)
- [x] lineage + hidden sources excluded by default (test: `test_session_search.py::test_discover_excludes_current_lineage`, `test_hidden_sources`)
- [x] redaction on every text (test: `test_session_search.py::test_secret_is_redacted`)
- Deliberate differences: none recorded beyond the limits above.

## Status

Done (2026-10-19).

## Integration (lead, P09 wave 1 landing, 2026-10-19)

Compare at integration: no reference file is named for this task, so there was nothing
to reopen; the research rows were rechecked. Host wiring done (inline executor only when selected;
the bound auxiliary client is handed over after `Agent.start`). The `AuxCaller` protocol now takes
`Sequence[dict[str, Any]]` so the real `AuxClient` satisfies it. Limits recorded as D-16.148.
