# Task: install.sh runtime stages (P23-05) Phase: 23 Owner: wave-36 builder I1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `scripts/install.sh` (target and libc
detection, pinned uv bootstrap with mirror fallback, Python bootstrap, dependency stage, PATH
wiring, config, setup/gateway, complete marker). Trap R10.16. Closed before writing.
Evidence: none.

What it does (own words):
- `runtime`: ensure the pinned uv. The target key comes from `uname` and, on Linux, the libc: the
  ELF interpreter name in the first 8 KiB of `/bin/sh` decides (`ld-musl-` → musl, `ld-linux` →
  glibc), else `ldd --version`, else a `/lib/ld-musl-*.so.1` probe. The archive is downloaded into a
  temp dir from the upstream URL; the mirror is tried **only** when curl fails with a network exit
  code (5, 6, 7, 18, 22, 28, 52, 55, 56). The SHA-256 (`sha256sum` or `shasum -a 256`) is verified
  whatever the origin; the binary lands in `<home>/tools/uv-<ver>-<target>/` and must answer
  `--version`. Failure classes `unsupported_platform`, `download_failed`, `download_digest_mismatch`,
  `uv_unusable`. Then Python: an existing 3.13/3.14 is accepted, otherwise uv installs 3.14 (managed
  interpreters under `<home>/tools/python`).
- `dependencies`: `uv sync --frozen --no-dev` in the checkout with the common extras; `--skip-browser`
  drops the browser extra. Skip choices are saved in config (`update.installs.<id>.skip`) so later
  installs and updates keep them; a later run without flags reads them back.
- `config`: home skeleton and `config.yaml` seeded through the installed package's own code (the
  same writer `indus config edit` uses), `.env` created empty with mode 0600 if absent.
- `products`: publish `~/.local/bin/indus` (a small launcher that execs the venv's `indus`; never
  overwrite a launcher that is not ours), publish built web pages when the checkout has them, add
  `~/.local/bin` to PATH once with a marked block (`# >>> indus >>>` … `# <<< indus <<<`): zsh →
  `.zshrc` + `.zprofile`; fish → `fish_add_path` in `config.fish`; bash → `.bashrc`, `.profile`, and
  `.bash_profile`/`.bash_login` when they exist.
- `setup` / `gateway`: only with a readable `/dev/tty` (else skipped with a reason).
- `complete`: write `<install>/.indus-bootstrap.json {schema: 1, commit, branch, channel,
  completed_at}` atomically and save an explicit channel choice.

Inputs → outputs: flags → tools under `<home>/tools`, `.venv` in the checkout, config files, launcher,
rc blocks, marker.

Files: `scripts/install/sh/20-runtime.sh`, `tests/integration/install/test_install_sh_runtime.py`.

Edge cases / failure modes:
- a server that answers 200 with a wrong body (exit 0) → `download_digest_mismatch`, mirror not tried.
- curl exit 22 (HTTP error with `-f`) → mirror; a non-network exit (e.g. 23 write error) → no mirror.
- digest unknown (provisional pins) → refuse before downloading.
- archive without `uv` → `uv_unusable`; binary that does not run → `uv_unusable`.
- rc file missing → created with the block; block already there → untouched (second run).
- `.env` existing with loose mode → tightened to 0600.

Our design differences / decisions:
- Python and uv live under the profile home (`<home>/tools`), so uninstall finds them.
- The launcher is a script (desktop P20-08 reads its `exec` line), exporting `INDUS_HOME` only for
  a non-default home.
- PATH wiring uses an exact marked block (uninstall removes it by marker, P23-20), not a regex over
  user lines.
- Web/TUI pages are published when the checkout already carries a build; building them from a
  release payload needs the release artefact manifest (P23-23) — follow-up.
- No separate package-manager stage; `uv sync --frozen` against the checkout's `uv.lock` is the
  hash-verified dependency install.

