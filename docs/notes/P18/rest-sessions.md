# Task: REST: sessions (P18-11) Phase: 18 Owner: wave-30 builder R1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/web_routers/sessions.py`
(route list, list filters and 503 handling, search lineage dedup, stats, mutation snapshot, delete
fence and idempotent absent delete, rename, export, prune with dry run, bulk and empty delete).
`web/src/pages/SessionsPage.tsx` was not reopened: F1's built page (`w29-f1.patch`:
`apps/web/src/lib/api.ts`, `restTypes.ts`, `testing/mockApi.ts`, `pages/sessions/sources.ts`,
notes `rest-client.md`, `page-sessions.md`) is the client this task serves. Research 09 §3.2/§3.4,
traps R09.21, R03.16, R03.22. Our `state/{browse,listing,search,deletion,maintenance,lineage,
portability,titles,sessions,runtime_ledger}.py`, `service/maintenance_gate.py`,
`cli/commands/sessions.py` (read-view pattern), `web/{scope,state}.py`. Reference closed before
writing. Evidence: none.

What it does (own words): the Sessions page's backend. Every read opens the profile's store through
the read-only browse path (`mode=ro`, no schema set-up, migration, checkpoint or creation): a
missing store is an empty list, a store that is busy, never set up, newer than us or damaged is a
503 with a code, never a 500 and never an empty page that looks authoritative (R03.16). Writes come
in three kinds:

- single-session changes (rename/archive/pin with PATCH, delete) carry a *mutation identity* read
  from the snapshot endpoint: a request id, the revision the client saw and the turn generation it
  saw. No identity → 409 `client_upgrade_required` (an old client); part of one → 409
  `mutation_identity_required`; a revision or generation that moved → 409 `stale_revision`.
  A delete also refuses a session with a turn queued or running (409 `session_busy`) or a turn
  lost in a restart (409 `unknown_execution`). Deletes leave the store's user tombstone, so the
  same delete retried after it succeeded finds the id absent and answers 200 `already_absent`
  (R03.22). The delete runs where the maintenance gate says: inside the daemon when one owns the
  profile, else here under the profile's ownership lock.
- bulk maintenance (bulk delete, delete-empty, non-dry prune) runs only while no daemon owns the
  profile: a daemon answering its control socket, or the ownership lock held, is 409
  `daemon_running` and nothing is written (R09.21). A dry-run prune is a read and always works.

Inputs → outputs (all under `?profile=`; reads use `read_scope`, writes `write_scope`):
- `GET /api/sessions` `source | sources | exclude_sources | tab (chats|automation|all) | order
  (created|recent) | archived (exclude|only|include) | limit (0..100, 50) | offset` →
  `{sessions: [SessionInfo], total, limit, offset}`.
- `GET /api/sessions/search?q=&limit=` → `{results: [{session_id, title, source, snippet, role,
  last_active}]}`; snippets mark matches with `<mark>`…`</mark>`.
- `GET /api/sessions/stats` → `{total, active, archived, messages, by_source}`.
- `GET /api/sessions/{id}` → `SessionInfo`; `/messages?limit=1..500&offset&order=oldest|latest`
  (default the latest 500) → `{session_id, messages, pagination}`; `/export` → JSON export objects;
  `/mutation-snapshot` → `{session_id, exists, revision, generation}`.
- `PATCH /api/sessions/{id}` `{title?, archived?, pinned?, request_id, expected_revision,
  expected_generation}` → `SessionInfo`.
- `DELETE /api/sessions/{id}?request_id&expected_revision&expected_generation` → `{ok, session_id,
  deleted, already_absent}`.
