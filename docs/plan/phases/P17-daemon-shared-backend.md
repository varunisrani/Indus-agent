# P17 · Daemon as shared backend

**Weeks:** 2.5 (calendar weeks 28–30, beside P16) · **Tracks:** backend (Python: service, gateway daemon, CLI), frontend (TypeScript: `@indus/client` WS transport, TUI attach) · **Depends on:** P15 (daemon process, control socket, admission pipeline), P12 (TUI, `tui_bridge`, `@indus/client`), P04 (`SessionService` interface, protocol package and codegen, server-request broker), P02 (store) · **Status:** planned

## Goal
The per-profile gateway daemon becomes the one host of `SessionService` for every surface (D-16.10,
R04.1). `indus` (classic chat, one-shots) and `indus --tui` attach to it by default over a loopback
WebSocket authenticated by a single-use ticket minted through the owner-only control socket; `--local`
keeps today's in-process mode. Inputs are admitted into a durable per-session FIFO; a cross-process
turn lease guarantees one writer per session; every control (interrupt, steer, approve) names the
generation it targets; events carry a sequence and a replay epoch so a client that reconnects asks
for "events since my cursor" and gets still-open server requests re-offered. A turn whose outcome is
unknown after a daemon restart is never resubmitted. When this phase ends, the CLI and the TUI can
attach to one daemon on the same session and each see the other's turns live, and every surface test
runs against both the in-process and the attached host. P18 (dashboard), P19 (ACP attach) and P20
(desktop) build on this transport.

No third-party protocol is introduced: the WS wire is our own JSON-RPC 2.0 contract from
`indus.protocol` (D-16.1, D-04.3), so there is no interop note. Subprotocol names follow the
glossary: `indus-gateway-v1` and `indus-gateway-ticket.<ticket>`.

