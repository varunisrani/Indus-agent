Wave 42 (P24 owed wiring, D-16.799) builds from main 1c29df1 (P24 landed 5c0cff8). P24's features are built and tested but
several are not called from the running product. Your job is to WIRE them, with end-to-end tests that drive the real entry
points (no new features). In parallel: a P25 integrator lands docs/notices/gates/parity (docs/, scripts/docs, scripts/release,
scripts/parity, tooling/checks, ci.yml) — do not touch those.
Read in full first: docs/plan/phases/LEAD-NOTES.md section "P24 waves 37–39 landing notes (integrator + lead)" (the exact
wiring list per slice), docs/plan/16-what-we-change.md D-16.799 row, docs/notes/P24/*.md for your seams (each note has a
"Wiring for the lead" list), docs/plan/phases/P24-multiplex-extras.md (Acceptance, Done when, the demos), AGENTS.md, HANDOFF.md.

Slices:
- WA (gateway / daemon): P24-01..04 multiplex host in `gateway run` (decide_second_run before the per-home lock, MultiplexHost
  claim, identify_fields, control_verbs, served tickets, per-profile adapters via ProfileScopeCache + AdapterTable, router
  identity, SharedIngress on the API server + webhook receiver; flip D-16.17 to "multiplex available, off by default");
  P24-08/09 relay front in the daemon (build_front → start, adapter per descriptor, router.attach(via_relay=True),
  native_state, on_interrupt stops the turn, idle exit → go_dormant); P24-06 room loop (lease → recover → start → member turn →
  settle, expire_turns, prune) + idle exit counts live room tasks; kanban BoardWatchers in the daemon when
  kanban.dispatch_in_gateway; bots_router on the API server. DEMO tests: two profiles served by one gateway with isolated
  secrets; a room with two members taking turns.
- WB (agent loop + tools): P24-15..17 LSP manager per host, LspToolHooks around the dispatcher hook seam (even when LSP is off),
  StopGates in core/loop.py::_handle_answer (LspStopGate, VerifyOnStopGate, PreVerifyGate) with continuation rows;
  register message_agent + install LocalBotTransport, a2a_call, browser_vault + browser_exec (add to BROWSER_TOOLS, regenerate
  golden prompts), the A2A registry entry + wizard step + target rule + matrix row; kanban_create delivery_origin; ProjectStore
  .join_session at session creation; suggestion offers from the review step (P24-46); preview automation server requests
  (P24-25). DEMO test: the LSP stop gate catches a type error the model introduced and the turn continues to a fix.
- WC (UI surfaces): desktop shell consumers of the contribution registry (routes / nav / panes) + plugins inventory page
  (P24-31/32); TUI wake → voice mode hand-off (P24-20; engine default per D-16.800 recommendation sherpa-onnx, unlockable);
  `indus dashboard --console` (P24-36); profile_rows in profiles.list (P24-27); learning-graph read (P24-28) if small; RFC 8252
  loopback broker on the serve (P24-29) if small, else record.

Rules: same as the w41 brief (.agents/wip/w41/brief-p25.md "Rules" block) — ENV RULE (two export statements; TMPDIR unset);
pytest ALWAYS with `--basetemp=/tmp/<slice>pt` (SHORT path; delete after each run) because long paths break AF_UNIX tests;
delete build output when a step passes; pnpm store /tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/
pnpm-store-p20 with --frozen-lockfile; no new packages; no credential-shaped literals; no real network; never pkill by pattern;
never git stash; never write under the real ~, /root or /. Each change: update the relevant P24 task note's Status and the
ACC rows your demo tests pin. D-ids: list under "Decisions for the lead". Never commit.
Before finishing: ruff check / format --check / pyright on your paths, lint-imports, pytest on the dirs you touched plus
tests/contract and tests/integration/gateway (short basetemp), tooling.checks --require-ref; TS checks if you touch TS.
Report: files, wiring done (each D-16.799 item: done / partial / not done), demo tests, check results, decisions, follow-ups.
