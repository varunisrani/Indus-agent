# Task: REST: status, logs, analytics (P18-13) Phase: 18 Owner: wave-30 builder R2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/web_routers/status.py`
(the `/api/status` payload, the public-projection rules, component health, the advisory pressure
blocks, the `/api/logs` handler at the end), `hermes_cli/web_routers/analytics.py` (route list,
`/api/analytics/usage` day grouping and its `days` clamp). F1's client and notes from
`w29-f1.patch` (`docs/notes/P18/{rest-client,page-logs-files}.md`, `lib/restTypes.ts`,
`testing/mockApi.ts`). Research 09 §3.4 (Status shape). Our `foundation/{log,redact}.py`,
`cli/commands/logs.py` (filter rules; not importable from `web`), `state/{browse,usage,schema,
gateway_runtime}.py`, R1's REST conventions (`w30-r1.patch`). Reference closed before writing.
Evidence: none.

What it does (own words): three read-only routes for the status strip, the Logs page and the
Analytics page.

- `GET /api/status` is on the shared public list (anyone who can reach the port may call it), so
  it carries only counts, enums and coarse numbers: the version; the daemon's state (its own word
  from `gateway_state.json`; `stopped` when there is no record or its process is gone; `unknown`
  when the heartbeat is older than 150 s, dated in the future or unreadable) and the heartbeat age
  in whole seconds; the platforms the daemon reports as `{name, state}`; `active_sessions` (of the
  20 most recently active sessions, the open ones active in the last 5 minutes) and
  `recent_sessions` (how many that window held); `auth_required` and `auth_providers`; health
  components (`store`, `daemon`, `platforms`) with an `overall` roll-up; memory and disk
  pressure. Pressure: memory `critical` below 64 MiB or 5 % available, `elevated` below 128 MiB or
  15 %; disk `critical` when free < 256 MiB or usage ≥ 95 % with < 1 GiB free, `elevated` when
  free < 512 MiB or usage ≥ 85 % with < 4 GiB free; else `ok`. Every collector is fail-safe: an
  error gives `{pressure: "unknown"}` (or the component's `unknown`), never a 500. The store is
  opened read-only and never created. No paths, pids, hosts, session ids, titles or error texts.
- `GET /api/logs?file=&lines=&level=&component=`: tail of `agent`, `errors` or `gateway` log of
  the profile; `lines` one of 50/100/200/500 (default 100); `level` = minimum severity
  (`DEBUG`..`ERROR`, `ALL`/absent = everything); `component` = logger prefix group (`agent`,
  `tools`, `gateway`, `cli`, `cron`, `all`/absent). A continuation line (traceback) shares its
  record's verdict. Every line goes through the profile's redactor (forced on) before it leaves.
  A missing file is an empty list.
- `GET /api/analytics/usage?days=` (default 30, 1..365; the page offers 7/30/90): per calendar
  day (the clock's zone) input tokens, output tokens, cost (billed where known, else estimated)
  and sessions started, for the days that have any; plus totals. Side calls (title, compression,
  vision…) in `model_usage` count toward the day their session started. Read-only store; a
  missing store is an empty answer; a busy or unreadable store is 503.

Inputs → outputs: query params → JSON; reads `read_scope` only; errors `{detail: {code, message}}`.

Files: `src/indus/web/routes/{status,logs,analytics}.py`, `tests/contract/web/test_status_routes.py`,
`tests/contract/web/test_logs_analytics_routes.py`, `tests/contract/web/rest_app.py` (the REST test
app builder, one module for P18-11..16 since integration); shared: ROUTERS lines, trap/ACC lines.

Edge cases / failure modes:
- Pressure exactly at a threshold (64 MiB, 5 %, 85 %/4 GiB, …): the comparison side is pinned by
  table tests.
- Status file: missing, unreadable JSON, unknown state word, `updated_at` 151 s old, 1 s in the
  future, dead pid → each mapped as above.
- Store missing (fresh install) → `active_sessions: 0`, store component `missing`, no file created.
- A collector raising → that block `unknown`, the rest still answered.
- Logs: bad `file`/`lines`/`level`/`component` → 400 with a code; huge file → only the last 8 MiB
  read; non-UTF-8 bytes replaced.
- Analytics: `days=0` / `400` → 422; sessions with NULL costs; archived and hidden sessions count
  (they were spent).

Our design differences / decisions:
- One payload of our own shape (see "Decisions"), not the reference's long list of gateway
  fields; no install id, curator, portal, profile topology or host paths (never public, even on
  loopback: the dashboard has other routes for them).
- Logs search (`search=`) not taken: the page has no search box.
- Analytics: only the usage route; per-model analytics are not on a 1.0 page.

Tests we will write: pressure table at every edge (memory and disk); collector raising →
`unknown`; status without a token (loopback) and without a cookie (gated) answers 200 and its body
has no path or pid; daemon state table (missing, stale 151 s, future-dated, running, stopped);
active-session count; a fresh home stays without a store after a status call; log lines carrying
a key are redacted; level/component/tail filters and continuation lines; bad params → 400;
analytics sums match a store built through the P02 writers; route list pinned.

## Decisions for the lead
- Status shape: `{version, daemon: {state, heartbeat_age_s}, platforms: [{name, state}],
  active_sessions, recent_sessions, auth_required, auth_providers, components: {store, daemon,
  platforms}, overall, memory: {pressure, available_percent}, disk: {pressure, used_percent,
  free_gb}}`. F1's restTypes has no status type yet; P18-25 should read this one.
- "recent 20" read as: the active count is taken over the 20 most recently active sessions, and
  that window's size is reported as `recent_sessions` (a count; the public probe never lists
  sessions).
- MB/GB in the thresholds are binary (MiB/GiB).
- Daemon `stopped` (not `unknown`) when the status record names a process that is gone.
- Memory is read from `/proc/meminfo` (`MemAvailable`/`MemTotal`); without it (macOS, Windows)
  psutil is used when installed, else `unknown`. A cgroup limit is not consulted.
- `/api/status` takes `?profile=` (unknown → 404) like every scoped route.
- Analytics `days` accepts 1..365 (422 outside), the page sends 7/30/90; days are calendar days in
  the clock's zone (the server's local zone in production).