## Tasks (27)
| ID | Task | Read in reference | New files | Verify |
|---|---|---|---|---|
| P17-01 | **Runtime ledger (store).** `RuntimeLedger` tables (our names, D-16.2): an owner-epoch row bumped on every daemon start (caller holds the profile ownership lock); admissions keyed by (principal, target session, request id) with a payload digest: an exact retry returns the existing row, a different payload for the same key is `admission_conflict`; at most 32 queued per session (`queue_full`); states `queued → started → terminal` (completed/failed/cancelled) or `unknown`; claim-head sets `started` with owner epoch and generation; steer moves a queued input into the running claim and can be reverted; replace-queued is atomic; on owner start every `started` row from an older epoch becomes `unknown` and is never replayed (R03.21); later inputs of that session wait until it is resolved. Retired ids leave a retirement marker so an exact retry of a deleted input does not resurrect it (R03.22). Refusal reasons are bounded codes plus a short detail with whitespace collapsed and non-printables dropped. All writes go through the P02 writer executor; time-budgeted retry (R03.8). | `hermes_state_runtime.py` [933: admission, claim, settle, unknown-on-start functions]; `hermes_state_runtime_workers.py` [skim]; research 03 §3.18 | `src/indus/state/runtime_ledger.py`, `tests/unit/state/test_runtime_ledger.py` | Exact retry returns the same row; changed payload → conflict; 33rd queued → `queue_full`; restart flips `started` to `unknown` and blocks the next queued row; retired id stays retired after an exact retry |
| P17-02 | **Admission service.** Service-level API over the ledger used by every surface and by messaging: `admit(principal, session, request_id, input) → Receipt {admission_id, state: queued/started/terminal/unknown, generation?, position?}`; a worker per session claims the head and runs the turn; `cancel(admission_id)`, `steer(admission_id, text)`, `discard_unknown(admission_id, expected_generation)` (operator resolution; generation checked, stale → `stale_generation`); `receipt(admission_id)` for clients that lost an ack. A terminal receipt on an exact retry lets the client replay the stored final text once instead of running again. Messaging users are told once per pause that the session is blocked behind an unknown turn (R06.19). | `gateway/session_admission.py` [8] and `gateway/session_retry_admission.py` [78: receipt kinds]; research 03 §3.18, 09 §3.11 step 3–5 | `src/indus/service/admissions.py`, `tests/unit/service/test_admissions.py` | Two concurrent admits of one request id → one row; discard with an old generation refused; terminal receipt replays text without a provider call (fake model call count 0) |
| P17-03 | **Durable turn lease.** One process at a time may load, run and flush a session: a lease row `{session_id, holder_id, holder_pid, holder_start_time, holder_ns, expires_at, generation}` in the store; TTL 300 s, refreshed every 60 s with a 20 s write patience and a 2 s expiry margin; a second holder waits up to 1,800 s (the user sees a "waiting for another run" status), and a follow-up that interrupts the wait is carried into the turn, not consumed; losing the lease (refresh fails past expiry) interrupts the turn with reason `lease_lost`. Holder liveness is pid + process start time + pid-namespace id (`/proc/self/ns/pid` inode where present, else host id), so a pid from another container never looks alive (R03.20). In-process leases capped at 512. Replaces the P02 per-session advisory file lock (D-16.69) for daemon-hosted sessions; `--local` processes take the same durable lease, so a local CLI and the daemon can never run one session together. | `agent/turn_facade_lease.py` [491: acquire, refresh, wait, loss]; `gateway/turn_lease.py` [179: in-process cap and wait]; research 01 §3.23 | `src/indus/state/turn_leases.py`, `src/indus/service/turn_lease.py`, `tests/unit/service/test_turn_lease.py`, `tests/integration/service/test_turn_lease_multiprocess.py` | Two processes contend: second waits, proceeds after release; SIGKILLed holder reclaimed after expiry; holder in another pid namespace (fake ns id) not treated as alive; refresh failure interrupts the turn with `lease_lost` (fake clock) |
| P17-04 | **Turn liveness watchdog.** Abort turns with no activity for 600 s (`agent.turn_liveness`; ≤0 disables), polled every 15 s. Activity = model deltas, tool heartbeats, approval waits counted as activity. The abort is bound to the observed (generation, last-activity timestamp) and revalidated under the activity lock before acting, so a turn that resumed in between is never cancelled. Emits `turn.done{reason: stalled}` through the normal single exit funnel. | `agent/turn_facade_lease.py` [491: skim]; research 01 §3.23 (`turn_liveness`) | `src/indus/service/turn_liveness.py`, `tests/unit/service/test_turn_liveness.py` | Fake clock: idle 600 s aborts; activity at 599 s resets; race where activity lands between observe and act → no abort; long approval wait not aborted |
| P17-05 | **Generation fences on controls.** Every control request names its target: `interrupt(session, generation)`, `steer(session, generation, text)`, `approval.respond(request_id, generation, choice)`, `clarify.respond(…)`, `stop(admission_id)`. A control whose generation is not the running one returns `stale_generation` with the current generation; it never falls through to "the latest turn", which may belong to another surface (R09.11 spirit, R01.4). Releases and settles are keyed by generation so a displaced turn cannot clear its successor (R06.2). | `gateway/session_controls.py` [791: control routing and generation checks]; `tui_gateway/methods_session_interrupt.py` [181] | `src/indus/service/fences.py`, `tests/unit/service/test_fences.py` | Interrupt with generation N−1 while N runs → `stale_generation`, N unaffected; approval answered for a withdrawn request is a no-op |
| P17-06 | **Event log and replay.** Every event emitted for a session carries `seq` (monotonic per session) and `replay_epoch` (random per daemon start). The daemon keeps a bounded replay buffer: ≤512 events and ≤4 MiB per session, ≤64 sessions, ≤64 MiB per process (oldest sessions evicted first); `events.since(session, epoch, cursor)` returns the missing events in order, or `session.replay_gap` when the epoch differs or the cursor fell out of the buffer, in which case the client reloads a snapshot (transcript + open requests + running state). Token deltas coalesced in 33 ms windows before buffering. | `tui_gateway/event_replay.py` [178: all] | `src/indus/service/event_log.py`, `tests/unit/service/test_event_log.py` | Hypothesis: any interleaving of appends and since-queries returns events in seq order without gaps or duplicates; epoch change → gap; byte cap evicts oldest; coalescing preserves concatenated text |
| P17-07 | **Durable server-request re-delivery.** Server requests (approval, clarify, sudo, secret) get ids `srq-<12 hex>` and live in the broker until settled; on reattach or `events.since`, still-open requests are re-offered with `replayed: true` and the same id, so a second answer from an outdated card has no effect; the first answer wins; withdrawal emits `request.settled{withdrawn}` so cards close. A viewer that cannot render a request kind answers `-32601` at once and the broker offers it to other viewers; if no viewer can, the request is settled as undeliverable immediately (not a timeout; R05.3, R02.5). A viewer may decline with 4404 ("not shown here") only when it advertised the decline capability. | `tui_gateway/server_requests.py` [394: ids, re-offer, not-shown code]; `gateway/session_server_requests.py` [173] | `src/indus/service/request_redelivery.py`, `tests/unit/service/test_request_redelivery.py` | Reattach re-offers an open approval with the same id; two answers → first wins; all viewers `-32601` → settled undeliverable in < 50 ms; declined by one viewer, answered by another |
| P17-08 | **Daemon session host with many viewers.** `DaemonSessionHost` implements the P04 `SessionService` interface inside the daemon: sessions created or resumed by any surface, each with zero or more attached viewers (connection id, surface kind `cli`/`tui`/`gui`/`acp`/`web`/`messaging`, capabilities). A turn keeps running when its viewer detaches; closing a client only detaches (R08.24 spirit). An `acp`-source session ends only when its last viewer detaches with an idle queue, or at daemon stop. Busy-input default per surface when the key is absent: TUI `queue`, others `interrupt` (R05.18). Messaging, cron and attached surfaces share this host, so one session can be watched from several places at once. | `gateway/session_authority.py` [1212: viewer attach/detach and ownership only]; `tui_gateway/session_registry.py` [850: skim]; research 05 §3.2 mode C | `src/indus/service/daemon_host.py`, `src/indus/service/viewers.py`, `tests/unit/service/test_daemon_host.py` | Detach mid-turn → turn completes and is persisted; two viewers both receive every event in order; ACP session ends after last detach only when idle |
| P17-09 | **Frozen creation policy.** At session creation the daemon freezes: cwd, model, provider, reasoning effort, toolsets, max turns, preloaded skills, checkpoints, yolo, accept-hooks, ignore-rules, base URL / key reference (never the key itself), safe mode, ignore-user-config. A resume may repeat the same values; a different value is refused with `policy_conflict` naming the field (R04.1). Launches that ignore user config must name both model and provider. Launch-option env vars are cleared before talking to the daemon; they belong to the session, not the daemon. | `hermes_cli/gateway_chat.py` [397: frozen fields]; research 04 §3.4.6 | `src/indus/service/creation_policy.py`, `tests/unit/service/test_creation_policy.py` | Resume with same model OK; with a different model → `policy_conflict{field: model}`; ignore-config launch without provider refused; secrets never stored in the frozen policy |
| P17-10 | **Single-use tickets and the shared WS auth helper (`service/ws_auth.py`, service layer so the gateway WS endpoint and the P18 web server both import it without importing each other).** Tickets are minted only through the control socket (P17-11): 32 random bytes, URL-safe base64; stored as a SHA-256 hash with `{purpose: ws-interactive/native-http, profile_id, instance_id, expires_at = now + 30 s}`; consumed atomically on first use (a second use, even within TTL, fails); purpose-bound; checked against a loopback peer and an absent or allow-listed `Origin`. Tickets never appear in URLs or logs (redaction filter pattern for the subprotocol prefix). The same module exports `authenticate_upgrade(scope, purpose)`: reads the offered subprotocols `indus-gateway-v1` + `indus-gateway-ticket.<t>`, consumes the ticket, checks loopback peer and Origin, returns the subprotocol to echo or a close code (4401 bad/used/expired ticket, 4403 bad Origin); query-string tickets are ignored and logged without the value. P18 consumes this helper for every dashboard WS upgrade (D-16.25) and extends it with the dashboard purposes (P18-06). The desktop no longer uses the `native-http` purpose (D-16.89); the purpose stays for other native clients. `profile_id` is a stable hash of the profile home; `instance_id` changes each daemon start, so a ticket from a previous daemon is useless (R08.4). | `gateway/control_socket.py` [542: `session-ticket` verb]; `gateway/browser_control_broker.py` [512: ticket TTL/single-use shape only] | `src/indus/service/ws_auth.py`, `tests/unit/service/test_ws_auth.py` | Reuse within TTL fails; expiry at 30 s (fake clock); wrong purpose fails; ticket string absent from captured logs |
| P17-11 | **Control socket attach verbs.** Add `session-ticket {purpose}` → `{ticket, ws_url, profile_id, instance_id}` and `describe` → `{profile_id, instance_id, code_version, ws_url, creatable_sources[], capabilities[]}` to the P15 socket (same one-exchange wire, same limits). Only owner-only socket access mints tickets (the socket file is 0600 and the client refuses a home writable by others). | `gateway/control_socket.py` [542: verb table] | `src/indus/gateway/daemon/attach_verbs.py`, `tests/unit/gateway/daemon/test_attach_verbs.py` | Verb round trip; group-writable home refused by the client; describe answers while a turn runs |
| P17-12 | **Shared RPC dispatcher.** One dispatcher for every JSON-RPC transport (stdio for the TUI child and `--local`, WS for the daemon): method table generated from `indus.protocol`; undeclared methods refused at import; unknown param keys rejected with the stable "unknown param" code and the key path; payloads validated against their models (raise under tests, log in production); handlers run with a per-call deadline; a handler crash is answered with the stable internal-error code, never kills the loop. P12 places its dispatcher in `tui_bridge/dispatch.py` (served by `tui_bridge/stdio_server.py`); this task moves it to `service/rpc/` (both transports import it from there; gateway may not import `tui_bridge`, which sits on the same layer) and leaves a thin re-export. | `tui_gateway/rpc_dispatch.py` [94]; `tui_gateway/contracts/registry.py` [199: declaration rules]; research 05 §3.3 | `src/indus/service/rpc/__init__.py`, `src/indus/service/rpc/dispatch.py`, `tests/unit/service/test_rpc_dispatch.py` | Unknown key → stable code with path; undeclared method fails at import (test module); handler crash answered, next call served |
| P17-13 | **WebSocket endpoint.** Starlette WS route in the daemon on 127.0.0.1 (ephemeral port recorded in the runtime status and returned by `describe`). Auth only through `service/ws_auth.authenticate_upgrade` (P17-10; D-16.25): the server echoes only `indus-gateway-v1` and closes 4401/4403 as that helper decides. After accept the server sends `gateway.ready {replay_epoch, heartbeat: true, profile_id, instance_id, capabilities}`. Per connection: write timeout 10 s, send deadline 30 s, inbound dispatch queue 256 (overflow → close 1013 "try again"), `gateway.ping`/pong, closing the socket only detaches its viewers. Logs redact URLs (query and user-info). | `tui_gateway/ws.py` [569: connection limits, coalescing, ready frame]; research 05 §3.2 shared channel; research 09 R09.2 | `src/indus/gateway/ws/server.py`, `tests/contract/gateway/test_ws_endpoint.py` | Ticket in subprotocol accepted once; reused → 4401; ticket in URL ignored; Origin `http://evil` → 4403; slow reader hits the send deadline and is closed without stalling other viewers; 257 queued requests → 1013 |
| P17-14 | **Subscriptions and cross-surface events.** `session.subscribe {session_id, epoch?, cursor?}` attaches a viewer (replays since cursor, re-offers open requests), `session.unsubscribe`; `sessions.watch` delivers `session.updated` (title, archived, pinned, running state, last activity) for list views across surfaces; viewer presence (`session.viewers`) so a surface can show "also open in the TUI". Fan-out order per session is the event-log order; a slow viewer never blocks others (per-viewer bounded queue of 1,024 events; overflow drops that viewer to a `replay_gap`). | `gateway/local_viewer_events.py` [240]; `gateway/session_events.py` [89] | `src/indus/service/subscriptions.py`, `tests/unit/service/test_subscriptions.py` | Viewer A submits, viewer B (subscribed) receives every delta in order; overflowing viewer gets `replay_gap` and recovers via snapshot; title change reaches a list watcher |
| P17-15 | **Ensure and discovery.** `indus gateway ensure [--json] [--timeout 60]`: if a live daemon answers `identify` for this profile, print the credential-free endpoint descriptor `{ws_url, profile_id, instance_id, code_version}`; otherwise start a detached daemon (never install a service; double-fork on POSIX, own session, stdio to the log) and wait for `identify` within the deadline. A daemon whose `code_version` differs from the CLI's is restarted once (graceful SIGUSR1, then wait). A deliberately stopped daemon (planned-stop record from P15-28) is not restarted by discovery-only calls; `ensure` without `--no-start` restarts it. Recover budget for automatic re-ensures: 3 per 60 s sliding window. Liveness is the socket answer, never a bare pid (R06.18). | `hermes_cli/gateway_runtime_discovery.py` [244]; `hermes_cli/gateway_runtime_start.py` [122]; research 05 §3.2 mode C steps 1–5, research 08 §3.4 | `src/indus/gateway/daemon/ensure.py`, `src/indus/cli/commands/gateway_ensure.py`, `tests/integration/gateway/test_ensure.py` | Cold start then warm ensure (second call does not spawn); version skew restarts once then succeeds; planned stop respected by discovery; fourth recover within 60 s refused with a clear verdict |
| P17-16 | **Python attach channel.** WS JSON-RPC client for Python surfaces: request ids are client integers, server-request ids strings; default request timeout 120 s (submit 1,800 s, approval respond 330 s, compress 660 s); heartbeat `gateway.ping` every 15 s, any inbound frame counts as liveness, no frame for 45 s closes the socket, ≤8 outstanding pings (R05.9); reconnect backoff 1 s doubling to 30 s, no jitter, counter reset on ready; replacing a transport rejects every in-flight request of the old one; close events from a discarded socket are ignored. Uses a fresh ticket per dial (P17-15/11). | `hermes_cli/gateway_client.py` [240]; `hermes_cli/gateway_connection.py` [395]; research 05 §3.2 shared channel | `src/indus/service/remote/channel.py`, `tests/unit/service/remote/test_channel.py` | Fake server streaming deltas without pongs stays connected; silent server closed at 45 s (fake clock); in-flight call on a swapped transport rejected; each reconnect mints a new ticket |
| P17-17 | **Remote session host.** `RemoteSessionService` implements the `SessionService` interface over the channel so surfaces do not care where the agent runs. Create is idempotent by client request id (a lost reply plus a retry never creates two sessions; R05.21); submit uses an admission request id; before admission a private prepared-submission journal entry (atomic file under `<home>/state/attach-journal/`, 0600) is written and retired only on an accepted receipt; on reconnect it calls `events.since` per session and re-subscribes, re-receiving open requests. Transport loss while a turn runs yields the error "outcome unknown, do not resend" and never an automatic resubmit (R09.11); an unknown receipt keeps the journal entry for an explicit user retry. | `hermes_cli/gateway_agent.py` [1090: event replay into callbacks, result shape]; research 08 §3.9 prepared-submission journal | `src/indus/service/remote/host.py`, `src/indus/service/remote/journal.py`, `tests/unit/service/remote/test_host.py` | Drop the reply to `session.create` and retry → one session; kill the socket mid-turn → caller gets outcome-unknown, journal entry kept, no second admission; reconnect replays missed deltas exactly once |
| P17-18 | **CLI attaches by default.** `indus`, `indus chat`, `-q` and `-z` call ensure and use `RemoteSessionService`; `--local` keeps the in-process host (the P06 route, still tested). Flags that need the local process (`--image` path staging, worktree creation, `--no-restore-cwd`, `--verbose` debug tracing) are refused in attach mode with exit 2 and a message naming `--local`; nothing silently falls back to a local agent (R04.1). Interrupt settle wait 10 s. Closing the terminal detaches; the daemon finishes accepted work. A finite query queued behind a turn whose owner was lost exits 3. `/discard <admission_id>` resolves an unknown turn (generation checked). The REPL renders remote events through the same callbacks as in-process (one renderer). | `hermes_cli/gateway_chat.py` [397]; `hermes_cli/gateway_chat_commands.py` [234: slash subset]; research 04 §3.4.6, §3.4.8 | `src/indus/cli/attach.py`, `tests/unit/cli/test_attach.py` | Each refused flag exits 2 and names `--local`; `--local` path unchanged (P06 suite green); lost-owner queue exits 3; `/discard` with a stale generation refused |
| P17-19 | **TUI attaches by default.** `indus gateway grant --json` (Python helper the renderer runs; 40 s exec timeout, 1 MiB output cap) ensures the daemon and prints `{url, protocols, profile_id, instance_id}` with the ticket only inside `protocols`; failures whose verdict is "gateway unavailable" are terminal (repair hint, no retry); others go to the client backoff. The renderer opens the WS via `@indus/client`, calls `runtime.describe`, publishes one synthetic ready, and reconnects with a fresh grant each time (recover budget 3 per 60 s). `indus --tui --local` keeps the P12 stdio child. An explicit `INDUS_TUI_GATEWAY_URL` never falls back to a local daemon. Closing the TUI only detaches. | research 05 §3.2 mode B and C; `tui_gateway/host_supervisor.py` [521: skim] | `src/indus/cli/commands/gateway_grant.py`, `apps/tui/src/transport/attach.ts`, `apps/tui/src/transport/attach.test.ts`, `tests/unit/cli/commands/test_gateway_grant.py` | Grant JSON shape; ticket absent from stdout outside `protocols` and from logs; vitest: terminal verdict shows hint without retry; explicit URL failure does not spawn a daemon |
| P17-20 | **`@indus/client` WS transport.** Finish the shared TS channel against the real daemon: ticket subprotocol, `gateway.ready` handling, replay cursor per session (≤256 cursors), `events.since` on reconnect, replayed server requests deduplicated by id (first respond/fail wins on the wire), decline only when the server advertised it, transport swap rejects in-flight requests, "subscribed" flag never reset on reconnect, close events identity-gated, unanswerable request kinds answered `-32601` at once (R05.3). Contract tests run the TS client against a Python daemon started by the test harness with the fake model. | research 05 §3.2 shared channel; research 08 §3.9 | `packages/client/src/ws-transport.ts`, `packages/client/src/replay.ts`, `packages/client/src/ws-transport.test.ts`, `packages/client/src/replay.test.ts`, `tests/contract/client/test_ts_client_against_daemon.py` | vitest with a fake WS; cross-language contract: TS client attaches, subscribes, receives deltas, answers an approval, reconnects and resumes from cursor without duplicates |
| P17-21 | **Store ownership across processes.** While a daemon owns the profile, bulk maintenance (`indus sessions prune/delete/repair/import`, `backup` of live DBs) goes through daemon RPC (`maintenance.run`) so the owner serialises it with live turns (R09.21); without a daemon the CLI takes the profile ownership lock itself. Listing and search open read-only connections that never create or migrate anything (R03.16). Multi-process stress test: daemon writing turns + two CLI readers + one dashboard-style reader for 60 s with a checkpoint in the middle: no lost rows, no `database is locked` surfaced, WAL never switched live (R03.1) (risk 6). | research 03 §3.2, R03.1, R03.16; research 09 R09.21 | `src/indus/service/maintenance_gate.py`, `tests/unit/service/test_maintenance_gate.py`, `tests/integration/state/test_multiprocess_daemon.py` | `sessions prune` with a live daemon runs inside the daemon (spy); read-only list never creates a file in an empty home; stress test passes |
| P17-22 | **Messaging on the shared host.** Gateway admission (P15-11) submits through the admission service (P17-02) instead of running turns directly, so chat inputs get the durable FIFO, the unknown-turn pause and the lease, and an attached CLI or TUI viewing a messaging session sees its turns live (read-only by default; submitting from the CLI into a messaging session posts the reply to that chat only with `--deliver`). P15-26's unknown handling now reads the ledger state. | `gateway/session_ingress.py` [426: admission into the authority]; research 06 §3.10, §3.22 | `src/indus/gateway/core/shared_host.py`, `tests/integration/gateway/test_messaging_on_shared_host.py` | Telegram fake message → admission row → turn; CLI viewer sees the deltas; daemon killed mid-turn → unknown, chat told once, `/reset` unblocks |
| P17-23 | **Idle exit for auto-started daemons.** A daemon started by `ensure` (not by a service manager or `gateway run`) exits after 600 s idle (`gateway.idle_exit_seconds`; 0 disables) when no viewer is attached, no turn runs, no admission is queued, no cron job is due within the next tick and no messaging platform is enabled; the stop reason `idle_exit` is recorded so the next `ensure` starts it again silently (K47). | `gateway/run_idle_exit.py` [225: all] | `src/indus/gateway/daemon/idle_exit.py`, `tests/unit/gateway/daemon/test_idle_exit.py` | Fake clock: exits at 600 s idle; attached viewer, due cron or enabled platform prevents exit; service-managed daemon never idles out |
| P17-24 | **Profile binding on attach.** `describe` and `gateway.ready` carry `profile_id` (stable hash of the profile home); clients refuse a mismatch with `profile_mismatch` before sending anything; every RPC in the daemon runs under the daemon's `Scope` (home + secrets + terminal policy bound together, R05.14, R03.23); the grant helper clears launch-option env vars before contacting the daemon. Multiplexed profiles stay out of scope (P24). | research 05 R05.14; research 09 §3.10 connection | `src/indus/service/remote/profile_guard.py`, `tests/unit/service/remote/test_profile_guard.py` | Client for profile A pointed at B's socket path refuses; launch env var (`INDUS_YOLO_MODE`) not visible to the daemon process |
| P17-25 | **Protocol additions and codegen.** Declare in `indus.protocol`: methods `runtime.describe`, `session.subscribe`, `session.unsubscribe`, `sessions.watch`, `events.since`, `admission.receipt`, `admission.discard`, `maintenance.run`; events `gateway.ready`, `session.replay_gap`, `session.updated`, `session.viewers`, `admission.state`, `request.settled`; error codes with one meaning each (D-16.27): `stale_generation`, `ticket_invalid`, `profile_mismatch`, `admission_conflict`, `queue_full`, `outcome_unknown`, `policy_conflict`, `lease_lost`, `not_shown`. Regenerate JSON Schema and `packages/protocol`; the stale-output check stays green. | research 05 §3.3; `tui_gateway/contracts/canonical.py` [545: method list only] | `tests/unit/protocol/test_attach_contract.py` | Schema snapshot includes every new name; each error code appears once in the table; codegen stale check fails when a model changes without regeneration |
| P17-26 | **Dual-host test matrix.** A `session_host` pytest fixture parametrised over `inprocess` and `attached` (daemon in a subprocess with the fake model); the P06 CLI chat suite, the P12 bridge suite and the P15 Demo 3 suite run under both (risk 4). A matching vitest setup runs the TUI integration tests against the stdio child and the daemon. Any behaviour difference fails unless listed with a D-id in the fixture's allow-list file. | — (our suites) | `tests/integration/service/conftest_hosts.py`, `tests/integration/service/test_dual_host_matrix.py`, `apps/tui/src/test/hosts.ts` | Both parametrisations green for the CLI chat suite; a seeded difference (attached host drops a usage event) fails the matrix |
| P17-27 | **Shared-backend demo suite.** E2E: start a daemon; attach the classic CLI and the TUI (Ink test renderer) to one session; a prompt sent from the CLI streams in both; an approval raised during it is answered from the TUI and the CLI card closes; detach the CLI mid-turn and the turn completes; restart the daemon mid-turn and both clients report the outcome as unknown and refuse to resend; `--local` still works with the daemon stopped. | — | `tests/e2e/service/test_shared_backend.py` | All steps pass in CI with the fake model |

