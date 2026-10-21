# Land P24 (the integrated tree in .agents/wip/w40/p24-int-wip.patch) on main, which now has P23

Worktree: /tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/verify20 (from main dfd60d7). Do NOT
commit; the lead commits and lands. ENV RULE: `export S=/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/land24`
as ONE statement, then `export HOME=$S/home XDG_CACHE_HOME=$S/home/.cache UV_CACHE_DIR=$S/home/.cache/uv
npm_config_cache=$S/home/.npm ELECTRON_CACHE=$S/electron-cache` as a SECOND statement, BEFORE the first uv/pnpm command. Leave
TMPDIR unset. Never write under the real ~, /root or /. Never pkill by pattern. Never `git stash`.

Read first: .agents/wip/w40/README.md, landing-p24.md (the P24 integrator's notes — the LEAD-NOTES section content),
licences-p24.txt; HANDOFF.md; AGENTS.md; docs/plan/phases/P24-multiplex-extras.md (Traps, Acceptance, Done when).

Steps:
1. `git apply --3way .agents/wip/w40/p24-int-wip.patch` (base 823caff; main has P23 fd44e16 on top). Resolve unions by reading
   both sides: docs/plan/16-what-we-change.md (P23 D-16.740..769 then P24 D-16.770..829, numeric order; §3.1 range rows),
   src/indus/cli/app.py REGISTRY (P23 uninstall / update + P24 rows), src/indus/foundation/config/schema.py and its test,
   tests/contract/cli/test_help_catalogue.py + non_tty_table.yaml, .github/workflows/ci.yml if it conflicts, any traps / ACC
   rows. uv.lock: take main's, then `uv lock` (online through the proxy; new extras relay, browser-cloud, vault, egress,
   wake-sherpa with sherpa-onnx + sherpa-onnx-core 1.13.8) and `uv sync --locked`. pnpm-lock must stay unchanged
   (`pnpm install --frozen-lockfile --store-dir /tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/pnpm-store-p20`).
2. Append the P24 landing notes to docs/plan/phases/LEAD-NOTES.md as "P24 waves 37–39 landing notes (integrator)" from
   landing-p24.md (plus a line on this landing). Check test_release_base / P23 checks still pass with P24's new desktop and
   web code (one release base).
3. FULL gate, every step to completion (the P24 integrator never finished the Python run or perf):
   uv sync --locked; ruff check; ruff format --check; pyright; lint-imports; `python -m indus.protocol.codegen --check`;
   scripts/install/build.py --check; install + docker + nix pins checks;
   `INDUS_STRESS_SECONDS=10 uv run pytest -q -p no:cacheprovider -rf -m "not perf"` (run it in the background with a log and
   poll; it takes ~35 min); then `uv run pytest -q -p no:cacheprovider -m perf` alone; `uv run python -m tooling.checks
   --require-ref`; pnpm lint, fmt:check, -r typecheck, check-ipc, gen-bridge-types --check, dump-slash-catalog --check;
   `CI=true pnpm test --maxWorkers=1`; the desktop Electron E2E lane under xvfb if Electron 44.5.1 can be fetched (zip checked
   against SHASUMS256) and the web Playwright lane (Chromium in /opt/pw-browsers). A failure that passes alone must pass alone
   3x AND be root-caused; never skip or mark. Fix real failures minimally and say exactly what you changed.
4. Credential-literal scan of the diff; no STAND-IN files; no conflict markers; no stray processes; nothing under /root or ~.
5. Stage with `git add -A`. If the lead asks you to wrap up: stage what you have and report where you stopped.
Report: conflicts resolved, uv lock result, every gate step with counts and rc, failures found + root causes + fixes, files
changed beyond the patch, follow-ups.