- Components: `daemon` is `ok` while running or draining, `down` when stopped, `degraded` while
  starting/stopping/degraded, `unknown` when unknown; `platforms` leaves out paused ones; `store`
  is `ok` (a missing store is fine), `busy`, `corrupt` or `unavailable`; `overall` is `ok` only
  when all three are.

Compare checklist (filled after the rewrite; reopened `status.py` (payload, component health,
pressure, logs handler) and `analytics.py` (usage route)):
- [x] public payload carries no paths, pids or host details (test: `tests/contract/web/test_status_routes.py::test_status_is_public_and_carries_no_paths_or_pids`)
- [x] reachable without a credential in both gate modes (test: `tests/contract/web/test_status_routes.py::test_status_is_public_in_gated_mode_too`)
- [x] collectors fail-safe (test: `tests/contract/web/test_status_routes.py::test_a_raising_collector_reads_unknown`)
- [x] active sessions: open and active within 5 min (test: `tests/contract/web/test_status_routes.py::test_active_sessions_counts_open_recent_sessions`)
- [x] status never creates the store (test: `tests/contract/web/test_status_routes.py::test_status_on_a_fresh_home_creates_no_store`)
- [x] gap closed: a draining daemon is healthy, a deliberately paused platform never degrades
  the roll-up, a damaged store reads `corrupt` (test: `tests/contract/web/test_status_routes.py::test_components_roll_up`)
- [x] logs: unknown file / component → 400; ALL means no filter; missing file → empty (tests:
  `tests/contract/web/test_logs_analytics_routes.py::test_bad_parameters_are_400`,
  `::test_level_component_and_tail_filters`, `::test_other_files_and_a_missing_file`)
- [x] analytics: day grouping, totals, `days` 1..365 (tests: `tests/contract/web/test_logs_analytics_routes.py::test_usage_sums_match_the_store`, `::test_usage_days_out_of_range_is_422`)
- Deliberate differences: our payload shape (above); thresholds and heartbeat limit from the row;
  side calls folded into their session's day instead of separate per-model/per-task tables; logs
  are redacted line by line on the way out (the reference relies on redaction at write time).
- Not taken: the gateway multiplexer topology, install id, update/config-version fields, curator,
  portal, learning graph, ops and system-stats routes of the same module (other rows or dropped),
  the logs `search` parameter, `/api/analytics/models`, FTS rebuild progress.

## Status

Done (2026-10-20). New: `src/indus/web/routes/{status,logs,analytics}.py`,
`tests/contract/web/{test_status_routes,test_logs_analytics_routes}.py` (builder: `rest_app.py`). Shared:
`src/indus/web/routes/__init__.py` (ROUTERS lines), trap R09.4 lines, ACC-L6-1 lines.

Integration (P18 REST integrator, 2026-10-20): applied with P18-11/12 and P18-15/16; the three
test app builders are one module, `tests/contract/web/rest_app.py`. The status shape is in F1's
client as `StatusResponse` with `api.status()` (`apps/web/src/lib/restTypes.ts`), for P18-25 to
read. Decisions numbered D-16.612 (status), D-16.613 (logs / analytics).