## Files to create (65)
| Path | What it holds | Task |
|---|---|---|
| `src/indus/state/runtime_ledger.py` | admissions FIFO, owner epoch, unknown handling | P17-01 |
| `tests/unit/state/test_runtime_ledger.py` | ledger rules | P17-01 |
| `src/indus/service/admissions.py` | admission service | P17-02 |
| `tests/unit/service/test_admissions.py` | receipts, discard | P17-02 |
| `src/indus/state/turn_leases.py` | lease table | P17-03 |
| `src/indus/service/turn_lease.py` | lease acquire/refresh/wait/loss | P17-03 |
| `tests/unit/service/test_turn_lease.py` | lease rules | P17-03 |
| `tests/integration/service/test_turn_lease_multiprocess.py` | cross-process contention | P17-03 |
| `src/indus/service/turn_liveness.py` | idle-turn watchdog | P17-04 |
| `tests/unit/service/test_turn_liveness.py` | watchdog races | P17-04 |
| `src/indus/service/fences.py` | generation checks for controls | P17-05 |
| `tests/unit/service/test_fences.py` | stale generation | P17-05 |
| `src/indus/service/event_log.py` | seq, epoch, replay buffer | P17-06 |
| `tests/unit/service/test_event_log.py` | replay properties | P17-06 |
| `src/indus/service/request_redelivery.py` | durable server requests | P17-07 |
| `tests/unit/service/test_request_redelivery.py` | re-offer, first answer wins | P17-07 |
| `src/indus/service/daemon_host.py` | daemon `SessionService` host | P17-08 |
| `src/indus/service/viewers.py` | viewer registry | P17-08 |
| `tests/unit/service/test_daemon_host.py` | detach, multi-viewer | P17-08 |
| `src/indus/service/creation_policy.py` | frozen launch policy | P17-09 |
| `tests/unit/service/test_creation_policy.py` | conflicts | P17-09 |
| `src/indus/service/ws_auth.py` | single-use tickets and the shared WS upgrade auth helper (P18 consumes) | P17-10 |
| `tests/unit/service/test_ws_auth.py` | ticket and upgrade rules | P17-10 |
| `src/indus/gateway/daemon/attach_verbs.py` | `session-ticket`, `describe` verbs | P17-11 |
| `tests/unit/gateway/daemon/test_attach_verbs.py` | verb round trips | P17-11 |
| `src/indus/service/rpc/__init__.py` | package | P17-12 |
| `src/indus/service/rpc/dispatch.py` | shared JSON-RPC dispatcher | P17-12 |
| `tests/unit/service/test_rpc_dispatch.py` | dispatcher rules | P17-12 |
| `src/indus/gateway/ws/server.py` | WS endpoint | P17-13 |
| `tests/contract/gateway/test_ws_endpoint.py` | upgrade auth, limits | P17-13 |
| `src/indus/service/subscriptions.py` | subscribe, watch, presence, per-viewer queues | P17-14 |
| `tests/unit/service/test_subscriptions.py` | fan-out order, overflow | P17-14 |
| `src/indus/gateway/daemon/ensure.py` | discover or start the daemon | P17-15 |
| `src/indus/cli/commands/gateway_ensure.py` | `indus gateway ensure` | P17-15 |
| `tests/integration/gateway/test_ensure.py` | cold/warm/skew/budget | P17-15 |
| `src/indus/service/remote/channel.py` | Python WS JSON-RPC channel | P17-16 |
| `tests/unit/service/remote/test_channel.py` | heartbeat, reconnect | P17-16 |
| `src/indus/service/remote/host.py` | `RemoteSessionService` | P17-17 |
| `src/indus/service/remote/journal.py` | prepared-submission journal | P17-17 |
| `tests/unit/service/remote/test_host.py` | idempotent create, unknown outcome | P17-17 |
| `src/indus/cli/attach.py` | attach-mode wiring and refused flags | P17-18 |
| `tests/unit/cli/test_attach.py` | exit codes 2/3, `/discard` | P17-18 |
| `src/indus/cli/commands/gateway_grant.py` | `indus gateway grant --json` | P17-19 |
| `apps/tui/src/transport/attach.ts` | TUI attach bootstrap | P17-19 |
| `apps/tui/src/transport/attach.test.ts` | bootstrap verdicts | P17-19 |
| `tests/unit/cli/commands/test_gateway_grant.py` | grant shape, redaction | P17-19 |
| `packages/client/src/ws-transport.ts` | WS transport with tickets | P17-20 |
| `packages/client/src/replay.ts` | cursor bookkeeping and replay | P17-20 |
| `packages/client/src/ws-transport.test.ts` | transport tests | P17-20 |
| `packages/client/src/replay.test.ts` | replay tests | P17-20 |
| `tests/contract/client/test_ts_client_against_daemon.py` | cross-language contract | P17-20 |
| `src/indus/service/maintenance_gate.py` | route maintenance through the owner | P17-21 |
| `tests/unit/service/test_maintenance_gate.py` | gate rules | P17-21 |
| `tests/integration/state/test_multiprocess_daemon.py` | 60 s stress | P17-21 |
| `src/indus/gateway/core/shared_host.py` | messaging → admission service bridge | P17-22 |
| `tests/integration/gateway/test_messaging_on_shared_host.py` | chat turns on the shared host | P17-22 |
| `src/indus/gateway/daemon/idle_exit.py` | idle exit | P17-23 |
| `tests/unit/gateway/daemon/test_idle_exit.py` | idle rules | P17-23 |
| `src/indus/service/remote/profile_guard.py` | profile id checks, env scrub | P17-24 |
| `tests/unit/service/remote/test_profile_guard.py` | mismatch refusal | P17-24 |
| `tests/unit/protocol/test_attach_contract.py` | new names and codes | P17-25 |
| `tests/integration/service/conftest_hosts.py` | dual-host fixture | P17-26 |
| `tests/integration/service/test_dual_host_matrix.py` | matrix runner and allow-list | P17-26 |
| `apps/tui/src/test/hosts.ts` | vitest dual-host setup | P17-26 |
| `tests/e2e/service/test_shared_backend.py` | M6 demo suite (CLI + TUI part) | P17-27 |

