# Integrate P16 E12 (P16-07 Matrix E2EE + P16-19 Weixin) + E13 (P16-02/03 WhatsApp bridge) + E14 (P16-21/22 Yuanbao + P16-30 Raft)

Worktree: /tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/verify15 (from main). Do NOT commit.
The lead commits and lands. Never write under the real ~ or /root: ALWAYS set INDUS_HOME (and HOME) to a temp dir before
running any `indus` command by hand. No network (the python-olm sdist build may be skipped if it needs a download; the
extras lane is optional, the base gate is not). Never pkill by pattern. No credential-shaped literals in tests. Clean room:
never copy reference code.

Patches (scratchpad/patches/): apply in this order with `git apply --3way`: w27-e12.patch (base 90209a7), w27-e13.patch
(base 90209a7), w27-e14.patch (base 846a142). Read .agents/wip/w27/README.md (E12, E13, E14 sections, incl. "Wiring
(core)" lists) in full first, the task notes in docs/notes/P16/ for these tasks, docs/plan/phases/P16-*.md rows 02, 03, 07,
19, 21, 22, 30 (+ Traps, Acceptance, Run until green), LEAD-NOTES P16 sections, AGENTS.md, HANDOFF.md.

Steps:
1. Apply in order; resolve as unions (registry, BUILTIN_NAMES order, schema rows, targets.py rules, gateway_setup STEPS,
   conftest, THIRD_PARTY_NOTICES, 00-layout pins, traps/ACC rows, matrix shared files).
2. Do the "Wiring (core)" for all three exactly as the README lists: registry rows (whatsapp, weixin, yuanbao, raft),
   BUILTIN_CREDENTIALS (weixin, yuanbao), BUILTIN_PUBLIC_ENV (raft), lock_env, toolsets yuanbao / indus-yuanbao /
   indus-gateway, UNTRUSTED_PREFIXES, gateway_tool_registry, cron KNOWN_PLATFORMS, send_command/directory, target rules
   (weixin, whatsapp), `indus send` test cases, Matrix install hint. Then delete tests/unit/gateway/adapters/yuanbao/
   _stubs_e14.py and every STAND-IN (`grep -rln '^# STAND-IN' src tests apps` must be empty).
3. Extras: add matrix-e2ee (Linux-only marker), weixin, yuanbao with the exact pins in the README; re-lock with
   `uv lock` (offline cache if possible; if the lock cannot be regenerated offline, report and stop at that step rather
   than hand-editing uv.lock). licenses_allow.json: check each new dist's licence from its metadata; NO copyleft. Record
   pycryptodome (BSD-2/public domain) and libolm (Apache-2.0, deprecated upstream) in THIRD_PARTY_NOTICES.
4. LEAD DECISION, Baileys (owner has not answered): ship WITHOUT the Baileys mapping. Delete `bindBaileys` and anything
   that names @whiskeysockets/baileys, libsignal or sharp from code, tests and docs except one neutral note in the E13 task
   note / LEAD-NOTES: "provider is operator-supplied through the WaSocket interface; no provider ships; Baileys pending an
   owner decision (copyleft deps)". The bridge must still start and fail closed with a clear error when no provider is
   configured; keep the fake-provider tests.
5. Security review: Matrix keys only to verified devices, store 0700/0600 + flock, recovery key only from SecretStore,
   `indus send` refused for encrypted rooms; Weixin tokens 0600, https Tencent hosts only for media; bridge loopback-only,
   constant-time token compare, minimal child env; Yuanbao credentials masked, yuanbao_send_dm approval each time; Raft
   bridge.json 0600; vendor tokens never in argv or logs.
6. D-ids: D-16.524 … D-16.539 in docs/plan/16-what-we-change.md §2.7 (+ §3.1 range row) from the builders' "Decisions"
   (incl. the Baileys decision above and libolm behind our Engine interface). LEAD-NOTES section
   "P16 wave 27 landing notes (integrator)" incl. follow-ups and what remains (P16-25, 33–36).
7. No conflict markers (grep '^<<<<<<< ' and '^>>>>>>> ').
8. Full gate, all green, no extras installed (`uv sync --locked`):
   uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run lint-imports
   INDUS_STRESS_SECONDS=10 uv run pytest -q -p no:cacheprovider -rf -m "not perf"
   uv run pytest -q -p no:cacheprovider -m perf     # alone, after
   uv run python -m tooling.checks --require-ref
   CI=true pnpm test --maxWorkers=1   (pnpm install --frozen-lockfile --offline first if node_modules is missing)
   plus `pnpm -r typecheck`. A failure that passes alone must pass alone 3x AND be root-caused; never skip or mark.
9. Credential-literal scan of the diff, no stray processes/sockets/bridges (stop anything you started), nothing under
   /root or ~.
10. Usage / restarts: the lead may message you to wrap up (then stage what you have and report where you stopped); if the
    container restarts, re-check the tree and re-run killed steps. Stage the final tree with `git add -A`.
Report: files changed beyond the patches, wiring done, stand-ins removed, lock/licence results, Baileys removal, D-ids,
security findings/fixes, gate counts, follow-ups.
