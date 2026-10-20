# Task: TUI attaches by default (P17-19) Phase: 17 Owner: wave-27 builder K8

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tui_gateway/host_supervisor.py`
(skim: module purpose and restart window only — it supervises the dashboard compute host, not the
TUI attach), `ui-tui/src/localGatewayBootstrap.ts` (skim: helper run limits, the start / recover
switch and the terminal-verdict test); compare step only: `ui-tui/scripts/gateway_bootstrap.py`
(the helper's control flow, env scrub and stdout discipline). Research 05 §3.1, §3.2 (mode B and C, shared channel),
§3.24, §3.30. Our P17-10 `service/ws_auth.py`, P17-11 `service/gateway_control.py`
(`session_ticket`, `describe`), P17-13 `gateway/ws/server.py` (ready frame, served methods), P12
`apps/tui/src/entry.tsx`, `app/appRuntime.ts`, `app/recovery.ts`, `cli/tui_launch.py`. Closed
before writing.
Evidence: none.

What it does (own words): `indus --tui` stops spawning its own `tui-bridge` child by default and
attaches to the profile's shared daemon over the loopback WebSocket, so the classic CLI and the
TUI can watch one session. Getting in takes a single-use ticket, which only the owner-only control
socket hands out; the renderer is Node and cannot speak to that socket safely, so it runs a small
Python helper, `indus gateway grant --json`. The helper makes sure the daemon runs (P17-15
ensure), asks the control socket for a ticket bound to the daemon's profile and instance, and
prints one JSON object `{url, protocols, profile_id, instance_id}`; the ticket appears only inside
`protocols` as `indus-gateway-ticket.<t>`, never in the URL, never in a log line, never on disk.
The renderer opens the socket with those subprotocols through `@indus/client`, waits for the
daemon's `gateway.ready`, asks `runtime.describe`, and only then publishes one ready event to the
app (one per connection). Every reconnect asks the helper again (tickets are single use).

Helper failures carry a verdict: `gateway_unavailable` is terminal (the daemon cannot be used from
here at all: no WebSocket endpoint, an unsafe home, a peer the OS cannot verify, another profile,
an OS without the control socket, a helper that cannot run) — the renderer shows the repair hint
once and does not ask again; anything else (`retry`: a timeout, a daemon still starting, a
restart race, too many tickets waiting) goes to the client's backoff. Re-ensures that may start a
daemon are rationed: 3 per 60 s sliding window; past that the renderer only discovers.

`indus --tui --local` keeps the P12 stdio child (the launcher exports `INDUS_TUI_LOCAL=1`).
`INDUS_TUI_GATEWAY_URL` names an explicit endpoint: the renderer dials it and nothing else — it
never runs the helper, never starts a daemon, never falls back. Closing the TUI only closes its
socket: the daemon and any running turn go on.

Inputs → outputs:
- `indus gateway grant --json [--mode start|discover|recover] [--timeout S]` → stdout one JSON
  object. Success (exit 0): `{"url", "protocols": ["indus-gateway-v1",
  "indus-gateway-ticket.<t>"], "profile_id", "instance_id"}`. Failure: `{"error": {"verdict":
  "gateway_unavailable" | "retry", "code", "message", "hint"?}}` with exit 2 (terminal) or 1
  (retry). Without `--json` a short human line on stderr, the grant on stdout.
- Renderer env: `INDUS_TUI_LOCAL=1` → stdio child; `INDUS_TUI_GATEWAY_URL` → explicit attach;
  else attach through `INDUS_TUI_GRANT_COMMAND` (a program, or a program with arguments; `gateway
  grant --json` is added unless it names `grant` itself; default `indus`).
- `apps/tui/src/transport/attach.ts`: `chooseTransport(env)`, `parseGrant(stdout, code)`,
  `runGrantHelper(...)`, `helperEnv(env)`, `RecoverBudget`, `AttachTransport` (a late-binding
  `Transport`: grant → WebSocket → describe → synthetic ready), `attachConnector(...)` (the
  `connect` factory the entry hands `IndusClient`).

Files: `src/indus/cli/commands/gateway_grant.py`, `tests/unit/cli/commands/test_gateway_grant.py`,
`tests/unit/cli/commands/_stubs_k8.py` (stand-in for P17-15 ensure), `apps/tui/src/transport/attach.ts`,
`apps/tui/src/transport/attach.test.ts`. Shared: `src/indus/cli/commands/gateway.py` (registry row
`grant`), `src/indus/cli/tui_launch.py` (`INDUS_TUI_LOCAL`, `INDUS_TUI_GRANT_COMMAND`) and
`tests/unit/cli/test_tui_launch.py` (one test), `tests/contract/cli/non_tty_table.yaml` (`gateway
grant` is long-running: it may start a daemon), `apps/tui/src/entry.tsx` (transport choice),
`apps/tui/src/app/appRuntime.ts` (a terminal close stops recovery; attach lines;
`client.capabilities` names `surface: "tui"` so the daemon applies the TUI busy default and
presence) and `apps/tui/src/app/sessionLifecycle.test.ts` (that call's expectation),
`apps/tui/src/app/rpc.ts` (the direct answer path sends the request's generation),
`apps/tui/src/app/sessionLifecycle.ts` (`carry(line)`: the fallback line survives the first
session's transcript reset), `apps/tui/e2e/harness.ts` (Demo 2 runs `--local`),
`docs/internals/traps-ledger.md` (R05.1), `docs/internals/acceptance-map.md` (ACC-J2-2,
ACC-K51-2).

Edge cases / failure modes:
- helper prints more than 1 MiB, or runs past 40 s → killed, `retry`;
- helper exits without JSON (import error, missing program) → terminal `gateway_unavailable`
  with the first stderr line as detail (never stdout, which could hold a ticket);
- helper JSON with a `url` that is not a credential-free loopback `ws://` URL, or protocols
  without exactly the two names → refused (`retry`), nothing dialled;
