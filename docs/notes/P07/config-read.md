# Task: `config show/get/path/env-path/edit` (P07-11) Phase: 07 Owner: P07 slice C builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/config.py` (`get_config_value`,
`redact_config_value`, `_is_secret_config_key`, `edit_config`, `_format_config_get_value`, the
config command table), `hermes_cli/config_show.py` (head only). Research `04-cli-config.md`
§3.11.4, §3.13.1–3.13.2; traps R04.4, R04.15. Closed before writing.
Evidence: none.

What it does (own words): the read side of `indus config`. `show` (also bare `indus config`)
prints where the settings live and the effective settings (built-in values merged with the user's
file) as YAML, with every credential masked. `get KEY` prints one value: names shaped like
environment variables come from the secret store, then the profile's `.env`, then the environment;
everything else from the merged settings. Output never bypasses masking by accident: printing goes
straight to the terminal (and into agent transcripts when the agent runs the command), so log
redaction cannot catch it; values under credential-shaped names are masked to their first and last
four characters (short ones become `[redacted]`), nested mappings and lists are masked the same way,
and `${VAR}` references are left as written. `--raw` turns masking off. `path` / `env-path` print
the two file paths. `edit` creates a missing file through the P01 writer (which writes only the
version stamp plus the commented example, never the default values, R04.4) and opens `$VISUAL`,
then `$EDITOR`, then the first of `nano`/`vim`/`vi` found.

Inputs → outputs:
- `config show` → path lines + masked YAML of the merged settings, exit 0.
- `config get KEY [--json] [--raw]` → value on stdout (`true`/`false`, `null`, YAML block for
  mappings/lists, JSON with `--json`); missing → stderr `Config key not set: KEY`, exit 1; a path
  under a known section that the schema does not define still prints, plus a stderr note.
- `config path` / `config env-path` → one path each.
- `config edit` → managed install: refusal, exit 1; seeds a missing file; no terminal → prints the
  path and exits 1 (an editor would wait on a terminal nobody has); else runs the editor and
  returns its exit code.

Files: `src/indus/cli/commands/config_read.py` (the `config` group and the read commands),
`tests/unit/cli/commands/test_config_read.py`.

Edge cases / failure modes:
- A broken `config.yaml`: the loader serves last-known-good or defaults; `show` says so on stderr.
- `get` of an env-shaped name present only in the shell environment still answers (the env view).
- `$EDITOR` with arguments (`code --wait`) is split shell-style; an editor that cannot start →
  stderr line, exit 1.
- Masking keeps references (`${OPENAI_API_KEY}`), numbers and booleans untouched.

Our design differences / decisions:
- Masking always applies unless `--raw`; the reference also skips it when redaction is switched off
  in settings. A switch meant for logs should not unmask a command whose output lands in
  transcripts.
- Seeding uses the P01 writer's create path (stamp + schema-generated commented example) instead of
  a second hand-kept example file; see "Decisions for the lead".
- `edit` without a terminal exits 1 with the path (non-TTY contract, R04.19).

Tests we will write:
- masking table (credential key, short credential, nested mapping, list, `${VAR}`, plain values,
  `--raw`); `--json`; output formats; missing key exit 1; env-shaped lookups (store, `.env`);
  phantom path note on stderr; `show` masks a planted key; `edit` seeds without default values
  (parse of the seeded file is only the stamp) and runs a fake editor; non-TTY edit exits 1;
  managed edit refused.

## Decisions for the lead
- The files table lists `src/indus/cli/config_example.yaml`. P01's writer already renders a
  commented example generated from the schema when it creates a file (`schema.render_example`,
  pinned by `test_example_parses_to_nothing_and_uncomments_to_defaults`), and the task brief says
  to write config only through the P01 writer. A second hand-kept example would drift, so `edit`
  seeds with `writer.save(scope, {})` and no `config_example.yaml` was added. `seed_config(scope)`
  is public so `doctor --fix` (slice D) can reuse it. To restore a hand-written example, ship the
  file and teach the writer's create path to use it.
- `security.redact_secrets: false` does not unmask `config get` (see above).

Compare checklist (filled after the rewrite; reopened `hermes_cli/config.py` get/edit parts):
- [x] env-shaped keys read from the env side, others from merged settings (tests:
  `test_config_read.py::test_get_env_shaped_reads_store_then_env_file`, `::test_get_scalar_formats`)
- [x] missing → "Config key not set", non-zero (test: `::test_get_missing_key_exits_1`)
- [x] credential masking, `${VAR}` never masked, recursive for containers (test:
  `::test_get_masking_table`)
- [x] phantom path note on stderr, stdout untouched (test: `::test_get_phantom_path_notes_on_stderr`)
- [x] seed from the commented example, never defaults (test: `::test_edit_seeds_example_only`)
- Deliberate differences: masking independent of the log-redaction switch; `$VISUAL` before
  `$EDITOR`; non-TTY `edit` exits 1 instead of starting an editor; masked form is `abcd…wxyz` from
  P01's `redact.mask`.
- Not taken: per-platform display-key redirection and legacy gateway paths (no gateway yet, P15);
  admin-managed banner (admin overlay not built, I20).

## Status

Done (2026-10-18). `src/indus/cli/commands/__init__.py` created (shared with slice A, P07-05).

## Integration (lead)

- `indus doctor --fix` seeds a missing settings file with `seed_config` (stamp + commented example).
