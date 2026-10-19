# Task: Cron ticker hosted in the daemon (P15-21) Phase: 15 Owner: wave-2 builder W2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/run.py` (the cron profile
homes helper, the per-profile "another gateway owns it" gate, the ticker start shim and the cron
drain constant only), `cron/scheduler_delivery.py` (delivery confirmation and status only: the
"success but not delivered" reading of a filtered send, unverified deliveries). LEAD-NOTES "P14
landing notes" (one backend `service.cron_host.CronHost`, ticker, deliverer registry, D-16.230
PluginUpkeep open question) and "P15 wave 1" notes. Closed before writing.
Evidence: none.

What it does (own words): the gateway daemon runs the profile's cron scheduler on its own event
loop instead of a separate `indus cron daemon` or a user timer. Every 60 s it runs one P14 ticker
pass (claims, attempt ledger, recursion guard, delivery and bookkeeping are P14's and unchanged).
While the daemon hosts the ticker it holds an ownership lease in the cron folder; a CLI
`indus cron tick` or `indus cron daemon` in another process reads the lease and stands down
("deferred") so two processes never race the same profile's schedule. On shutdown the daemon stops
starting passes, waits up to 30 s for runs in flight (then cancels them) and drops the lease.
Delivery goes through P14's `cron.delivery`, which now finds the platform deliverers P15-20
registers; the delivery status split (R07.17) is P14's and only exercised here. Cron outputs that
only narrate silence ("(silent)", "*staying quiet*", 🔇 …, at most 64 characters) are settled as
silent runs instead of being delivered. Credential shapes in cron output are masked by P14's
delivery whatever the log-redaction setting.

Inputs → outputs:
- `GatewayCronHost(scope, *, backend=None, clock=None, interval=60, drain_s=30)`; `await start()`,
  `await stop()`, `await tick_now()` (one pass, tests and `/cron` later); `owner` (the lease).
- `narrates_silence(text) -> bool` (pure).
- In `cron/ticker.py` (P14 file, listed in the phase's "Files to update"): `claim_ticker(home, now)`,
  `release_ticker(home)`, `ticker_owner(home, now) -> TickerOwner | None`; `Ticker(env, hosted=...)`
  — an un-hosted ticker returns `TickReport("deferred", …)` while a live owner exists.

Files: `src/indus/gateway/cron_host.py`, `tests/unit/gateway/test_cron_host.py`,
`tests/integration/gateway/test_cron_delivery.py`; shared: `src/indus/cron/ticker.py`.

Edge cases / failure modes:
- lease written by a process that died: the holder stamp (pid + pid namespace + host, P02
  `state.holders`) proves it dead, or the recorded process start time no longer matches, or the
  heartbeat is older than 180 s (three passes) → not an owner; the CLI ticks again.
- lease from another host / namespace (shared volume): only the heartbeat age decides.
- malformed or unreadable lease file → not an owner (fail open: a CLI tick runs; P14's tick lock and
  fire claims still prevent a double fire).
- a pass raising does not stop the loop; the next pass runs at the next interval.
- stop while a run is in flight: drain 30 s, then cancel (P14 `Ticker.drain`).
- narration filter: never applied to failures, never to long text, never to text that merely
  mentions silence inside a real report.

Our design differences / decisions:
- The "control-socket probe" of the phase row becomes a lease file in the cron folder: `cron` sits
  below `gateway`/`service` in the layer contract, and the CLI must not import the gateway
  (D-16.246). The control socket (W4, P15-27) can add a second probe later; the lease is enough to
  stop double firing and needs no running socket.
- `indus cron tick` while deferred prints why and exits 0 (same as "another tick is running"), so a
  user timer left in place does not report failures while the gateway runs. `indus cron daemon`
  keeps looping and takes over when the gateway stops.
- The narration filter turns the outcome into `silent` before P14's finish step (so it is booked
  as a quiet success, not as a delivery failure).

Tests we will write: fake clock — a job due at T fires once at the first pass ≥ T and not again on
the next pass; `indus cron tick` (CronHost.tick) with a live gateway lease refuses with the message;
stale heartbeat / dead holder / other start time → the CLI ticks; `cron daemon` defers then takes
over after release; delivery failure leaves the run `ok` with `delivery_failed` on the attempt;
narration filter table; stop drains a running job; integration: a Telegram-named fake adapter gets
the cron output in its home channel (stand-in deliverer for P15-20), a credential shape masked with
log redaction off.

## Decisions for the lead
- D-16.230 (cron + plugins): **no `PluginUpkeep` for cron runs in the daemon.** A job must behave
  the same whether the daemon or `indus cron tick` fires it, and the CLI path has no plugin host;
  cron sessions keep P14's tool set (no plugin tools, sections or `pre_llm_call`/shell hooks). If
  wanted later, `CronAgentRunner` is the one place to add it for both paths.
- Ticker ownership is a lease file `<home>/cron/ticker_owner.json` (0600, atomic replace), refreshed
  each pass; stale after 180 s. Deferred tick exit code 0.
- Narration filter wording set is ours (≤64 chars after trimming wrappers).
- Wiring (W3, P15-24): one call, `indus.gateway.extras.install_extras(daemon, cron=True)`, after the
  `AdmissionPipeline` is built and before events are accepted. It needs `daemon.scope`,
  `daemon.pipeline`, `daemon.on_start(async hook)`, `daemon.on_stop(async hook)` (`DaemonSeat`);
  sets `pipeline.enrich` (P15-23) and `pipeline.on_request` (P15-22), puts the album collector and
  the prompts in `pipeline.front_doors`, starts `GatewayCronHost` on start and stops it (30 s
  drain, lease dropped) on stop. Integration (lead): the daemon runs this as `extras.ExtrasPart`
  (BACKGROUND; `install_extras` in `prepare` through an `ExtrasSeat`); the drain reads
  `agent.cron_drain_timeout` (30 s); the stand-ins are gone (`test_cron_delivery` uses P15-20's
  router).

Compare checklist (filled after the rewrite; reopened the cron gate, ticker shim and drain constant
in `gateway/run.py`, and the delivery confirmation in `cron/scheduler_delivery.py`):
- [x] one in-process ticker per profile, 60 s passes; another process's ticker stands down while a
  live gateway owns the profile, liveness never by bare pid (`tests/unit/gateway/test_cron_host.py::test_cron_tick_refuses_while_a_live_gateway_owns_the_profile`, `::test_a_dead_holder_a_reused_pid_or_a_bad_file_owns_nothing`, `::test_cron_daemon_defers_then_takes_over`)
- [x] the host never defers to itself (reference compares the liveness answer with its own pid; we
  use `Ticker(hosted=True)`) (`::test_cron_daemon_defers_then_takes_over`)
- [x] due at T fires once at the first pass ≥ T (`::test_a_due_job_fires_once_at_the_first_pass_after_its_time`)
- [x] delivery status separate from run status, R07.17 (`::test_a_delivery_failure_leaves_the_run_ok`)
- [x] a filtered (narrated-silence) output is not counted as delivered (`::test_a_narrated_silence_is_not_delivered`, `::test_silence_narration_table`)
- [x] masking with log redaction off; Telegram-named fake gets the output in its home channel (`tests/integration/gateway/test_cron_delivery.py::test_a_cron_job_reaches_the_telegram_home_channel_masked`)
- [x] drain on shutdown: reference waits 65 s for its delivery thread; ours is the phase's 30 s
  over P14's async drain (`::test_stop_drains_then_cancels_a_run_in_flight`)
- [x] multi-profile ticking from one host (reference multiplexing) is not in P15 (one profile per
  gateway, D-16.17).
- Note: the ledger attempt keeps the runner's own status (`ok`) when the narration filter settles
  the job as `silent`; the job record and the delivery show the filtered outcome.
