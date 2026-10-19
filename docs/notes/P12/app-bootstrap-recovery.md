# Task: App composition, session bootstrap and crash recovery (P12-27) Phase: 12 Owner: wave-9 builder (app)

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `ui-tui/src/app/gatewayRecovery.ts`
(whole), `ui-tui/src/app/useSessionLifecycle.ts` (whole: create, resume, activate, busy guard,
active-session file), `ui-tui/src/app/useMainApp.ts` (the recovery refs and the event / request /
exit effect), `ui-tui/src/app/createGatewayEventHandler.ts` (the ready handler and the startup
prompt scheduler only), `ui-tui/src/canonicalGateway.ts` (`localCreationOptions` only). Research
`05-tui.md` §3.1, §3.6, §3.19, §3.24, §3.29, §3.30, R05.21. Ours: every `docs/notes/P12/*.md`
(wiring left for this task), the wave-9 lead notes, `src/indus/tui_bridge/methods_session.py`,
`methods_config.py`, `server_requests.py`, `stdio_server.py`. Evidence: none.

What it does (own words): the renderer's top level. `entry.tsx` builds the client (stdio child),
then an `AppRuntime` that owns every non-visual piece: the event handler, the turn controller, the
server-request cards, slash dispatch, submission and queue, completion, config sync, the session
lifecycle and the crash recovery. `App.tsx` lays the stores out on screen: transcript (settled
messages from `transcriptStore` plus the live turn), the open card or picker, the queued preview,
the status rule, the completion list and the composer (hidden while a card owns the keyboard), and
mounts the key router.

When the bridge says `gateway.ready` the runtime declares its capabilities (server requests), loads
the slash catalogue, then opens a session in this order: (1) a recovery target left by a crash is
resumed with status "recovering session" — the target is kept until a resume succeeds, even if the
child dies again meanwhile, and nothing typed before the crash is sent again; (2) else a startup
resume id from the launcher is resumed and then the startup prompt is sent; (3) else the setting
`display.tui_auto_resume_recent` (off by default) picks `session.most_recent`; otherwise status
"starting session" and `session.create` with the launch options from the environment and a fresh
idempotency key that is reused when the same create is retried after a lost reply (R05.21). The
startup prompt (one-shot query and/or image) is sent exactly once, literally: no slash routing, no
`!` shell, no `{!…}` expansion. Every session change writes the stored id to the launcher's
active-session file. Config sync starts after the first session.

When the child dies (or the link closes) the turn is reset, busy/compacting cleared, flow cards
settled with their refusals, and the session id parked as the recovery target. Restarts are
budgeted: 3 per sliding 60,000 ms. Inside the budget the runtime says "restarting and reopening
your chat" and starts a new child at once; the next ready resumes the same stored session. Over the
budget it prints one error with the exit code and the last stderr line (once per outage) and keeps
retrying in the background on a backoff (1 s doubling, 30 s cap); a later success still reopens the
same chat and never creates a new one.

The session switcher (J13) is a card: `+ new` row, then sessions live in this process, then stored
sessions from `session.list`; keys Up/Down, Enter, Ctrl+N, Ctrl+R, Ctrl+D, `d` `d`, Esc; refresh
every 1,500 ms keeps the selection on the same id; switching is refused while busy.

Inputs → outputs: `gateway.ready`, `transport.exit` / link close, launch env (`INDUS_TUI_RESUME`,
`INDUS_TUI_QUERY`, `INDUS_TUI_IMAGE`, `INDUS_MODEL`, `INDUS_TUI_PROVIDER`, `INDUS_CWD` /
`INDUS_TUI_CWD`, `INDUS_TUI_REASONING`, `INDUS_TUI_TOOLSETS`, `INDUS_TUI_SKILLS`,
`INDUS_TUI_MAX_TURNS`, `INDUS_TUI_TOOL_PROGRESS`, switches `INDUS_TUI_{YOLO,IGNORE_RULES,SAFE_MODE,
IGNORE_USER_CONFIG,CHECKPOINTS,PASS_SESSION_ID,ACCEPT_HOOKS}` (names aligned with the P12-32
launcher; the `INDUS_YOLO_MODE`-style general names are read too), `INDUS_TUI_ACTIVE_SESSION_FILE`,
`INDUS_TUI_BRIDGE_COMMAND` (program, optionally with arguments)) → `client.capabilities`,
`commands.catalog`, `session.resume|create|most_recent|list|close`, `config.get`, `prompt.submit`
(startup prompt only), `image.attach`, `config.set key=verbose`; stores (`uiStore`,
`transcriptStore`, `overlayStore`), transcript lines, the active-session file, `client.start()`
restarts.

