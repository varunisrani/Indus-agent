# Task: Canary dress rehearsal and update from the last canary (P25-22) Phase: 25 Owner: wave-44 builder R2

Status: done for the automated half (2026-10-22), landed with wave 44 (F-1 fixed at the integration,
D-16.960); the real canary run is the owner's. Details at the end.

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): none — no reference product file
applies. Process only: the clean-room-rebuild skill's reference 08 §6 (dress rehearsal on the
release commit, dry run first) and §8 (update from the last pre-release build: offered without a
channel switch, version reported, data intact). Our own P23 notes (`docs/notes/P23/update-*.md`,
`versioning.md`, `release-workflow.md`, `install-sh-*.md`). Evidence: none.

What it does (own words): before `v1.0.0` the owner runs the P23 release workflow in canary mode on
the candidate commit — a dry run first, then for real — and on each OS a machine that already has
the previous canary and its data updates to the new build. The source-install half of that check
is automated: `tests/e2e/upgrade/test_canary_to_stable.py` builds two releases locally from this
checkout's own source tree (a canary `X.Y.Z-canary.<stamp>`, then the stable `X.Y.Z` it leads to,
named by `scripts/release/versioning.py`), installs the canary with the real `install.sh` stages
from a local bare "origin", gives the install some data through the canary's own CLI (a session
with one turn against the scripted model server, a credential in the secret store, a second
profile, the launcher), then publishes the stable and runs the canary's own `indus update` through
the launcher. It checks what reference 08 §8 asks for: the update is offered on the saved `canary`
channel without switching (a canary install reads stable tags too), the version is reported
(`--check`, `--plan`, the finish line, the receipt), and the data is intact (the session resumes
with the new code, the credential, the profile and the launcher are still there, the channel is
still `canary`). The desktop updater half, signing, the image push and the three-OS runs need the
owner's machines and secrets; `docs/internals/rehearsal-log.md` is the template the owner fills.

Inputs → outputs: this checkout's tracked source files (pyproject, lock, `src/`, `skills/`,
`locales/`, the build hook, `.gitignore`) → two tagged commits in a temp bare repository → a
canary install in a temp home → `indus update` exit codes, its `--plan` JSON, the receipt, the
checkout's HEAD and status, and the data read back with the new code.

Files: `tests/e2e/upgrade/__init__.py`, `tests/e2e/upgrade/world.py` (the shared temp world),
`tests/e2e/upgrade/conftest.py`, `tests/e2e/upgrade/test_canary_to_stable.py`,
`docs/internals/rehearsal-log.md`, `pyproject.toml` (the `release` marker),
`.github/workflows/release.yml` (the release lane runs the test), `docs/plan/16-what-we-change.md`
(D-16.935, D-16.936).

Edge cases / failure modes:
- no network: every `uv` child runs with `UV_OFFLINE=1`, `UV_PYTHON_DOWNLOADS=never` and the
  running interpreter as `UV_PYTHON`; packages come from the uv cache the lane's own `uv sync`
  filled. The installer's `runtime` and `dependencies` stages download a pinned uv and sync the
  standard extras, so the test does the dependencies step itself (the same `uv sync --frozen
  --no-dev`) and saves an empty extras list (the cache holds the base set only); the real stages run
  for everything else (prerequisites, repository, config, products, complete);
- the update hands off with `exec` to the new code's `--finish`, so it runs in a child process,
  never in the test's interpreter;
- the one-shot turns use `--local` so no gateway daemon is started (a daemon would outlive the test);
  the scripted model server is our own child, stopped by closing its stdin;
- a source install's `indus --version` prints the source tree's `0.0.0` (releases stamp the version
  into build trees only, P23-01); the version a source install is on comes from its release tag
  (`indus update --check` / `--plan`), so the test reads it there;
- disk: the two releases share one bare repository; each venv is hard-linked from the uv cache; the
  test removes its checkout, venvs and repositories when it ends.

Our design differences / decisions:
- a `release` marker; the test runs only when `INDUS_RELEASE_LANE=1` (release.yml's `ci` job sets it
  for its test step) and skips with that reason elsewhere, so the PR gate stays fast;
- POSIX only (`platforms("posix")`): the Windows source install is the owner's rehearsal row.

Tests we will write:
- canary install → stable published → `--check` offers it on `canary` (exit 10) → `--plan` names
  from/to versions and `channel_source: saved` → `indus update -y` via the launcher → HEAD on the
  stable tag, clean checkout, `.venv-prev` kept, receipt `ok` → `--check` up to date, channel still
  canary → session listed and resumed by the new code, credential, profile and launcher intact.

## Decisions for the lead
- D-16.935: the `release` marker and `INDUS_RELEASE_LANE=1` (release.yml `ci` job, test step).
- D-16.936: how the two releases are built and installed offline (extras saved as `[]`).
- Findings for the triage (rehearsal log §5): F-1 the installers are not attached to releases
  (release blocker for the documented install command); F-4 a source install's `--version` prints
  `0.0.0`.

Compare checklist (no reference file; checked against skill reference 08 §6 and §8):
- [x] the update is offered without switching channel: a canary install with the saved `canary`
  channel is offered the next stable (`--check` exit 10, `--plan` `channel_source: saved`)
  (test: `tests/e2e/upgrade/test_canary_to_stable.py::test_a_canary_install_updates_to_the_next_stable_with_its_data`)
- [x] the new version is reported: `--check` before and after, `--plan` from/to, the finish line,
  the receipt's `to_version` (same test)
- [x] data survived: the session lists and resumes with the new code (2 → 4 messages), the secret
  store and credential pools are byte-for-byte unchanged and `auth list` shows the credential,
  the second profile, the launcher unchanged and executable, the channel still `canary` (same test)
- [x] the checkout is clean on the stable tag, `.venv-prev` kept, the update marker released, no
  gateway left running (same test)
- [x] dry run before the real canary run, every OS logged: `docs/internals/rehearsal-log.md` §2–§3
- Deliberate differences: the dependencies step is ours (offline), not the installer's stage
  (D-16.936); the desktop updater half and the keychain are owner rows.
- Not taken: Windows source install in the test (POSIX venv layout; owner row).

## Status

Done (2026-10-22) for what an agent may do. `tests/e2e/upgrade/test_canary_to_stable.py` passes
in the release lane here (`INDUS_RELEASE_LANE=1`, ~25 s, uv offline from the shared cache); it
skips with its reason elsewhere. release.yml's `ci` job sets the flag for its test step (workflow
shape tests green). The rehearsal log template is ready for the owner. Open (owner): the real
canary dry run and run, signing (P23-15/16), the per-OS update rows, the desktop updater half,
the keychain checks. Landed with wave 44 (integration on main `7d9a864`): the release lane ran
green again there (`INDUS_RELEASE_LANE=1 pytest tests/e2e/upgrade`, offline) and is now also the
`upgrade` release gate (D-16.961); F-1 is fixed (D-16.960, RM30) and F-4 is RS25. Checked and not a gap: the repository's `.gitignore` names only `.venv/`,
yet `.venv-prev` / `.venv-next` never show as untracked (a later update would stash them) because
uv writes a `.gitignore` into every venv it creates; the test asserts a clean `git status`.
