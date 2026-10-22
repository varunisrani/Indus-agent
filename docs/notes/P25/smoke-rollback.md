# Task: Post-release smoke and rollback kit (P25-23) Phase: 25 Owner: wave-44 builder R2

Status: done for what an agent may do (2026-10-22), landed with wave 44 (F-1 fixed at the integration,
D-16.960); the runs on three OSes are the owner's. Details at the end.

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): none — no reference product file
applies. Process only: the clean-room-rebuild skill's reference 08 §9 (post-release smoke on a
clean machine per OS using only public downloads; the user rolls back: release back to draft, fix
forward, never unpublish or reuse a version). Our own P23 notes (`release-workflow.md`,
`versioning.md`, `install-sh-*.md`, `install-ps1*.md`, `uninstall.md`) and `scripts/release/publish.py`.
Evidence: none.

What it does (own words): three scripts the owner runs right after a release (and on the canary
rehearsal artefacts) on a clean machine or VM, one per OS. Each takes the release tag and works
only from what that release published: the installer script from the release's download URL, the
desktop package from the same release, and the files the installer put on disk (the release's own
checkout, which holds the scripted model server used as the fake-model endpoint). Everything runs
in a throwaway folder with its own `HOME` and Indus home, so a smoke never touches the person's real
setup. Steps: install, `indus --version`, `indus setup --non-interactive` (it prints the commands;
the script then points the model at the fake endpoint with `indus config set`), one turn where the
model asks for a command that needs approval (an unattended one-shot must refuse it and the reply
must say so), a second turn and a resume of the first session, `indus doctor`, the dashboard on
loopback (the page answers and the API refuses a request without the session token), the licences
file inside the desktop package, a desktop launch (a person confirms the chat window; recorded as
manual otherwise), and `indus uninstall -y` keeping the data. Each step prints one
`<step> PASS|FAIL|SKIP <detail>` line and a summary; the exit code is 1 when any step failed.
`--dry-run` prints the plan and the URLs and fetches nothing (the tests use it).

`docs/internals/rollback.md` is the owner's rollback sheet: what each switch stops (release back to
draft: the desktop feed, `install.sh`'s latest lookup and the download page; image aliases back to
the previous digests; the channel record edited by hand because the controller never moves it
backwards; source installs keep seeing the final tag, so fix forward with the next patch), the exact
commands with placeholders, and the never-do list (no tag or version deleted, moved or reused).

Inputs → outputs: `--tag vX.Y.Z` (or a canary tag), `--repository OWNER/NAME` (default the release
base, D-16.690), `--channel`, `--workdir`, `--desktop FILE|auto|skip`, `--dry-run` → step lines,
a summary, exit 0/1, a log folder.

Files: `scripts/release/smoke/linux.sh`, `scripts/release/smoke/macos.sh`,
`scripts/release/smoke/windows.ps1`, `docs/internals/rollback.md`,
`tests/unit/release/test_smoke_scripts.py`, `tests/e2e/upgrade/test_smoke_steps.py` (release
lane), `docs/plan/16-what-we-change.md` (D-16.937 – D-16.942).

Edge cases / failure modes:
- the dashboard pages are built on first launch in a source checkout (pnpm and Node needed); on a
  clean machine without them the dashboard step is a SKIP with that reason, not a hang (bounded wait);
- no display (CI VM, SSH session): the desktop launch is SKIP (manual); the licences check still
  opens the package (AppImage `--appimage-extract`, DMG `hdiutil attach -readonly`, Windows installer
  to a temp folder is not possible without running it, so the installed folder is read after a
  per-user install);
- a missing tool (curl, git, python for the fake endpoint) fails the first step that needs it with
  what to install;
- every child is bounded by a timeout; the fake endpoint and the dashboard are stopped by their own
  pid (never by pattern);
- `--dry-run` must not touch the network (the tests run it).

Our design differences / decisions:
- the fake-model endpoint is the release's own `scripts/parity/scripted_model_server.py` from the
  installed checkout (no extra download, and the same scripts the parity runs use);
