# Integrate parked P16 adapters: E9 (P16-14 Feishu/Lark, P16-15 Feishu doc tools + comment intake) + E10 (P16-24 BlueBubbles, P16-28 SimpleX, P16-29 Buzz)

Worktree: /tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/verify11 (from main 79eade2). Do NOT
commit. The lead commits and lands. Never write under the real ~ or /root. No network. Nothing else runs in parallel.

Patches (scratchpad/patches/, base d50d5d3): w25-e9.patch then w25-e10.patch. Main has since landed P16-04..27 batches
(registry rows, PlatformEntry.lock_env, BUILTIN_PUBLIC_ENV, gateway/adapters/_jws.py, Adapter.set_drain_probe,
mask_number in _identity.py) and P17-01..12. Read .agents/wip/w25/README.md (E9 + E10 sections) in full, the five
builders' notes in docs/notes/P16/ (feishu.md, feishu-tools.md, bluebubbles.md, simplex.md, buzz.md), LEAD-NOTES
"P16 E6 + E7 + E8 landing notes (integrator)" (the latest precedent) and the E4+E5 / E2+E3 ones, docs/plan/phases/
P16-more-platforms.md rows 14, 15, 24, 28, 29, AGENTS.md, HANDOFF.md.

Steps:
1. `git apply --3way` E9 then E10; resolve as unions (contract conftest one import per case; schema rows; pyproject
   extras feishu, simplex, buzz; THIRD_PARTY_NOTICES; 00-layout pins; acceptance/traps rows).
2. Registry rows + BUILTIN_NAMES + BUILTIN_CREDENTIALS exactly as in the README; lock_env on stable ids (feishu
   FEISHU_APP_ID; bluebubbles BLUEBUBBLES_SERVER_URL; simplex SIMPLEX_WS_URL — consider whether it should be public env
   rather than a credential; buzz: the relay URL + derived pubkey is not readable from env, so choose BUZZ_RELAY_URL or the
   credential and record why). Hints from the notes.
3. P16-15 core wiring: add the toolset rows from the README (`feishu`, `indus-feishu`, indus-gateway includes) in
   tools/toolsets.py; call `feishu.tools.register_tools(tools)` in daemon/process.py after load_builtins; remove the
   test's temporary patch of the rows; add "feishu_" to core/untrusted.UNTRUSTED_PREFIXES if consistent with the module.
   Tests: default_toolsets("feishu") finds the bundle; the doc tools are registered in the daemon and absent elsewhere.
4. Re-lock (`uv lock`): coincurve 21.0.0 is the only new package (buzz extra). Read its licence from the installed
   dist-info in a throwaway venv and confirm MIT OR Apache-2.0; flag anything else. Pin rows in 00-layout §4.
5. `indus send` tests through the real send_command for feishu, bluebubbles, simplex, buzz (send-only).
6. Port clashes: BlueBubbles default 8645 = WeCom callback default; Graph and LINE both 8646. Record them for P16-36
   doctor; do not renumber defaults unless the phase file says so.
7. Security review: Feishu webhook signature on the raw body before parse, verification token constant-time, encrypted
   mode refuses plaintext, AES via cryptography; doc tools fence untrusted output, cap sizes, never open links;
   BlueBubbles derived token constant-time, password only inside the transport; SimpleX loopback-only by default and
   files only inside files_folder; Buzz event id + BIP-340 signature verified with coincurve (no EC maths in src/ — the
   _jws guard test style can be extended to secp256k1 constants if cheap); secrets never in logs/reprs/errors/argv.
8. D-ids: D-16.460 … D-16.479 in docs/plan/16-what-we-change.md §2.7 (+ §3.1 range row). LEAD-NOTES section
   "P16 E9 + E10 landing notes (integrator)".
9. No `# STAND-IN` first line anywhere; no conflict markers (grep '^<<<<<<< ' and '^>>>>>>> ').
10. Full gate, all green, no extras installed (`uv sync --locked`):
    uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run lint-imports
    INDUS_STRESS_SECONDS=10 uv run pytest -q -p no:cacheprovider -rf -m "not perf"
    uv run pytest -q -p no:cacheprovider -m perf     # alone, after
    uv run python -m tooling.checks --require-ref
    CI=true pnpm test --maxWorkers=1   (run `pnpm install --frozen-lockfile --offline` first if node_modules is missing)
    Also run the Buzz coincurve path once with `--extra buzz` (BIP-340 vectors). A failure that passes alone must pass
    alone 3x AND be root-caused; never skip or mark tests. Never pkill by a pattern that could hit other worktrees' runs.
11. Credential-literal scan of the diff (incl. PEM, JWT, nsec1…, hex private keys), no stray processes, nothing under
    /root or ~.
12. Usage / restarts: the lead may message you to wrap up; if the container restarts, re-check the tree and re-run
    killed steps. Stage the final tree with `git add -A`.
Report: files changed beyond the patches, registry rows + lock keys, P16-15 wiring, coincurve licence, D-ids, security
findings/fixes, gate counts, follow-ups.
