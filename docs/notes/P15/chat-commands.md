# Task: Chat slash commands (P15-13) Phase: 15 Owner: wave-18 builder G4

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/slash_commands.py`
(tables and dispatch), `gateway/slash_commands_session.py` (new / resume / sessions / title),
`gateway/slash_commands_model.py` (precedence and `--once`), `gateway/slash_commands_status.py`
(skim). Research 06 §3.11, R04.28. Evidence: none.

What it does (own words): runs `/commands` typed in a chat. A word resolves through the shared
slash registry (`indus.service.commands.registry`: aliases, then a unique prefix among the
gateway's names; an ambiguous prefix gets a "did you mean" answer). The admin tiers (P15-10) gate
every command, quick and plugin ones included. Then, in order: `command:<name>` hooks (deny /
handled / rewrite), the two handler tables by name (no if-chains) — **any time** (status, approve,
deny, help, commands, yolo, verbose, usage, bg, btw, platform, version, subgoal, heartbeat) and
**idle only** (new, resume, sessions, title, model, sethome, whoami, retry, undo, compress,
approvals, personality, skills, memory, reload-mcp, moa) plus the idle forms of stop, queue,
steer, goal and loop — then the user's quick commands (`exec` through the env builder, 30 s
limit, redacted output; `alias` re-resolves), plugin commands, `/<skill-name>` (rewritten into a
skill invocation turn), and finally a short "unknown command" hint. A registry command the
gateway does not run yet answers that it is not available in chat. Mid-run variants for the busy
guard (stop, new, queue, steer, goal, loop) live here too.

Behaviour: `/new` (alias `/reset`) points the key at a fresh session (the old one is closed);
`/resume [name]` switches the key to an earlier session of this chat (by title, id or id prefix;
other chats' sessions only for admins); `/sessions [search q]` lists this chat's sessions
(`/sessions all` cross-origin, admin only); `/title [name]`; `/model [name] [--provider p]
[--global] [--once]` (session override persisted in the route across restarts, credentials
re-resolved never stored; precedence session → channel → global; `--once` applies to the next
turn only and is reverted on every exit path); `/sethome` writes the platform's home channel;
`/bg <prompt>` runs an isolated session and posts its result with a task id and a done/failed
prefix; `/whoami` shows the scope and tier; `/platform list|pause|resume <name>` (pause keeps the
connection, drops inbound). `/new` and `/undo` ask for a numbered confirmation while
`approvals.destructive_slash_confirm` is on (1 go ahead, 2 always — turns the question off — 3
cancel); `/reload-mcp` the same while `approvals.mcp_reload_confirm` is on. Sudo and secret
prompts are not answerable in chat (the service refuses them; the chat is told).

Inputs → outputs: a command event + its session key → reply text (or a rewritten event that
continues to the agent); state changes in the route, config, session store and service.

Files: `src/indus/gateway/core/commands.py`, `tests/unit/gateway/core/test_commands.py`,
`src/indus/service/commands/registry.py` (shared: gateway-only entries `sethome`, `platform`,
`bg`, `commands`, `verbose`, `personality`, owner P15).

## API
`indus.gateway.core.commands`:
- `CommandContext(pipeline, event, key, source, cmd, args, busy)`; `Handler = (ctx) -> Awaitable[
  str | Rewrite | None]`; `Rewrite(text)` continues to the agent with new text.
- `ANYTIME: dict[str, Handler]`, `IDLE_ONLY: dict[str, Handler]`, `MID_RUN: dict[str, Handler]`.
- `ChatCommands(pipeline, *, switcher=None, skills=None, plugin_commands=None, command_hooks=None)`:
  `resolve(word) -> Resolution`, `dispatch_idle(event, key) -> str | Rewrite | None`,
  `dispatch_busy(event, key) -> str | None`, `runnable(source) -> list[str]`.
- `PendingConfirms` (numbered confirmations; an admission interceptor).
- `run_exec_quick(scope, command, *, clock, timeout=30.0) -> str`.

Edge cases / failure modes:
- `/etc/hosts` is not a command (P15-03 parsing) and reaches the agent.
- unknown `/foo` → hint; an ambiguous prefix → candidates.
- `/model --once` restored after success, failure and interrupt.
- `/sessions all` from a non-admin → this chat's sessions plus a note.
- a quick `exec` command's environment carries no provider keys (env builder) and its output is
  redacted; it is killed after 30 s.
- `/stop` with nothing running; `/approve` with nothing waiting.

Our design differences / decisions:
- `/whoami` is in the idle table (as the phase row lists it); while busy it answers the fixed busy
  reply. It is part of the tier floor regardless.
- Cross-origin listing needs an explicit admin (tiers on and the caller listed); with tiers off
  nobody gets it from chat (fail closed).
- Not in P15 chat (answer "not available in chat yet"): `branch`, `save`, `reasoning`, `fast`,
  `voice`, `rollback`, `refine`, `agents`, `context`, `history`, `journey`, `skills`, `memory`,
  `personality`, `checkpoints`, `learn`, `curator`, `bundles`, `reload-skills`, `mcp` (later chat
  work, P2); `restart`, `update`, `pause`, `topup`, `insights`, `kanban`, `profile`, `login`,
  `footer`, `busy`, `autoprune`, `debug`, `diff`, `topic`, `suggestions`, `codex-runtime`,
  `review`, `egress`, `start`, `plan`, `init`, `blueprint` are reference commands we do not offer
  in chat (P15-25 owns restart/drain; the rest are not features of ours or are dropped).
- `/verbose` cycles a per-chat tool-progress mode (off → new → all → verbose) that P15-16 reads.
- Reply copy is ours.

Tests planned: table test of every command idle and busy (handler or fixed reply); `/model --once`
reverted after success, failure and interrupt; `/sessions` cross-origin refused for a non-admin;
quick-command env has no provider keys and output redacted; unknown `/foo` hint; `/etc/hosts`
reaches the agent; `/new` rotates the route; `/sethome` writes the config; `/bg` posts a prefixed
result with a task id; `/platform pause` drops inbound; tiers deny a non-admin.

## Decisions for the lead
- Registry rows added (CHAT_ONLY, owner P15): `sethome`, `platform`, `bg` (alias `background`),
  `commands`, `verbose`, `personality`; their English lines added to `locales/en.yaml` `slash:`
  (the registry test keeps both equal).
- Telegram's `/start` ping is answered with silence (no registry row).
- `/approve all` answers every waiting approval with "once".
- Plugin slash commands and `command:<name>` hooks are seams (`ChatCommands(plugin_commands=,
  command_hooks=)`): P13's plugin registry has no command table yet and the gateway hook files are
  P15-29; the daemon wires both.
- Config row needed (P15-02 owns the schema): `approvals.destructive_slash_confirm: bool`
  (read with default False, as the classic CLI does today).

Compare checklist (filled after the rewrite; reopened `gateway/run_busy.py` handler tables,
`gateway/run_inbound.py` command resolution and canonical special cases,
`gateway/run_inbound_hooks.py` quick / plugin commands, `gateway/slash_commands*.py` handlers):
- [x] two tables by name, no if-chains (`tests/unit/gateway/core/test_commands.py::test_tables_cover_the_gateway_registry_and_hold_no_overlap`)
- [x] every command idle and busy (`::test_every_command_answers_while_idle`, `::test_every_command_while_busy_runs_its_variant_or_the_fixed_reply`)
- [x] `/model --once` reverted after success, failure and interrupt (`::test_model_once_is_reverted_on_every_exit_path`); precedence session → channel → default and the route override (`::test_model_precedence_session_over_channel_and_persisted_in_the_route`)
- [x] `/sessions` cross-origin admin only (`::test_cross_origin_session_listing_is_admin_only`); `/title`, `/resume` (`::test_title_sessions_and_resume`); `/new` rotates (`::test_new_rotates_the_route_to_a_fresh_session`); confirmations (`::test_new_and_undo_ask_first_when_confirmation_is_on`); `/undo`, `/retry` (`::test_undo_and_retry_take_back_the_last_message`)
- [x] quick `exec`: no provider keys, redacted, 30 s limit; alias (`::test_quick_exec_has_no_provider_keys_and_redacted_output`, `::test_a_quick_exec_command_is_stopped_after_its_limit`)
- [x] unknown `/foo` hint, prefixes, terminal-only, `/etc/hosts` reaches the agent (`::test_unknown_commands_get_a_hint_and_paths_reach_the_agent`)
- [x] tiers gate built-in, quick and plugin commands; plain chat never gated (`::test_admin_tiers_gate_commands_but_never_plain_chat`)
- [x] `/sethome`, `/bg`, `/platform`, idle `/queue` / `/steer`, hooks, skills (`::test_sethome_writes_the_home_channel`, `::test_bg_posts_a_prefixed_result_with_its_task_id`, `::test_platform_pause_drops_inbound_until_resumed`, `::test_idle_queue_and_steer_send_their_text_as_a_turn`, `::test_command_hooks_can_deny_answer_or_rewrite`, `::test_a_skill_command_becomes_a_skill_turn`)
- Reference plain table: status, approve, deny, bg, btw, subgoal, heartbeat, yolo, verbose, help,
  commands, version handled; context, agents → not in chat yet (P2); restart, update, pause,
  kanban, busy, footer, profile, login, autoprune → not offered (P15-25 owns restart; the rest are
  not ours or later). Idle table: whoami, platform, stop, memory, skills, approvals, model, retry,
  sethome, compress, usage, reload-mcp, title, resume, sessions, goal, loop handled (memory and
  skills answer "not in chat yet"); reasoning, fast, save, branch, rollback, refine, voice,
  curator, bundles, reload-skills, personality → not in chat yet; topic, codex-runtime,
  suggestions, topup, insights, debug, diff, review → not offered. Special cases: new/reset,
  queue, steer, undo, moa handled; start silent; egress, learn, plan, init, blueprint not offered.

## Status

Done (2026-10-19). `src/indus/gateway/core/commands.py`; tests `tests/unit/gateway/core/test_commands.py` (22).
