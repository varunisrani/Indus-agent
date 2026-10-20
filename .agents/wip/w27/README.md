# Wave 27 — parked builder patches

## w27-e11.patch: P16-31 setup wizard + P16-32 target parsers — being integrated by the lead (verify13)

## w27-k9.patch: P17-22 messaging on the shared host — done (base ac389c3)
- Builder checks: unit/contract/integration/e2e gateway + service 2680 passed / 53 skipped (Demo 3 3x green, gateway_e2e
  crash recovery green); ruff, pyright, lint-imports, tooling.checks ok. No new packages. Full suite + perf not run.
- New gateway/core/shared_host.py (MessagingHost), test_messaging_on_shared_host.py, docs/notes/P17/messaging-shared-host.md.
- Shared edits: gateway/core/admission.py (admissions when pipeline.shared set; in-process path unchanged otherwise),
  daemon/recovery.py (reads the runtime ledger), daemon/process.py (host open, WS mount, stop order), daemon/lifecycle.py
  (drain stops each claim's own admission), daemon/side_services.py (AttachVerbs gets book + host view + ws_url),
  core/commands.py (/bg via intake helpers), test_control_part.py (asserts the mounted endpoint), traps R03.21/R06.19,
  ACC-B25-2.
- Wiring: one instance_id per daemon start (TicketBook + owner epoch); start order … RECOVERY parts → host opens → boot
  recovery → held notices released → held inbound replayed → BACKGROUND → WS mounted → ready; stop order WS endpoint →
  BACKGROUND → platforms → RECOVERY → pipeline → replies → automation → shared.aclose → host.aclose → closers → CONTROL
  (book revoked) → token locks/store.
- Fixed: D-16.339 race again on the admission path (listener marks the claim finished from the running ticket).
- Decisions: `deliver` seam (non-messaging principals post to chat only with payload deliver: true); no drain gate=; host
  opens late (falls back to in-process turns if it cannot open); DaemonOptions shared_host/attach_ws default on; WS
  origins empty; paused conversation drops further messages; /stop edge with a host-started turn queued ahead.
- NOT done (needs a landed-contract change, lead to decide): AdmissionService.discard_unknown(id, generation, *,
  terminal_write=None) so resolve_unknown can close the lost turn in its transcript.
- Follow-ups: P17-23 idle exit ignores messaging viewers; P17-18/25 `deliver` field; abandon stop task at loop end.

## w27-k6.patch: P17-15 ensure + P17-16 attach channel + P17-17 remote session host — done (base ac389c3)
- Builder checks: remote/ensure/contract/protocol/tui_bridge/gateway CLI/ws 1282 passed / 47 skipped (remote suite 3x);
  ruff, pyright, lint-imports, tooling.checks (incl. protocol_stale) ok. No stand-ins, no new deps.
- New: service/gateway_ensure.py (NOT gateway/daemon/ensure.py — layer contract: CLI and service both need it),
  cli/commands/gateway_ensure.py, service/remote/{channel,ws_client,host,journal}.py; tests + test doubles (wire.py);
  notes ensure.md, remote-channel.md, remote-host.md ("Interface for other slices").
- Shared: cli/commands/gateway.py lazy `ensure`; non_tty_table `gateway ensure` long_running; protocol PromptSubmitParams
  .request_id (regenerated TS); gateway/ws/methods.py request_id → admission request id under principal attach:<surface>;
  traps R05.9/R05.21/R06.18/R08.4/R09.11; ACC-K51-4.
- Wiring for K7: home_dialer(home, env, budget=RecoverBudget()) + Channel(dial, surface="cli") +
  RemoteSessionService(channel, Journal(home)).start(); RemoteHost(build) for the REPL; OutcomeUnknown → "do not resend";
  RemoteHost.wait returns RemoteTurn. K8: ensure(...) + grant_for(home); Ensured.terminal. Works end to end once K9's
  mounted WS endpoint lands.
- Decisions: INDUS_GATEWAY_AUTOSTART=1 on ensure-started daemons (P17-23 seam); deliberate stop = planned_stop:<reason>
  != restart; OutcomeUnknown → runtime.outcome_unknown until P17-25; principal attach:<surface>; admission↔turn matching by
  generation / first later turn until P17-25 admission.state.
- Follow-ups: gateway_control.query peer-uid check; compaction child ids only after resume/subscribe; redirect /
  session.mutate / admission.receipt not served over WS; describe vs identify version source.

## w27-k8.patch: P17-19 TUI attaches by default + P17-20 @indus/client WS transport — done (base ac389c3)
- Builder checks: pnpm 899 passed (61 files), typecheck clean (protocol/ui/client/tui), biome clean; pytest grant/gateway/
  launcher + contract client/cli/gateway 936 passed / 47 skipped; cross-language contract test 4x green; ruff, pyright,
  lint-imports, tooling.checks ok. TUI e2e lane not run (node-pty cannot build here). No new deps.
- Stand-in: tests/unit/cli/commands/_stubs_k8.py for K6's ensure. K8 ASSUMED `indus.cli.commands.gateway_ensure
  .ensure_endpoint(scope, *, start, timeout)` → mapping {ws_url, profile_id, instance_id, code_version}, raising an error with
  `code` and `terminal`. K6 ACTUALLY built `indus.service.gateway_ensure.ensure(home, env, mode=...)` + `grant_for(home)` with
  `Ensured.terminal` — adapt `_ensure_from_p17_15` / `_from_ensure_error` in gateway_grant.py to K6's API and delete the stub.
- Shared: cli/commands/gateway.py `grant` row; cli/tui_launch.py exports INDUS_TUI_GRANT_COMMAND and INDUS_TUI_LOCAL (reads
  args.local via getattr — K7 adds `local` to GlobalArgs); non_tty_table `gateway grant` long_running; packages/client
  channel/client/index/localEvents; apps/tui entry.tsx, appRuntime.ts, sessionLifecycle.ts(+test), rpc.ts, e2e harness
  (Demo 2 with INDUS_TUI_LOCAL=1); traps R05.1/R05.3/R05.9; ACC-J2-2, ACC-K51-2, ACC-K51-4.
- Decisions: terminal verdicts list; terminal verdict at startup runs the local stdio child with a visible line; explicit
  INDUS_TUI_GATEWAY_URL only dialled (no helper/start/fallback, offers indus-gateway-v1 without ticket); reconnect via
  session.subscribe with cursor; ready without epoch clears cursors; profile_mismatch refused.
- Follow-ups: TUI session.steer sends no generation (P17-26); "agent process stopped; restarting…" wording in attach mode;
  bridge-only methods over the daemon (dual-host matrix); runtime.describe declared/served by P17-25.
- Incident: one `indus gateway grant` run without INDUS_HOME appended a keychain warning line to /root/.indus/logs/agent.log
  and errors.log and touched /root/.indus/cache/scratch/.last_prune (files pre-existed). Owner item.
