# Task: Shell hooks and `indus hooks` (P13-19) Phase: 13 Owner: builder N3

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/shell_hooks.py` (config shape,
payload and response shapes, consent file, spawn and failure rules), `hermes_cli/hooks.py` (the verbs
list / test / revoke / doctor and what each prints).
Evidence: none.

What it does (own words): the user can attach their own commands to agent events in config:

```yaml
hooks:
  pre_tool_call:
    - command: ~/.indus/agent-hooks/guard.sh
      matcher: "terminal|write_file"   # tool events only; regex, full match
      timeout: 30                      # seconds, default 30
  pre_llm_call:
    - command: ./context.sh
hooks_auto_accept: false
```

Events: `pre_tool_call`, `post_tool_call`, `pre_llm_call`, `on_session_start`, `on_session_end`.
Each configured command becomes one callback on the P13-17 hooks bus (owner `shell-hooks`), so it
gets the bus's deadline, suppression and failure policy like a plugin hook. A firing writes one JSON
object to the command's stdin and reads an optional JSON object from its stdout.

Payload (stdin): `hook_event_name`, `session_id`, `cwd`, `profile`, then per event `tool_name`, `tool_input`
(tool events), `tool_response` (`post_tool_call`), `prompt` (`pre_llm_call`: the user's message),
and `extra` (the other JSON-safe keywords; the conversation history is never sent). Field names follow
the Claude Code hook convention (a third-party convention, so a script written for it works).

Response (stdout, optional):
- block: `{"decision": "block", "reason": …}`, `{"action": "block", "message": …}`,
  `{"hookSpecificOutput": {"permissionDecision": "deny", "permissionDecisionReason": …}}`, or exit
  code 2 (stderr is the reason) — `pre_tool_call` only;
- ask a human: `permissionDecision: "ask"` or `{"action": "approve"}` → the bus's approve directive;
- modify: `{"action": "modify", "args": {…}}` or `hookSpecificOutput.updatedInput` → shallow merge;
- inject: `{"context": …}`, `additionalContext`, or `hookSpecificOutput.additionalContext` →
  `pre_llm_call` context (lands in the user message).

Consent: a command runs only after the user agreed to that exact (event, command) pair once. Grants
live in `<home>/hooks_consent.json` keyed by `sha256(event \0 command)`; the first 12 hex chars are
the grant id shown by `indus hooks list` and taken by `revoke`. `INDUS_ACCEPT_HOOKS=1` (scope env),
`hooks_auto_accept: true` or an `accept=True` caller (the `--accept-hooks` flag) grant without a
prompt and record the grant. Only an interactive surface asks; every non-interactive surface (cron,
gateway, API, ACP, `-z`) never prompts and skips unconsented hooks with one warning each. A changed
command string is a new pair, so editing a hook asks again.

Running a hook: through the shell in its own process group (`start_new_session`), env from the P05
builder `child_env(scope, session_id=…)` (secrets and `INDUS_*` scrubbed), cwd = the session's
working folder. The process group is killed on every exit path: own timeout, the bus cancelling the
callback at its deadline, an exception, or interpreter shutdown. Output read is capped (64 KiB per
stream). Failure policy: `pre_tool_call` fails closed (spawn error or timeout raises, the bus turns
that into a block); every other event fails open (warning, no answer). A non-zero exit other than 2
is a warning and stdout is still parsed (Claude Code convention), except the shell's own 126 / 127
("cannot execute" / "not found"), which count as a start failure; `fail_closed: true` on a
`pre_tool_call` entry makes every failure block.

`indus hooks`: `list` (configured hooks by event, grant id and state, script changed since grant),
`test <event> [--payload JSON|@file] [--for-tool NAME]` (runs the event's hooks once through the same
path and prints exit, output and the parsed answer; a hook without consent is not run unless
`--accept-hooks`), `revoke <id|command>`, `doctor` (command found and executable, consent, a dry run
with the default payload).

Files: `src/indus/plugins/shell_hooks.py`, `src/indus/cli/commands/hooks.py`,
`tests/unit/plugins/test_shell_hooks.py`, `tests/unit/cli/commands/test_hooks.py`; shared:
`src/indus/foundation/config/schema.py` (`hooks`, `hooks_auto_accept`) and its test,
`src/indus/cli/app.py` (`hooks` group), `tests/contract/cli/non_tty_table.yaml`,
`tests/contract/cli/test_help_catalogue.py`.

Edge cases / failure modes:
- unknown event name, non-list entries, missing command: warned and skipped, others still register;
- bad regex matcher: literal comparison; matcher on a non-tool event: ignored with a warning;
- timeout outside 1..600: clamped; the bus deadline (30 s) still bounds it;
- unreadable consent file: treated as no grants (fail closed: hooks skipped), never overwritten
  silently — a grant write rewrites only a readable file;
- a hook that forks a background child holding the pipes: the group kill closes them;
- safe mode / `--ignore-user-config`: no hooks (config not read).

Our design differences / decisions:
- Consent file `hooks_consent.json` keyed by hash with short ids (`revoke <id>`), ours.
- Runs through the shell (`sh -c` / `cmd /c`), like the Claude Code convention, not argv splitting.
- Default timeout 30 s (plan), max 600 s.
- Claude Code's `permissionDecision: "ask"` maps onto our approve directive; `"allow"` is a no-op
  (it never skips our own approval rules).

Tests we will write: fake hook script blocks a tool through the bus; modify rewrites args; exit 2
blocks with stderr; inject reaches `pre_llm_call` context; unconsented hook on a cron surface skipped
with a warning (no prompt asked); interactive consent recorded, revoke → asked again; timeout blocks a
`pre_tool_call` and kills the process group (sentinel child gone); post hook failure fails open;
env scrubbed (a secret in the parent is absent); `hooks test` prints the parsed response; list,
revoke and doctor through the CLI.

## Decisions for the lead
- No session host creates a hooks bus yet. `register_shell_hooks(bus, scope, surface=…, accept=…,
  ask=…)` is the entry point; `plugins.session.plugin_options` (N3) calls it. The global
  `--accept-hooks` flag is not added to `indus` itself (it would reach nothing until the host wiring
  exists); `INDUS_ACCEPT_HOOKS`, `hooks_auto_accept` and `indus hooks test --accept-hooks` work now.
  Add the global flag with the host wiring (GlobalArgs + global_options, pass `accept=`).
- `on_session_start` / `on_session_end` call sites do not exist in core yet (bus observers, P13-17);
  shell hooks for them register and fire once a host emits those hooks.

Compare checklist (filled after the rewrite; reopened `agent/shell_hooks.py`, `hermes_cli/hooks.py`):
- [x] block via exit 2 with stderr message (`test_shell_hooks.py::test_exit_two_blocks_with_stderr`)
- [x] `decision`/`action` dialects for block and modify (`::test_block_dialects`, `::test_modify_rewrites_args`)
- [x] context answer for non-tool events (`::test_context_reaches_pre_llm_call`)
- [x] matcher full-match on tool name, invalid regex literal (`::test_matcher`)
- [x] process tree killed on timeout (`::test_timeout_blocks_and_kills_the_group`)
- [x] consent per (event, command); opt-in channels; non-interactive never prompts
  (`::test_unattended_surface_skips_unconsented`, `::test_auto_accept_channels`)
- [x] revoke removes the grant (`test_hooks.py::test_revoke_then_list_shows_not_granted`)
- [x] `hooks test` shows the parsed decision even when the hook failed (`test_hooks.py::test_test_prints_parsed`)
- Deliberate differences: the reference records the script's mtime at approval and reports drift; we
  do the same in `list` (mtime stored with the grant). The reference's `fail_closed` is kept (opt-in);
  its "approve" verb for Claude Code `decision: approve` is not mapped (it means allow there).
- Gap found at Compare: the payload did not name the firing profile — added (`profile`), pinned by
  `test_shell_hooks.py::test_payload_shape`.
- Not taken: `failClosed` spelling alias, Windows script-interpreter mapping (we run through the
  shell); outbound webhooks under `hooks:` (not in this phase); `pre_verify` shell hooks (no
  call site yet; easy to add to `EVENTS`).

## Status

Done (2026-10-19). Shared files listed above.