- daemon's ready names another profile or instance than the grant → socket closed, terminal
  `profile_mismatch`; another instance → retry (the daemon restarted between grant and dial);
- WebSocket closed 4401 (ticket used / expired) → retry with a fresh grant; 4403 → terminal;
- `runtime.describe` not served (`-32601`, the daemon predates P17-25) → the daemon's own ready is
  used; any other describe failure, or no published ready within 15 s of the dial (no ready frame,
  or describe unanswered) → the socket is closed (backoff);
- terminal verdict before any attach succeeded in this run → the renderer says why and runs the
  local stdio child instead (visible line, never silent); after an attach succeeded → hint only,
  no retry, no local fallback (the daemon may still own the session's lease);
- explicit URL refused or unreachable → backoff on that URL only; no helper run, no daemon spawn.

Our design differences / decisions:
- Our names (`gateway grant`, `INDUS_TUI_LOCAL`, `INDUS_TUI_GRANT_COMMAND`); the helper is an
  `indus` subcommand (not a script found next to the sources), so an installed wheel has it.
- The helper scrubs launch-option env vars (`INDUS_TUI_*`, `INDUS_MODEL`, `INDUS_YOLO_MODE`, …)
  before anything that may start a daemon, and the renderer runs it with a scrubbed env too.
- Verdict travels as a JSON field plus an exit code, not as a message prefix.
- Local fallback on a terminal verdict at startup only (lead brief: the stdio path must keep
  working when no daemon can be used); the reference has no such fallback.

Tests we will write:
- Python: grant JSON shape and exit codes; the ticket only inside `protocols` (absent from the
  rest of stdout, stderr and captured logs); each verdict (ensure failure kinds, `ws_unavailable`,
  `unsafe_home`, `forbidden`, `profile_mismatch` / `stale_instance`, `capacity`); launch env
  scrubbed before ensure; describe-only fallback when ensure is unavailable; `--mode discover`
  never starts; human output without `--json`.
- vitest: grant parsing (shape, URL safety, protocols); terminal verdict shows the hint once and
  the helper is not run again; retry verdict goes to backoff and runs the helper again;
  recover budget 3 per 60 s; explicit URL failure never runs the helper; describe → one
  synthetic ready; `-32601` describe tolerated; profile mismatch closes; startup fallback to local;
  the ticket never reaches the log ring.

## Decisions for the lead
- Verdict codes (control-wire strings until P17-25): terminal = `ws_unavailable`, `unsafe_home`,
  `forbidden`, `profile_mismatch`, `unsupported`, `no_ensure`, `helper_failed`; everything else
  `retry`.
- Startup local fallback on a terminal verdict (above). To drop it, make `attachConnector`'s
  `fallback` null in `entry.tsx`.
- `runtime.describe` is not declared yet (P17-25): the renderer calls it untyped and tolerates
  `-32601` by using the daemon's `gateway.ready` (it already carries profile, instance,
  capabilities and replay epoch).
- Explicit `INDUS_TUI_GATEWAY_URL` offers `indus-gateway-v1` only (no ticket): ticket auth for an
  explicit endpoint belongs to the remote-connection work (P24).
- The ensure seam: `gateway_grant` calls P17-15 through `indus.cli.commands.gateway_ensure`
  (assumed `ensure_endpoint(scope, *, start, timeout) -> Mapping` with `ws_url`, `profile_id`,
  `instance_id`, `code_version`, raising an exception that carries `code: str` and `terminal:
  bool`; stand-in `tests/unit/cli/commands/_stubs_k8.py`); when that module or function is
  missing it falls back to `describe` (no start) and reports `no_ensure` (terminal) when nothing
  answers. The cli layer may not import `indus.gateway` (D-16.246), so K6's CLI module is the
  natural home of the function.
