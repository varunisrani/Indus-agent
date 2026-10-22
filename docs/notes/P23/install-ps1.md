# Task: `install.ps1` core and stages (P23-06) Phase: 23 Owner: wave-36 builder I2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `scripts/install.ps1` (1–143 param
block, dot-source guard, generated pin block; 143–383 8.3 path handling and resolved-path report;
617–835 log helpers, native-call wrapper, `Fail`, receipt writer, failure class, frame writer, stage
table; 836–1243 prerequisites and repository stage; 1244–1505 runtime/deps/products/config/setup/
gateway/complete; 1567–1670 stage dispatch and entry). Research 10 §3.10, §3.11; traps R10.14,
R10.15; P20-09 contract `apps/desktop/electron/installer/stageProtocol.ts` (ours). Closed before
writing.
Evidence: none.

What it does (own words): the Windows installer. One PowerShell file (built from fragments) that a
person runs from PowerShell (`irm …/install.ps1 | iex`, or `& ([scriptblock]::Create((irm …)))
-Channel canary`), that `install.cmd` launches from Command Prompt, and that the desktop app drives
stage by stage (`powershell.exe -NoProfile -ExecutionPolicy Bypass -File install.ps1 --stage NAME
--json --non-interactive --indus-home H`). It installs the runtime the desktop app uses: a git
checkout, pinned uv, Python 3.14, the locked environment, the `indus` launcher on the user PATH.
The CLI, TUI and gateway on native Windows are supported through WSL2 (D-16.55); the script says so
at the end.

- Same ladder and manifest as `install.sh`, from one shared table `scripts/install/stages.json`
  (embedded into both built scripts): `prerequisites, repository, runtime, dependencies, config,
  products, setup, gateway, complete`; `desktop` is dispatchable with `-Stage` but not on the
  ladder. `-Manifest` prints `{protocol_version: 1, stages: [{name, title, category,
  needs_user_input}]}` as one compact JSON line on stdout; `-Stage X -Json` prints one frame
  `{ok, stage, skipped, reason?}`; `-NonInteractive` (alias `-SkipSetup`) skips the stages that
  need input with reason "needs user input".
- Parameters: `-Branch`, `-Commit`, `-Channel` (env `INDUS_INSTALL_CHANNEL`), `-IndusHome`
  (default `INDUS_HOME`, else `%LOCALAPPDATA%\indus`), `-InstallDir` (default
  `<IndusHome>\indus-agent`), `-Manifest`, `-Stage`, `-Json`, `-NonInteractive`, `-SkipSetup`,
  `-IncludeDesktop`, `-SkipBrowser`, `-SkipComputerUse`, `-ShowResolvedPaths`. The POSIX spellings
  the desktop and `install.sh` use (`--stage`, `--indus-home`, `--non-interactive`, `--dir`, …,
  also `--flag=value`) arrive through a remaining-arguments parameter and are mapped onto the same
  options. Missing value → exit 2; unknown flag → exit 1; bad channel → exit 2 (as `install.sh`).
- Dot-sourcing (`. .\install.ps1`) defines the functions only: nothing runs, no env is rewritten.
- Long paths: a profile folder Windows exposes through an 8.3 alias (`FIRST~1.LAS`) is expanded
  back to its long form for `TEMP`, `TMP`, `LOCALAPPDATA`, `APPDATA`, `USERPROFILE` before
  anything else (kernel32 `GetLongPathNameW`, else rebuilding the path on a profile root proven
  long). `-ShowResolvedPaths` prints `{long_profile_root, normalized, resolver, temp, indus_home,
  install_dir}` as JSON and exits without touching anything.
- A home inside (or equal to) the install dir is refused before any download (the tool store
  would land inside the checkout).
- Receipt `<home>\logs\install_receipts\<id>.json` `{id, installer: "ps1", outcome,
  failed_stage?, failure_class?, started_at, finished_at}` via temp + rename, first writer wins
  (an in-process flag and an existing file both make a later write a no-op). The full ladder's
  `finally` tries `interrupted`, which is a no-op after a recorded outcome (R10.15). Never uploaded
  (D-16.49).