## Files to update (17)
| Path | Change | Task |
|---|---|---|
| `src/indus/protocol/methods.py` | new methods (P04 file) | P17-25 |
| `src/indus/protocol/events.py` | new events (P04 file) | P17-25 |
| `src/indus/protocol/errors.py` | new stable error codes (P04 file) | P17-25 |
| `packages/protocol/` (generated) | regenerated output, never hand-edited | P17-25 |
| `src/indus/state/schema.py` | runtime ledger and lease tables (P02 declarative schema) | P17-01, P17-03 |
| `src/indus/state/session_lock.py` | per-session file lock kept only for stores without the lease table; documented as superseded (D-16.69, P02 file) | P17-03 |
| `src/indus/service/broker.py` (P04-21) | broker delegates persistence and re-offer of server requests to `request_redelivery.py` | P17-07 |
| `src/indus/service/session_service.py` | interface gains `subscribe`, `events_since`, `receipt`; in-process host implements them trivially (P04 file) | P17-08 |
| `src/indus/tui_bridge/dispatch.py` | becomes a thin re-export of `service/rpc/dispatch.py` (P12 file) | P17-12 |
| `src/indus/tui_bridge/stdio_server.py` | import the dispatcher from `service/rpc` (P12 file) | P17-12 |
| `src/indus/gateway/control_socket.py` | register attach verbs (P15-27 file) | P17-11 |
| `src/indus/gateway/daemon/process.py` | mount the WS endpoint, daemon host, idle exit (P15-24 file) | P17-13, P17-23 |
| `src/indus/gateway/core/admission.py` | hand admitted inputs to `shared_host.py` (P15-11 file) | P17-22 |
| `src/indus/gateway/daemon/recovery.py` | read unknown state from the runtime ledger (P15-26 file) | P17-22 |
| `src/indus/cli/chat.py` | choose attached host by default, `--local` flag (P06 file) | P17-18 |
| `apps/tui/src/entry.tsx` (P12-07) | choose WS attach by default, `--local` stdio | P17-19 |
| `src/indus/cli/commands/sessions.py` | maintenance subcommands go through the gate (P07 file) | P17-21 |