Files: `apps/tui/src/app/{App.tsx,appRuntime.ts,sessionLifecycle.ts,recovery.ts,launchOptions.ts}`,
tests `app/{sessionLifecycle,recovery,launchOptions,appRuntime}.test.ts`, `app/App.test.tsx`,
`app/bridgeChild.test.ts` (real child), helper `app/testRuntime.ts`,
`apps/tui/src/pickers/{SessionSwitcher.tsx,sessionSwitcherState.ts,sessionSwitcherState.test.ts}`,
`apps/tui/src/entry.tsx` (BootView replaced; `bootView.tsx` deleted). Shared: `apps/tui/package.json`
(`build` script → `dist/entry.js`, lead request), `slash/localCommands.ts` + `slash/types.ts`
(`/sessions new` through a new optional `newLiveSession` hook).

Edge cases / failure modes:
- the child dies during the recovery resume → target kept, next ready tries the same id again.
- the child dies before any session existed → restart within budget, the next ready runs the normal
  bootstrap (create, reusing the pending idempotency key).
- a resume refused by the server (e.g. not found) → one error line, the target is kept, no new
  session is invented; the user can pick one with `/sessions` or `/new`.
- a create whose reply was lost (timeout or link closed) → the next create with the same options
  reuses the same key; a different create (other title) gets a new key.
- `transport.exit` arrives before the link close for the stdio child (exit code, last stderr kept
  for the message); the fake server only closes the link — both paths end in one handler, and a
  close of a replaced transport (our own restart) is ignored.
- queued prompts never sent stay queued and drain after the reopen; a prompt already handed to the
  bridge is never sent again (the queue drops an item when it is sent).
- startup prompt: sent once per process, never after a recovery; an image that fails to attach
  stops the prompt with one line.
- `max_turns`: an integer string becomes a number, anything else stays the trimmed word.
- skills: comma or newline separated, trimmed, duplicates dropped, order kept.
- exit paths (`/quit`, Ctrl+D, Ctrl+C on an empty idle composer, update 42) go through
  `Lifecycle.quit`, which unmounts Ink, resets modes and stops the child.

Our design differences / decisions:
- our protocol has one session id (no separate runtime vs stored id): the stored id is the
  `session_id` the bridge returns (it may change after compression; `session.info` keeps it fresh).
- `session.create` declares only `title`, `cwd`, `model`, `toolsets`, `idempotency_key` (unknown
  keys are refused by the bridge). The launch options builder computes the full set (provider,
  skills, max turns, flags) but only declared keys are sent; the others are named once in a system
  line instead of being dropped silently. `source: tui` is set by the bridge itself. Bridge follow-up.
- the idempotency key is our `idempotency_key` (the reference calls it a request id).
- capabilities are declared first on each ready (before any resume), so pending server requests are
  delivered to a client the bridge already knows can answer them.
- the non-visual wiring lives in `appRuntime.ts` (testable without Ink); `App.tsx` only renders.
- live sessions: the switcher keeps sessions this process attached as "live"; `/new` closes the one
  it leaves (`session.close`, the stored row stays), switching from the switcher keeps it live.
- `d` `d` on a stored row calls `session.delete`; our bridge has no such method yet, so the answer
  is "not available yet" (bridge follow-up).
- no WebSocket/attached-socket path (D-16.10): every link loss is a child loss.

Tests we will write:
- recovery: budget planner (window slides, limit 3); 4th loss in 60 s prints one error with code and
  stderr, background retry on backoff, 5th success still reopens the same id; restart line inside
  budget; replaced-transport closes ignored.
- lifecycle: bootstrap order (recovery target > startup resume > auto-resume-recent > create);
  lost create reply retried with the same key; startup prompt `/help` sent as text once; image
  attach before the prompt; active-session file written on each change; resume fills the transcript.
- launch options: env → params table (max turns int/word, skills dedupe, truthy words).
- runtime against the fake server: kill mid-turn → `session.resume` with the same id, no
  `prompt.submit` replayed, busy cleared, queue kept; switch refused while busy.
- switcher state: refresh keeps selection by id, `d` then other key cancels delete, rows order and
  subagent sources hidden, busy refusal.
- App render: transcript + composer + status visible, card hides the composer.
- real child: `indus tui-bridge --stdio` with a local fake OpenAI-compatible endpoint: prompt, kill
  the child mid-stream, same session reopened with one user row.

