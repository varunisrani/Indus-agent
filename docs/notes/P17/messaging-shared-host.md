# Task: Messaging on the shared host (P17-22) Phase: 17 Owner: wave-27 builder K9

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/session_ingress.py` (all:
how a chat message is admitted into the session authority, the delivery waiter, the crash-marker
hand-over, the once-per-pause notice, recovered no-waiter replies). Research 06 §3.10 (busy guards)
and §3.22 (drain, restart, unknown turns), trap R06.19. Our P17-02 `service/admissions.py`, P17-08
`service/daemon_host.py` and `service/viewers.py`, P17-07 `service/request_redelivery.py`, P17-13
`gateway/ws/server.py`, P17-11 `gateway/daemon/attach_verbs.py`, P15-11 `gateway/core/admission.py`,
P15-24..26 `gateway/daemon/process.py`, `lifecycle.py`, `recovery.py`, LEAD-NOTES "P17 wave 2
landing notes" and `docs/notes/P17/ws-endpoint.md` (wiring list). Closed before writing.
Evidence: none.

What it does (own words): the gateway daemon stops running chat turns on the session service
directly. It builds one `DaemonSessionHost` over its chat service at start; every chat message the
intake decides to run becomes an admission in the session's durable FIFO (the runtime ledger), and
the host's worker runs it under the turn lease, the liveness watchdog and the generation fences.
The intake waits for the admission to settle and keeps every rule it had: one claim per chat key,
the busy guards, the reply threaded to the person's message, the final passing through the ledgered
sender before the crash marker is cleared (R06.5), failure notices. Turns the service starts by
itself (goal continuations, background-subagent results, steer leftovers) enter the same FIFO, and
their finals still go to the chat. Because the messaging sessions live on the shared host, a CLI
or TUI attached over the WebSocket can watch them and see the deltas live; a turn submitted from
such a surface is not posted to the chat unless the admission asks for it (`deliver`).

After a crash, a turn that had started is `unknown`: it is never run again (R03.21), and later
input of that session waits behind it. Boot recovery now reads the ledger: a marker whose admission
never started (still queued) or that settled is not an unknown turn (the marker is cleared and the
queued input runs); a stored final written after the turn began is delivered once and the unknown
row is discarded, so the conversation goes on; otherwise the route is paused and the person is told
once (`/reset` rotates to a fresh session). Any other unknown row whose session has a chat route
pauses that route too, and a worker that finds a queued input behind an unknown turn tells the chat
once (R06.19) — after recovery has run, never before.

The daemon also mounts the P17-13 WebSocket endpoint on that host and serves the attach verbs with
the host and the endpoint's URL, so `describe` advertises the host's sources and capabilities and
`session-ticket` mints.

Inputs → outputs:
- chat message (after every P15 gate) → `host.submit(session, text, principal="messaging:<platform>:<user>",
  request_id=<marker token>, busy="queue")` → receipt; `wait` → settled row (outcome, generation,
  detail) → failure notice when `failed`; the final reaches the chat through the turn-end listener.
- turn end → ledger row of (session key, generation) → its request id (the reply anchor), its
  admission id (the final gate the intake waits on), whether the final goes to the chat.
- server requests of a messaging session → the intake's viewer (`messaging:<key>`, kinds approval
  and clarify) → cards in the chat; answers go back through the service broker (first answer wins).
- daemon start/stop → host open/close, WS endpoint start/stop, status surface `ws`.

Files: `src/indus/gateway/core/shared_host.py` (new: the bridge), `tests/integration/gateway/
test_messaging_on_shared_host.py` (new). Shared: `src/indus/gateway/core/admission.py` (P15-11 file,
named in the phase file), `src/indus/gateway/daemon/recovery.py` (P15-26 file, named),
`src/indus/gateway/daemon/process.py` (host, WS mount, stop order), `src/indus/gateway/daemon/
lifecycle.py` (drain stops the claim's admission), `src/indus/gateway/daemon/side_services.py`
(attach verbs over the daemon's book, host view and URL), `src/indus/gateway/core/commands.py`
(`/bg` through the intake's session helpers), `tests/integration/gateway/test_control_part.py`
(the attach-verb test now sees the mounted endpoint).

Edge cases / failure modes:
- a message admitted behind an unknown turn (receipt `blocked_by`) → its admission is cancelled,
  the route paused, the person told once; nothing waits forever;
- the ledger refuses (`queue_full`, a deleted session) → a short notice, nothing runs;
- `/new` or `/stop` while the admission is still queued → the handler is cancelled and its
  admission cancelled with it (never answered later in the old conversation);
- a stop's drain → the claim's admission is stopped by its own generation (queued: cancelled);
- a turn end the ledger cannot map (no row) → delivered as before, without an anchor;
- a turn submitted by an attached CLI into a messaging session → not posted to the chat unless
  its payload says `deliver: true`;
- the host cannot open (service already has sessions, store error) → logged, the daemon runs chat
  turns in-process as before and mounts no WebSocket;
- no WebSocket implementation importable → logged, `ws_url` stays null (`session-ticket` answers
  `ws_unavailable`).

Our design differences / decisions:
- Our names (`MessagingHost`, `Settled`, `TurnOwner`). The intake keeps the waiter and its claim;
  the reference hands the crash marker from the drain to the adapter lifecycle. Ours keeps one
  delivery path (the turn-end listener) for every turn of a messaging session and waits on a final
  gate keyed by the admission id, so the R06.5 order holds without moving markers between objects.
- The marker token is the admission's request id (`turn-<n>-<hex>`, unique per daemon life), so
  recovery maps a marker to its ledger row.
- Messaging inputs use busy mode `queue` at the host: the intake already decided (interrupt,
  queue, steer) before it admits.
- No owner drain `gate=` on admissions: the intake's pause probe already answers new chat turns
  while draining, and a gate would also stop claims of follow-ups the drain lets through.
- The reference saves messages sent while restarting and answers them later; we keep the P15
  restarting notice.

Tests we will write (`tests/integration/gateway/test_messaging_on_shared_host.py`):
- a fake Telegram DM → one admission row (principal `messaging:telegram:<user>`, request id = the
  marker token, terminal completed) → the reply in the chat, threaded; the model ran once;
- a viewer attached to the messaging session receives the turn's deltas and `turn.done` in order;
- a turn admitted for the session by another surface is not posted to the chat; with
  `deliver: true` it is;
- a daemon killed mid-turn (a started row from an older owner, its marker on the route) → the next
  boot: the row is unknown, nothing is re-run, the chat is told once, a second message is dropped,
  `/reset` unblocks and the next message runs in a fresh session;
- a marker whose admission was only queued → not paused, the input runs once after the restart;
- the attach verbs: `describe` lists the host's sources and capabilities, `session-ticket` mints,
  the WebSocket accepts the ticket and sends `gateway.ready`; at stop the endpoint closes before the
  host and the book is revoked.

## Wiring (as built)

Start (`GatewayDaemon._start`): assemble (store, chat service, pipeline) → parts prepare →
CONTROL parts (control socket; `DaemonBridge` builds `AttachVerbs(home, daemon.tickets,
host=<daemon host view>, ws_url=daemon.ws_url)`) → platforms connect (intake holds inbound) →
RECOVERY parts (delivery-ledger sweep) → **`_open_host`**: `MessagingHost(store)`,
`DaemonSessionHost.open(service, store, instance_id=daemon.instance_id, leases=TurnLeases(store),
liveness=LivenessSettings.from_scope(scope), on_blocked=shared.on_blocked)`, `shared.bind(host)`,
`pipeline.shared = shared` → boot recovery (reads the ledger) → `shared.release_blocked(recovery.blocked)`
→ held inbound replayed → BACKGROUND parts → **`_mount_ws`**: `WsEndpoint(host, daemon.tickets,
scope=scope)`, `start()` on 127.0.0.1:0, `status.set_surface("ws", endpoint.status_row())` → ready.

`instance_id` is minted once per daemon start (`ws_auth.new_instance_id`) and shared by the ticket
book (`TicketBook.for_home(home, instance_id=…)`, built in `GatewayDaemon.__init__`) and the owner
epoch (`begin_owner_epoch`), so `describe.instance_id` names the epoch's owner.

Stop order (`_stop_everything`): systemd stopping, loop watchdog, daemon tasks → **WS endpoint stop**
(sockets closed 1001, viewers detached; surface row `stopped`) → BACKGROUND parts → platforms
disconnected → RECOVERY parts → pipeline closed → replies closed → automation stopped → **host
`aclose`** (admissions closed: running turns interrupted `shutdown`, workers settle; sessions
closed; service closed; event-log flusher stopped) — or the service alone when no host was opened →
closers → CONTROL parts (control socket; ticket book revoked) → token locks, store released.
Before that, `_shutdown` drains chat turns: each claim's admission is stopped by its own
generation (`pipeline.interrupt_claim`), flagged restart-interrupted as before.

## Decisions for the lead
- `deliver` seam for attached surfaces: a turn admitted into a messaging session by any principal
  other than `messaging:*` / `internal:*` is posted to the chat only when its admission payload has
  `deliver: true`. `DaemonSessionHost.submit` and the WS `prompt.submit` do not carry it yet
  (P17-18 `--deliver` / P17-25 protocol field).
- The intake's viewer counts as an attached viewer of every messaging session it touched; P17-23's
  idle exit should ignore `messaging` viewers (an enabled platform already blocks idle exit).
- No admission `gate=` for drains (see above); the follow-up "a worker that meets a refusing gate
  exits" therefore does not arise here.
- The host opens after the RECOVERY parts (platforms connected, inbound held) rather than at
  assembly, so queued input left by the previous daemon runs with its chat reachable; a blocked
  notice raised before recovery is held and re-checked against the ledger afterwards.
- A host that cannot open leaves the daemon on the in-process path (logged), instead of failing the
  start.
- WS `origins` allow-list: empty (only clients without an `Origin` header) until a config key exists.

- Messages sent to a paused conversation are dropped after the one notice (the P15 rule); the
  reference saves them in the paused FIFO and never answers them. Ours never fills the FIFO behind
  a lost turn (`queue_full` cannot follow from a pause).
- Not wired: `resolve_unknown(terminal_write=)` closing the lost turn in its transcript (wave-1
  wiring list). `AdmissionService.discard_unknown` takes no `terminal_write`; adding it changes a
  landed contract (P17-02), so the discards here (recovered reply, `resolve_unknown`) close the row
  only. Proposed: `discard_unknown(id, generation, *, terminal_write=None)` passed through to the
  ledger, with the gateway writing a short assistant note.
- `/stop` while the chat's admission is still queued behind a host-started turn interrupts that
  running turn and leaves the chat's own input to run next (before: the input was dropped with the
  queue). `/new` and a cancelled handler cancel the queued admission.
- Stop-race fix carried over (D-16.339 on the shared host): the claim is marked finished at the
  turn's end, from the running ticket (a plain read before any await), so a stop after the reply
  went out neither interrupts nor resumes it — the admission settles only after a thread hop, which
  made Demo 3's first test send the "stopped" notice after the answer under load.

Compare checklist (filled after the rewrite; reopened `gateway/session_ingress.py`):
- [x] queue-full refusal answered with a short notice, nothing committed (`QUEUE_FULL_TURN`;
  other refusals answer the failed-turn notice with the reason)
- [x] once-per-pause notice for input that lands behind an unknown turn (tests:
  `test_messaging_on_shared_host.py::test_a_turn_lost_with_its_daemon_pauses_the_chat_once_and_never_runs_again`)
- [x] the crash marker is cleared only after the reply is in the delivery ledger (final gate keyed
  by the admission id; `test_boot_recovery.py::test_a_stop_after_the_reply_went_out_neither_interrupts_nor_resumes_the_turn`,
  `tests/integration/gateway/test_gateway_e2e.py`, Demo 3 crash step)
- [x] gap found: the reference sends a recovered no-waiter reply only after its terminal outcome
  committed; ours sends at the turn's end, so a kill between the send and the settle left an
  `unknown` row with no marker that would have paused a chat whose answer was already stored —
  fixed: recovery owes such a stored reply once through `crash_reply_sink` (the delivery ledger
  skips it when it went out) and discards the row; pinned by
  `test_messaging_on_shared_host.py::test_a_lost_host_turn_whose_reply_was_stored_is_owed_once_not_paused`
- [x] a marker whose admission never started is not a lost turn
  (`::test_a_marker_whose_admission_never_started_runs_it_once`)
- Deliberate differences: see the decisions above (dropped messages while paused; listener
  delivery with a final gate instead of marker hand-over; no drain gate).
- Not taken: the "no viewer, no turn" completion note, managed workers, API-server turns through
  this path (they have their own surface service), multiplexed execution scope (P24),
  content-clause refusal notices (no native payload validation here), the transient
  `runtime_draining` / `session_busy` pause notices (no drain gate; the intake answers busy itself).

## Status

Done (2026-10-20). New: `src/indus/gateway/core/shared_host.py`,
`tests/integration/gateway/test_messaging_on_shared_host.py` (7 tests). Shared:
`gateway/core/admission.py`, `gateway/core/commands.py` (`/bg`), `gateway/daemon/process.py`,
`gateway/daemon/lifecycle.py`, `gateway/daemon/recovery.py`, `gateway/daemon/side_services.py`,
`tests/integration/gateway/test_control_part.py` (the attach-verb test asserted the pre-mount
state), `docs/internals/traps-ledger.md` (R03.21, R06.19), `docs/internals/acceptance-map.md`
(ACC-B25-2). Handed on: P17-18 / P17-25 (`deliver` field on submits into messaging sessions),
P17-23 (ignore `messaging` viewers for idle exit).

## Integration (P17-15..22 integrator, 2026-10-20)

Lead decision applied: `AdmissionService.discard_unknown(id, generation, *, terminal_write=None)`
(passed to `ledger.resolve_unknown`; `DaemonSessionHost.discard_unknown` passes it on).
`admissions.close_lost_turn(note, now=)` appends a short assistant note to the lost turn's session
when its last active message is the person's (skipped for no row / no messages / a reply already
there / a compressed-away session), in the discard's transaction. Wired: `recovery.resolve_unknown`
(operator) and the WebSocket `admission.discard` (CLI `/discard`). Not on the recovered-reply
discard (`MessagingHost.discard`): that transcript already ends with the stored final. The WS
endpoint is reached end to end by the CLI, a second client and the TS client in
`tests/integration/cli/test_attach_daemon.py`.

## Wave 46 (RS6: a lost turn whose reply was saved)

Read: none from the reference (follow-up from LEAD-NOTES "P23 wave 36", landing on P20).

- Cause: boot recovery settled an `unknown` admission whose stored final was there only for
  sessions with a chat route (`_pause_unknown` skipped routeless sessions). A TUI/desktop/CLI
  session whose daemon died after the reply was streamed and saved, before the admission's
  terminal write, therefore stayed `unknown` and its surfaces showed "a turn was lost" + Discard.
- Now a routeless session's unknown admission is discarded at boot (no note appended, nothing
  sent; `RecoveryReport.saved_turns`) when `_turn_saved` proves the reply was this turn's: an
  active user message written at or after the admission, and the transcript ends with a final
  assistant reply written after it. A reply of an earlier turn (the prompt predates the admission,
  e.g. a queued input that was claimed and died before its prompt was written) is no proof: the row
  stays `unknown` for the person to discard. Chat-routed sessions keep the existing rule.
- Test: `tests/integration/gateway/test_messaging_on_shared_host.py::
  test_a_routeless_lost_turn_whose_reply_was_saved_is_not_shown_as_lost` (saved → discarded,
  transcript unchanged, no model call, nothing sent; unproven → still `unknown`). Fail-first on
  main: the saved row stays `unknown` (and the report has no `saved_turns`).

### Decisions for the lead
- W46-f-2 (D-16.1009): proof is timestamps (`created_at` vs message timestamps, the same clock), as the
  existing routed rule uses. The admission's claim time is overwritten by the epoch flip, so the
  admission time is the bound. A per-turn id on stored messages would be exact (follow-up).
