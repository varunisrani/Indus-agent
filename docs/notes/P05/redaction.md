# Task: Output redaction (P05-06) Phase: 05 Owner: wave-1 builder A

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/redact.py` (structure only: the
outline of pattern families, the env-dump / secret-file command classification, the terminal-output
entry and the egress entry; no regex bodies copied), `website/docs/user-guide/security.md`
(§Credential Redaction). Research `02-tools.md` §3.14 (output redaction bullet). Closed before
writing.
Evidence: none.

What it does (own words): every piece of tool output that goes back to the model, or onto disk as
a spill file, receipt or overflow log, passes one function that masks credentials: known secret
shapes (vendor key prefixes, bearer/authorization headers, URL passwords, private-key blocks, JWTs,
credential-named `KEY=value` pairs) and the literal value of every secret the scope holds (secret
store and the profile `.env`). It is command-aware: the output of an env dump (`env`, `printenv`,
`export -p`, `set`, `declare`) or of a command that reads a secret-bearing file (`.env`, shell rc,
credential dotfiles, the Indus config) gets the `KEY=value` pass; other output (source files,
configs, logs) skips that naive pass so code like `MAX_TOKEN = 4096` or `token = make_token()`
stays readable. Without it the model reads keys straight out of `env` and `cat .env`.

Inputs → outputs: `redact_output(text, *, command=None, force=False, scope=None) -> str`.
- `scope=None` uses the current scope; no scope at all → shapes only, always on (fail closed).
- `force=True` masks even when `security.redact_secrets` is off (spill files, receipts, overflow
  logs). Without force the switch is read from the scope at call time.
- `command=None` (unknown source) → full pass including assignments.
- `is_env_dump(command)`, `reads_secret_file(command, *, indus_home=None)` exposed for callers.
- `spill_redactor(scope)` → `Callable[[str], str]` (force mode); `tools.spill.host_spill_for`
  uses it by default (P04-12 interface).
- Extra shape on top of P01: secret-bearing request headers (`x-api-key: …` and friends).
- Masking (P01): first 4 + `…` + last 4 for values ≥ 12 chars, else the short marker.

Files: `src/indus/safety/redaction.py`, `tests/unit/safety/test_redaction.py`; shared:
`src/indus/foundation/redact.py` (an `assignments` switch threaded through the pass),
`src/indus/tools/spill.py` (default force redactor).

Edge cases / failure modes:
- Empty text returned unchanged; non-str never reaches it (typed).
- A command split by pipes/`;`/`&&`: any segment that is an env dump counts; quoted separators do
  not split (`awk '{print $1; print $2}'`).
- `grep`/`awk`/`sed` take a pattern first: only later operands are files.
- Leading `VAR=x` assignments and `sudo` before the dump/reader are skipped.
- Secret values shorter than 8 chars are not exact-matched (P01 rule), shapes still apply.
- Redaction must never raise: a failing secret store or unreadable `.env` falls back to shapes.
- A code line `API_KEY = os.getenv("X")` stays readable in every mode (reference values kept).

Our design differences / decisions:
- Built on the P01 redactor (one implementation): we add only the command classification, the
  header shape and the scope plumbing.
- Short-value mask stays P01's `[redacted]` instead of the row's `***`: P01-17 fixed that marker
  for every surface (logs, receipts); one marker is easier to grep. Change in one constant.
- URL query-parameter masking stays on (P01 default) for tool output.

Tests we will write:
- corpus of our own fake secrets built at runtime (no vendor-shaped literals in the file): every
  family masked in plain output; scope secret value masked anywhere (mid-word, multi-line).
- env-dump pass: `env`, `printenv`, `export -p`, `set`, `sudo env`, `FOO=1 env`, `ls; env`, and
  `cat .env`, `head ~/.bashrc`, `grep KEY .env` vs `grep .env app.py` (pattern, not file).
- code output (`cat app.py`) keeps `MAX_TOKEN = 4096`, `token = make_token()` and
  `API_KEY = os.getenv(...)` but masks a vendor-shaped key inside it.
- force mode with redaction off; non-force with redaction off unchanged.
- no scope → shapes masked; spill sink writes redacted text.

## Decisions for the lead
- Short mask marker: kept P01's `[redacted]` (see above); flip `MASK_SHORT` if `***` is wanted.

Compare checklist (filled after the rewrite; reopened `agent/redact.py`):
- [x] env-dump command set and quote-aware segment split (`test_redaction.py::test_dump_commands_get_assignment_pass`,
  `::test_is_env_dump`)
- [x] secret-file readers and pattern-first readers; Indus config only under the Indus home
  (`::test_reads_secret_file_under_scope_home`, `::test_other_commands_keep_code_readable`)
- [x] code mode skips the assignment pass (`::test_other_commands_keep_code_readable`)
- [x] force overrides the switch (`::test_force_masks_with_switch_off`, `::test_spill_files_are_force_redacted`)
- [x] fail closed when masking breaks (`::test_failure_withholds_text`)
- [x] gap: diagnostic lines in source-shaped output print values — fixed by a line-scoped
  assignment pass, pinned by `::test_diagnostic_lines_get_assignment_pass_in_code_mode`
- [x] gap: chat-bot token shape and secret request headers — added in `redaction.py`, pinned by
  `::test_bot_token_shape_masked`, `::test_secret_headers_masked`
- Deliberate differences: the reference keeps URL credentials and query parameters unmasked by
  default in tool flows; P01 masks them and we keep P01's choice. Short mask stays `[redacted]`.
- Not taken: re-matching tokens split by control characters; the non-reusable sentinel for file
  reads (belongs to `read_file`, P05-16); phone-number and form-body masking; plugin-registered
  patterns (no plugin surface yet); backup copies of the config (`config.yaml.<stamp>`) as secret
  files (our backup names differ; revisit with P07-22).

## Status

Done (2026-10-18). `redact_output`, `is_env_dump`, `reads_secret_file`, `spill_redactor` in
`src/indus/safety/redaction.py`. Shared: `foundation/redact.py` gained an `assignments` switch
(`redact_text`, `Redactor.redact`); `tools/spill.host_spill_for` now force-redacts by default.
Terminal/process tools (P05-11..14) call `redact_output(text, command=cmd)`.