- "one turn with an approval": `indus -z` auto-approves by design, so the smoke uses a finite
  `chat -q` without a terminal; it passes when the command did not run and the run either refused
  it (`--local`) or left the approval waiting for a terminal (through the daemon); the interactive
  approval is the person's check (`--interactive` resumes the session for them) (D-16.938);
- the smoke sets `security.secret_store: file` so nothing reaches the machine's keychain
  (D-16.939);
- the desktop app has no in-app licences screen today: the smoke checks that
  `THIRD_PARTY_NOTICES.md` is inside the package and reports the missing screen (follow-up).

Tests we will write:
- shellcheck over both shell scripts when shellcheck is installed (skip otherwise); `bash -n`;
- `--dry-run` of each shell script prints every step and only release-base URLs, and calls no
  tool (tripwire `curl`, `git`, `hdiutil`, `python3`, `uv` on PATH);
- usage errors exit 2 before anything runs; a `--workdir` holding files is refused;
- the default repository in all three scripts equals `RELEASE_REPOSITORY`;
- the PowerShell script is ASCII (Windows PowerShell 5.1), parses (pwsh when installed) and names
  the same steps;
- `rollback.md` names every alias and image `scripts/release/claims.py` moves, and every step;
- release lane: the shell script's post-install steps, sourced, pass on a locally built release.

## Decisions for the lead
- D-16.937 – D-16.942 (scripts, approval check, secret store, desktop checks, rollback, the
  release-lane pin of the steps).
- Findings for the triage (rehearsal log §5): F-1 the release workflow does not attach
  `install.sh` / `install.ps1` / `install.cmd` to releases, so the documented install command and
  the smoke's first step fail (must-fix before a public release); F-2 no in-app licences screen;
  F-3 a script install has no dashboard pages without pnpm/Node; F-5 attached vs `--local`
  `chat -q` treat an unanswerable approval differently (both safe).

Compare checklist (no reference file; checked against skill reference 08 §9):
- [x] install from public downloads only: every URL in the scripts is a release download of the
  chosen repository or loopback (test: `tests/unit/release/test_smoke_scripts.py::test_scripts_fetch_only_release_downloads`)
- [x] first run, one turn, approval, resume, doctor, dashboard gate and login, licences, uninstall
  keeping data (release lane: `tests/e2e/upgrade/test_smoke_steps.py::test_the_smoke_steps_pass_on_a_local_source_install`)
- [x] desktop: package from the release, licences inside it, launch confirmed by a person,
  signature step on macOS and Windows (scripts; owner's run)
- [x] rollback: release back to draft, aliases back, channel record by hand, fix forward, never
  reuse or delete a version (`docs/internals/rollback.md`; test: `::test_rollback_sheet_names_every_alias_the_controller_moves`)
- Deliberate differences: no registry package to deprecate in a rollback (Indus Agent publishes
  none; the levers are the release, the image aliases and the channel record); the smoke cannot
  drive a desktop window, so a person confirms it.
- Not taken: "diagnostics export holds no secret" and "pairing from a second device" from the
  skill's generic list: no such step in P25-23's row; left to the rehearsal log's manual rows.

## Status

Done (2026-10-22) for what an agent may do. The three scripts, the rollback sheet and the tests are
in place; `tests/unit/release/test_smoke_scripts.py` passes (shellcheck and pwsh are not installed
here: those three cases skip; PSScriptAnalyzer is not available in this container at all and was
not run — owed on a Windows runner); the release-lane test runs the Linux script's post-install
steps green on a locally built release (dashboard-login fails there by design). Running the
scripts against the canary rehearsal artefacts on three OSes is the owner's (rehearsal log §4).
Landed with wave 44 (integration on main `7d9a864`): F-1 fixed — `release.yml`'s `upload` job now
attaches `install.sh`, `install.ps1` and `install.cmd` from the admitted commit (D-16.960, RM30);
F-2, F-3, F-5 are RS23, RS24, RS26. shellcheck, pwsh and PSScriptAnalyzer still not run (none
installed here; RS9 adds shellcheck to CI).