- The helper also refuses a daemon whose `profile_id` is not `stable_profile_id(home)`
  (`profile_mismatch`, terminal) before asking for a ticket (a P17-24 rule, cheap here).
- Without `--json` the command mints nothing and says whether an endpoint is ready (exit 0 / 1).
- `gateway grant` is a `long_running` row in the non-TTY contract table (with no arguments it may
  start a daemon), like `gateway start`.
- The launcher exports `INDUS_TUI_LOCAL=1` when `args.local` is set (read with `getattr`: the
  `--local` flag itself is P17-18's) and `INDUS_TUI_GRANT_COMMAND` (the `indus` program, or the
  user's own value).
- Recovery wording: a lost socket still prints the P12 line "The agent process stopped;
  restarting and reopening your chat…" (attach-specific wording is a follow-up).

Compare checklist (filled after the rewrite; reopened `ui-tui/src/localGatewayBootstrap.ts`,
`tui_gateway/host_supervisor.py`):
- [x] helper limits 40 s / 1 MiB (test: `attach.test.ts::runs the helper with its limits, the mode and a scrubbed env`)
- [x] terminal verdict → hint, no retry (test: `attach.test.ts::a terminal verdict after attaching shows the hint once and is not retried`)
- [x] start first, then recover under a 3 per 60 s budget, else discover (test: `attach.test.ts::asks start first, then recover within the budget, then discover`)
- [x] ticket only in the subprotocol, never URL / log (tests: `test_gateway_grant.py::test_the_grant_carries_the_ticket_only_inside_protocols`, `attach.test.ts::the ticket reaches only the WebSocket's subprotocols: no log line, no event`)
- [x] launch policy env dropped before the daemon can be started (test: `test_gateway_grant.py::test_launch_options_are_scrubbed_before_ensure`)
- [x] gap found: the reference's helper sends everything it imports or starts printing to stderr
  so only the grant reaches stdout — fixed with `redirect_stdout(sys.stderr)` around the grant,
  pinned by `test_gateway_grant.py::test_stray_prints_during_ensure_never_reach_stdout`.
- [x] gap found: a socket that opens but never sends ready would have hung (the heartbeat starts
  only on ready) — fixed with one 15 s deadline from dial to published ready, pinned by
  `attach.test.ts::a socket that opens but never gets ready, or never describes, is given up at 15 s`.
- Deliberate differences: the reference marks only "this interpreter cannot load the agent" as
  terminal (a message prefix on stderr); we carry a verdict field and treat every refusal no
  retry can fix as terminal (no endpoint, unsafe home, unverified peer, another profile, no
  control socket). The reference passes its whole env to the helper; we scrub launch options on
  both sides. The reference discovers first on a recover and re-ensures only when nothing is
  attachable; our `recover` asks ensure directly (ensure on a live daemon only discovers). The
  reference finds a Python interpreter; we run the `indus` program the launcher names. We
  validate the grant's URL and subprotocols in the renderer; the reference trusts the JSON.
  Startup fallback to the local child on a terminal verdict is ours (lead brief).
- Not taken: the multiplexed-profile control home (P24), the Windows named pipe (D-16.55), the
  dependency-environment activation (no such packaging layer here).

## Status

Done (2026-10-20). `indus gateway grant`, the renderer's attach bootstrap and the launcher
switches are in; the TUI attaches by default. Real end-to-end attach waits for P17-22 (the daemon
mounts the WebSocket endpoint; until then the grant answers `ws_unavailable` and the TUI runs the
local child with a visible line) and for K6's ensure (stand-in in tests; the grant falls back to
`describe` without it).

## Integration (P17-15..22 integrator, 2026-10-20)

- Stand-in removed (`_stubs_k8.py` deleted). `gateway_grant.ensure_endpoint(scope, *, mode,
  timeout)` calls `service.gateway_ensure.ensure` in the grant's own mode (`start` / `discover` /
  `recover`; the renderer rations recoveries, the helper is a fresh process) and raises
  `GrantRefused(reason, detail, terminal=Ensured.terminal)`: a daemon stopped on purpose met by
  `recover` is terminal with a hint (`planned_stop`), the budget and start failures are `retry`.
  The ticket comes from `remote.channel.grant_for(home)` (`describe` then `session-ticket`); a
  `DialRefused` keeps its reason (terminal when either side says so); ids that differ from the
  ensured endpoint (a restart in between) are `stale_instance`, retry. The describe-only fallback
  and `no_ensure` are gone (D-16.493). The launch-env scrub is now `creation_policy`'s, shared with
  `ensure` (D-16.498).
- End to end: `tests/integration/cli/test_attach_daemon.py` runs `indus gateway grant --json`
  against a real daemon and attaches `@indus/client` (`tests/contract/client/ts_grant_script.ts`,
  grant on stdin) through it.