Tests we will write:
- fake curl exit 22 → mirror used; 200 with a bad body → digest mismatch and the mirror never
  called; exit 23 → no mirror; musl/glibc detection from generated ELF headers; uv placed and run;
  PATH block added once across two runs for bash/zsh/fish; `.env` 0600; non-TTY skips setup;
  bootstrap marker written; config seeding snippet works against the real package.

## Decisions for the lead
- Extras installed: `web docs pty dashboard acp messaging` + `browser` unless skipped (= `all`);
  `--skip-computer-use` is recorded only (no computer-use extra exists yet).
- Saved keys (interface for U1's schema): `update.installs.<id16>.channel` (string) and
  `update.installs.<id16>.skip` (comma-separated words `browser`, `computer-use`), written with
  `indus config set --force`.

Compare checklist (filled after the rewrite; reopened the runtime functions):
- [x] mirror only on network exit codes, digest checked whatever the origin (tests: `tests/integration/install/test_install_sh_runtime.py::test_network_failure_uses_the_mirror`, `::test_every_network_code_permits_the_mirror`, `::test_other_curl_failures_never_try_the_mirror`, `::test_a_bad_body_with_exit_0_is_a_digest_mismatch_and_no_mirror`, `::test_a_mirror_with_bad_bytes_is_refused_too`)
- [x] unknown digest downloads nothing (test: `::test_provisional_pins_download_nothing`)
- [x] uv placed under `<home>/tools`, must run; broken archive / binary → `uv_unusable` (tests: `::test_an_archive_without_uv_is_unusable`, `::test_a_uv_that_does_not_run_is_removed`)
- [x] libc from the ELF interpreter (tests: `::test_libc_from_the_elf_interpreter`, `::test_a_non_elf_probe_is_skipped`)
- [x] Python accepted or installed (tests: `::test_python_is_installed_only_when_none_fits`, `::test_an_existing_python_is_accepted`)
- [x] dependencies + skip choices now or saved (tests: `::test_dependencies_sync_the_lock_with_the_common_extras`, `::test_skip_browser_now_or_saved_earlier`, `::test_config_creates_env_0600_and_saves_the_extras`)
- [x] config seeding through the package; `.env` 0600 (test: `::test_config_seeds_the_home_and_a_private_env_file`)
- [x] launcher + PATH block once per rc file for bash/zsh/fish (tests: `::test_path_block_added_once_across_two_runs`, `::test_zsh_and_fish_rc_files`, `::test_launcher_for_a_custom_home_and_a_foreign_launcher`)
- [x] no terminal → setup/gateway skipped (test: `::test_without_a_terminal_setup_and_gateway_skip`); marker (test: `::test_complete_writes_the_marker_and_saves_an_explicit_channel`)
- [x] whole ladder end to end, launcher runs `indus --version` (test: `::test_a_full_non_interactive_install_ends_with_a_working_command`)
- [x] gap found: a uv that does not run stayed installed, so every rerun failed the same way; the
  entry is now removed (pinned by `::test_a_uv_that_does_not_run_is_removed`)
- Deliberate differences: uv's managed Pythons go under `<home>/tools/python`; one marked PATH block
  per rc file instead of skipping files that already mention `.local/bin` (uninstall needs the exact
  block); the gateway stage asks before installing a service; a failed channel save warns instead
  of failing the finished install; Python is accepted from any interpreter in range.
- Not taken: the reference's package-manager bootstrap (we sync `uv.lock` directly), the macOS
  Command Line Tools wait loop (we fail with the instruction), the Node/libatomic/libstdc++ checks
  (no Node bootstrap here), building the desktop app.

## Status

Done (2026-10-21). Web/TUI pages from a release payload are a follow-up (needs P23-23's
artefact manifest).

## Integration (wave 36)

Settings key reconciled with P23-17 (D-16.748): the config stage always writes `updates.installs.<id16>.extras` = the list it synced (`indus update` re-syncs exactly that), and the complete stage writes `.channel` for an explicit `--channel`; the `.skip` key is gone — a later install leaves out the browser group when the saved extras lack it. Downloads use `--proto '=https' --proto-redir '=https'`.
