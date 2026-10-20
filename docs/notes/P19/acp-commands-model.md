# Task: Slash commands, model and mode (P19-12) Phase: 19 Owner: wave-30 builder A2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `acp_adapter/gateway_commands.py`
(route table, forward, rename, rewind, steer, mutation texts), `acp_adapter/commands.py` (the
in-process command list and advertisement), `acp_adapter/model_catalog.py` (picker rows),
`acp_adapter/gateway_server.py` (`set_session_model`, `_model_confirmer`, `_confirm_deferred_pick`,
`set_session_mode`, `set_config_option`). Our `service/commands/registry.py`,
`tui_bridge/methods_slash.py` (the in-process handlers a surface runs; not importable from
`indus.acp`), `cli/slash/control_cmds.py` (`/model` parsing), `core/route.RouteBinding.apply`,
`providers/route.resolve`, `state/rewind`, `acp/sessions.py` (modes, the model config option).
Closed before writing.
Evidence: none.

What it does (own words):

Advertising: after attach (`available_commands_update`) the editor gets the registry commands that
make sense there — those offered on the desktop surface, plus `tools` — without `branch` (the
editor forks) and terminal-only ones (`quit`, `clear` screen, `history`, `config` …), then the
user's quick commands and the skill commands (names only, with their note).

Running (a registered word or alias at the start of a prompt with no attachments):

| command | here |
|---|---|
| `help` | the advertised list as text |
| `branch` | refused: "use your editor's fork action" |
| `steer <text>` | guidance for this editor's running turn; idle → if the last prompt was just cancelled, it is sent again with the steer as a correction, else the text runs as a prompt |
| `queue <text>` | the text runs as the next prompt (the daemon queues it; local: after the turn) |
| `title [name]` | rename mutation; bare form shows the title |
| `undo [N]` / `retry` | rewind mutation; `retry` resubmits the last user text |
| `tools` | the session's tool names |
| `model [id]` | with an argument: the model mutation (below); without: the current model |
| `compress [focus]` | compress mutation |
| `new` / `clear` | reset mutation (this session starts over) |
| anything else | the backend's slash executor (`run_command`); a refusal is the command's reply, never a failed prompt |

A missing argument gives the usage text. Every reply is an agent text chunk and the prompt ends
`end_turn`.

Model: the pinned schema (v1.19.0) has no `session/set_model` (D-16.601): the picker is the
`configOptions` select `model` (values `provider:model`; `custom:<name>:<model>` for named
endpoints), changed through `session/set_config_option{configId:"model", value}` and by `/model`.
A guarded target (the route resolves but the session's context in use does not fit the target's
window) asks first with a permission card — "Switch anyway" (`allow_once`) / "Cancel"
(`reject_once`); declined or dismissed → error `model_switch_cancelled`, nothing changed. During a
running turn the pick is deferred to the next turn with a notice ("… applies from your next
message"); a deferred pick refused at the boundary is announced. The profile default is never
changed.

Mode: one mode, `default` (ask before edits), in both hosts (D-16.29). `session/set_mode` with
another id, or `session/set_config_option` for the edit-approval policy with a non-`ask` value →
error `acp_edit_policy_mutation_unavailable`; any other config write is accepted as a no-op and
answered with the current config options.

Inputs → outputs: prompt text `/word args` → reply text; `session/set_mode`,
`session/set_config_option` → responses.

Files: `src/indus/acp/commands.py`, `src/indus/acp/model_mode.py`,
`tests/contract/acp/test_commands.py`, `tests/contract/acp/test_model_mode.py`; shared:
`acp/backend.py` (`steer`, `run_command`, mutation status `confirm`), `local_host.py` (model,
rewind, reset mutations; slash executor; steer), `attached_host.py` (steer; slash executor answers
unavailable), `acp/sessions.py` (`available_commands` delegates here).

Vendor names used (schema v1.19.0): `available_commands_update{availableCommands[{name,
description, input{hint}}]}`, `session/set_mode` (`SetSessionModeRequest{sessionId, modeId}`),
`session/set_config_option` (`SetSessionConfigOptionSelectRequest{sessionId, configId, value}`,
`SetSessionConfigOptionResponse{configOptions}`), `SessionConfigOptionSelect{type:"select", id,
name, category, currentValue, options}`, `session/request_permission` for the guard card.

Edge cases / failure modes:
- `/etc/hosts …`, `/tmp/x`, unknown `/word` → prompt text;
- a command with attachments → prompt text (the reference did the same);
- attached mode: mutations the daemon does not serve yet (`session.mutate`, D-16.602) and the
  slash executor answer "not available while attached; run `indus acp --local`" as the reply;
- `/model` with an unknown provider or model → the resolver's message as the reply.

Our design differences / decisions:
- Guard = the target's context window is smaller than the context the session uses now (our
  product has no other model guard); easy to extend in `model_mode.guard`.
- In-process command handlers live in the local host (the ACP layer cannot import the TUI bridge).

Tests we will write:
- `/branch` refused text; unknown `/foo` sent as prompt; `/etc/hosts` sent as prompt;
- `/title x` renames; `/help`; `/steer` idle runs as prompt;
- guarded model declined → error and no mutation; mid-turn switch deferred notice;
- set_mode `accept-edits` refused in both modes; non-ask edit policy refused; other config no-op.

## Decisions for the lead
- `AcpBackend` gains `steer` and `run_command`; `MutationResult.status` gains `confirm`.
- Commands advertised = desktop-surface commands + `tools`, minus `branch`/`quit`.

Compare checklist (filled after the rewrite; reopened `gateway_commands.py`, `commands.py`, `gateway_server.py` model/mode):
- [x] route table rows (test: `tests/contract/acp/test_commands.py`)
- [x] refusal is the reply, never a failed prompt (test: `test_commands.py::test_branch_is_refused_and_unknown_words_are_prompt_text`, `::test_attached_commands_without_a_daemon_verb_answer_as_a_reply`)
- [x] guarded pick card; declined → `model_switch_cancelled` (test: `tests/contract/acp/test_model_mode.py::test_a_declined_guarded_pick_changes_nothing`, `::test_an_accepted_guarded_pick_switches`)
- [x] deferred pick notice and boundary refusal (test: `test_model_mode.py::test_a_pick_during_a_turn_waits_for_the_next_one`, `::test_a_deferred_pick_refused_at_the_boundary_is_announced`)
- [x] single mode in both hosts (test: `test_model_mode.py::test_only_the_ask_mode_exists`)
- [x] context meter refreshed after `/compress` and `/new`
- Deliberate differences: no `session/set_model` (schema v1.19.0, D-16.601) — the `model` config
  option and `/model`; a guarded pick deferred to a turn boundary is refused there with a notice
  (the reference asked again at the boundary); `/steer` while idle resends a just-stopped prompt
  with the steer as a correction; the model catalogue offers the current model only.
- Not taken: the reference's per-provider model catalogue (`model_catalog.py`) — follow-up.

## Status

Done (2026-10-20), wave-30 builder A2. Shared: `acp/backend.py` (`steer`, `run_command`, mutation
status `confirm`, code `unsupported_command`), `local_host.py` (model / rewind / reset mutations,
in-process info commands, title before the first reply), `attached_host.py` (`steer`;
`run_command` answers unavailable), `sessions.py` (`available_commands` → `commands.advertised`).
