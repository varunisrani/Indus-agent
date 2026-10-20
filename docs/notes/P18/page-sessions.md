# Task: Sessions page (P18-19) Phase: 18 Owner: builder F1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `web/src/pages/SessionsPage.tsx`
(filters, snippet marks, scroll-to-hit, prune dialog, delete flow, resume link: skimmed by search),
`web/src/pages/SessionsPage_sources.tsx` (automation source list), `web/src/lib/session-prune.ts`,
`web/src/lib/session-mutations.ts`. Research spec 09 §3.2 (Sessions page) and §3.4. Evidence: none.

What it does (own words): lists the selected profile's sessions with three tabs: Chats (everything
except automation sources), Automation (cron, tool, one-shot, API server, ACP) and All, plus an
exact-source dropdown. A search box runs the full-text search and shows each hit's snippet with the
matched words marked; opening a hit scrolls the first mark into view. A stats bar shows totals and
per-source counts. Expanding a session loads its history and renders it as Markdown-ish text with
each tool call in a collapsible block. Inline rename, JSON export (a download), prune older than N
days (always a dry run first that shows how many would go, then a confirm), delete (fetch the
mutation snapshot, send its identity, and turn each 409 code into a plain sentence) and a play
button that opens `/chat?resume=<id>`.

Inputs → outputs: `GET /api/sessions`, `/api/sessions/search`, `/api/sessions/stats`,
`/api/sessions/:id/messages`, `/mutation-snapshot`, `PATCH`/`DELETE /api/sessions/:id`,
`/export`, `POST /api/sessions/prune` → list, detail, toasts, navigation.

Files: `apps/web/src/pages/SessionsPage.tsx`, `apps/web/src/pages/sessions/SessionDetail.tsx`,
`apps/web/src/pages/sessions/sources.ts`, `apps/web/src/pages/SessionsPage.test.tsx`.

Edge cases / failure modes:
- 409 codes: `session_busy`, `unknown_execution`, `mutation_identity_required`,
  `client_upgrade_required`, `daemon_running` (bulk maintenance) → each its own sentence; others →
  the server's message.
- Snapshot says the session no longer exists → delete still sends request id + revision.
- Search snippets carry markers from the server (`<mark>`-style tokens); we never inject HTML: the
  snippet is split on the markers and rendered as React nodes.
- Prune with a non-number or negative day count → refused client-side.

Our design differences / decisions:
- Snippet markers: the client accepts `<mark>…</mark>` pairs in the snippet text and splits on them
  itself (no HTML injection); P18-11 must emit exactly those markers.
- Markdown view is a light renderer of our own (headings, code fences, paragraphs) — no Markdown
  library (none named in the row).

Tests we will write:
- Each 409 code shows its message; prune confirm shows the dry-run count; resume navigates with the
  id; snippet highlight rendered as `<mark>`; tabs send the right source filters; rename patches.

## Decisions for the lead
- Prune body `{older_than_days, dry_run}` → `{removed | would_remove, skipped_open}`; P18-11 to match.

Compare checklist (filled after the rewrite; reopened `web/src/pages/SessionsPage.tsx` by search):
- [x] each fenced-delete 409 has its own sentence (test: `apps/web/src/pages/SessionsPage.test.tsx::explains a 409`)
- [x] snapshot identity on delete and rename (tests: `SessionsPage.test.tsx::sends the snapshot identity and removes the row`, `::renames with the mutation identity`)
- [x] snippet marks and scroll to first hit (test: `SessionsPage.test.tsx::renders search snippets with marks and scrolls the opened hit to its first match`)
- [x] resume link keeps the profile (test: `SessionsPage.test.tsx::resumes a session in Chat with its id`)
- Deliberate differences: prune always runs a dry run first and shows its count (the reference prunes directly).
- Not taken: empty-session bulk delete, owner backfill, import (not in the row).

## Status

Done (2026-10-20). Route shapes listed in the report for P18-11.
