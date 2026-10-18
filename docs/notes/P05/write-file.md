# Task: `write_file` + write guards (P05-18) Phase: 05 Owner: wave-2 builder A

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/file_tools_write_guards.py`
(whole: sensitive prefixes, protected instruction gate, binary-document guard, stale blocker,
internal-content check), `tools/file_tools.py` (write handler, rewrite hint), `tools/file_state.py`
(per-path lock, cross-agent stamps), `agent/file_safety.py` (write-denied home files and dirs,
approval-required `~/.ssh/config`). Closed before writing.
Evidence: none.

What it does (own words): replaces a whole file (parents created) and answers `verified: true`
once the bytes on disk hash to what was meant. Guards, in order: protected paths after realpath
(`/etc/`, `/boot/`, systemd unit dirs, macOS private etc / var-db / var-root, Docker sockets; in
every home a write can reach: `.ssh` (but `~/.ssh/config` asks for approval), `.aws`, `.kube`,
sudoers, `.netrc`; Indus secret stores and config) → plain text into a binary document refused
(office/ODF/EPUB/RTF always; PDF and other binary extensions only over an existing file) →
protected instruction files (`AGENTS.md`, `CLAUDE.md`, `PERSONA.md`, `.cursorrules`, any case, plus
`security.protected_instruction_files` globs) always ask the person (no session/always choice,
not bypassed by auto-approve, blocked without a surface) → content that is our own status text or
line-numbered `read_file` output refused. Then, under a per-path lock, the stale-overwrite rule
(R02.14): replacing an existing file needs a current baseline for this task (full unredacted
read, its own write, or its own patch made from a baseline) and the same bytes on disk; otherwise
nothing is written and the model is told to read, merge and retry. A rewrite of ≥20,000 chars
that keeps ≥80 % of the lines gets a "use patch" hint. A call without `content` gets an error
explaining that long arguments get dropped under context pressure.

Inputs → outputs: `{path, content}` → `{bytes_written, dirs_created, verified, resolved_path,
files_modified, hint?}`; refusals `{"error": …, stale_write_blocked?: true, path}`. Config
`security.protected_instruction_files` (list of globs), `approvals.timeout`.

Files: `src/indus/tools/builtin/write_file.py`, `src/indus/safety/path_guards.py`,
`tests/unit/tools/builtin/test_write_file.py`, `tests/unit/safety/test_path_guards.py`; shared:
`src/indus/tools/builtin/__init__.py` (module list line).

Edge cases / failure modes:
- symlink into `~/.ssh` refused (realpath); `..` traversal neutralised before matching.
- CRLF file stays CRLF and a UTF-8 BOM stays when the model sends bare text.
- approval: deny, timeout ("silence is not consent"), withdrawn / undeliverable ("not answered")
  are reported distinctly; all leave the file untouched.
- file changed on disk after the read → refused; own write then rewrite → allowed.
- a write through `terminal` never creates a baseline (only these tools set one).

Our design differences / decisions:
- Path rules live in `safety/path_guards.py` as pure functions over resolved strings plus the
  scope (homes from the scope, never `Path.home()` directly), so `patch` (P05-19) and V4A (P05-20)
  share them.
- The protected-file approval is one `approval` server request over the broker with choices
  `once`/`deny` (the P05-09 human step adds session/always for commands; this gate never
  persists).
- Not taken: cross-agent "a sibling wrote this file" warnings (P10 subagents), safe-root env
  restriction, sandbox-mirror lost-write guard (P11 mounts).

Tests we will write:
- stale cases: never read, partial read, changed on disk after a full read, own write then
  rewrite, own patch from a baseline; new file allowed.
- guards: `/etc/x`, symlink into `~/.ssh`, `~/.ssh/config` asks, secret store, docx refused, PDF
  new allowed / existing refused, AGENTS.md asks (approve, deny, timeout, no broker), read-output
  echo refused, missing content message, rewrite hint, CRLF/BOM kept, verified flag.

## Decisions for the lead
- The profile's `config.yaml` is write-denied as well (a prompt-injected agent could switch
  approvals off by editing it); `security.protected_instruction_files` is read with an empty
  fallback (schema key to add with P05-08's schema edits).

Compare checklist (filled after the rewrite; reopened `tools/file_tools_write_guards.py`,
`tools/file_tools.py`):
- [x] Stale cases (`tests/unit/tools/builtin/test_write_file.py::test_existing_file_never_read_is_refused`,
  `::test_partial_read_is_not_enough`, `::test_file_changed_on_disk_after_the_read_is_refused`,
  `::test_own_write_then_rewrite_is_allowed`, `::test_terminal_writes_make_no_baseline_and_other_tasks_have_none`,
  `::test_patch_from_a_baseline_keeps_it_and_blind_patch_does_not`)
- [x] Guard order: protected paths incl. symlink into ~/.ssh, ssh config approval, binary documents,
  instruction files (approve / deny / timeout / withdrawn / unsupported / no surface), read-output
  echo (`::test_system_and_credential_paths_are_refused`, `::test_ssh_config_asks_for_approval`,
  `::test_binary_documents_are_refused`, `::test_instruction_file_refusals_are_worded_apart`,
  `::test_instruction_file_without_a_surface_is_blocked`, `::test_read_output_echo_and_stub_text_are_refused`)
- [x] Missing content wording, rewrite hint, CRLF/BOM kept, verified flag
  (`::test_missing_content_explains_dropped_arguments`, `::test_rewrite_hint_for_mostly_unchanged_big_files`,
  `::test_crlf_and_bom_are_kept`); path rules in `tests/unit/safety/test_path_guards.py`
- Deliberate differences: the stale check compares the sha256 of the bytes (the reference uses
  a stat signature plus digest); the instruction-file and ssh-config questions go straight to
  the broker with once/deny; the Indus home is not exempt from the instruction gate (our persona
  file lives there and steers every run).
- Lint hooks for P05-20 (`lint_gate` before the write, `lint_after` after it; no-ops until
  `indus.tools.builtin.lint` exists) are marked `HOOK(P05-20)` in `write_file.py`.
- Not taken: cross-agent sibling-write warnings (P10), safe-root env restriction, sandbox-mirror
  guard (P11), JSON NaN/syntax gate (belongs to the P05-20 lint gate).

## Status

Done (2026-10-18). Shared: `src/indus/tools/builtin/__init__.py`.

## Integration (lead)

Merged in the P05 wave 2 integration (2026-10-18, lead's verify tree).
- Lint wired (P05-20): `lint_gate(path, old_text, new_text)` → `lint.gate` (before the write; an
  already-broken data file may be edited), `await lint_after(path, old, new, host=, cancel=)` →
  `lint.lint_edit`, reported as `lint` only for `introduced`/`preexisting`. The `importlib` hook
  is gone. Tests: `test_write_file.py::test_lint_gate_refuses_a_new_data_file_that_does_not_parse`,
  `::test_lint_gate_lets_an_already_broken_file_be_repaired_in_steps`,
  `::test_lint_after_reports_an_introduced_python_error`.
- `security.protected_instruction_files` (list, default empty) added to the config schema.