- `POST /api/sessions/prune` `{older_than_days >= 1, dry_run}` → `{dry_run, count, skipped_open}`.
- `POST /api/sessions/bulk-delete` `{ids (1..500)}` → `{ok, deleted, skipped_active}`.
- `DELETE /api/sessions/empty` → `{ok, deleted}`.
- Errors: FastAPI `{detail: {code, message}}` (what F1's client reads).

Files: `src/indus/web/routes/sessions.py` (routes), `src/indus/web/routes/sessions_store.py` (read
view, row shaping, snapshot and fence checks, error mapping), `tests/contract/web/test_sessions_routes.py`,
`tests/contract/web/rest_app.py` (app + scope builder, shared by P18-11..16 since integration),
`tests/fixtures/store/build_dashboard.py` (store fixture); shared: one line in
`src/indus/web/routes/__init__.py`, `state/listing.py` (`order="activity"`, `count_by_source`).

Edge cases / failure modes:
- `limit=501` on messages → 422 (FastAPI bound), `limit=101` on the list → 422.
- Store file missing → empty list / zero stats / 404 for one session / snapshot `exists: false`;
  nothing is created (store mtime and directory listing unchanged after every read).
- `uninitialised` / `incompatible` store → 503 `store_unavailable`; a lock or I/O error → 503
  `store_busy`; a damaged file → 503 `store_corrupt`.
- Snapshot of an absent id: revision 0, generation null; a delete of it with request id + revision
  0 is the idempotent 200.
- Search on a damaged index would need a write (detach); the read-only view refuses it → 503.
- Compression chains: the list shows one row per conversation (listing does it); messages read the
  whole lineage up to the requested segment so the history is not cut at a rotation; delete takes
  the whole chain (`whole_chain=True`, as the gate's daemon op does).
- A route path segment literally `stats`, `search`, `prune`, `bulk-delete` or `empty` cannot be a
  session id here (static routes first); our ids are generated, never those words.

Our design differences / decisions:
- Revision is computed, not stored: a 31-bit CRC of the session row's user-visible fields (title,
  flags, counters, end, activity). Any change to the row moves it; 0 means absent.
- Generation is the runtime ledger's per-session counter (`runtime_generations`, 0 before the first
  turn).
- `stale_revision` is our code for a moved snapshot (the client re-reads and asks again).
- Single delete uses the maintenance gate (daemon op `delete` when a daemon owns the profile); the
  identity and busy checks run on a read-only view just before, so a turn starting in between is
  caught only by the delete's own lease guard (`SessionGuarded` → `session_busy`).
- Prune requires `older_than_days >= 1` (0 would sweep every ended session).
- Search dedups by session id (not by lineage root) and fills spare slots with title / first-ask
  matches from the listing query.

Tests we will write (store fixture with sessions of every source, a busy session, an unknown-turn
session): list filters/tabs/order/paging/total; `<mark>` snippet; stats; messages paging + 422;
export; snapshot; PATCH identity rules + rename; delete: no identity / partial / stale / busy /
unknown / success then retry 200 / tombstone blocks recreate; prune dry run 200 and non-dry with the
ownership lock held → 409 and with a daemon answering → 409; bulk delete and empty delete 409 the same
way and work when stopped; missing store creates nothing; store mtime unchanged after reads;
uninitialised store → 503; busy connect → 503; every write route depends on `write_scope`; the route
list constant equals the app's routes.

Route list (`{method, path, scoped}`, the shape of F1's route list; `ROUTE_LIST` in
`routes/sessions.py`, pinned by a test):

```json
[
  {"method": "GET", "path": "/api/sessions", "scoped": true},
  {"method": "GET", "path": "/api/sessions/search", "scoped": true},
  {"method": "GET", "path": "/api/sessions/stats", "scoped": true},
  {"method": "POST", "path": "/api/sessions/prune", "scoped": true},
  {"method": "POST", "path": "/api/sessions/bulk-delete", "scoped": true},
  {"method": "DELETE", "path": "/api/sessions/empty", "scoped": true},
  {"method": "GET", "path": "/api/sessions/{session_id}", "scoped": true},
  {"method": "GET", "path": "/api/sessions/{session_id}/messages", "scoped": true},
  {"method": "GET", "path": "/api/sessions/{session_id}/export", "scoped": true},
  {"method": "GET", "path": "/api/sessions/{session_id}/mutation-snapshot", "scoped": true},
  {"method": "PATCH", "path": "/api/sessions/{session_id}", "scoped": true},
  {"method": "DELETE", "path": "/api/sessions/{session_id}", "scoped": true}
]
```

## Decisions for the lead
- Shared edit in `state/listing.py`: `list_sessions(..., order="activity")` (newest activity stamp,
  else start) and `count_by_source(store, filters)`. The activity order uses the row's own stamp,
  not the chain tip's; a rotated conversation whose root has an old stamp can sort low until the
  listing grows a tip-aware order.
