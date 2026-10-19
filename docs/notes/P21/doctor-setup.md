# Task: Doctor and setup entries for the cloud backends (P21-13) Phase: 21 Owner: P21 slice B3 builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/doctor_tools.py`
(`_check_daytona_backend`, `_check_vercel_backend`, `_check_terminal_backend`),
`hermes_cli/setup_terminal.py` (the backend picker and the per-backend setup steps). Closed before
writing. Evidence: none.

What it does (own words): `indus doctor` says, for each of the four new backends (singularity,
modal, daytona, vercel_sandbox), in one line, whether it can run here and if not why; for the
backend the profile has selected it adds the detail rows (CLI or SDK, credentials present, a
writable scratch/cache dir, and for Singularity how full the scratch dir is). `indus setup`'s
terminal step shows every backend with a one-line description and, once one is picked, asks only
for what that backend needs, storing credentials in the profile's secret store (never in `.env`,
never printed). Without a terminal, `indus setup --non-interactive --terminal-backend NAME` reads
the credentials from the environment, stores them and selects the backend.

Inputs → outputs:
- `indus.cli.doctor.remote_backends`:
  - check `remote_backends` ("Cloud and container backends", order 105): one finding per backend,
    id `backend.<name>.ready`; `ok` when the guarded row probe passes; otherwise `info` with the
    reason for a backend that is not selected, and `info` pointing at the detail rows for the
    selected one (the detail rows carry the error, so a problem is counted once).
  - `detail_findings(scope, backend) -> list[Finding]` used by `check_backend` (shared,
    `cli/doctor/checks.py`) for the four names: `backend.<name>` (CLI path / SDK distribution and
    version, or the reason naming the extra), `backend.<name>.credentials` (names present, values
    never shown; missing names and a fix), `backend.<name>.settings` (mode/disk refusals from the
    backend's own `requirement_reason`, only when credentials are fine), `backend.<name>.scratch`
    (scratch dir for Singularity, the sync-back cache dir `<home>/cache/sync-back` for the SDK
    backends: writable, or its nearest existing parent is), `backend.singularity.disk` (usage vs
    `terminal.disk_warning_gb`: `warn` above, `ok` below).
- `indus.cli.setup.remote_backends`:
  - `DESCRIPTIONS` (one line per built-in backend, our words), `label(name)`;
  - `NEEDS` per backend: the first sign-in set from `vendor_env.REQUIRED_SETS` with prompts
    (tokens asked as secrets, project/team ids as text);
  - `configure(ctx, backend) -> None`: the interactive questions after the picker (says whether the
    SDK extra or CLI is there; asks each missing field; for a field already set asks whether to
    replace it, default no; blank = skip; stores through the profile `SecretStore`);
  - `apply_non_interactive(scope, backend, env) -> int`: unknown name → 2; for the SDK backends the
    first-set names found in the environment are stored; if the backend still cannot sign in the
    missing names are listed and the run ends 1 without changing `terminal.backend`; otherwise the
    backend is saved and 0 returned. Output names variables only.
- shared: `cli/setup/__init__.py` (`--terminal-backend` option), `cli/setup/wizard.py`
  (`SetupOptions.terminal_backend`; the non-interactive branch calls `apply_non_interactive`),
  `cli/setup/sections.py` (`terminal_section` labels from `DESCRIPTIONS`, then `configure`),
  `cli/doctor/checks.py` (`check_backend` hands the four names to `detail_findings`; the module
  imports `remote_backends` so its check registers), `tests/unit/cli/commands/test_doctor.py`
  (check order list gains `remote_backends`).

Edge cases:
- No SecretStore on the scope: the profile's store is opened as `security.secret_store` says
  (`cli.commands._support.secret_store`), never `.env`.
- A stored value is never shown; the prompt says "already set (secret store)" or "(environment)".
- Vercel OIDC token: not asked and not stored by setup (short-lived); it still counts when present
  in the environment at run time (the backend's alternative sign-in).
- `--terminal-backend` without `--non-interactive` on a terminal: still never asks (the flag means
  "set it from the environment"); documented in the help text.
- Doctor never imports an SDK (package metadata only) and never calls the network.

Our design differences / decisions (for the lead):
- No automatic install of the SDK extra from setup (the reference installs it); we print the
  `pip`/`uv` extra to install. Smallest choice; easy to add later.
- No `.vercel/project.json` lookup for defaults (reference reads the nearest link file); asked
  plainly instead.
- The selected backend's summary row is `info` so the same problem is not counted twice in the
  doctor's "issues left".

Tests planned:
- `tests/unit/cli/doctor/test_remote_backends.py`: doctor (CliRunner through the app dispatch)
  per state: ok (extra present via metadata stub, credentials in a fake keychain), missing extra
  (names the extra), missing token (names the variable, value never printed); singularity CLI
  found/missing and disk warning; scratch not writable; summary rows for the four backends.
- `tests/unit/cli/setup/test_remote_backends.py`: interactive setup picks daytona and the key lands
  in the fake keychain, not `.env`, never in the output; existing key kept by default; vercel asks
  three fields; non-interactive with env credentials stores and selects; missing env → exit 1 and
  config unchanged; unknown backend → 2.

Compare checklist (after writing):
- [x] reference checks credentials by env presence only; ours reads secret store then env (D-16.22).
- [x] reference offers managed Modal billing in setup; ours has no managed mode (D-16.16).
- [x] reference writes credentials to `.env`; ours to the secret store (P4).
- [x] reference shows Singularity only on Linux; ours lists it everywhere and the probe says why it
  cannot run (CLI not found). Decision for the lead.
