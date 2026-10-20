# Task: `session/list` (P19-07) Phase: 19 Owner: wave-29 builder A1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `acp_adapter/server.py`
(`list_sessions`, page size), `acp_adapter/session.py` (`_normalize_cwd_for_compare`,
`list_sessions`, `_session_info`, `_build_session_title`, `_format_updated_at`),
`acp_adapter/catalog.py` (`catalog_sessions`, one row per logical conversation). Our
`state/listing.list_sessions`, `state/browse.open_readonly`. Closed before writing.
Evidence: none.

What it does (own words): the editor's history panel. We read at most 1,000 `acp`-source
conversations from the store through a read-only connection (both host modes), skip empty ones,
represent each logical conversation once (a compressed conversation by its root id, with its tip's
title and activity), filter by the editor's cwd when it sends one, order by last activity (newest
first), and page by 50: the `cursor` is the last session id of the previous page; an unknown cursor
gives an empty page (never the first page again, R09.16); `nextCursor` is present only when more
rows remain.

Cwd comparison: both sides are expanded (`~`), translated (a Windows drive path → `/mnt/<drive>/…`,
a `/mnt/X/` drive letter lowered) and resolved with `realpath`, so `/private/var/x` matches
`/var/x` and `/private/tmp` matches `/tmp` on macOS; a missing path compares lexically.

Inputs → outputs: `session/list {cwd?, cursor?}` → `{sessions: [{sessionId, cwd, title,
updatedAt}], nextCursor?}`.

Files: `src/indus/acp/listing.py`, `tests/unit/acp/test_listing.py`.

Vendor names used (0.12.1 / v1.19.0): `session/list` (stable in this schema; unstable in the
reference's pin), `ListSessionsRequest{cwd, cursor}`, `ListSessionsResponse{sessions, nextCursor}`,
`SessionInfo{sessionId, cwd, title, updatedAt}`.

Edge cases / failure modes:
- No store yet → empty list.
- Title: stored title, else the first user message preview, else the folder name, else
  "New conversation".
- `updatedAt`: ISO 8601 UTC.

Tests we will write:
- 120 rows → pages 50/50/20 with cursors; unknown cursor → `[]`;
- `/private/var/x` matches `/var/x` (symlinked temp dirs); Windows drive form matches its mount;
- non-ACP rows excluded; empty rows excluded.

## Decisions for the lead
- none.

Compare checklist (filled after the rewrite; reopened `server.list_sessions`, `session.py`,
`catalog.py`):
- [x] paging, cursor, unknown cursor
- [x] normalisation
- [x] logical id per conversation
- Deliberate differences: none intended.

## Status

Done (2026-10-20), wave-29 builder A1. Tests: see the task's Verify line; all green with the gate checks. Trap/ACC ledger rows for R09.9/R09.13/R09.14/R09.16 and ACC-M1/M2/M8 left to the lead (follow-up).
