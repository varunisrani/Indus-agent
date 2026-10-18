# Task: repository skeleton, rules, CI and agent mailbox (P00-scaffold) Phase: 00 Owner: P00 scaffold builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `LICENSE` only (its text is reproduced
in `THIRD_PARTY_NOTICES.md` as the licence requires). No product code was read: this task builds the
empty project frame, not a reference feature. Our own plan was read: `docs/plan/README.md` (P1–P8).

What it does (own words): gives the repository everything a builder needs before the first feature
lands. A uv-managed Python package `indus` with a placeholder `indus` command, a smoke test, lint and
format settings, and a locked dev toolchain. Rules files (`AGENTS.md`, `CLAUDE.md`) and the written
clean-room protocol tell every agent how to read, note, rewrite, compare and check. CI runs lint,
format, tests and the clean-room guards against the pinned reference fetched outside the workspace,
and checks the PR body on pull requests. The `.agents/` mailbox and `HANDOFF.md` let two agents on two
accounts pass the work between them through the branch alone.

Inputs → outputs: `uv sync` → a venv with pytest and ruff; `indus --version` → `indus 0.0.1`;
`indus` → one line saying nothing is implemented yet, exit 0; push / PR → CI job `checks`.

Files: `pyproject.toml`, `uv.lock`, `src/indus/{__init__,__main__,cli}.py`, `tests/test_smoke.py`,
`.gitignore`, `.python-version`, `.editorconfig`, `README.md`, `THIRD_PARTY_NOTICES.md`, `AGENTS.md`,
`CLAUDE.md`, `docs/plan/01-clean-room-protocol.md`, `docs/notes/README.md`, `docs/notes/_template.md`,
`.github/workflows/ci.yml`, `.github/pull_request_template.md`, `.agents/*`, `HANDOFF.md`.

Edge cases / failure modes:
- `indus` with unknown arguments: argparse prints usage and exits 2 (standard behaviour, fine for now).
- CI reference fetch: a wrong or missing commit must fail the job (SHA is verified after checkout).
- `tooling/checks` and `tooling.checks.prbody` are written in parallel by another builder; CI calls
  them by module name and assumes exit 0 = ok, 1 = violations, 2 = setup problem.
- pytest `testpaths` names `tooling/checks/tests`, which does not exist in this worktree yet; pytest
  skips a missing testpath with no error.
- The mailbox scripts run from any checkout; they never touch the working tree except committing
  named paths.

Our design differences / decisions:
- Product names only: Indus Agent, `indus`, `INDUS_`, `~/.indus`. No reference names under `src/`.
- No runtime dependencies yet. Dev group pinned: pytest 9.1.1 (2026-06-19), ruff 0.16.10
  (2026-10-01); 0.17.x is newer than two weeks, so not taken.
- Build backend hatchling; `src/` layout.
- The CLI uses argparse for the placeholder; the real CLI framework is chosen in the CLI phase.

Tests we will write:
- `tests/test_smoke.py`: package version string; `main(["--version"])` output; `main([])` prints the
  placeholder line and returns 0; `python -m indus --version` in a subprocess.

## Decisions for the lead
- **Product licence.** No `LICENSE` file for Indus Agent was added: choosing the product's own
  licence is the owner's decision. `pyproject.toml` therefore carries no `license` field yet.
- **Similarity allow-list.** `THIRD_PARTY_NOTICES.md` reproduces the reference's MIT licence text
  word for word (required by the licence). The prose similarity guard will match it; the guards
  builder should allow-list that file (or the licence block) with the reason "licence text that the
  MIT licence requires us to reproduce".
- **`.agents/` scripts.** Copied from the clean-room skill's tooling scripts with the default branch
  set to `main`; they are process tooling, not product code.

Compare checklist (filled after rewrite; reopened `LICENSE`):
- [x] Licence text and copyright line match the reference `LICENSE` exactly (diffed).
- Not taken: everything else in the reference; this task builds no reference feature.

## Status

Done (2026-10-18). All files above created; nothing outside them touched. Left uncommitted for the lead.
