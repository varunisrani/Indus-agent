# Task: `install.ps1` Windows bootstrap and cmd shim (P23-07) Phase: 23 Owner: wave-36 builder I2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `scripts/install.ps1` (383–616 arch,
verified download with mirror, progress download, pinned uv, pinned git, git on PATH; 765–771 uv
version check; 1341–1363 desktop presence; 1435–1505 desktop stage, artefact probe, shortcut
creation 1506–1565), `scripts/install.cmd` (all, 28 lines). Research 10 §3.11; trap R10.16. Closed
before writing.
Evidence: none.

What it does (own words): the Windows-only pieces of the PowerShell installer, plus the Command
Prompt entry point.

- Architecture: the machine's (registry `PROCESSOR_ARCHITECTURE`), not the PowerShell process's
  (an x64 PowerShell on Windows on Arm must still fetch arm64 tools): `x64` or `arm64` →
  targets `windows-x64` / `windows-arm64`.
- Verified download: the pinned upstream URL first; the mirror only when the upstream failed with
  a network-class error (DNS, connect, timeout, connection reset/closed, or an HTTP error status
  — the PowerShell equivalent of curl's 5/6/7/18/22/28/52/55/56). A TLS trust failure is never a
  routing problem and stops. Whatever origin served the bytes, the SHA-256 must match the pin; a
  mismatch removes the file and fails `download_digest_mismatch` without trying the mirror
  (R10.16). No candidate left → `download_failed`. A pin without a SHA-256 is refused before
  anything is fetched.
- Portable Git: the pinned self-extracting archive per arch, verified, then run with `-o<dir> -y`
  under a 600 s limit; on overrun the extractor's process tree is killed by its pid (`taskkill /T
  /F /PID`) and the stage fails `git_extract_failed`; a non-zero exit or a missing `cmd\git.exe`
  also fails `git_extract_failed`. Extracted into a temp dir and moved to `<home>\tools\git-<ver>`
  only when complete; the process PATH gets `<entry>\cmd` (never the user PATH).
- Pinned uv: the same verified download (zip), `uv.exe` (and `uvx.exe`) moved into
  `<home>\tools\uv-<ver>-<target>\`, then run once (`uv --version`); a cached copy that does not run
  is replaced; failure classes `unsupported_platform` (no pin for the arch), `uv_unusable`.
- Long paths: clones get `core.longpaths=true`; when the OS long-path switch is off and the
  install path is long, a warning says how to turn it on.
- `-IncludeDesktop`: `products` (and `-Stage desktop`) checks the desktop build output
  (`apps\desktop\release\win-unpacked\Indus.exe` or `win-arm64-unpacked`), fails
  `products_build_failed` when it is missing, and creates a Start-menu shortcut `Indus.lnk`
  (best effort; a shortcut failure only warns).
- `scripts/install.cmd`: a short batch file for Command Prompt: it starts Windows PowerShell with
  `-NoProfile -ExecutionPolicy Bypass` for that one process, downloads the hosted `install.ps1`
  (the latest release asset) and runs it, passing the batch file's own arguments through; on a
  non-zero exit it prints the manual PowerShell command and pauses, then exits 1.

Inputs → outputs: pins (embedded) + arch → verified files in `<home>\tools`, process PATH, failure
classes; shim arguments → one PowerShell run.

Files: `scripts/install/ps/10-bootstrap.ps1`, `scripts/install.cmd`,
`tests/integration/install/test_install_ps1_bootstrap.py`.

Edge cases / failure modes:
- HTTP 404 from upstream: mirror tried (HTTP errors count as network class, as curl 22 does).
- Upstream serves bytes with a wrong digest: refused at once, the mirror is not tried.
- Extractor hangs (antivirus prompt, full disk): killed at 600 s, temp dir removed.
- The self-extractor is a GUI-subsystem program: started as a process object and waited on, never
  with `&`.
- `uv.exe` missing from the archive → `uv_unusable`.
- The shim must not depend on delayed expansion or labels (a batch file with LF endings misreads
  labels), so it is written with CRLF and has no `goto`.

Our design differences / decisions:
- Seams for tests: the downloader, the process starter, the killer, the clock and the sleeper are
  script-scope scriptblocks (`$script:IndusDownload`, `$script:IndusStartProcess`,
  `$script:IndusKillTree`, `$script:IndusNow`, `$script:IndusSleep`) that the default code sets
  and a test replaces after dot-sourcing; the extractor wait is a polling loop on the injected
  clock so a fake clock can run past the limit.
- No progress bar runspace (the reference's download-with-progress): `Invoke-WebRequest` with its
  own progress silenced; the desktop shows its own progress.
- Shortcut: Start menu only (no desktop shortcut), no icon-cache refresh.
- The shim fetches the **latest** release's `install.ps1`; its URL derives from the one release
  constant (`pins.json` `release.repository`), checked by a test.

Tests we will write:
- Static: the bootstrap fragment is in the built script; the network-class list; digest check
  precedes any use; 600 s limit; kill by pid only; the shim's URL equals the template from the
  release constant, it has CRLF endings, bypass for this process only, pause on failure, no
  reference names, passes `%*`.
- With `pwsh` (skipped when absent): fake downloader returning bad bytes → `download_digest_mismatch`
  and the mirror never called; network failure → mirror used; fake extractor that never exits +
  fake clock → killed by pid and `git_extract_failed`; no pin for the arch → `unsupported_platform`.

## Decisions for the lead
- Pins accessor and builder stand-in: see `install-ps1.md`.
- Shim URL: `https://github.com/<release.repository>/releases/latest/download/install.ps1`. Owner
  decision D-16.690 changes `pins.json` `release.repository` and the shim's one `set` line (the
  test fails until both agree).