- Machine output (manifest, frames, resolved paths) goes straight to the process's stdout
  (`[Console]::Out`), never into the PowerShell pipeline, so it cannot mix with returned values;
  human lines use `Write-Host`; child output goes to `<home>\logs\install.log`.

Inputs → outputs: parameters / POSIX flags / env → stdout JSON (manifest, frame, paths), exit code
(0, 1 failure or unknown flag, 2 usage), receipt file, `install.log`, a checkout, `<home>\tools`,
`<home>\bin\indus.cmd`, a user PATH entry, `<install>\.indus-bootstrap.json`.

Files: `scripts/install/ps/00-core.ps1`, `scripts/install/ps/20-stages.ps1`,
`scripts/install/ps/90-entry.ps1`, `scripts/install/stages.json`, `scripts/install.ps1` (built),
`tests/integration/install/test_install_ps1.py`, `tests/integration/install/ps_static.py`,
`tests/integration/install/_stubs_i2.py` (stand-in builder; see decisions).

Edge cases / failure modes:
- Run under `iex` (no file): `exit` would close the person's window, so the exit code is left in
  `$LASTEXITCODE` instead; only a `-File` run exits.
- Windows PowerShell 5.1 reads a BOM-less script in the ANSI code page: the built file is ASCII
  only; 5.1 lacks `??`, `?.`, ternaries, `&&`/`||` and `ConvertFrom-Json -AsHashtable`: none used.
- A native tool's stderr under `$ErrorActionPreference = 'Stop'` turns into a terminating error
  in 5.1: native calls run through one wrapper that relaxes the preference locally.
- A run on a non-Windows host fails `prerequisites` with `unsupported_platform` and a pointer to
  `install.sh` (dot-sourcing and the manifest still work there, which is how CI tests it).
- Ctrl-C ends the pipeline without reaching `catch`; `finally` records `interrupted`.
- Repository stage keeps R10.14: an occupied non-checkout dir is refused (`dir_not_checkout`), a
  checkout with no commits is moved aside to `<dir>.broken-<UTC stamp>`, a fresh clone goes into
  a sibling staging dir (`--filter=blob:none`, 3 attempts with 5 s / 10 s pauses, then one
  `--no-checkout` attempt; `--commit` must be an ancestor of the branch; checkout 2 attempts) and
  is renamed into place only when complete; local edits are stashed as
  `indus-install-autostash-<stamp>` after clearing an unmerged index; a non-fast-forward reset
  happens only after the diverged commits are saved to `refs/indus/install-backups/<stamp>`.
  Stable resolves the newest `vX.Y.Z` via the GitHub API, then the `releases/latest` redirect,
  then `git ls-remote --tags`; canary the newest `vX.Y.Z-canary.<stamp>` tag; main the branch tip.
  An existing checkout keeps the channel saved under `update.installs.<id16>.channel` unless
  `-Channel` is given.

Our design differences / decisions:
- Our names: `indus`, `INDUS_*`, stage names `runtime`/`dependencies` (not venv/python-deps),
  receipts under `logs\install_receipts` (not a telemetry folder), installer word `ps1`.
- No package-manager layer: `dependencies` runs `uv sync --frozen --no-dev` with the extras the
  shared table lists (browser extra dropped by `-SkipBrowser`); `config` seeds the home through the
  installed package (as install.sh does) and saves skip choices as `update.installs.<id>.skip`
  through `indus config set --force`, so later installs and updates keep them.
- No "source completion" helper and no AppContainer ACL grant: `products` publishes the launcher
  and the PATH entry; the desktop part (P23-07) checks the built app and makes shortcuts.
- `-ProtocolVersion` is not offered (the manifest carries the version).
- The COM long-path resolver is left out (blocked in the same locked-down hosts as P/Invoke);
  kernel32 then profile-root substitution.

Tests we will write:
- Static (always): the built file equals the stand-in builder's output; ASCII only; param names;
  embedded stage table equals `stages.json`; the manifest the table yields equals the P20-09
  contract shape and the frame fixtures; every `Indus`-named function called is defined; no
  PowerShell 7-only syntax; balanced braces; the dot-source guard precedes the entry call.
