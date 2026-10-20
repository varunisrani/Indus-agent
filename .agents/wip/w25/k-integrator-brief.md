# Integrate P17 wave 2: K3 (P17-08 session host, P17-09 creation policy) + K4 (P17-10 tickets/ws_auth, P17-11 attach verbs, P17-12 RPC dispatcher)

Worktree: /tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/verify9 (from main). Do NOT commit.
The lead commits and lands. Never write under the real ~ or /root. No network.
Running in parallel (do not touch their areas): E4+E5 integrator (gateway adapters whatsapp_cloud, sms, line, ntfy,
irc; registry/config lock-key field), builders E6/E7/E8 (gateway/adapters teams, msgraph, google_chat, qqbot,
dingtalk, wecom). Expect overlaps in gateway/registry.py or config only if you touch them — avoid unless needed.

Patches (scratchpad/patches/, base d50d5d3): w25-k3.patch then w25-k4.patch. Read .agents/wip/w25/README.md (K3 + K4
sections) in full first, the P17 task notes (docs/notes/P17/daemon-host.md, creation-policy.md, tickets.md,
attach-verbs.md, rpc-dispatch.md), docs/plan/phases/P17-daemon-shared-backend.md rows 08–12 (+ Traps, Acceptance),
LEAD-NOTES "P17 wave 1 landing notes (integrator)", AGENTS.md, HANDOFF.md.

Steps:
1. `git apply --3way` K3 then K4; resolve as unions (errors.py codes, traps rows, acceptance rows).
2. Replace K4's stand-in with K3's real host: `AttachHost` (`creatable_sources()`, `capabilities()`) must be served by
   `DaemonSessionHost` (add those two sync reads if missing); swap StubHost in test_attach_verbs.py; delete
   tests/unit/gateway/daemon/_stubs_k4.py. `grep -rln '^# STAND-IN' src tests` must be empty.
3. Daemon-start wiring K4 listed, where the daemon already has the seams (do it; else record as owed to P17-13):
   TicketBook.for_home + AttachVerbs(host=DaemonSessionHost, ws_url=...); merge attach.handlers() into
   DaemonBridge.control_handlers(); attach.close() and book.revoke_all() at stop; gateway.ready carries profile_id /
   instance_id / replay_epoch. If building the DaemonSessionHost inside the daemon is not yet possible without P17-13+,
   record exactly what is missing.
4. LEAD RULING: the redaction of `indus-gateway-ticket.<t>` and of a `ticket` query value must be a FULL mask (no
   leading/trailing characters kept). Test it.
5. Consistency: one vocabulary for errors (runtime.* codes incl. policy_conflict/policy_refused, stale_generation,
   ticket refusal reasons) and states; layer rules (service below gateway/cli; ws_auth in service; nothing in service
   imports gateway).
6. ROOT-CAUSE: K3's wider run had 5 failures in tests/integration/core/test_d04_1_terminal_backstop.py /
   test_d04_1_terminal_tool_backstop.py under load (pass alone). The last five "load-only" failures in this repo were
   real bugs. Reproduce deterministically (e.g. simulate delayed scheduling around the backstop), find the cause, fix
   the product or the test's real flaw, and add a test that fails without the fix. If they are pure wall-clock
   performance assertions, they belong under the `perf` marker (run alone) — justify it in LEAD-NOTES.
7. Security review: tickets (secrets, single use, TTL, purpose/profile binding, digest storage, constant-time compare,
   never logged, spent on any failed redemption, query-string tickets refused); session-ticket owner-only verified by
   the OS peer check; loopback-only WS; RPC dispatcher refuses undeclared methods and unknown params, never logs params;
   creation policy refuses credential-shaped key refs and user:pass@ base URLs; viewers only see sessions they attached.
8. D-ids: D-16.420 … D-16.439 in docs/plan/16-what-we-change.md §2.7 (+ §3.1 range row). LEAD-NOTES section
   "P17 wave 2 landing notes (integrator)" incl. the wiring still owed to P17-13..27.
9. No conflict markers (grep '^<<<<<<< ' and '^>>>>>>> ').
10. Full gate, all green, no extras installed (`uv sync --locked`):
    uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run lint-imports
    INDUS_STRESS_SECONDS=10 uv run pytest -q -p no:cacheprovider -rf -m "not perf"
    uv run pytest -q -p no:cacheprovider -m perf     # alone, after
    uv run python -m tooling.checks --require-ref
    CI=true pnpm test --maxWorkers=1   (run `pnpm install --frozen-lockfile --offline` first if node_modules is missing)
    Never skip or mark tests to get green (except a justified move to `perf` per step 6).
11. Credential-literal scan of the diff, no stray processes or sockets, nothing under /root or ~.
12. Usage: the lead may message you to wrap up; then stop, reach a consistent state, report.
Stage the final tree with `git add -A`.
Report: files changed beyond the patches, stand-in removal, wiring done/owed, the backstop root cause, D-ids, security
findings/fixes, gate counts, follow-ups.
