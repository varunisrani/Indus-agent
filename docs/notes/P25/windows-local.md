# Task: Native Windows terminal backend, best effort (P25-24) Phase: 25 Owner: builder R3

## Status

Done, best effort (2026-10-22). The backend, its tests (63 run on Linux; 7 real Git Bash cases run
only on Windows and could not be run here) and the guide section are written. Changed outside the
task's two files: `backends/registry.py` (the `local` row opens this backend on Windows),
`platform/known_dirs.py` (the Git for Windows roots table; `%ProgramFiles(x86)%` expands),
`scripts/docs/gen_reference.py` (a note for `INDUS_GIT_BASH`) and the regenerated
`docs/user/reference/environment-variables.md`, `docs/internals/acceptance-map.md` (ACC-Q6-3),
`.github/workflows/ci.yml` (the advisory `windows-local` lane), `docs/user/getting-started/windows.md`.
Handed on: the first green `windows-local` run (then drop `continue-on-error`), and the follow-ups
at the end. Landed with wave 44 (integration on main `7d9a864`): D-16.950 – D-16.958 copied into
`16-what-we-change.md` §2.7; the Linux-runnable cases pass in the full gate; the 7 Windows cases
remain owed to the `windows-local` lane; follow-ups triaged as RL26. The integration moved the
advisory `continue-on-error` from the job to its install and test steps and put the job under
`gate` (D-16.963), since the CI gate must need every job.

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/environments/local.py` (the
Windows branches only: path translation helpers, the Git Bash tool directories, the Windows temp
dir choice, the Windows kill; skimmed by search, no code kept), `website/docs/user-guide/windows-native.md`
(the Git Bash and UTF-8 console sections only). Evidence: none.

What it does (own words): on native Windows the `local` terminal backend runs every command through
Git Bash (the `bash.exe` of Git for Windows) instead of a POSIX `bash`. The rest of the backend
contract stays the same: one fresh shell per command, the shell snapshot, the cwd marker, bounded
capture, rc 124 at our deadline, rc 130 on interrupt, the whole process tree stopped on either.
What has to change on Windows:

- Finding the shell. `bash.exe` is looked up with the platform lookup (`indus.platform.locate`):
  an explicit `INDUS_GIT_BASH`, then the portable Git the PowerShell installer unpacks into
  `<home>\tools\git-<version>`, then the Git that `git.exe` on PATH belongs to, then the usual Git
  for Windows install folders (a new `known_dirs` table), then any `bash.exe` on PATH. The WSL
  launcher (`System32\bash.exe`, the WindowsApps alias) is never taken: it would run the command in
  Linux, with Linux paths. MinGit has no bash and does not count.
- Paths. Python, the file tools and `env.cwd` keep native paths (`C:\Users\me`); scripts handed to
  bash get MSYS paths (`/c/Users/me`); the cwd bash reports (`pwd -P` prints MSYS form) is turned
  back into the native form before it becomes the environment's cwd. Both directions are pure
  functions with a table test that runs on every OS.
- Temp dir: `terminal.temp_dir` when it exists, else `<home>\cache\terminal`, written with forward
  slashes (`C:/Users/me/.indus/cache/terminal`), which both Python and bash accept. `TMP`/`TEMP`
  are not used (spaces, 8.3 short names).
- Stopping a tree: Windows has no process groups or signals for a console-less tree. Each command
  starts suspended, is put into its own job object, then resumed; stopping terminates the job,
  which takes every descendant with it.
- Console encoding: children get `PYTHONUTF8=1`, `PYTHONIOENCODING=utf-8` and a UTF-8 `LANG`
  unless set; each command's own hidden console is switched to code page 65001 when `chcp.com`
  exists; output is decoded as UTF-8 with replacement.

Inputs → outputs: `terminal.*` config, `INDUS_GIT_BASH`, the scope env and home → a started
`LocalWindowsEnvironment` (name `local`, flags as the POSIX local backend) whose `execute` answers
`ExecResult` exactly like the POSIX one; `shell_path()` / `native_path()` for callers that hand
file arguments across.

Files: `src/indus/backends/local_windows.py` (new), `tests/unit/backends/test_local_windows.py`
(new), `src/indus/backends/registry.py` (the `local` row opens the Windows backend on Windows),
`src/indus/platform/known_dirs.py` (Git for Windows roots table; `%ProgramFiles(x86)%` expands),
`docs/user/getting-started/windows.md` (the best-effort section), `.github/workflows/ci.yml` (a
best-effort Windows lane for the gated tests), `docs/user/reference/environment-variables.md`
(regenerated for the new names; `scripts/docs/gen_reference.py` gains the `INDUS_GIT_BASH` note),
`docs/internals/acceptance-map.md` (ACC-Q6-3).

Edge cases / failure modes:
- No Git Bash anywhere → `BackendUnavailable` naming Git for Windows, the installer and WSL2.
- `INDUS_GIT_BASH` set to a missing file → refused (an explicit choice never falls through).
- Only the WSL launcher on PATH → treated as missing.
- Paths: lower/upper drive letters, `C:` alone, trailing separators, UNC shares, `\\?\` long-path
  prefixes, `/cygdrive/c`, relative paths, MSYS-only paths (`/usr/bin`, `/tmp`) that map into the
  Git install or the user temp dir, and paths that have no native form without the Git root.
- A reported cwd that does not exist natively is not adopted (same rule as POSIX).
- Long scripts: the Windows command line stops at 32,767 characters and MSYS re-parses (and globs)
  the command line it is given, so the script goes in a private file and bash runs the file.
- Output pipes cannot be `select`ed on Windows: the reader polls the pipe for available bytes, so it
  can still stop shortly after the leader exits while a background grandchild holds the pipe.
- A host where job assignment fails (an old Windows inside a job that forbids nesting): the leader
  alone is killed, logged once.

Our design differences / decisions:
- Our names: `LocalWindowsEnvironment`, `open_local_windows`, `find_git_bash`, `GitBash`,
  `to_shell_path`, `to_native_path`, `windows_temp_dir`.
- One backend name `local` on every OS; the registry picks the implementation by OS family.
- Stop is immediate (job termination); the POSIX grace step has no console-less equivalent.
- Background jobs of a command that ended normally keep running (no kill-on-close on the job),
  matching POSIX; a timed-out or interrupted command loses its whole tree, also matching POSIX.
- MSYS automatic argument conversion is off by default (`MSYS_NO_PATHCONV=1`,
  `MSYS2_ARG_CONV_EXCL=*`, both only when unset): native flags such as `/F` must reach native
  programs unchanged; our own paths are converted explicitly.
- Git Bash's tool folders go first on the child PATH so `find`, `sort` and friends are the GNU ones,
  not the System32 lookalikes.
- No sudo on Windows.
- The Windows-only contract subset lives in this task's test file behind `platforms("windows")`;
  the shared contract suite is POSIX-shaped and stays as it is.

Tests we will write:
- Path conversion table, both directions and round trips (every OS).
- Temp dir choice (every OS, with real folders).
- Git Bash lookup order, the WSL launcher refused, the explicit override, MinGit, nothing found
  (every OS, with fake trees and `family="win32"`).
- Child environment: PATH order, UTF-8 and MSYS defaults kept when set, PYTHONPATH strip (every OS).
- Script text: MSYS cwd and snapshot paths in the wrapper; the reported cwd converted back and
  adopted only when it exists (every OS).
- Registry: the `local` row opens the Windows backend when the OS family is `win32` (every OS, with
  the opener patched).
- Windows only (`platforms("windows")`): echo, cwd marker and `cd`, deadline rc 124, interrupt
  rc 130, a background grandchild killed with the tree, stdin, UTF-8 output.

## Decisions for the lead
- The Windows CI lane is added with `continue-on-error: true` (best effort, D-16.55): it cannot be
  run from this Linux sandbox. Drop the flag once it has been green on `windows-latest`.
- D-ids D-16.950 – D-16.958 below; D-16.959 unused.

| D-id | Area | Decision |
|---|---|---|
| D-16.950 | Windows `local` backend (P25-24) | Git Bash runs every command; same backend name and flags; the registry opens it when the OS family is `win32`; best effort (D-16.55) |
| D-16.951 | Git Bash lookup | `INDUS_GIT_BASH` (refused when missing) → installer portable Git `<home>/tools/git-*` (newest) → the Git of `git.exe` on PATH → `known_dirs.git_for_windows_roots` → `bash.exe` on PATH; System32/SysWOW64/Sysnative/WindowsApps launchers and MinGit never count |
| D-16.952 | Paths | cwd and Python paths native; scripts MSYS; `pwd -P` converted back; drive, UNC, `\\?\`, `/cygdrive/`, Git-root and `/tmp` rules; `shell_path` / `native_path` on the environment; MSYS argument conversion off unless the user set it |
| D-16.953 | Temp dir on Windows | `terminal.temp_dir` (existing) → `<home>/cache/terminal` with forward slashes (pruned once per process) → the system temp dir; never `TMP`/`TEMP`/`TMPDIR` |
| D-16.954 | Script delivery | each command's script is a 0600 file in the temp dir run as `bash [-l] <file>` (32,767-character command line, MSYS command-line globbing); removed once the process has exited, leftovers at cleanup |
| D-16.955 | Stopping | suspended start, own job object, resume; stop and force-stop both terminate the job at once (no grace on Windows); no kill-on-close, so a finished command's background jobs live on |
| D-16.956 | UTF-8 | `PYTHONUTF8`, `PYTHONIOENCODING`, `LANG` defaults; the command's hidden console set to code page 65001 when `chcp.com` exists; UTF-8 decoding with replacement |
| D-16.957 | Child PATH | Git Bash's `mingw64/bin`, `clangarm64/bin`, `mingw32/bin`, `usr/local/bin`, `usr/bin`, `bin` (those that exist) first, then Indus's own bin dir when missing, then the user's PATH without repeats (compared without case or trailing separators) |
| D-16.958 | Windows CI lane | `windows-local` job runs `tests/unit/backends/test_local_windows.py` on `windows-latest`, best effort (`continue-on-error`) until it has been green |

Compare checklist (filled after the rewrite; reopened the Windows branches of
`tools/environments/local.py` and closed it again):
- [x] Native ↔ MSYS cwd conversion, drive letters and `/cygdrive/` (test:
  `test_local_windows.py::test_native_to_shell_table`, `::test_shell_to_native_table`, `::test_round_trip`)
- [x] Paths written into scripts are MSYS form, never `C:/` (test: `::test_scripts_see_msys_paths`)
- [x] A reported MSYS cwd is converted before the existence check, so a valid cwd is not "missing"
  (test: `::test_reported_cwd_comes_back_native`)
- [x] Temp dir under the home cache with forward slashes (test: `::test_temp_dir_is_the_home_cache_with_forward_slashes`)
- [x] MSYS argument conversion off unless the user set it; `PYTHONUTF8` default (test: `::test_run_env_utf8_msys_defaults_and_path`)
- [x] Git Bash tool folders ahead of System32 lookalikes; PATH key casing kept (test:
  `::test_path_value_puts_git_tools_first_without_repeats`)
- [x] Gap found: the reference's tool-folder list also covers 32-bit and `usr/local/bin` — fixed by the
  wider `GIT_TOOL_DIRS` (ARM64 `clangarm64` added too), pinned by `::test_arm64_git_lists_its_own_tool_folder`
- [x] The WindowsApps alias is not taken (test: `::test_the_wsl_launcher_is_never_taken`)
- [x] Hidden console window for children (`CREATE_NO_WINDOW` in `SPAWN_FLAGS`)
- Deliberate differences: the reference stops only the leader on Windows (identity-checked
  terminate); we stop the whole tree through a job object (the plan asks for it). The reference
  maps `/mnt/<letter>` to a drive; Git Bash mounts no `/mnt`, so we map only `/cygdrive/<letter>`
  and treat `/mnt/...` as a path under the Git root. The reference honours `TMPDIR` on Windows; we
  use the configured dir or the home cache only. The reference resolves bash through its own
  package manager record first; we use the installer's portable Git folder, then PATH and the known
  folders (D-16.951). Scripts go through a file, not `-c` (D-16.954).
- Not taken: matching a relative configured cwd against the tail of the launch dir (not
  Windows-specific; the POSIX backend does not do it either).

## Follow-ups
- Run the `windows-local` lane once on `windows-latest`; fix what it finds, then drop `continue-on-error`.
- Native Windows still has POSIX-only paths that do not go through this backend: background
  processes (`backends/processes.py`), `indus verify`'s host (`cli/commands/verify.py` calls
  `open_local`), cron scripts and the code-execution launcher (`LocalProcess`). Each needs its own
  Windows branch or a clear "use WSL2" refusal.
- The Windows guide says "Classic CLI and terminal UI: works, best effort"; recheck that row once
  the lane is green.
