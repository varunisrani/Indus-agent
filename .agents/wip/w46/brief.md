# Wave 46 brief: release-checklist items agents can close (parallel, time-boxed)

Base: main `5d33c85`. In parallel and NOT yours: wave-44 integrator (scripts/release/*, docs/internals/release-checklist.md,
rehearsal-log.md, rollback.md, src/indus/backends/local_windows.py, backends/registry.py, platform/known_dirs.py,
.github/workflows/release.yml), wave-45 builders S45 (tools/lsp tier, gateway/rooms prompt, desktop IpcRegistry.seal, web
cookies/CSRF, SPA CSP) and P45 (message_agent in bot chat, suggestion offers, LspUpkeep.release, `indus lsp restart`).
Do not touch those areas. The release checklist rows (RM/RS ids) are in `.agents/wip/w44/w44-r1.patch` (search `| RM`, `| RS`).
Read first: AGENTS.md, HANDOFF.md, the threat model (docs/internals/threat-model.md) where relevant, the task notes the row cites.
Clean room: never copy reference code. Task notes: a "Wave 46" section in the existing note of the area you change.
TIME BOX: about 35 minutes of work. Keep scope tight; if an item is bigger than that, do the safe part and record the rest.
Decisions: label them `W46-<slice>-<n>` under "Decisions for the lead" (the integrator assigns D-16 numbers).

Slices (builder id in brackets):
- [a] RM2 / SEC-4 fence laundering. Design (lead decision, owner may revisit): taint, not per-RPC fencing. If an
  `execute_code` script called any tool whose output is untrusted (core.untrusted.UNTRUSTED_NAMES) through its RPC, the
  script's final output reaches the model fenced; scripts still get plain JSON answers. Same for `delegate_task`: if the
  child turn read untrusted output, its returned summary is fenced. Tests in tests/security (fail-first), threat-model row
  + SEC-4 flipped to fixed.
- [b] RM8 re-lock: aiohttp 3.14.3 → 3.14.4, pynacl 1.5.0 → 1.6.2 (uv.lock), vite 8.3.2 → 8.3.3 (pnpm-lock.yaml), within the
  14-day exclude-newer rule (check upload dates; if one is too new, leave it and record). Network ONLY through the proxy for
  `uv lock` / `pnpm install` / the audit refresh. Then `python -m tooling.checks.deps_audit --refresh` (network mode) and drop
  the allow entries that no longer apply. certifi stays as is (owner decision RM7). Run the tests that the bumped packages
  touch (slack adapter, nacl users, vite build of apps/web) + tooling/checks/tests.
- [c] RM4: SEC-6 replace `isdigit()`-then-`int()` on outside text with a strict ASCII-digit parse helper with a length cap
  (one shared helper, every spot the threat model counts); SEC-8 pass an explicit built env to the dashboard's git update and
  doctor probes (the person's own editor may keep the person's env: record). Tests for the helper + a sample of spots.
- [d] RM21 the nine desktop `data-ui` hooks (`OWED_HOOKS` in scripts/parity/scenarios.py: onboarding dialog + five steps,
  transcript tool row, files and review pane toggles) and empty OWED_HOOKS; F-2 a desktop licences screen (shows the packaged
  THIRD_PARTY_NOTICES.md; reachable from settings/about). TS: pnpm lint / fmt / typecheck / check:ipc + vitest you touch.
- [e] RS16 remaining desktop contribution areas (title bar, palette command, keybind, theme) consuming the contribution
  registry, and the plugins inventory pane. TS checks as above. Do not edit the files [d] edits for hooks; coordinate by
  keeping to the contribution consumers.
- [f] RS4 TUI model picker: a custom endpoint whose /v1/models lists models must show them (setup already does); RS6 a daemon
  killed between a streamed reply and its terminal write must not show "a turn was lost" with Discard when the reply was
  saved; F-5 make `chat -q` with no terminal behave the same `--local` and attached (choose: refuse, matching --local; record).
- [g] RS7 `notices.py refresh` writes notices_data.json in the layout Biome accepts; RS9 shellcheck step in ci.yml for the
  installer fragments + release scripts (pin the shellcheck version; do not touch release.yml); RS19 `post_tool_call` fires
  for a call a `before` hook blocked (with blocked=True in the payload); RS11 socket tests use a short temp dir helper so a
  long basetemp does not break them, and fix the line-wrap dependency in test_manual_approval_denied_spawns_nothing.
- [h] RS10 user-guide pages (docs/user/) for the P24 features now wired: rooms, board, bots and peers, relay, themes, pets,
  vault, egress, `indus pm`, LSP, verify. Read the code and task notes for accurate behaviour; add to mkdocs nav;
  `mkdocs build --strict` and the docs tests must pass; gen_reference --check stays fresh.
- [k] F-3 a script install has no built dashboard pages: ship the prebuilt web dist with the install (decide where it is built:
  the release workflow's artefact is NOT yours — record what release.yml must add; make install.sh / the Python side use a
  bundled dist when present and give a clear message when absent); F-4 a source install's `indus --version` prints the
  installed release (from the install receipt / git tag) instead of 0.0.0.

Rules (ENV RULE), for builder <x>:
- `export S=/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/w46<x>` as ONE statement, then a SECOND
  statement: `export HOME=$S/home XDG_CACHE_HOME=$S/home/.cache UV_CACHE_DIR=/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/uv-cache-shared npm_config_cache=$S/home/.npm INDUS_HOME=$S/indus`.
  Repeat both in every Bash call. TMPDIR unset. Never run `indus dashboard` by hand. Use your worktree's own .venv
  (`uv sync --locked`, except [b] who re-locks).
- pytest ALWAYS with `--basetemp=/tmp/w46<x>` (SHORT path; delete after each run); only the dirs you touched plus
  tests/contract and tests/security. pnpm store scratchpad/pnpm-store-p20 with --frozen-lockfile (except [b]).
- No new packages (except [b]'s bumps); no credential-shaped literals; no real network (except [b] via the proxy); never pkill
  by pattern; never git stash; never write under the real ~, /root or /. Never commit. Delete build output when done.
Before finishing: ruff check / format --check / pyright on your paths, lint-imports, `python -m tooling.checks
security_static`, targeted pytest, tooling.checks --require-ref; TS checks if you touch TS.
Report: files, each item done / partial / not done, tests (fail-first for fixes), check results, decisions, follow-ups.