- With `pwsh` (skipped when absent): manifest JSON (PowerShell and POSIX spellings) equals the
  table; `--stage setup --json --non-interactive` frame; unknown flag exit 1, missing value 2, bad
  channel 2; dot-sourcing runs nothing; receipt written once (second write and `interrupted` are
  no-ops); `-ShowResolvedPaths` JSON shape; unknown stage frame.

## Decisions for the lead
- Builder: `scripts/install/build.py` is I1's (P23-03). The PowerShell half is a stand-in in
  `tests/integration/install/_stubs_i2.py` (`render_ps1(root, pins_text)`): header comment, then
  `ps/00-core.ps1` with its `# @@INDUS_EMBED@@` line replaced by two single-quoted here-strings
  (`$script:IndusStagesJson` = compact `stages.json`, `$script:IndusPinsJson` = compact
  `pins.json`), then the other `ps/*.ps1` in name order. Move it into `build.py` (and its
  `--check`) at integration and regenerate `install.ps1` from the real `pins.json`; the committed
  `install.ps1` embeds the stand-in pins (fake digests).
- Pins shape (`pins.json` is I1's; matched to their file): `uv.version`,
  `uv.targets["windows-x64"|"windows-arm64"].{url, mirror, sha256}`, `git_for_windows.version`,
  `git_for_windows.targets[...]` (same fields), `python.provision`, `python.floor`. One accessor
  (`Get-IndusPin`) reads it. A `sha256` of null (I1's provisional pins) makes the download refuse
  (`download_failed`): real digests are owed before any Windows install can work.
- Release origin (D-16.690, owner pending): ONE constant, slice I1's `pins.json`
  `release.repository` (`indusagi/indus-agent`); clone URL, API URL, `releases/latest` URL and the
  `install.cmd` URL all derive from it (env `INDUS_REPO_URL` overrides the clone source only).
- `stages.json`: both slices wrote one. Mine is I1's table (same titles, categories,
  `title_with_desktop`, note) plus two keys the PowerShell script reads: `dispatch_only`
  (`["desktop"]`) and `extras` (`always`, `unless_skip_browser`, `unless_skip_computer_use`; the
  list equals install.sh's `INS_EXTRAS_COMMON` + `browser`). Merge = I1's file + those two keys
  (install.sh could then read its extras from the table too).
- Receipt timestamps: ISO 8601 UTC strings (`2026-10-21T10:00:00Z`), as install.sh writes them.

Compare checklist (filled after the rewrite; reopened `scripts/install.ps1`):
- [x] Param block, dot-source guard, `iex` exit rule (test:
  `tests/integration/install/test_install_ps1.py::test_top_level_runs_nothing_but_the_guarded_entry`,
  `::test_param_block_is_the_first_statement_and_names_every_parameter`, `::test_pwsh_dot_sourcing_runs_nothing`)
- [x] Stage table, manifest, frames, non-interactive skip, unknown stage (tests:
  `::test_stage_table_is_the_shared_ladder`, `::test_manifest_from_the_table_passes_the_desktop_parser`,
  `::test_pwsh_input_stages_skip_with_a_frame_when_non_interactive`, `::test_pwsh_unknown_stage`)
- [x] Receipt first-writer-wins, `finally` interrupted no-op (tests:
  `::test_receipt_is_first_writer_wins_and_the_finally_records_interrupted`, `::test_pwsh_receipt_is_written_once`,
  `::test_pwsh_failed_ladder_records_one_failed_receipt`)
- [x] Path normalisation and resolved-path report (test: `::test_pwsh_show_resolved_paths`)
- [x] Home inside the install dir refused before downloads (test: `::test_pwsh_home_inside_the_install_dir_is_refused`)
- [x] Repository stage rules (R10.14): preflight before Git, move-aside, staging clone + rename,
  3 + 1 clone attempts, ancestor check, 2 checkout attempts, unmerged index cleared, autostash label,
  backup ref before reset, stable lookups API -> redirect -> ls-remote, unreachable stable keeps a
  rerun's checkout. Static only here (no Windows); the Windows install-e2e lane runs it.
- [x] Gap found: child output was only logged; the reference streams it when the output is redirected
  (the desktop driver reads it) or in CI — fixed in `Invoke-IndusLogged`.
- Deliberate differences: no `-ProtocolVersion`; the products title follows the shared table's
  `title_with_desktop` under `-IncludeDesktop`; an existing checkout with a saved release channel moves
  forward to that channel's newest release when it descends from HEAD (the reference leaves every
  chosen-channel checkout to the updater); `uv sync` instead of a package-manager bootstrap;
  receipts in `logs\install_receipts` with ISO timestamps; the user PATH keeps its registry value
  kind (the reference's `SetEnvironmentVariable` flattens `%VARS%`); TLS 1.2 is switched on for 5.1.
- Not taken: promisor-pack marking and treeless-to-blobless conversion (our clones are blobless from
  the start), `--set-channel` at `complete` (U1's updater owns saved channels), COM long-path resolver,
  AppContainer ACL grant, desktop shortcut on the desktop, icon cache refresh, progress-bar runspace.

## Status

Done (2026-10-21). Static tests pass here; the pwsh tests skip (no PowerShell on this host) and run
where `pwsh` is installed. Changed outside the task's files: `docs/internals/traps-ledger.md`
(R10.15), `docs/internals/acceptance-map.md` (ACC-S2-1, ACC-S2-2). The builder stand-in in
`tests/integration/install/_stubs_i2.py` moves into I1's `build.py` at integration, and
`scripts/install.ps1` is then rebuilt from the real `pins.json`.

## Integration (wave 36)

Integration: the stand-in builder is removed — `scripts/install/build.py` builds `install.ps1` the way described above, `--check` covers it, and the committed script embeds the real `pins.json` (recorded digests). Settings key reconciled (D-16.748): `Get-IndusInstallSetting` reads `updates.installs.<id>.*` (lists too); the config stage writes `.extras` (the synced list) and the complete stage `.channel` for an explicit `-Channel`; `.skip` is gone.

## Wave 47 (builder f, 2026-10-22): parity with install.sh for the pages step and marker `version`

Status: done (2026-10-22), not yet landed. UNVERIFIED ON WINDOWS: no `pwsh` on the build host, so
the new code is checked statically only (ASCII, PS 5.1 syntax guards, parity tests below).

Read: no reference file. Files: `scripts/install/ps/20-stages.ps1` (`Install-IndusWebPages`,
called from `Invoke-IndusProducts`; `Invoke-IndusComplete` record), rebuilt `scripts/install.ps1`
(`python scripts/install/build.py`), tests `tests/integration/install/test_install_ps1.py`
(`test_products_fetch_the_release_pages_like_install_sh`,
`test_marker_carries_the_release_version_like_install_sh`).

Products: like `ins_web_pages` (wave 46 k), a developer's `apps\web\dist\index.html` is kept;
otherwise, with Git on PATH (`Use-IndusGit`), `Invoke-IndusLogged ... -MayFail` runs
`<install>\.venv\Scripts\python.exe -m indus.web.bundle fetch --repo <install> --target
<install>\src\indus\web\static`. Exit 0 → ok line, 3 → the pnpm hint, anything else → a warning
with the child's error line; the stage never fails on the pages and `$LASTEXITCODE` is reset.
Complete: the marker gains `version` between `channel` and `completed_at` (the key order of
install.sh's printf): `git tag --points-at HEAD --list 'v*'`, filtered by the stable/canary tag
patterns, newest by `Select-IndusNewestTag`, without the `v`; `''` off a release. The parity test
reads the key list from both scripts and requires them equal.

Fail-first: both parity tests fail against the base `install.ps1` and pass on the rebuilt one.

Follow-up: run the `needs_pwsh` tests and a real ladder on Windows (rehearsal row, Windows source
install) to confirm the fetch and the marker `version` there.