## Fixtures and fakes (2)
| Path | What it holds | Task |
|---|---|---|
| `tests/fakes/daemon.py` | harness that starts a daemon subprocess with the fake model, a temp home and a short-lived ticket helper | P17-15, P17-26 |
| `tests/fixtures/service/dual_host_allowlist.yaml` | documented, D-id-cited differences between hosts (starts empty) | P17-26 |

## Task notes (27)
`docs/notes/P17/<slug>.md`: `runtime-ledger` (01), `admissions` (02), `turn-lease` (03),
`turn-liveness` (04), `generation-fences` (05), `event-log` (06), `request-redelivery` (07),
`daemon-host` (08), `creation-policy` (09), `tickets` (10), `attach-verbs` (11), `rpc-dispatch` (12),
`ws-endpoint` (13), `subscriptions` (14), `ensure` (15), `remote-channel` (16), `remote-host` (17),
`cli-attach` (18), `tui-attach` (19), `client-ws` (20), `store-ownership` (21),
`messaging-shared-host` (22), `idle-exit` (23), `profile-guard` (24), `protocol-attach` (25),
`dual-host-matrix` (26), `shared-backend-demo` (27).

## Run until green
```bash
cd indus-agent
uv sync --all-extras
uv run ruff check
uv run ruff format --check
uv run pyright src/indus/service src/indus/state src/indus/gateway
uv run pytest tests/unit/service tests/unit/state tests/unit/gateway tests/unit/cli tests/unit/protocol tests/contract tests/integration/service tests/integration/state tests/integration/gateway tests/e2e/service -p no:randomly
pnpm install
pnpm --filter @indus/protocol check:stale
pnpm --filter @indus/client test && pnpm --filter @indus/client typecheck
pnpm --filter @indus/tui test && pnpm --filter @indus/tui typecheck
uv run python -m tooling.checks --require-ref
```
On any failure, fix the cause and rerun from `uv sync`. Never relax the dual-host matrix by adding an
allow-list entry without a D-id, and never hand-edit generated protocol files.

