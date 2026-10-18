# Task: REPL core (P06-04) Phase: 06 Owner: P06 slice B builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `cli.py` (the `run()` loop shape:
one long-lived prompt application, a worker that runs turns, cleanup on exit),
`hermes_cli/cli_terminal_input.py` (key binding helpers: submit keys, backslash continuation,
extended Enter reporting), `hermes_cli/cli_tui_mixin.py` (only the Ctrl+C handler and the busy-input
router, to see the priority order and the three busy modes). Research `04-cli-config.md` §3.6,
`05-tui.md` §3.16 (Ctrl+C resolution) and traps R05.5, R05.18. Closed before writing.
Evidence: none.

What it does (own words): the classic chat prompt. One prompt_toolkit prompt stays on screen for
the whole session on the main thread; the turn itself runs on `InProcessHost`'s loop thread. Events
and server requests come back through the host's thread-safe feed; a pump thread hands each item to
the prompt's own event loop, so every piece of REPL state is touched by one thread only. The REPL
never opens the store or runs tools: everything goes through the host.

Inputs → outputs:
- Enter: send the draft (a line ending in `\` becomes a newline and the draft stays open).
- Alt+Enter, Ctrl+J, and Shift+Enter when a terminal sends its kitty / modifyOtherKeys form: newline.
- Ctrl+G or Ctrl+X Ctrl+E: edit the draft in `$EDITOR`.
- Ctrl+D: exit, only on an empty draft (with text it does nothing). Ctrl+Z: suspend (Unix).
- Ctrl+C priority (R05.5): (1) a card is open → the card's cancel answer; (2) a non-empty draft →
  cleared; (3) a turn runs and no Ctrl+C in the last 2 s → `interrupt(session, generation)` and the
  partial reply is sealed; (4) otherwise a second press within 2 s of the previous one exits, a
  lone press only prints how to leave.
- Enter while a turn runs, by `display.busy_input_mode`: `interrupt` (default; unknown values
  too) → `redirect`; if the host rejects it, interrupt the turn and send the text next;
  `queue` → held and sent after `turn.done`; `steer` → `steer`; not taken → queued. The first busy
  Enter prints a one-time hint and saves `onboarding.seen.busy_input: true`.
- Lines starting with `/` or `!` go to the command hook (slice C's dispatch); unhandled → prompt.
- Multi-line paste: kept in the draft, one dim preview line printed.
- History: `<home>/input_history`, written through `fsio` (atomic, owner-only), last 1,000 entries.

Files: `src/indus/cli/repl/__init__.py`, `src/indus/cli/repl/app.py`, `src/indus/cli/repl/keys.py`,
`src/indus/cli/repl/history.py` (new), `tests/fakes/service.py`, `tests/unit/cli/repl/test_keys.py`.

Edge cases / failure modes:
- Interrupt racing the turn's end: the interrupt names the turn's generation, so a stale one is a
  no-op at the service; the REPL ignores its result.
- A turn ends while text is queued: queued prompts drain one per `turn.done`, in order.
- Host call fails (`ProtocolError`, busy): one red line, the REPL stays up.
- Config write for the hint fails (managed install): hint shown, nothing saved, no crash.
- `$EDITOR` missing: prompt_toolkit falls back to its own editor list.

Our design differences / decisions:
- An idle Ctrl+C on an empty draft needs a second press within 2 s to exit (the reference exits on
  the first); a stray press never kills the session.
- We do not push the kitty keyboard protocol or modifyOtherKeys to the terminal; we only map their
  Shift+Enter sequences when a terminal already sends them.
- Clock-driven: the 2 s window uses the scope clock (fake in tests).
- Not here: image paste, voice keys, stash, live monitor, dock (later phases or not planned).
- Gateway-client behaviours (busy input always queues at the gateway, Ctrl+D detaching) arrive with
  P17 (I55).

Interfaces for slices A and C:
- `indus.cli.repl.run_repl(host, session_id, *, scope, options=None, commands=None, banner=None,
  recap=None, **repl_kwargs) -> int` — the entry the chat command calls (prints the banner and
  recap, runs the prompt, returns 0). `options` defaults to `ReplOptions.from_scope(scope)`; pass
  `ReplOptions.from_scope(scope, model=..., title=..., auto_approve=...)` to fill the status bar.
  `status_source=` (a callable returning `StatusSnapshot`) may feed context use, cost and jobs.
- `CommandHook = Callable[[Repl, str], bool]`: called with every entered line before it goes to
  the model; True means handled. Without a hook only `/quit` and `/exit` are understood.
- Slice C wiring (`src/indus/cli/repl/slash_bridge.py`, used when `run_repl(..., slash_ctx=ctx,
  on_resume=fn)`): `bang.is_bang` → `bang.run_bang` (inside `run_in_terminal`, prompt put away);
  `is_slash` → `dispatch(line, ctx)` and the `SlashResult` is applied (clear_screen, switch_to →
  `repl.attach`, resumed → `on_resume(session_id)` = slice A's `indus.cli.resume` behaviour,
  submit → `repl.send`, prefill → draft, exit → leave); other lines → `take_resume_pick`; turn end
  → `after_turn(ctx)` once `repl.pending` is empty; Tab → `REGISTRY.complete(text,
  runnable_names(ctx))` (assumed importable from `indus.cli.slash.dispatch`). Slice C's modules
  are not in this worktree: tests use `tests/unit/cli/repl/_stubs_slice_c.py` (STAND-IN) and the
  bridge carries a pyright pragma to drop at integration.
- What a handler may use on `Repl` (all on the prompt's loop thread): `print(renderable)`,
  `note(text)`, `error(text)`, `send(text)`, `queue_prompt(text)`, `interrupt()`,
  `request_exit()`, `attach(session_id)` (after `/new`, `/resume`, `/branch`), `busy`,
  `session_id`, `turn_id`, `generation`, `pending` (deque of queued prompts, for `/queue`),
  `renderer.show_reasoning` (for `/reasoning show|hide`), `title`, `auto_approve` (status bar),
  `options.busy_mode`, `renderer.ring` (last 200 lines).
- `tests/fakes/service.py::FakeHost` is the fake host for REPL and slash tests.

Tests we will write:
- Pipe input: Ctrl+C order with a fake clock (draft → interrupt → exit within 2 s; >2 s apart is
  no exit); Ctrl+D with text does nothing, on empty exits.
- Busy modes on a fake host: interrupt → redirect (and interrupt + queue on rejection), queue →
  sent after `turn.done`, steer → steer (queued when refused), unknown mode → interrupt; one-time
  hint saved once.
- Keys: Alt+Enter / Ctrl+J newline, trailing backslash continues, slash/bang lines reach the hook.
- History file round trip.

## Decisions for the lead
- `display.busy_input_mode`, `display.cli_refresh_interval`, `onboarding.seen.busy_input` are read
  with fallbacks; slice A owns the schema rows (P06-03).
- Idle exit needs a double Ctrl+C (see above); change `CtrlCPolicy.idle_needs_double` to revert.

Compare checklist (filled after the rewrite; reopened `cli.py`, `hermes_cli/cli_terminal_input.py`,
`hermes_cli/cli_tui_mixin.py`):
- [x] Ctrl+C priority, open card first (tests: `test_keys.py::test_ctrl_c_policy_order`,
  `test_keys.py::test_ctrl_c_clears_draft_then_interrupts_then_exits_within_two_seconds`,
  `test_cards.py::test_ctrl_c_on_the_card_denies_with_cancel_and_keeps_running`)
- [x] Busy routing of the three modes, steer refusal → queue, redirect refusal → interrupt + send
  next, one-time hint persisted (tests: `test_keys.py::test_busy_*`)
- [x] Submit keys, backslash continuation, legacy Ctrl+J submit behind
  `display.cli_multiline_shortcuts: false` — gap found on compare, added
  (`test_keys.py::test_ctrl_j_sends_when_multiline_shortcuts_are_off`)
- [x] Busy-time input never stranded: queued prompts drain one per `turn.done`
  (`test_keys.py::test_busy_queue_mode_sends_after_the_turn_ends`)
- [x] prompt_toolkit's CPR warning kept out of the chat (found in a pty smoke run; the reference
  silences it too)
- Deliberate differences: Ctrl+D with text does nothing (the phase row) where the reference deletes
  the character under the cursor; idle Ctrl+C needs a double press; no kitty/modifyOtherKeys push;
  no image paste, voice, stash, monitor, dock, pet.
- Arrives with P17 (I55): gateway-client behaviours — busy input always queued at the gateway,
  Ctrl+D detaching a running turn, refused flags, reduced slash set, exit 3.

Also checked by hand: a pseudo-terminal run against the real `InProcessHost` + fake model
(prompt, streamed reply, approval card answered `1`, Ctrl+C sealing a stalled reply, Ctrl+C again
leaving) — no test file, the P06-14 e2e covers it in CI.

## Status

Done (2026-10-18; slice C wiring added after the lead's note). Shared changes: `src/indus/cli.py` moved unchanged to `src/indus/cli/__init__.py`
(slice A replaces it), `pyproject.toml` (prompt_toolkit, rich), `uv.lock` (local re-lock only),
`THIRD_PARTY_NOTICES.md`, `00-layout-and-conventions.md` §4 rows. Slice A calls `run_repl`; slice C
plugs its dispatch in as `commands=` (interface above).

## Integration (lead)

- `_stubs_slice_c.py` (STAND-IN), its `install()` line and the pyright pragma in
  `slash_bridge.py` are gone; the bridge imports the real `indus.cli.slash` modules.
- Completion: the registry's `complete(text, names, extra=…)` returns `Completion(text, meta)`
  objects, not strings; `SlashCompleter` takes both and shows the note as display meta
  (`complete_for(ctx)` passes `runnable_names(ctx)` as names and extra).
- Slash and bang lines run in a terminal section (`slash_bridge.terminal_section`): prompt put
  away with `run_in_terminal`, stdout/stderr pointed back at the real terminal while
  `patch_stdout`'s proxy is installed (otherwise output and questions only appear after the
  command ends, because the proxy flushes through the blocked loop), and on the main thread a
  plain `KeyboardInterrupt` for Ctrl+C with the loop's wake-up fd detached (the prompt's own
  SIGINT handler would otherwise only run after the command). This is what makes Ctrl+C during
  `!cmd` give 130, and lets `/new`'s confirm and the bang approval card read a line.
- `wire_slash` binds the context's `out` (`repl.print`), `is_busy`, `confirm` and `ask_approval`
  to the REPL; outside a terminal section both questions answer "no"/nothing. After each command
  `sync_from` copies the reasoning display switch, auto-approve, title and (after `/model`) the
  model into the REPL's status.
- `run_repl` gained `on_ready(repl)` (runs after wiring, before the banner): the launch adapter
  uses it to keep the REPL for `on_resume` and to post the seeded `-q` text.
- `test_slash_bridge.py` rewritten against the real dispatch with C's `FakePort` and a real store.