- Revision = CRC of the row (no stored counter); generation = runtime ledger counter.
- Single delete with a live daemon runs through `maintenance-run delete` (the gate's existing op);
  the fence check is read-side only. A daemon-side fenced delete verb is a follow-up.
- `skipped_open` = open (never ended) sessions quiet past the cutoff + ended ones spared because a
  live lease holds them.
- Error body is `{detail: {code, message}}`; `web/scope.py`'s 404/400 use `{detail: {error,
  message}}`, which F1's client reads as message-only (no code). Integrator: add `code` there.
- Found in W2's `web/scope.py`: `unscoped_write_routes(app)` only looks at `app.routes`, and
  FastAPI 0.142 wraps included routers (`_IncludedRouter`), so for routers added through
  `ROUTERS` it finds nothing and passes vacuously. Our tests use their own walker
  (`tests/contract/web/rest_app.py::api_routes`, which looks into `original_router`); the
  W2 helper should do the same.
- Gate seam for tests: `dashboard_state(app).extras["sessions.gate"]` = callable(scope) →
  `MaintenanceGate`; default the real gate.

Compare checklist (filled after the rewrite; reopened `hermes_cli/web_routers/sessions.py`):
- [x] list filters, archived tri-state, paging, total; busy store 503, unreadable/corrupt 503
  (tests: `tests/contract/web/test_sessions_routes.py::test_list_tabs_sources_order_and_paging`,
  `::test_a_busy_store_is_503`, `::test_an_uninitialised_store_is_503`)
- [x] list `limit` capped at 100 (test: `::test_list_limit_bounds`)
- [x] search: empty query → no results; one hit per conversation; title/id matches fill the
  remaining slots (gap closed: the reference adds a title lane because the index covers messages
  only) (tests: `::test_search_marks_matches`, `::test_search_falls_back_to_titles`)
- [x] stats fields (test: `::test_stats`)
- [x] messages: default latest 500; an explicit page without `order` counts from the start (gap
  closed: first version always anchored at the latest page); compression ancestors included
  (tests: `::test_one_session_messages_and_export`, `::test_a_compressed_conversation_reads_as_one`)
- [x] snapshot of an absent id: revision 0, generation null (test: `::test_a_missing_store_is_empty_and_never_created`)
- [x] delete fence codes; absent id is an idempotent 200; tombstone keeps the id dead
  (tests: `::test_delete_fence_codes`, `::test_delete_then_retry_is_idempotent`)
- [x] delete with a live daemon runs in the daemon (test: `::test_delete_runs_inside_a_live_daemon`)
- [x] rename/archive under the identity (test: `::test_rename_and_archive_under_the_identity`)
- [x] prune dry run is a read; non-dry, bulk and empty delete are refused while the daemon runs
  (tests: `::test_prune_dry_run_then_real`, `::test_bulk_maintenance_refuses_while_the_daemon_holds_the_lock`,
  `::test_bulk_maintenance_refuses_while_a_daemon_answers`, `::test_bulk_and_empty_delete_when_stopped`)
- [x] reads never write (test: `::test_reads_never_write_the_store`)
- Deliberate differences: snapshot fields are `revision`/`generation` (F1's contract, not the
  reference's names); `older_than_days < 1` and > 500 ids are 422 (validation) instead of 400;
  the prune answer is F1's `{dry_run, count, skipped_open}` (no candidate list or date range);
  export is our portability objects in one JSON body (re-importable with `indus sessions import`),
  capped by `sessions.resume_message_limit` (413), instead of a streamed document; a dry-run prune
  needs `?profile=` on a multi-profile host (every write route depends on `write_scope`); `hidden`
  and `unread` are not patchable (the page does not use them).
- Not taken: import, owner backfill, latest-descendant, timeline and messages-around routes,
  empty-count, `full`/`min_messages`/`cwd_prefix` list options, inline images, cron scheduler
  ownership stamps (no page here uses them); prefix wildcards in search (our search module routes
  short partial words through its trigram index instead).

## Status

Done (2026-10-20). Shared: `src/indus/web/routes/__init__.py` (one ROUTERS line),
`src/indus/state/listing.py` (`order`, `count_by_source`), traps R03.16/R03.22/R09.21 lines,
ACC-L3-1 lines. Wiring owed by the integrator: `install(app, ProfileScopes(scope))` in
`create_app` (P18-10), else every route here answers 500.

Integration (P18 REST integrator, 2026-10-20): `create_app` installs the profile resolver itself
(P18 W2 integration), so the builder no longer does; W2's `scope.py` bodies carry `code` and
`unscoped_write_routes` walks included routers (both fixed at the P18 landing, D-16.593); the
real app's unscoped writes are now pinned as one list (`tests/contract/web/test_route_list.py::
test_every_other_write_route_needs_a_profile`). Decisions numbered D-16.610.