## Compare checklist (reopen the reference after writing)
- [ ] Every frozen creation field and every refused flag in research 04 §3.4.6 is handled or recorded with a D-id (we keep `--local` instead of refusing everything: D-16.10).
- [ ] Ticket rules: single use, 30 s, purpose-bound, loopback peer, no Origin, never in URL or log.
- [ ] Shared-channel numbers (120 s timeout, 15 s/45 s heartbeat, ≤8 pings, 1→30 s backoff) match research 05 §3.2.
- [ ] Every way a client can lose its transport mid-turn yields "outcome unknown" and no resend.
- [ ] Replay: epoch change and evicted cursor both produce a gap and a snapshot reload.
- [ ] Turn lease numbers (TTL 300 s, wait 1,800 s, refresh margins) and loss behaviour match research 01 §3.23.
- [ ] Every server-request kind is re-offered on reattach and settles exactly once.
- [ ] Every surface the reference attaches (classic chat, TUI) attaches here; ACP and desktop are recorded as P19/P20.

## Traps covered
| Trap | Pinned by |
|---|---|
| R04.1 classic chat as a daemon client, frozen policy, refused flags | P17-09, P17-18 |
| R05.1 one explicit transport per mode | P17-19, P17-26 |
| R05.3 unanswerable server requests answered at once | P17-07, P17-20 |
| R05.9 any frame counts as liveness | P17-16, P17-20 |
| R05.14 profile scope bound per RPC | P17-24 |
| R05.15 stable error codes | P17-25 |
| R05.18 busy-mode default per surface | P17-08 |
| R05.21 idempotent session create | P17-17 |
| R08.4 one-use credentials, fresh per dial | P17-10, P17-16 |
| R09.2 WS auth through one helper (subprotocol, not headers) | P17-13 |
| R09.11 never resend a turn with unknown outcome | P17-17, P17-18 |
| R09.21 owner-held data not bulk-edited by other processes | P17-21 |
| R03.1 never live-downgrade WAL under multiple processes | P17-21 |
| R03.16 read-only browsing never writes | P17-21 |
| R03.20 pid liveness across namespaces | P17-03 |
| R03.21 started work becomes unknown, never replayed | P17-01, P17-22 |
| R03.22 deleted ids stay dead | P17-01 |
| R03.23 profile cross-talk | P17-24 |
| R01.4 interrupt races (generation-bound controls) | P17-05 |
| R06.2 claim and release by generation across processes | P17-03, P17-05 |
| R06.18 liveness = socket answer | P17-15 |
| R06.19 unknown turns pause the FIFO | P17-02, P17-22 |
| R02.5 undeliverable approval distinct from timeout or deny | P17-07 |

