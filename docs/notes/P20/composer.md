# Task: Composer (P20-19) Phase: 20 Owner: wave-34 builder C

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/src/app/chat/composer/`
(file list and `index.tsx` skimmed: editor, completion drawer, attachments, status stack, restored-draft
notice), `apps/desktop/src/app/session/hooks/use-prompt-actions/` (submit ordering, prepared
submissions, upload helper — skimmed). Research 08 §3.13 (composer), R08.23. Closed before writing.
Evidence: none.

What it does (own words): the composer is where a person writes the next message. It is a text
editor with two completion triggers — `/` at the very start (slash commands, through the daemon's
`complete.slash`, curated by P20-24) and `@word` anywhere (paths, through `complete.path`) — and a
list the person moves through with the arrows and takes with Tab/Enter. Files can be dropped or
pasted: in a sandboxed renderer a dropped `File` has no path, so its bytes are uploaded through
main's REST proxy (`/api/files/upload`) and the agent gets the uploaded path; a file chosen in the
native picker has a path, which in local mode is checked by main's fs guard and passed as is, and in
remote mode is read as bytes and uploaded — the remote host never receives a client path (R08.23).
Images become `image.attach`; other files become an `@path` reference in the text. Up/Down in an
**empty** composer walks the person's earlier prompts; with text in it they move the caret as usual.
Above the editor a per-conversation status stack shows todos (from `todo.updated`), the queue
(P20-21) and slots for subagents and background jobs; a ridge hides and shows it. A model + effort
pill opens the picker (P20-23). While a turn runs the send button becomes Stop / Steer / Queue
(P20-21). A session that vanished (verified: `session.history` answers `SESSION_NOT_FOUND`) drops the
window to a fresh draft that adopts the unsent text once and shows an inline "restored" strip with
Undo — no toast, no navigation, no focus change, and a fresh draft that already has text is never
overwritten.

Inputs → outputs: keystrokes, drops, pastes, picker paths, session events → `prompt.submit` (via
`ChatActions`, P20-21), `image.attach`, REST upload, `complete.slash` / `complete.path`, slash
dispatch (P20-24).

Files: `apps/desktop/src/chat/composer/{Composer.tsx,completion.ts,attachments.ts,goneSession.ts}`,
`Composer.test.tsx`, `goneSession.test.ts`.

Names needed from slice R (stand-ins created, see the report):
- `src/gateway/connectionStore.ts`: `GatewayConnection { mode, codeVersion, request(method, params,
  options?), onSessionEvent(sessionId, listener) → unsubscribe, reconnect() }`, `REQUEST_TIMEOUTS`,
  `useGatewayConnection()`, `GatewayConnectionProvider`.
- `src/i18n/index.ts`: `useT()` → `t(key, vars?)`; `src/i18n/en.ts`: `en` (spreads our
  `CHAT_STRINGS`, `src/chat/strings.en.ts`).

Edge cases / failure modes:
- completion answers out of order: a generation counter drops every answer but the newest;
- daemon unreachable: slash completion falls back to the offline catalogue; path completion is empty;
- attachment over 256 MiB refused before any read; guard refusal shown inline on the chip;
- IME composition: Enter while composing never submits;
- history recall with text present: arrows are left to the editor;
- gone session while the fresh draft has text: the fresh draft is kept, the old text is offered
  only through the strip (copy), never pasted over;
- adopting twice (two "gone" signals for the same session): adopts once.

Our design differences / decisions:
- a plain `<textarea>` with chips instead of a contenteditable rich editor (directive chips and the
  pop-out floating composer are later);
- uploads for every pathless file (local mode too), so both modes share one byte path.

Tests we will write:
- remote mode stages bytes (upload called, no client path reaches `image.attach`); local mode with a
  picker path goes through the guard and passes the path;
- history recall only when empty; slash completion applied; stale completion dropped;
- gone session: adopts once, keeps a non-empty fresh draft, undo restores.

## Decisions for the lead
- Upload body through `indus:api`: `{ multipart: { path, overwrite: false, file: { name, type,
  base64 } } }` to `POST /api/files/upload`; slice M's proxy (P20-14) encodes it as multipart. Uploads
  land in `<session cwd>/.indus/attachments/<id>-<name>`. Change both in `attachments.ts`.
- Local path guard: `indusDesktop.fs.readText({ path, maxBytes: 1 })` succeeding means main's fs
  guard accepts the path (P20-29 may add a dedicated check channel; swap `guardLocalPath`).

Compare checklist (filled after the rewrite; reopened the files in `Read:`):
- [x] Remote mode ships bytes, never a client path (test: `Composer.test.tsx::remote mode uploads
  bytes: no client path reaches the remote host`); local picked path goes through the guard
  (`::local mode passes a picked path after main's guard, without an upload`); guard refusal keeps
  the text (`::a refused attachment keeps the message unsent`); 256 MiB cap before any read.
- [x] History recall only in an empty composer (test: `::history recall works only in an empty composer`).
- [x] Gone-session draft adopted once, never over a non-empty fresh draft, undoable, no focus change
  (tests: `goneSession.test.ts` (7), `Composer.test.tsx::a session that vanished hands its text…`).
- [x] Slash and `@` completion through daemon methods; stale answers dropped (tests:
  `::slash completion comes from the daemon, curated, and Tab takes it`, `::a slow answer for an
  older word never replaces the newer list`).
- [x] Stop / Esc while a turn runs target the exact generation (`::while a turn runs: …`).
- [x] Fresh draft: `session.create` with an idempotency key kept after an uncertain answer; the
  sticky model is applied to the new session.
- Deliberate differences: textarea + chips instead of the reference's rich editor; one upload path.
- Not taken: pop-out composer, directive chips with hover grace, voice button (later tasks).

## Status

Done (2026-10-21). Wiring into the chat route (mount `<Composer sessionId>` under the transcript) is
slice R's / the lead's.

Integration (wave-34 merge, D-16.682): mounted by `src/app/chatSlots.tsx` (navigation on create,
gone and `/new`; `/reasoning show|hide`; slash output above the composer); the person's message is
shown at once through the transcript's local rows (`localRows`, `src/chat/localEcho.ts`); the
connection is a `ChatConnection`; an unknown mode (no descriptor yet) counts as remote.