## Decisions for the lead
- `session.create` launch options beyond the declared keys are reported, not sent (see above).
- `session.delete` is not in the protocol; the switcher's `d d` reports it as unavailable.
- `/sessions new` starts a new session (keeping the current one live): a two-line change in
  `slash/localCommands.ts` (shared).
- capabilities before session bootstrap (reason above), contrary to the order in the wave notes.
- the hyperlink capability is not in `terminal/capabilities.ts`; the app turns OSC 8 links on for
  the terminal families that capabilities already recognises and off under a multiplexer.

Compare checklist (filled after the rewrite; reopened the five reference files above):
- [x] budget 3 per sliding 60 s, granted restarts recorded, older ones pruned (test:
  `app/recovery.test.ts::grants three restarts inside one sliding window`, `::forgets restarts older than the window`)
- [x] target kept across a death before ready, cleared only by a resume of that id (test:
  `recovery.test.ts::keeps the target across a death before ready, and drops it once resumed`)
- [x] over budget: one error with exit code and last stderr line per outage, retries go on, the same
  chat reopens (tests: `recovery.test.ts::4th loss in 60 s says the error once, ...`,
  `appRuntime.test.ts::4th death in 60 s prints the error once; a later success still resumes the same id`)
- [x] busy/compacting cleared and the turn reset on exit; nothing re-sent (test:
  `appRuntime.test.ts::child dies mid-turn: same stored id resumed, nothing re-sent, queue kept`,
  `bridgeChild.test.ts::kill mid-turn: ...` against the real child)
- [x] ready order: recovery target → startup resume (+ startup prompt) → auto-resume-recent → create
  (tests: `sessionLifecycle.test.ts::bootstrap order` group)
- [x] "recovering" status shown while the recovery resume runs (test: `::a recovery target wins over the launcher's resume id ...`)
- [x] startup prompt literal, once, image first, destination re-checked (tests: `::resumes the launcher's id, then sends the startup prompt literally`,
  `::never sends the startup prompt twice, nor after a recovery`, `::attaches the startup image before the prompt; ...`)
- [x] creation options: skills split on comma/newline and deduped, max turns number or word,
  truthy switches (test: `launchOptions.test.ts::maps every launcher variable to a creation option`)
- [x] idempotent create (test: `sessionLifecycle.test.ts::a lost create answer is retried with the same idempotency key`,
  `::a create cut by a child death reuses its key after the restart`)
- [x] active-session file `{session_id}` on every change (test: `::each change writes the stored id to the active-session file`)
- [x] busy guard on switching (tests: `::switching is refused while a turn runs`,
  `sessionSwitcherState.test.ts::refuses switching, new and closing the open session while busy`)
- [x] gap found: the tool-progress mode is validated against off/new/all/verbose — fixed in
  `toolProgressMode`, pinned by `launchOptions.test.ts::accepts only known tool-progress modes` and
  `sessionLifecycle.test.ts::applies a valid mode to the new session and reports an invalid one`
- [x] gap found: the view scrolls to the newest row after a resume — `scrollToBottom()` on every open.
- [x] gap found (ours): a queued prompt could drain into the dead or the new child when the turn reset
  fired before the session was parked — the stores now drop the session id in the same update that
  clears busy (pinned by the mid-turn death test above).
- Deliberate differences: the reference resumes only when a session exists; we own the child, so a
  child that died before any session is restarted too and the next ready runs the normal bootstrap.
  A refused launcher resume id falls back to a new session (the reference leaves none). Resuming a
  stored session from the switcher keeps the previous one live instead of closing it. Toolsets are
  trimmed and deduped. Only plain integers count as numeric max turns ("5.0" stays a word). An invalid
  tool-progress mode is reported instead of refusing the launch. Capabilities are declared before the
  session bootstrap.
- Not taken: provider setup panel before create (our bridge answers create with PROVIDER_UNAVAILABLE,
  shown as one error line), credential/config warnings on create (not in our protocol), in-flight
  turn hydration on resume and shared-daemon attach/detach bookkeeping (no daemon, D-16.10), client
  env hand-off (not declared by `session.create`), slash catalogue before a session (loaded on every
  ready instead).

## Status

Done (2026-10-19). Shared files listed above. P12-33 drives `node apps/tui/dist/entry.js` (built by
`pnpm --filter @indus/tui run build`) with `INDUS_HOME`, `INDUS_TUI_BRIDGE_COMMAND`, and the fake
model behind a custom OpenAI-compatible `model.base_url` (the pattern in `app/bridgeChild.test.ts`).