Compare checklist (filled after the rewrite; reopened `scripts/install.ps1`, `scripts/install.cmd`):
- [x] Arch from the registry (machine, not process)
- [x] Mirror only on network class; digest always (tests:
  `tests/integration/install/test_install_ps1_bootstrap.py::test_mirror_only_after_a_network_class_failure_and_digest_always_checked`,
  `::test_pwsh_digest_mismatch_refuses_and_never_tries_the_mirror`, `::test_pwsh_network_failure_uses_the_mirror_and_verifies_it`,
  `::test_pwsh_tls_failure_never_tries_the_mirror`, `::test_pwsh_missing_digest_is_refused_before_any_download`)
- [x] Git extractor bounded at 600 s, killed by pid, moved when complete (tests:
  `::test_git_extract_is_bounded_and_killed_by_pid`, `::test_pwsh_extract_timeout_kills_the_child_by_pid`,
  `::test_pwsh_git_extract_timeout_fails_with_its_class`)
- [x] uv staged and run once; no pin → unsupported_platform (tests: `::test_uv_failure_classes`,
  `::test_pwsh_no_pin_for_the_machine_is_unsupported`)
- [x] Desktop check and Start menu entry (test: `::test_desktop_check_and_start_menu_entry`)
- [x] Shim text and failure path (test: `::test_cmd_shim_calls_the_latest_release_installer`)
- Deliberate differences: the reference's mirror fallback takes any non-TLS WebException; ours only the
  network/HTTP-status class (closer to R10.16's curl list); Git's `usr\bin` is not added to PATH
  (`cmd\git.exe` sets up its own); the shim passes its arguments through and runs the script as a
  scriptblock so options work; uv's cached copy is checked by running it (the folder is per version).
- Not taken: download progress runspace, HEAD size probe, AppContainer ACL grant.

## Status

Done (2026-10-21). Static tests pass; the pwsh cases skip here and run where `pwsh` is installed.

## Integration (wave 36)

The shim URL is checked against `RELEASE_REPOSITORY` too (`tests/unit/packaging/test_release_base.py`, D-16.740).
