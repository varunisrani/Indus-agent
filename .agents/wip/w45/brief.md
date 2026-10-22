# Wave 45 brief: open security findings + P24 partial items

Base: main `7d9a864` (wave 43 security landed). Wave 44 (release prep: scripts/release, docs/internals/release-checklist.md,
rehearsal-log, rollback, backends/local_windows.py, registry.py, known_dirs.py, release.yml, ci.yml) is being integrated in
parallel: do NOT touch those files. Read in full first: AGENTS.md, HANDOFF.md, docs/internals/threat-model.md (rows and
findings named below), docs/plan/phases/LEAD-NOTES.md sections "P24 wiring wave 42 landing notes (integrator)" and "P25 wave 43
landing notes (integrator)", D-16.878 in docs/plan/16-what-we-change.md. Clean room: never copy reference code.
Task notes: add a "Wave 45" section to the existing note of the area you change (Status at the top).

Slices:
- S45 (security, D-16.970..979): fix the three open findings in the smallest correct way, each with a regression test in
  tests/security (cite it in the threat-model row; the doc test checks paths) and flip the finding to fixed:
  - SEC-14: one language-server tier per process; on a multiplex host servers for profile B get profile A's child env and
    lsp.servers. Make the tier per profile scope (keyed by the profile's home) and prove B's server gets B's env/settings.
  - SEC-15: room turn prompts list other members' replies as unfenced `who: text` lines; a newline can forge a line in the
    person's name. Frame the transcript as data (reuse core.untrusted's fence) so a member's reply cannot forge a speaker.
  - SEC-18 (four small items): IpcRegistry.seal() must sender-check unregistered sync channels too; gated mode must refuse an
    unsafe request with no Origin (and no Referer) unless it carries the non-browser credential path the docs allow (read the
    code; choose the safe rule, record it); prefer the `__Host-` cookie name on https and refuse the unprefixed name there;
    SPA script CSP: replace the one inline bootstrap with a hash or external script so `script-src 'self'` holds. TS changes
    need pnpm lint / fmt / typecheck / check:ipc and the vitest files you touch.
- P45 (P24 partial items from D-16.878, D-16.980..989):
  - `message_agent` offered in bot-chat turns (per the P24 task note's intent; respect the profile's toolset policy).
  - suggestion offers when an integration is connected (not only from reviews).
  - LspUpkeep.release before a worktree is removed, and `indus lsp restart` (CLI + protocol verb if the plan names one; run
    `python -m indus.protocol.codegen` and keep `--check` green; regenerate reference pages with gen_reference if they change).
  Leave the remaining desktop contribution areas and the RFC 8252 broker alone (record them as still open).
  S45 also edits the LSP tier: P45 must not change tools/lsp internals beyond adding release/restart entry points — if you need
  more, record it.

Rules (ENV RULE), for builder <x> in {s45, p45}:
- `export S=/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/<x>` as ONE statement, then a SECOND
  statement: `export HOME=$S/home XDG_CACHE_HOME=$S/home/.cache UV_CACHE_DIR=/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/uv-cache-shared npm_config_cache=$S/home/.npm INDUS_HOME=$S/indus`.
  Repeat both in every Bash call. TMPDIR unset. Never run `indus dashboard` by hand.
- Use your own worktree's `.venv` (`uv sync --locked`), not the main checkout's.
- pytest ALWAYS with `--basetemp=/tmp/<x>pt` (SHORT path; delete after each run). pnpm store scratchpad/pnpm-store-p20 with
  --frozen-lockfile (only if you need TS).
- No new packages; no credential-shaped literals (build fake tokens at runtime); no real network; never pkill by pattern;
  never git stash; never write under the real ~, /root or /. Never commit. Delete build output when a step passes.
Before finishing: ruff check / format --check / pyright on your paths, lint-imports, `python -m tooling.checks security_static`,
pytest on the dirs you touched plus tests/contract and tests/security (short basetemp), tooling.checks --require-ref; TS checks
if you touch TS.
Report: files, each item done / partial / not done, tests (fail-first where it is a fix), check results, decisions (D-ids),
follow-ups.