## Acceptance
| Feature | ACC ids (planned) |
|---|---|
| A63 cross-process turn lease and liveness watchdog | ACC-A63-1 (two processes, one writer), ACC-A63-2 (lease loss interrupts), ACC-A63-3 (idle abort) |
| K51 shared per-profile daemon for CLI and TUI (P2; dashboard P18, ACP P19, desktop P20) | ACC-K51-1 (CLI attach), ACC-K51-2 (TUI attach), ACC-K51-3 (tickets single-use), ACC-K51-4 (replay since cursor), ACC-K51-5 (generation fences) |
| I55 classic chat as a daemon client with `--local` (P2 part) | ACC-I55-1, ACC-I55-2 |
| J2 TUI WebSocket attach (P2 part) | ACC-J2-2 |
| B25 durable input ledger | ACC-B25-1 (exact retry), ACC-B25-2 (unknown never replayed) |
| K47 idle exit for auto-started daemons (P2 part) | ACC-K47-1 |
| K32 control socket attach verbs | ACC-K32-2 |

## Done when
- All commands in "Run until green" pass, including the dual-host matrix and the TS-client-against-
  daemon contract test.
- `indus` and `indus --tui` attach to the profile daemon by default (starting it when needed) and
  `--local` still runs in-process.
- Demo (part of M6; the dashboard half lands in P18): the classic CLI and the TUI are attached to one
  daemon on the same session; a prompt typed in one streams in both; an approval raised in the turn is
  answered from the other surface; killing a client leaves the turn running; restarting the daemon
  mid-turn shows "outcome unknown" in both and neither resends.
