# Integrate P23 (w36 I1, I2, K1, N1, U1): installers, uninstall, Docker, Nix, update, versioning, release

Worktree: /tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/verify19 (from main 7cd4273). Do NOT commit;
the lead commits and lands. ENV RULE: `export S=/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/int36`
as ONE statement, then `export HOME=$S/home XDG_CACHE_HOME=$S/home/.cache UV_CACHE_DIR=$S/home/.cache/uv
npm_config_cache=$S/home/.npm` as a SECOND statement, BEFORE the first `uv` command. Leave TMPDIR unset. Never write under the
real ~, /root or /. Never pkill by pattern. The P20-25..40 integrator runs in parallel in verify18 (apps/desktop): do not
touch apps/desktop except where step 3 says so, and then only in your tree.

Patches (all base d5e575e; apply with `git apply --3way` in this order): .agents/wip/w36/w36-i1.patch, w36-i2.patch,
w36-k1.patch, w36-n1.patch, w36-u1.patch. Read .agents/wip/w36/README.md (every section — each lists what to reconcile) in
full first, docs/notes/P23/*.md, docs/plan/phases/P23-packaging-release.md (+ Traps, Acceptance, Run until green, Done when),
AGENTS.md, HANDOFF.md.

Steps:
1. Apply in order; resolve unions: cli/app.py REGISTRY (uninstall before acp, update after acp), contract cli golden order +
   non_tty_table, traps/ACC rows, tests/unit/packaging/__init__.py, tests/integration/install/__init__.py,
   scripts/install/stages.json (I1's table + I2's dispatch_only and extras), docs/notes/P23/tool-versions.md (merge N1, K1,
   I1, I2 PowerShell 5.1/7, U1 action SHAs), pyproject.toml.
2. Reconcile (README lists details):
   - ONE release base: `RELEASE_REPOSITORY` in indus.foundation.versions is the source; install pins.json
     release.repository, docker/pins.json images, nix container.image default, install.cmd URL and the desktop update feed
     default must derive from it or be checked equal by a test. Keep the value `indusagi/indus-agent` for now (owner decision
     D-16.690 pending; the repo today is varunisrani/Indus-agent) and record it.
   - Install config key: installers (install.sh, install.ps1) write U1's `updates.installs.<id16>.channel` and
     `.extras`/skip as the schema defines; one name, tests on both sides.
   - Move I2's render_ps1 into I1's scripts/install/build.py (+ --check), delete _stubs_i2.py, rebuild install.ps1 from the
     real pins.json.
   - K1: Docker extras use `messaging` (+ web, …) per I1's extras; uv_artefact() keys match I1's pins.json shape.
   - release.yml ↔ docker.yml: align U1's workflow_call to K1's interface (commit, attempt_ref, phase test|publish, digest
     outputs). Desktop lanes: align to P20-38's scripts (dist:unsigned, output apps/desktop/build/release) — record as
     P23-15/16 work if signing scripts are missing.
   - N1's Nix refusal in U1's update channels ("Nix owns updates: run `nix flake update` and rebuild"); I2's uninstall install
     kind via U1's detector if it is a drop-in.
   - The update marker contract D4 (desktop) reads: `indus update` writes `<home>/.indus-update-in-progress` as
     {format: 1, owner, pid, process_start, created_at} (see docs/notes/P20/updates.md once P20 lands; if not on main yet,
     match the note in .agents/wip/w35/w35-d4.patch).
3. Network (allowed through the proxy for these only): `uv lock` (I1's dynamic version, extras messaging/all, hatchling pin);
   `uv run python scripts/install/pins.py update --uv 0.11.32 --git 2.51.0.windows.1` (verify Git for Windows version + arm64
   asset); `uv run python scripts/docker/pins.py refresh`; verify the pinned GitHub Action SHAs in docker.yml, release.yml,
   canary.yml (and the Nix lane you add) against the GitHub API; if a step cannot complete, leave the value null and record it.
4. Add the Nix CI lane N1 described (ubuntu + macos, pinned installer, flake lock, flake check, run --version) and the Docker
   lint lane hooks; add docker/, .github/, nix/ to tooling/checks scan lists if that is a small change (else record).
5. Security review: installers never download without a pinned digest; mirror only on network-class errors; no shell
   injection in install.sh / install.ps1 (quote everything; no eval of remote data); uninstall never deletes outside the
   install and data paths it lists, never pkills by name; update keeps .venv-prev and refuses managed installs; release
   tooling never publishes in tests; no secret values anywhere (names only).
6. D-ids: D-16.740 … D-16.769 in docs/plan/16-what-we-change.md §2.7 (+ §3.1 range row) from the builders' decisions.
   LEAD-NOTES section "P23 wave 36 landing notes (integrator)" incl. Done-when status (P23-15/16 still open) and owner items.
7. No conflict markers; no STAND-IN files.
8. Full gate: uv sync --locked; ruff check + format --check; pyright; lint-imports; codegen --check;
   `INDUS_STRESS_SECONDS=10 uv run pytest -q -p no:cacheprovider -rf -m "not perf"`; perf alone; tooling.checks --require-ref;
   CI=true pnpm test --maxWorkers=1 + pnpm -r typecheck (pnpm install --frozen-lockfile with store
   /tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/pnpm-store-p20 if node_modules is missing). A
   failure that passes alone must pass alone 3x AND be root-caused; never skip or mark.
9. Credential-literal scan of the diff; no stray processes; nothing under /root or ~.
10. Stage with `git add -A`. If the lead asks you to wrap up: stage what you have, report where you stopped.
Report: files changed beyond the patches, reconciliations done, network steps (what was fetched / still null), D-ids,
security findings/fixes, gate counts per step with rc, Done-when, owner items, follow-ups.
