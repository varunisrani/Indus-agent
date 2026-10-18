# P06 · Classic CLI chat → Demo 1 (walking skeleton)

**Weeks:** 11 (1 week) · **Track:** B · **Depends on:** P05 (core tools, approvals), P04 (`SessionService`, in-process host, slash-relevant core APIs, route binding), P01 (profile pre-parse, config, `.env` load, logging), P02 (session listing/resume queries) · **Status:** planned

## Goal
A person types `indus`, gets a chat prompt, asks "create hello.py and run it", watches the answer
stream, approves the command on a card, sees it run locally, and quits; `indus -c` resumes with the
history intact. The same flow runs in CI against the fake model. One-shot `indus -z "…"` prints only
the final text and exits with the documented code. The agent runs in the CLI process (P1,
D-16.10, inventory I55) behind `SessionService`, so the P17 daemon can replace the host later
without touching the REPL. This phase is the **walking skeleton** (Demo 1, milestone M1) and seeds
the acceptance map and traps ledger pages (D-15.3).

## Tasks (15)

| ID | Task | Read in reference | New files | Verify |
|---|---|---|---|---|
| P06-01 | **Entry point and startup order.** `indus` console script → `indus.cli.entry:main`. Order, each step before any app import that depends on it: (1) P01's profile pre-parse scans argv for `-p/--profile` (skipping values of value-taking flags, using the flag list exported by P06-02's app and a hand-kept fallback) and strips it, sets the scope home (R04.2); (2) `.env` load with home-overrides-shell (P01); (3) bridge `security.redact_secrets` and `--yolo`/`INDUS_YOLO_MODE` into the scope **before** the approval or redaction modules are imported (R04.13, R04.14); (4) logging setup (P01) with mode `cli`; (5) `--version`/`-V` fast path printing version, install method (git/docker/nix/dev), install dir — must finish in <150 ms (D-04.4), measured by a test that imports with `-X importtime` budget; (6) build the lazy click app (P06-02) and dispatch; an integer non-zero return becomes the exit code. A failure inside the profile layer prints one warning and continues on the default profile; a named profile that does not exist exits 1. Replaces the P00 placeholder `src/indus/cli.py` with the `cli/` package. | `hermes_cli/main.py` [3602: `main`, `_apply_profile_override`; skip updater re-exports], `hermes_cli/_startup_fast.py` [260: version printer], research `04` §3.1 | `src/indus/cli/__init__.py`, `src/indus/cli/entry.py`, `src/indus/cli/version.py`, `tests/unit/cli/test_entry.py` | `-p work` in any position resolves the profile and is stripped; `--reasoning high chat` does not read `high` as a command; yolo env set before the approval module import (import-order spy); version path under budget; missing named profile → exit 1 |
| P06-02 | **click app, lazy groups, global flags, unknown-command UX.** A lazy-loading click group (`LazyGroup`: subcommand name → `module:attr`, imported on first use) so `indus --help` imports no subcommand module. Global flags per research `04` §3.2.1 under our names: `-m/--model`, `--provider`, `--reasoning`, `-t/--toolsets`, `-r/--resume`, `-c/--continue [NAME]` (optional value: click `is_flag=False, flag_value="__latest__"`), `--in DIR`, `--no-restore-cwd`, `-s/--skills`, `--yolo`, `--pass-session-id`, `--ignore-user-config`, `--ignore-rules`, `--safe-mode`, `--tui/--cli`, `-z/--oneshot`, `--usage-file`, `-V`. Multi-word session names after `-c` coalesced; values given before `chat` are not overwritten by the subcommand's defaults (R04.21). Bare `indus` = `chat`. Help order fixed by a registration list (stable). Unknown first positional: exactly three lines — "'X' is not an indus command.", then a "did you mean: a, b, c?" (≤3 matches, cutoff 0.6) or a moved/plugin hint, then "Run `indus --help` to see all commands."; exit 2; same inside nested groups. Exports the value-taking flag set for P06-01. | `hermes_cli/_parser.py` [394: flag list], `hermes_cli/main.py` [3602: `_parse_cli_args`, routing workaround — to know the cases, not the method], `hermes_cli/cli_unknown_command.py` [21] | `src/indus/cli/app.py`, `src/indus/cli/lazy.py`, `src/indus/cli/unknown.py`, `tests/unit/cli/test_app.py` | `CliRunner`: `indus -c model chat` (session named "model"), `indus -c my long name`, `indus -m x chat` keeps `x`; `--help` import spy shows no subcommand modules imported; unknown command three lines + exit 2; help order golden |
| P06-03 | **`chat` command and session selection.** Chat-only flags `-q/--query`, `--query-file` (`-` = stdin; nothing shell-interpreted), `--oneshot`, `-Q/--quiet`, `--max-turns`, `--source` (default `cli`). Interface choice: `--cli` → classic; `--tui` → "TUI arrives in P12" message and exit 2 for now; stdin or stdout not a TTY → classic (R04.19); else `display.interface`. Session selection: `--in DIR` (expand `~`, must exist else exit 1, chdir, implies `--no-restore-cwd`); `-c NAME` by title or id (miss → stderr "No session found matching 'NAME'." + hint, exit 1); bare `-c`/`--resume latest` → this terminal's breadcrumb session if valid (`session.terminal_continue`), else the most recent session for this workspace (git root, else cwd), none → exit 1; literal `latest` beats a session titled "latest". Creation flags (cwd, model, provider, reasoning, toolsets, max turns, skills, yolo, pass-session-id, ignore-rules, safe-mode) are frozen into the session at creation; a resume may repeat the same values, a different value is refused with exit 2 (kept from R04.1 even in-process). Builds the scope, the `InProcessHost`, and hands off to the REPL, one-shot, or seeded-query path (`-q` on a TTY without `--oneshot`/`-Q` submits the text literally as the first turn and keeps the REPL open). | `hermes_cli/main.py` [3602: `cmd_chat`, `_resolve_continue_arg`], research `04` §3.4.1–3.4.5 | `src/indus/cli/chat.py`, `src/indus/cli/session_select.py`, `tests/unit/cli/test_chat_select.py` | Store fixture with sessions in two workspaces: breadcrumb wins; workspace fallback; title vs `latest`; frozen-flag conflict exit 2; non-TTY forces classic; seeded `-q` does not interpret `/` or `!` |
| P06-04 | **REPL core.** prompt_toolkit `PromptSession` on the main thread; the agent runs on `InProcessHost`'s loop thread (D-04.1 e); events are pulled through a thread-safe queue and rendered by P06-05. Keys: Enter send; Alt+Enter / Ctrl+J newline (Shift+Enter when the terminal reports it); trailing `\` continues; Ctrl+G or Ctrl+X Ctrl+E opens `$EDITOR`; Ctrl+D exits on an empty prompt; Ctrl+Z suspends. **Ctrl+C priority**: clear a non-empty draft first, else interrupt the running turn (`SessionService.interrupt` with the turn's generation), else a second press within 2 s exits (R05.5). Busy input (`display.busy_input_mode`: `interrupt` default, `queue`, `steer`; unknown → interrupt) with a one-time hint remembered in `onboarding.seen.busy_input`. Multi-line paste shows a one-line preview. Input history in `<home>/input_history`. The REPL never touches the store or tools directly. | `cli.py` [1793: `main()` loop shape], `hermes_cli/cli_terminal_input.py` [602: key bindings], research `04` §3.6 | `src/indus/cli/repl/__init__.py`, `src/indus/cli/repl/app.py`, `src/indus/cli/repl/keys.py`, `tests/unit/cli/repl/test_keys.py` | prompt_toolkit pipe input: Ctrl+C order (draft → interrupt → exit within 2 s, fake clock); busy modes route to interrupt/queue/steer on a fake service; Ctrl+D with text does nothing |
| P06-05 | **Streaming render.** rich-based renderer fed by protocol events: `message.delta` streamed into a live markdown region (flushed at line boundaries, final render on `turn.done`); reasoning hidden by default (shown dim with `/reasoning show`); tool trail lines (`tool.started` → spinner line with the tool's emoji and a short argument preview, `tool.completed` → duration and ok/error mark); `notice`/`status` lines; on interrupt the partial reply is sealed at once and later deltas ignored (R05.4 applied to the classic surface). Final reply markdown: verbose fences and bold/italic wrappers stripped when `display.final_response_markdown: strip`. Output ring of the last 200 lines for redraw. Spinner verbs and all copy written fresh. | `hermes_cli/cli_stream_mixin.py` [807: delta handling and sealing], `hermes_cli/cli_render.py` [skim] | `src/indus/cli/repl/render.py`, `tests/unit/cli/repl/test_render.py` | Recorded event streams (fixtures) rendered to a `Console(record=True)`: golden text; deltas after interrupt ignored; reasoning hidden/shown |
| P06-06 | **Banner, recap and status bar.** Banner: model, terminal backend, cwd, tool and skill counts (copy ours). Resume recap ("previous conversation"): last `display.resume_exchanges` (10) exchanges, user ≤300 chars, assistant ≤200 chars and ≤3 lines. Bottom toolbar: model (truncated past 26 chars) │ context used/max (`~` prefix when part is a local estimate) │ fill bar │ cost (`n/a` until P08 pricing) │ compression count │ background jobs │ turn duration │ title │ auto-approve warning; width tiers ≥76 full, 52–75 compact, <52 minimal; context colours <50 % / 50–80 / 80–95 / ≥95. Refresh `display.cli_refresh_interval` (1.0 s). | `hermes_cli/cli_status_bar_mixin.py` [1243: segments and width tiers], `hermes_cli/cli_session_mixin.py` [1163: recap] | `src/indus/cli/repl/status.py`, `src/indus/cli/repl/banner.py`, `tests/unit/cli/repl/test_status.py` | Width-tier goldens at 40/60/100 cols; colour thresholds; recap truncation table |
| P06-07 | **Approval card, clarify and sudo prompts.** The REPL answers server requests from the event stream: approval card shows the **server-redacted**, wrapped command and description and quick picks by digit `1` once, `2` session, `3` always, `4` deny (deny asks for an optional reason) (R05.11, R05.22); a countdown shows `approvals.timeout_s`; Ctrl+C on the card answers deny-with-cancel, not a crash. Clarify renders 1–5 questions with numbered choices, multi-select, "skip"; sudo prompt is masked. While a card is open, other input is held. All copy fresh. | `hermes_cli/cli_modal_mixin.py` [skim: card flow], `tools/approval_prompt.py` [361: choice semantics] | `src/indus/cli/repl/cards.py`, `tests/unit/cli/repl/test_cards.py` | Pipe-input tests: each digit maps to its answer; long command wraps without clipping; timeout path shows "no answer"; clarify multi-select answer shape |
| P06-08 | **Slash-command registry.** One ordered registry, the single source for dispatch, completion, help-by-category and (later) gateway/TUI menus: `CommandDef{name, description_key, category: session|config|tools|info|exit, aliases, args_hint, subcommands, surfaces: {cli, tui, gateway, desktop}, busy_policy: dispatch|reject|interrupt_then_dispatch (default reject), argument_mode: options|text|mixed}`. Case-insensitive names; aliases resolve through the registry; descriptions via the English catalogue key (fallback text). Tab completion of names and subcommands in the REPL. **Location (lead ruling):** the definitions and the alias/prefix resolver live in `indus.service.commands` (service layer), not under `indus.cli`, so `tui_bridge`, `gateway` and `acp` read the same table without importing the CLI; each surface keeps its own handlers (the classic CLI's under `src/indus/cli/slash/`). | `hermes_cli/commands.py` [568: entry shape, registry], `website/docs/reference/slash-commands.md` [340] | `src/indus/service/commands/__init__.py`, `src/indus/service/commands/registry.py`, `tests/unit/service/commands/test_registry.py` | No duplicate names/aliases; every entry has a handler or a declared owner phase; completion lists aliases once; help grouping golden; `indus.service.commands` imports nothing from `indus.cli` (import-linter) |
| P06-09 | **Slash dispatch + session commands.** Dispatch precedence: lowercase the first word for matching only; resolve alias; built-in handler; then user quick commands (P06-11) → plugin commands (P13 seam) → skill commands (P09 seam) → unique-prefix expansion; an `observer` hook seam fires before built-ins and can never block. Busy policy enforced. Session commands: `/new` (alias `/reset`; `now`/`-y` skips confirm), `/clear` (screen + new session), `/history`, `/title [name]`, `/resume [name]` (numbered picker; any other command disarms it), `/retry`, `/undo [N]`, `/save json|md|html [file]` (via P02 export), `/branch [name]` (P02 copy), `/sessions`. | `cli.py` [1793: `process_command`], `hermes_cli/cli_commands_mixin.py` [2416: session command handlers] | `src/indus/cli/slash/__init__.py`, `src/indus/cli/slash/dispatch.py`, `src/indus/cli/slash/session_cmds.py`, `tests/unit/cli/slash/test_dispatch.py` | Precedence order with fakes at each level; prefix ambiguity → list; `/new` while busy → interrupt then dispatch; `/undo 2` hides two turns (store check); `/resume` picker disarmed by another command |
| P06-10 | **Control and info commands.** `/model [model] [--provider p] [--global]` (switch among configured providers only via P04 `RouteBinding`; rejected while busy; `--global` persists through the config writer, clearing a stale base URL when leaving a custom endpoint — R04.9), `/reasoning level|show|hide [--global]` (ladder `none…ultra`), `/yolo` (toggle auto-approve, stored on the session), `/approvals [manual|smart|off]` (writes `approvals.mode`; managed-install or unreadable-config errors shown, never fatal; re-reads and reports if the change did not take), `/tools list|enable|disable` (session-scoped; resets the session as the prompt depends on tools), `/config` (redacted effective config), `/status`, `/context`, `/usage`, `/whoami`, `/help [filter]`, `/version`, `/stop` (kill this session's background processes + interrupt), `/steer text`, `/queue text|list|clear|rm N`, `/compress` (calls P08's engine through a seam; prints "arrives with P08" until then), `/quit` (alias `/exit`). | `hermes_cli/cli_commands_mixin.py` [2416: handlers for these commands], `hermes_cli/approval_mode.py` [66], `hermes_cli/cli_model_switch_mixin.py` [skim] | `src/indus/cli/slash/control_cmds.py`, `tests/unit/cli/slash/test_control_cmds.py` | `/model` while busy rejected; failed switch rolls back (fake route that fails mid-apply); `/approvals off` persists and is read back; `/yolo` survives resume; `/queue` runs after the turn |
| P06-11 | **Bang shell + quick commands.** `!cmd`: only a leading `!` after trim (`!!` passes a literal `!`; bare `!` prints usage); interactive local sessions only; runs on the host (never the sandbox backend) in the session cwd (else config cwd, else process cwd); goes through the P05 approval pipeline as a local non-isolated command (floors, deny globs, dangerous prompt) — if the guard cannot load, refuse (intended difference D-16.77: fail closed, R04.27); env from `safety.child_env` (refuse to launch if it fails); merged stdout/stderr streamed; timeout 120 s → kill, code 124; Ctrl+C → kill, 130, session survives; launch failure 127; nothing enters history. Quick commands (`quick_commands.<name>`): `exec` runs through the shell with the child env (provider keys stripped), 30 s timeout, output redacted (R04.28); `alias` re-dispatches `/<target> <args>`. | `hermes_cli/bang_shell.py` [141], `cli.py` [1793: quick command handling] | `src/indus/cli/bang.py`, `src/indus/cli/quick_commands.py`, `tests/unit/cli/test_bang.py`, `tests/unit/cli/test_quick_commands.py` | `!rm -rf /` blocked by the floor; `fix it!` is a prompt; env probe in `!env` has no provider key; timeout 124 (fake clock + real process); quick exec output redacted |
| P06-12 | **One-shot `-z` and finite `chat -q`.** `-z PROMPT`: no banner, spinner, tool previews or session line; only the final reply on stdout; approvals auto-approved for the run **but floors still apply** (a hardline command is refused, M1 criterion); toolsets from `-t` (`all`/`*` allowed) else the `cli` platform set; only `--provider` without `--model` → usage error. Exit codes: 0 completed; 2 failed/partial/budget/not completed (even if text printed) and usage errors; 130 interrupted; 1 completed with no text. `chat -q` finite runs (non-TTY, `--oneshot`, `-Q`): 0 completed; 1 failed/partial/budget/never ran; 130 interrupted (R04.20). `--usage-file PATH`: JSON written even on failure (cost fields `null` with `cost_status: unknown` until P08; token counters, api_calls, model, provider, session_id, completed/failed/partial/interrupted, exit_reason); a failed write never changes the exit code. Shutdown: run registered cleanups (backends, processes, clients), flush, close logging, then `os._exit(rc)`. | `hermes_cli/oneshot.py` [665: flow, exit codes, usage file], `hermes_cli/cli_single_query.py` [586] | `src/indus/cli/oneshot.py`, `tests/unit/cli/test_oneshot.py`, `tests/integration/cli/test_oneshot_exit_codes.py` | Subprocess runs with the fake model (env selects it): each exit code row; stdout contains only the final text; usage file on failure; hardline under `-z` refused |
| P06-13 | **Resume behaviour.** On resume: restore the session's recorded cwd unless `--no-restore-cwd` or `--in` (a missing dir → warning, stay); print the recap (P06-06); rehydrate per-session switches (auto-approve, reasoning, toolsets) from the session row; write this terminal's breadcrumb (keyed by the controlling TTY or `INDUS_TERMINAL_ID` when set) on every new or resumed session. | `hermes_cli/cli_session_mixin.py` [1163: resume path], research `04` §3.4.3 | `src/indus/cli/resume.py`, `tests/unit/cli/test_resume.py` | cwd restored / not restored; breadcrumb per terminal id; switches rehydrated |
| P06-14 | **Demo 1 end to end.** An e2e test drives the real `indus` binary in a pseudo-terminal (`pexpect`) against the fake model scripted for "create hello.py and run it": streaming visible, approval card answered `1`, command runs on the local backend, final answer, `/quit`; then `indus -c` shows the recap and `/history` lists the turn; store passes the alternation validator. Second test: kill the process with SIGKILL right after the approval is answered and before the tool row is written, restart with `indus -c`, assert the command ran at most once (marker file count) and the transcript is valid (M1 criteria). A third, manual script `scripts/demo1.sh` runs the same flow against a real provider key (documented, not in CI). | none (our flow) | `tests/e2e/test_demo1.py`, `tests/e2e/conftest.py`, `scripts/demo1.sh`, `tests/fixtures/model_scripts/demo1.yaml` | The two e2e tests pass in CI (Linux, macOS lanes); demo script exits 0 by hand with a key |
| P06-15 | **Acceptance map + traps ledger with checks (D-15.3).** The pages and both checks exist since P00-04 (skeleton rows); this task fills and extends them. Fill `docs/internals/acceptance-map.md` (one section per inventory area; every P0/P1 row gets ≥1 `ACC-<feature>-<n>` id; landed ids name tests as `file > class > test`; others carry `Review: <task id>` from the phase files) and `docs/internals/traps-ledger.md` (every R-id from research §6 lists with its pinning test or `Review: <task id>`). `tooling/checks/acceptance.py` parses the map without running tests: fails when a P0/P1 feature has no id, a cited test does not exist or is skipped, or a row has neither a test nor a review line; `--strict` also fails on review rows (v1.0 gate). `tooling/checks/traps.py` fails when an R-id from the research specs has no row. Seed both pages from P04–P06 rows now marked landed. | none (our tooling); `docs/plan/03-feature-inventory.md`, research §6 lists | `tooling/checks/tests/test_acceptance.py`, `tooling/checks/tests/test_traps.py` | Seeded violations (missing id, skipped test, missing R-id) fail; current pages pass non-strict |

## Files to create (46)

| Path | What it holds | Task |
|---|---|---|
| `src/indus/cli/__init__.py`, `src/indus/cli/entry.py`, `src/indus/cli/version.py`, `tests/unit/cli/test_entry.py` | entry, startup order, version | P06-01 |
| `src/indus/cli/app.py`, `src/indus/cli/lazy.py`, `src/indus/cli/unknown.py`, `tests/unit/cli/test_app.py` | click app, lazy loading, unknown UX | P06-02 |
| `src/indus/cli/chat.py`, `src/indus/cli/session_select.py`, `tests/unit/cli/test_chat_select.py` | chat command, selection, frozen flags | P06-03 |
| `src/indus/cli/repl/__init__.py`, `src/indus/cli/repl/app.py`, `src/indus/cli/repl/keys.py`, `tests/unit/cli/repl/test_keys.py` | REPL core | P06-04 |
| `src/indus/cli/repl/render.py`, `tests/unit/cli/repl/test_render.py`, `tests/fixtures/event_streams/*.jsonl` | streaming renderer + recorded streams | P06-05 |
| `src/indus/cli/repl/status.py`, `src/indus/cli/repl/banner.py`, `tests/unit/cli/repl/test_status.py` | status bar, banner, recap | P06-06 |
| `src/indus/cli/repl/cards.py`, `tests/unit/cli/repl/test_cards.py` | approval/clarify/sudo cards | P06-07 |
| `src/indus/service/commands/__init__.py`, `src/indus/service/commands/registry.py`, `tests/unit/service/commands/test_registry.py` | shared slash-command registry (service layer) | P06-08 |
| `src/indus/cli/slash/__init__.py`, `src/indus/cli/slash/dispatch.py`, `src/indus/cli/slash/session_cmds.py`, `tests/unit/cli/slash/test_dispatch.py` | CLI dispatch + session command handlers | P06-09 |
| `src/indus/cli/slash/control_cmds.py`, `tests/unit/cli/slash/test_control_cmds.py` | control/info commands | P06-10 |
| `src/indus/cli/bang.py`, `src/indus/cli/quick_commands.py`, `tests/unit/cli/test_bang.py`, `tests/unit/cli/test_quick_commands.py` | bang shell, quick commands | P06-11 |
| `src/indus/cli/oneshot.py`, `tests/unit/cli/test_oneshot.py`, `tests/integration/cli/test_oneshot_exit_codes.py` | one-shot + finite chat | P06-12 |
| `src/indus/cli/resume.py`, `tests/unit/cli/test_resume.py` | resume behaviour | P06-13 |
| `tests/e2e/test_demo1.py`, `tests/e2e/conftest.py`, `scripts/demo1.sh`, `tests/fixtures/model_scripts/demo1.yaml` | Demo 1 | P06-14 |
| `tooling/checks/tests/test_acceptance.py`, `tooling/checks/tests/test_traps.py` | ledgers + checks | P06-15 |

## Files to update (7)

| Path | Change | Task |
|---|---|---|
| `src/indus/cli.py` (P00 placeholder) | delete; replaced by the `src/indus/cli/` package (a module and a package of the same name cannot coexist) | P06-01 |
| `src/indus/__main__.py` (P00) | call `indus.cli.entry:main` | P06-01 |
| `pyproject.toml` | `[project.scripts] indus = "indus.cli.entry:main"`; deps click, prompt_toolkit, rich (exact pins); dev dep pexpect | P06-01 |
| `src/indus/foundation/config/schema.py` (P01) | `display.*` keys used here (interface, busy_input_mode, final_response_markdown, resume_*, cli_refresh_interval), `quick_commands`, `session.terminal_continue` | P06-03 |
| `tooling/checks/__main__.py` | register `acceptance` and `traps` | P06-15 |
| `.github/workflows/ci.yml` | e2e lane for `tests/e2e` on Linux and macOS | P06-14 |
| `docs/internals/acceptance-map.md`, `docs/internals/traps-ledger.md`, `tooling/checks/acceptance.py`, `tooling/checks/traps.py` (exist since P00-04) | fill the pages with ids and review lines; extend the checks with the skipped-test rule and `--strict` | P06-15 |

## Fixtures and fakes

| Path | What it holds | Task |
|---|---|---|
| `tests/fixtures/event_streams/*.jsonl` | recorded protocol event streams (plain answer, tool trail, interrupt mid-delta, approval request) — also handed to the TUI frontend track | P06-05 |
| `tests/fixtures/model_scripts/demo1.yaml` | fake-model script for Demo 1 | P06-14 |
| `tests/fakes/service.py` | fake `SessionService` for REPL/slash unit tests (records calls, emits scripted events) | P06-04 |

## Task notes (15)
`docs/notes/P06/<slug>.md`: `entry-startup` (01), `click-app` (02), `chat-select` (03),
`repl-core` (04), `render` (05), `status-bar` (06), `cards` (07), `slash-registry` (08; records the service-layer location),
`slash-dispatch` (09), `control-cmds` (10), `bang-quick` (11), `oneshot` (12), `resume` (13),
`demo1` (14), `ledgers` (15).

## Run until green
```bash
cd indus-agent
uv sync --all-extras
uv run ruff check && uv run ruff format --check
uv run pyright src/indus/cli src/indus/service/commands
uv run pytest tests/unit/cli tests/unit/service/commands tests/integration/cli tests/e2e
uv run python -m tooling.checks --require-ref
uv run python -m tooling.checks acceptance traps
```
On any failure, fix the cause and rerun from `uv sync`. Never edit a recorded event stream to make
a render test pass; record a new one and say why in the note.

## Compare checklist (reopen the reference after writing)
- [ ] Startup order: profile → env → redaction/yolo bridges → logging → parse (R04.2, R04.13, R04.14)
- [ ] Every global flag and its inheritance; `-c` optional value cases (R04.21)
- [ ] Unknown-command three-line output and exit 2
- [ ] Interface choice and non-TTY fallbacks (R04.19)
- [ ] Frozen creation flags on resume (R04.1 rule kept in-process)
- [ ] Keys and Ctrl+C priority; busy modes (R05.5)
- [ ] Slash dispatch precedence and busy policy; registry is the single source
- [ ] Bang shell host-only, guarded, sanitised env, codes 124/130/127 (R04.27)
- [ ] Quick commands scrubbed and redacted (R04.28)
- [ ] Both exit-code tables and the hard exit (R04.20)
- [ ] Reference behaviours that only exist because chat is a gateway client (refused flags, reduced slash set, exit 3) are listed in the note as "arrives with P17" (I55)

## Traps covered
| Trap | Pinned by |
|---|---|
| R04.1 classic chat as gateway client (frozen-flag rule kept; client mode deferred to P17) | P06-03 |
| R04.2 profile before any import | P06-01 |
| R04.9 stale route on provider switch (`/model --global`) | P06-10 |
| R04.13 redaction bridge timing | P06-01 |
| R04.14 `--yolo` timing | P06-01 |
| R04.19 non-TTY behaviour (interface choice) | P06-03 |
| R04.20 two exit-code tables + hard exit | P06-12 |
| R04.21 optional-value `-c` routing | P06-02 |
| R04.27 bang shell not a bypass | P06-11 |
| R04.28 quick commands hold secrets | P06-11 |
| R05.4 seal partial reply on interrupt (classic surface) | P06-05 |
| R05.5 Ctrl+C priority | P06-04 |
| R05.11 approval quick picks are digits | P06-07 |
| R05.22 approval command redacted and wrapped | P06-07 |
| R01.3 / R01.4 (end to end) | P06-14 |

## Acceptance
| Feature | Planned acceptance ids |
|---|---|
| I1 `indus` command, bare = chat | ACC-I1-1 |
| I2 unknown-command UX | ACC-I2-1 |
| I3 global flags | ACC-I3-1 (`-p` anywhere), ACC-I3-2 (`-c [NAME]`) |
| I4 classic REPL | ACC-I4-1 (keys), ACC-I4-2 (status tiers) |
| I5 one-shot `-z` | ACC-I5-1 (stdout purity), ACC-I5-2 (exit codes) |
| I6 `chat -q` (P1 part) | ACC-I6-1 |
| I7 exit codes | ACC-I7-1 |
| I9 slash registry + precedence | ACC-I9-1 |
| I10 quick commands | ACC-I10-1 |
| I11 bang shell | ACC-I11-1 |
| I26 `/model` | ACC-I26-1 |
| I45 version output | ACC-I45-1 |
| I51 info commands | ACC-I51-1 |
| I55 in-process chat | ACC-I55-1 |
| B2 resume by id/title/latest | ACC-B2-1 |
| B3 per-terminal breadcrumb | ACC-B3-1 |
| B8 `/new`, `/retry`, `/undo` | ACC-B8-1 |
| B9 `/branch` | ACC-B9-1 |
| B23 resume recap | ACC-B23-1 |
| B26 `/queue` | ACC-B26-1 |
| B30 cwd restore, `--in` | ACC-B30-1 |
| E3 `/approvals` | ACC-E3-2 |
| E6 `/yolo` remembered per session | ACC-E6-2 |
| E18 isolation flags | ACC-E18-1 |
| A53 reasoning ladder (`/reasoning`, `--reasoning`) | ACC-A53-2 |

## Done when
- All suites pass; the acceptance and traps checks pass in non-strict mode with P04–P06 rows
  landed and every other P0/P1 row carrying a review line.
- **Demo 1**, live with a real API key: `indus` → "create hello.py and run it" → streamed answer →
  approval card → command runs locally → `/quit` → `indus -c` shows the recap and intact history.
  The same flow passes in CI with the fake model, including the kill-after-approval test.
- `indus -z "…"` exit codes match the table; a hardline command is refused under `-z`.
