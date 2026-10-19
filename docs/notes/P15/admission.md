# Task: Admission pipeline and session claim (P15-11) Phase: 15 Owner: wave-18 builder G4

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/run_inbound.py`
(`_handle_message`, `_hm_admit_event`, the estop gate, stale-slot eviction and the gate order),
`gateway/run_inbound_hooks.py` (seam order: pre-dispatch, post-admission, command hooks, quick and
plugin commands). Research 06 §3.5, §3.10, R06.2, R06.21, R01.16. Evidence: none.

What it does (own words): the runner's single intake. Every adapter hands its events to
`AdmissionPipeline.accept` (installed as the adapter's sink). Control commands (`/stop`, `/new`,
`/queue`, `/status`, `/approve`, `/deny`) are handled inline in the adapter's own await; a key
that is busy goes to the busy guard; anything else starts its own handler task. The handler runs
the ordered gates, each of which may end the message:

1. per-chat identity context reset, then bound to this event (`GatewayIdentity`, a context
   variable; `identity_env()` gives a child process exactly this chat's identity);
2. identity check (a known platform and a chat id; paused platforms drop inbound);
3. startup-restore queue (events arriving while boot recovery runs are kept and replayed in order
   by `finish_restore()`);
4. `pre_gateway_dispatch` plugin hook (skip drops, rewrite replaces the text; fail-open);
5. authorization (P15-08 chain) and, on denial, the unknown-sender handler (pair / ignore /
   decline); bot senders count once against the bot-loop guard (P15-10); mention gating in
   groups (commands and replies to our own messages pass);
6. emergency-stop gate (an injected probe; a paused gateway answers new turns with the paused
   notice, recognised commands and messages for a running or prompting session still pass);
7. pending prompt replies (interceptors: destructive-command confirmations from P15-13 here;
   P15-22 adds clarify answers);
8. busy guard (P15-12) when the key has a claim or the service runs a turn for its session;
9. slash dispatch (P15-13) or the session claim;
10. `post_gateway_admission` hook (once per admitted message, fail-open), media enrichment seam
    (P15-23; default: reply-context quote + text), then the turn through `SessionService.submit`.

Claim before the first await (R06.2): `ClaimTable.claim(key)` installs a sentinel synchronously
and returns None when one exists, so two events for one key with no await between them start one
turn. Each claim has a run generation; `release(claim)` frees the key only while that claim is
still the current one, so a displaced turn (after `/new` or `/stop` dropped it) cannot clear its
successor. Before the busy check a leaked claim (its task finished without releasing) or a stale
one (older than `agent.gateway_timeout` with nothing running) is evicted. The concurrency cap
(P15-10) is taken with the claim and released only by the claim that still owns the key.

Turn: the routing index (P15-07) maps the key to a session (created through
`SessionService.create` on first use, re-created when the stored session vanished); the session is
attached (`resume`) when needed; the effective model is applied (one-turn override → session
override → channel override → profile default); the durable active-turn marker is set; the
event's model text is submitted; the handler waits for the result. Final replies of every
completed turn of a routed session — ours, steer follow-ups, P10 injected turns — leave through
one `on_turn_end` listener to the route's origin, through the outbound sender (P15-17; stood in by
a test stub here). A failed turn gets a short notice; an interrupted one says nothing. On every
exit path: one-turn model restored, marker cleared (token-guarded), claim released (generation
guarded), the next queued message for the key started.

Inputs → outputs: `InboundEvent` from adapters → replies through `ReplySender`; turns on the
service; route rows in the store.

Files: `src/indus/gateway/core/admission.py`, `tests/unit/gateway/core/test_admission.py`,
`tests/unit/gateway/core/_kit.py` (fake chat adapter, pipeline builder),
`tests/unit/gateway/_stubs_g4.py` (STAND-IN outbound sender), `src/indus/tools/toolsets.py`
(shared: `indus-telegram`, `indus-discord`, `indus-slack` bundles).

## API

`indus.gateway.core.admission`:
- `GatewayIdentity(platform, chat_id, user_id, thread_id, session_key, session_id)`;
  `current_identity()`, `bind_identity(identity)`, `reset_identity()`, `identity_env(identity=None)`
  (`INDUS_GATEWAY_PLATFORM`, `INDUS_GATEWAY_CHAT_ID`, `INDUS_GATEWAY_USER_ID`,
  `INDUS_GATEWAY_THREAD_ID`, `INDUS_GATEWAY_SESSION_KEY`, `INDUS_SESSION_ID`; empty dict outside
  a chat).
- `Claim`, `ClaimTable(clock)`: `claim(key) -> Claim | None` (synchronous), `current(key)`,
  `release(claim) -> bool`, `drop(key) -> Claim | None`, `evict_stale(key, *, stale_after, running)`.
- `ReplyKind = Literal["final", "notice", "ack", "command"]`;
  `ReplySender` protocol: `async reply(source, text, *, reply_to=None, kind="final")`.
- `IntakeSettings` (profile, busy mode and ack switches, merge windows, per-user session flags,
  stale timeout, confirm switches, toolset per platform); `IntakeSettings.from_scope(scope)`.
- `ModelSwitcher` protocol + `RouteSwitcher` default (resolves a route, `binding.apply`).
- `AdmissionPipeline(scope=, service=, sender=, store=, authorizer=, unauthorized=, ...)`:
  `accept(event)` (adapter sink), `handle(event)`, `start()` / `aclose()`, `begin_restore()` /
  `finish_restore()`, `session_key(source)`, `busy(key)`, `pending` (per-key FIFO), and the
  helpers the busy guard and the commands use (`run_admitted`, `new_session`, `session_for`).

Edge cases / failure modes:
- two events for one key with zero awaits → one turn; the second goes through the busy guard.
- a stored session that no longer exists → a new session, route rotated.
- `SESSION_BUSY` on submit (a service follow-up turn started first) → the event goes back to the
  head of the queue and runs when that turn ends.
- hooks raising → fail open (logged).
- authorization raising → denial (the chain fails closed itself).
- 50 interleaved chats, each spawning a subprocess after awaits → each sees only its own identity.

Our design differences / decisions:
- One intake object instead of a runner mixin; gates are methods in a fixed order.
- No durable admission FIFO / session authority (the reference's multi-surface authority is P17);
  the per-key queue is in memory.
- Mention gating runs in admission (after authorization) instead of inside each adapter.
- Replies of service-initiated turns (steer follow-ups, goals, loops, subagent results) are sent
  by the same `on_turn_end` listener as ours.
- The emergency-stop probe is injected (no `indus pause` sentinel exists yet; E16 is P2).
- Identity context is our own (`INDUS_GATEWAY_*` env names); the child-env builder does not read
  it yet (wiring for the lead).

Tests planned: claim race (zero awaits → one turn); displaced release keeps the successor; stale
and leaked claims evicted; 50 interleaved chats subprocess identity; paused gate passes `/status`,
blocks text; restore queue replays in order; pre-dispatch skip/rewrite; post-admission once;
unauthorized DM gets a pairing code and never reaches the model; group without mention ignored;
end to end through the fake platform: authorized DM → fake-model turn → reply on the fake wire.

## Decisions for the lead
- `ReplySender.reply(source, text, *, reply_to, kind)` is the seam P15-17's sender must offer (or
  be wrapped into); `tests/unit/gateway/_stubs_g4.py` stands it in.
- `identity_env()` should be merged into `safety.child_env.child_env(extra=...)` by the terminal and
  code tools (one line each) so tool subprocesses carry the chat identity.
- Gateway sessions declare `approval` and `clarify` as answerable; sudo and secret requests are
  refused by the broker at once and the chat gets a "not supported here" notice.
- Toolset per platform: `indus-<platform>` when that toolset exists, else the service default
  (`indus-telegram`, `indus-discord`, `indus-slack` added to `tools/toolsets.py`, all = `core`;
  `indus-gateway` now includes them).
- The concurrency cap and the active-turn marker are released only by the claim that still owns
  the key; a claim taken away by `/new` / `/stop` gives its cap slot back at once.
- `identity_env()` names start with `INDUS_`, which `child_env` blocks by prefix: wiring them into
  tool subprocesses needs them in `KEPT_PRODUCT_NAMES` (or a forced entry) — safety-owned file.
- `post_gateway_admission` is an observer in P13's hook bus, so a plugin cannot consume a message
  there (the reference lets it); add a directive form to the bus if that is wanted.
- The active-turn marker is cleared when the turn ends, not after the final is in the ledger:
  P15-19 moves the clear behind the ledger write (R06.5).

Compare checklist (filled after the rewrite; reopened `gateway/run_inbound.py` `_hm_admit_event`,
`_handle_message`, the estop gate and the eviction helpers):
- [x] gate order: context reset → identity → restore queue → pre-dispatch hook → authorization /
  unknown sender / bot-loop count → pause gate → prompt replies → stale eviction → busy guard →
  idle commands → claim → post-admission → turn (`tests/unit/gateway/core/test_admission.py` end-to-end and gate tests)
- [x] claim synchronously before the first await, release by generation, leaked/stale eviction
  (`tests/unit/gateway/core/test_admission.py::test_two_events_for_one_key_with_no_await_between_start_one_turn`,
  `::test_claims_are_synchronous_and_released_by_generation`,
  `::test_a_displaced_turn_cannot_release_its_successors_claim`,
  `::test_leaked_and_stale_claims_are_evicted_before_the_busy_check`)
- [x] per-handler context reset (`::test_a_subprocess_sees_only_its_own_chat_under_50_interleaved_chats`)
- [x] pause gate lets recognised commands through (`::test_the_pause_gate_passes_status_but_blocks_new_turns`)
- [x] startup-restore queue (`::test_events_during_startup_restore_are_replayed_in_order`)
- [x] pre-dispatch skip / rewrite, post-admission once, fail-open (`::test_the_pre_dispatch_hook_may_rewrite_or_drop_and_admission_fires_once`, `::test_a_hook_that_raises_fails_open`)
- [x] unauthorized senders never reach the model (`::test_an_unauthorized_dm_gets_a_pairing_code_and_never_reaches_the_model`, `::test_an_allowlist_makes_strangers_silent_and_they_never_reach_the_model`)
- [x] concurrency cap (`::test_the_concurrency_cap_refuses_a_new_session`); mention gating (`::test_a_group_message_is_answered_only_when_it_mentions_the_bot`)
- Deliberate differences: no multiplex identity canonicalisation or parked-route refusal (P24);
  Slack ignored channels belong to the Slack adapter (P15-35); no session authority / durable FIFO
  / orphan rescue (P17); no reaped-session eviction (no ended-session signal in our store yet);
  external-drain and Telegram topic-lobby gates come with P15-25 / P15-33; scale-to-zero and
  metrics stamps not taken.

## Status

Done (2026-10-19). `src/indus/gateway/core/admission.py`; tests in `tests/unit/gateway/core/test_admission.py` (18) with the kit
`tests/unit/gateway/core/_kit.py` and the sender stand-in `tests/unit/gateway/_stubs_g4.py`.

Integration (lead): the stand-in is gone. Replies go through `gateway/outbound/replies.py`
`PlatformReplies` (one P15-17 `OutboundSender` per platform; `notice` without a message to reply
to is unsolicited, `ack` is ephemeral, D-16.250); `_kit.RecordingReplies` records the kinds in
tests. `approvals.destructive_slash_confirm` now has its schema row (default false).
