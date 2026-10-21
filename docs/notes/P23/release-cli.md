# Task: Release command and attempt refs (P23-22) Phase: 23 Owner: wave-36 builder U1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `scripts/release.py` (argparse,
`main` dispatch, the changelog helpers), `scripts/releases/stable.py` (`admit_claim`,
`validate_claim`, `tagger_epoch`, `check_claim`), `scripts/releases/sequencer.py` (`plan` ordering,
`classify_runs`), `website/docs/developer-guide/stable-releases.md` ("Run, publish, or abandon").
Research 10 §3.17, §3.21, trap R10.29. Closed before writing. Evidence: none.

What it does (own words): a maintainer cuts a release with
`scripts/release/release.py --commit <sha on origin/main> [--bump patch|minor|major] [--canary]
[--autopublish] [--lanes a,b] [--dry-run]`. The command refuses while any earlier attempt is still
open (no abandon marker and no final tag) — without a terminal it just refuses; on a terminal it
offers to abandon it, default no. The commit must be on main and descend from the newest
`vX.Y.Z`. It derives the next version (P23-21), checks that every commit author since the last tag
is in the contributor map, then pushes an annotated attempt ref `rc.<N>-v<version>` whose message is
the claim (commit, attempt, kind, autopublish, lanes, epoch) — pushing it is the lock: a second
cutter loses the push. It creates one draft GitHub release on the attempt ref with a "do not
publish by hand" block above and below the changelog, and dispatches `release.yml` on the default
branch with the attempt ref as input. `abandon --version V` writes the marker ref
`abandoned-rc.<N>-v<V>` (the attempt ref itself stays) and deletes the draft. Agents only ever run
`--dry-run`, which prints every check and the full plan and creates nothing (no fetch, no push, no
API call).

Inputs → outputs: git repo + GitHub client + contributor map → plan lines, attempt ref, draft,
dispatch; exit 0 / 1 (refused or a check failed) / 2 (usage).

Files: `scripts/release/release.py`, `scripts/release/changelog.py`, `scripts/release/lanes.py`,
`contributors/README.md`, `tests/unit/release/test_release_cli.py`,
`tests/fakes/release/fake_github.py`.

Edge cases / failure modes:
- the commit is not on main / not a descendant of the newest release / unknown → refused;
- the next version already has a final tag (a publication still finishing) → refused;
- an attempt push rejected (someone else claimed) → refused with the winner's ref;
- unknown lane name → usage error;
- an unmapped author → the release is refused (stable) or warned (canary, so the daily canary does
  not stall);
- epoch: the tagger time, forced above every earlier claim's epoch (monotonic).

Our design differences / decisions:
- the contributor map is one file per author under `contributors/`, named by the first 16 hex of
  SHA-256 of the lower-cased email (one file per email, but no email in a file name; R10.29); the
  file holds the public credit name. It starts empty;
- lanes: `ci`, `docker`, `nix`, `install-e2e`, `desktop-mac`, `desktop-win`, `desktop-linux`,
  `upgrade`, all on by default; `--lanes` replaces the set;
- no commit/one-off builds, channel retirement, Store lanes (D-16.15, D-16.58);
- in a dry run the GitHub state is read from local tags only and the plan says so.

Tests we will write: fake `git` remote (a real bare repo) and an in-memory GitHub: open attempt
refused (non-TTY) and abandon-and-recut offered on a TTY; non-descendant commit refused; dry run
prints the plan and creates nothing; claim message fields; unmapped author fails; abandon writes the
marker ref and deletes the draft.

## Decisions for the lead
- Hashed contributor file names (see above); canaries warn on unmapped authors instead of failing.

Compare checklist (filled after the rewrite; reopened the release entry, stable claim helpers and
the sequencer):
- [x] one open attempt of any version blocks a new cut; non-TTY refuses, TTY default no
  (test: `tests/unit/release/test_release_cli.py::test_open_attempt_refuses_without_a_terminal`)
- [x] commit must have the newest release as an ancestor (`::test_non_descendant_commit_is_refused`)
- [x] claim binds commit, attempt, autopublish, lanes, epoch; epoch = tagger time
  (`::test_claim_message_fields`)
- [x] abandon keeps the attempt ref and writes the marker (`::test_abandon_writes_the_marker_ref`)
- [x] draft carries the do-not-publish blocks (`::test_real_run_pushes_the_claim_and_drafts_once`)
- Deliberate differences: no `--only/--with/--without` (one `--lanes`); no `publish` subcommand
  here (the controller is `publish.py`, P23-23).
- Not taken: canary pruning, bundle env overrides, commit builds.

## Status

Done (2026-10-21). New top-level `contributors/` directory (README only).
