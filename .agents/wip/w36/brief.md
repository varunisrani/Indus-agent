Wave 36 (P23 packaging and release, all tasks except P23-15/16 desktop artefacts which wait for P20 integration) builds from
main d5e575e. Landed: P00–P19, P21, P22; P20-01..24 (desktop; P20-25..39 parked in .agents/wip/w35 and being finished in
parallel — do not touch apps/desktop). Read in full first: docs/plan/phases/P23-packaging-release.md (your rows, Files,
Fixtures, Traps, Acceptance, Run until green, Done when), AGENTS.md, HANDOFF.md, docs/plan/phases/LEAD-NOTES.md (P07, P17,
P20 sections your rows rely on), docs/plan/16-what-we-change.md §2.7 rows for P23 and D-16.56 / D-16.58 / D-16.64 / D-16.690
(release base: owner decision pending — read it from ONE config constant so it can be changed in one place), and the code
your rows build on (cli/, service/, scripts/, .github/workflows/).

Slices (each owns only its tasks' files; stand in other slices' pieces only under tests/.../_stubs_<slice>.py with FIRST
LINE "# STAND-IN: removed at integration", and state the interface you assumed):
- I1 = P23-01 no-PyPI build guard + packaging metadata, P23-02 bootstrap pins, P23-03 install.sh core, P23-04 install.sh
  repository stage, P23-05 install.sh runtime stages. The stage manifest / frame shape is the contract P20-09's desktop
  installer runner already drives (apps/desktop/electron/installer/stageProtocol.ts): match it exactly, do not change it.
- I2 = P23-06 install.ps1 core and stages, P23-07 install.ps1 Windows bootstrap + cmd shim, P23-20 uninstall finished.
  (No Windows here: test the PowerShell build output statically and with pwsh only if it is installed; skip cleanly
  otherwise.)
- K1 = P23-08 product Docker image, P23-09 sandbox images, P23-10 container entry (dispatcher, stage-2, wrapper), P23-11
  compose files + image lint, P23-12 Docker publishing workflow. No docker daemon here: test Dockerfile/compose statically
  (lint, structure, pinned digests as constants) and the entry scripts directly; publishing workflow never runs.
- N1 = P23-13 Nix flake and packages, P23-14 NixOS and home-manager modules. No nix here: write the files, test what can
  be tested (structure, option names, generated pins), and record what needs a nix CI lane.
- U1 = P23-17..19 update (install kinds, channels, plan; apply, verify, rollback; finish, restart, receipts, CLI) +
  P23-21 versioning and channel records, P23-22 release command and attempt refs, P23-23 release workflow and publication
  controller. Git operations in tests use local temp repos only (no network, no real remotes); releases never publish.

Rules:
- ENV RULE: `export S=<your scratch dir>` as ONE statement, then `export HOME=$S/home XDG_CACHE_HOME=$S/home/.cache
  UV_CACHE_DIR=$S/home/.cache/uv npm_config_cache=$S/home/.npm` as a SECOND statement. Do NOT set TMPDIR to the long
  scratchpad path (Unix socket paths > 108 bytes break tests). Never write under the real ~, /root or /. Shell scripts
  under test run with HOME and INDUS_HOME pointing into pytest tmp_path.
- Installers must never curl | sh anything unpinned: every download is pinned by URL + SHA-256 from pins.json; the mirror
  is used only on the network error codes the row lists. No real network in tests (fake curl / fake HTTP on loopback).
- No new runtime Python dependencies; dev-only tools only if the row needs them (exact pins, allowed licences, uploaded >= 14
  days before exclude-newer 2026-10-04; leave uv.lock to the lead and list each package).
- No credential-shaped literals anywhere; signing keys / tokens only as names of env vars, never values; never pkill by
  pattern; stop every process you start.
- D-ids: do not number them; list intended differences under "Decisions for the lead" in each task note.
- Every task: note first in docs/notes/P23/<slug>.md; Read the reference paths in your row, close them, write; Compare
  after; tests per Verify; update only your trap/ACC rows. Clean room: never copy reference code, comments or docs.
- Work only in your worktree; never commit.
Before finishing: uv run ruff check <paths>, uv run ruff format --check <paths>, uv run pyright <paths>, uv run lint-imports,
uv run pytest <your test dirs> -q -p no:cacheprovider -m "not perf", uv run python -m tooling.checks --require-ref; shellcheck
on your shell scripts if available.
Usage: the lead may message you to wrap up. Then stop starting new tasks, get finished tasks passing, report done / partial.
Report: files (new / shared), task status, test/check results, wiring for the lead, decisions, packages + licences,
follow-ups.
