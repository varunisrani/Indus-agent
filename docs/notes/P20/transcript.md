# Task: Transcript (P20-18) Phase: 20 Owner: wave-34 builder R

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`):
`apps/desktop/src/app/session/hooks/use-message-stream/index.ts` (imports, flush constants, the
event → transcript mapping and its file list), `.../use-message-stream/utils.ts` (constants only),
`apps/desktop/src/lib/error-surface.ts` (layer list and recovery plan shape),
`apps/desktop/e2e/core/README.md` (oracle invariants). Closed before writing.
Evidence: none.

What it does (own words): one transcript per session, built from two sources: the stored
conversation (`session.history`, persisted rows) and the live event stream of the session's
connection (`turn.started`, `message.delta`, `reasoning.delta`, `tool.*`, `message.complete`,
`turn.done`, notices, async completions). Rows the composer submits appear at once as local user
rows (`addLocalUser`) and are bound to their turn when `prompt.submit` answers. Deltas are appended
raw (the concatenation of deltas is exactly the streamed text); the final `message.complete` only
adds what is not on screen yet. A snapshot never wipes newer live content: it replaces the
persisted base and each live turn is matched against it (user text, then the reply); a live turn
the snapshot already holds is dropped, a newer one stays. A snapshot older than one already
applied is ignored (generation counter). So every persisted user/assistant message renders exactly
once and in order, whichever comes first: the reply completing or the hydrate resolving.

Rendering: streaming markdown (own block parser, React elements only, no raw HTML), code blocks
highlighted by Shiki, `$$…$$`/`$…$` math by KaTeX and ```mermaid fences by Mermaid, each loaded
lazily on first use (until loaded the plain text shows). Reasoning blocks show only when
`display.show_reasoning` is on (toggled live through the store). Tool rows show a structured
summary (verb + target), expand to arguments/output/images; red only when the tool failed
explicitly — a bare non-zero exit (exit 1 without an error message) is neutral. Text before a tool
call is an interim message. Background completions (learning review, heartbeat) are collapsed
disclosures. The user message of the turn in view is sticky. Long sessions render a window of the
newest items and page older ones in on scroll / "Show earlier"; a timeline rail (one marker per
prompt) jumps to a turn; each session's reading position is restored when it is shown again; when
the reader scrolls up, follow stops and a "Scroll to bottom" button resumes it. A failed turn ends
in an error card naming the layer (provider, endpoint, streaming, auth, daemon, local runtime,
disk) with that layer's actions. Links in model text never navigate: a click asks main's audited
opener (`external.open`), which needs a real user click.

Inputs → outputs: connection events + `session.history` → `SessionTranscript` (items) →
`<Transcript sessionId>`.

Files: `apps/desktop/src/chat/{Transcript,ToolRow,Timeline,Markdown,RichBlocks,ErrorCard}.tsx`,
`messageStream.ts`, `transcriptStore.ts`, `toolSummary.ts`, `failure.ts`, `markdown.ts`,
`messageStream.test.ts`, `Transcript.test.tsx`; oracle `apps/desktop/testing/oracle.ts` (shared
with P20-40); fixtures `apps/desktop/testing/fixtures/streams/*.json`.

Edge cases / failure modes:
- events repeated by a replay (seq ≤ applied) → ignored; a `session.replay_gap` → re-hydrate;
- `message.complete` text that differs from the streamed text → only the missing tail is added
  (or the last segment replaced when the stream lost characters inside it); never twice;
- events for a turn never seen starting (joined mid-turn) → an assistant item is opened for it;
- tool events after the turn ended → attached to that turn's item, never a new message;
- hydrate resolving before / after `message.complete` → same final transcript;
- 2,000 messages → only the window is in the DOM;
- unclosed code fence or math while streaming → shown as an open block, no flicker into text.

Our design differences / decisions:
- Pure reducer (`applyEvent`, `applyHistory`) + a small store that batches notifications every
  33 ms; React reads it through `useSyncExternalStore`.
- Our failure layers come from the turn's `ExitReason` and failure text (our protocol has no
  structured error surface yet); "daemon" and "local runtime" come from the connection state.
- Own markdown block parser (no markdown library); Shiki runs on its JavaScript regex engine
  (the CSP forbids `wasm-unsafe-eval`), Mermaid with `securityLevel: "strict"`.

Tests we will write: reducer over the stream fixtures (oracle: each persisted message once, in
order; delta concatenation equals the stream; reconnect splices; replayed requests); both orders of
"reply completes" vs "hydrate resolves"; tool outcome colours; failure layer table; component tests
(jsdom): window only for 2,000 messages, timeline jump, scroll-to-bottom, reasoning toggle, sticky
user row, link click goes through `external.open`.

## Contract for slice C

`src/chat/messageStream.ts` (types and pure functions)
```ts
export type ToolState = "running" | "ok" | "failed" | "neutral";
export interface TextPart { readonly type: "text"; readonly text: string }
export interface ReasoningPart { readonly type: "reasoning"; readonly text: string }
export interface ToolPart {
  readonly type: "tool"; readonly callId: string; readonly name: string;
  readonly args: Readonly<Record<string, unknown>> | null; readonly state: ToolState;
  readonly progress: string | null; readonly fraction: number | null;
  readonly result: string | null; readonly durationMs: number | null;
}
export type AssistantPart = TextPart | ReasoningPart | ToolPart;
export interface UserItem { readonly kind: "user"; readonly key: string; readonly text: string;
  readonly turnId: string | null; readonly localId: string | null; readonly persisted: boolean;
  readonly rowId: number | null }
export interface AssistantItem { readonly kind: "assistant"; readonly key: string;
  readonly turnId: string | null; readonly parts: readonly AssistantPart[];
  readonly streaming: boolean; readonly persisted: boolean; readonly interrupted: boolean }
export interface ErrorItem { readonly kind: "error"; readonly key: string; readonly turnId: string;
  readonly layer: FailureLayer; readonly message: string; readonly retryable: boolean;
  readonly resetsAt: string | null }
export interface AsyncItem { readonly kind: "async"; readonly key: string; readonly title: string;
  readonly lines: readonly string[] }
export interface NoticeItem { readonly kind: "notice"; readonly key: string;
  readonly level: "info" | "warning" | "error"; readonly text: string }
export type TranscriptItem = UserItem | AssistantItem | ErrorItem | AsyncItem | NoticeItem;
export interface SessionTranscript {
  readonly sessionId: string;
  readonly items: readonly TranscriptItem[];
  readonly running: boolean;          // a turn is in flight
  readonly turnId: string | null;     // the running (or last) turn
  readonly generation: number | null; // the running turn's generation (for interrupts)
  readonly hydrated: boolean;         // history loaded at least once
  readonly lastSeq: number;
}
```
`src/chat/failure.ts`: `type FailureLayer = "provider" | "endpoint" | "streaming" | "auth" |
"daemon" | "runtime" | "disk"`, `failureActions(layer, retryable): FailureAction[]` with
`type FailureAction = "retry" | "choose-model" | "open-settings" | "sign-in" | "reconnect" | "open-logs" | "new-session"`.

`src/chat/transcriptStore.ts`
```ts
export class TranscriptStore {
  constructor(connection: GatewayConnection, options?: { clock?: Clock; flushMs?: number });
  get(sessionId: string): SessionTranscript;
  subscribe(sessionId: string, listener: () => void): () => void;
  open(sessionId: string): Promise<void>;       // watch the session + hydrate
  close(sessionId: string): void;
  hydrate(sessionId: string): Promise<void>;    // session.history, generation-fenced
  addLocalUser(sessionId: string, text: string): string;               // → localId (optimistic row)
  bindLocalUser(sessionId: string, localId: string, turnId: string): void; // after prompt.submit
  dropLocalUser(sessionId: string, localId: string): void;             // submit refused
  readonly showReasoning: boolean;
  setShowReasoning(show: boolean): void;
  subscribeSettings(listener: () => void): () => void;
}
export function TranscriptStoreProvider(props: { store: TranscriptStore; children: ReactNode }): JSX.Element; // in src/chat/transcriptReact.tsx
export function useTranscriptStore(): TranscriptStore;
export function useTranscript(sessionId: string): SessionTranscript;
```
`src/chat/Transcript.tsx`: `<Transcript sessionId={id} onFailureAction={(action, item) => …} footer={<CardStack …/>} />`
(`footer` renders after the last item, inside the scroll area: the transcript-level host for C's cards).
`src/chat/ChatView.tsx`: `<ChatView sessionId={id | null} composer={<Composer …/>} cards={…} />` — the
lead drops C's `Composer` and `CardStack` into these slots at merge; `src/app/Shell.tsx` takes
`statusBar={<StatusBar/>}` the same way.

Strings: `src/i18n/en.ts` is one flat record; C appends its keys in its own block at the end of
the file (`// slice C`), and calls `const t = useT(); t("composer.placeholder")`.

## Decisions for the lead
- Reasoning blocks hidden by default (`display.show_reasoning` false) until the settings page
  (P20-32) reads the real value; `setShowReasoning` toggles live.

Compare checklist (filled after the rewrite; reopened the files in `Read:`):
- [x] Transcript-once oracle over every stream fixture (basic, tool turn, reconnect splice, hydrate races both orders, replay gap): each stored message once and in order, no transient duplicate or reorder, delta concatenation equals the stream (test: `src/chat/messageStream.test.ts::stream fixtures under the transcript-once oracle`; oracle `testing/oracle.ts`).
- [x] "Reply completes" vs "hydrate resolves" in both orders keep the latest reply (fixtures `hydrate-race-*.json`); an older snapshot is ignored (test: `::ignores a snapshot older than the one applied`).
- [x] Flush cadence 33 ms as the reference's delta flush.
- [x] Final reply only adds what is missing; a garbled closing message is replaced by the stored final (tests: `reconcileFinal`).
- [x] Failure layers per the row; card actions per layer (tests: `failure layers`, `Transcript.test.tsx::a failed turn ends in an error card…`).
- [x] Tool rows: structured summary, expandable output/images, red only for explicit failure, bare exit 1 neutral (tests: `tool rows`, `Transcript.test.tsx::colours tool rows…`).
- [x] Window of 60 items for a 2,000-message session, paging, timeline jump, scroll-to-bottom, restored reading position, live reasoning toggle, links through `external.open` (tests: `src/chat/Transcript.test.tsx`).
- [x] Shiki (JavaScript engine), KaTeX and Mermaid load lazily and render under the strict CSP from `file://` in Electron 44.5.1 (manual smoke run, no console errors).
- [x] Gap found: a replay gap left the broken live tail on screen beside the fresh snapshot — fixed: a snapshot after a gap keeps only unstored local rows (fixture `replay-gap.json`).
- Deliberate differences: the reference collapses duplicate finals with edit-distance heuristics; we match the closing message and the stored rows exactly (whitespace-insensitive). Billing failures fold into the provider layer (the row's list).
- Not taken: subagent/background-review/MOA/pet events, todo cleanup, generated-image echo removal (later phases).

## Status

Done (2026-10-21). New fixture folder `apps/desktop/testing/fixtures/streams/` (7 files) and the
shared oracle `apps/desktop/testing/oracle.ts` for P20-40. Slots for slice C: `ChatView` `composer`/`cards`,
`Transcript` `footer`/`onFailureAction` (ChatView supplies defaults: reconnect, open logs, settings, new chat).

Integration (wave 35, P20-40 findings): stored history is read over REST (`GET /api/sessions/{id}/messages` through main's proxy, `src/chat/restHistory.ts`) because the daemon's WebSocket does not serve `session.history` (D-16.718); a session opened before the connection is ready loads when it becomes ready; `applyHistory` no longer keeps a finished live reply the snapshot already holds when its user row was bound to another turn id (D-16.719, `messageStream.test.ts::a finished reply whose user row is bound elsewhere is not shown twice by the snapshot`).

## Wave 47 (builder a, 2026-10-22): tool rows rebuilt from history keep summary and Arguments

Status: done (fail-first tests below fail on the snapshot base and pass with the fix).

Read: no reference file and no Evidence (our own display projection).

Bug: after a window reload the transcript is rebuilt from stored rows, and tool rows lost their
verb target (path / command) and the expanded row's Arguments: `HistoryMessage` carried only the
tool's name and output, and both the desktop's REST flattening (`restHistory.ts`) and the daemon's
`display_messages` dropped the call's arguments.

Fix:
- Protocol: `HistoryMessage.arguments` (optional object, tool rows only), codegen regenerated
  (`packages/protocol/src/generated/`).
- `indus.tui_bridge.methods_session.display_messages` gives each tool row its call's arguments,
  parsed (stored as JSON text or an object; anything that is not an object is left out) and masked
  with `redact_arguments`, as `tool.started` masks them (B-REDACT-1).
- `GET /api/sessions/{id}/messages` (`web/routes/sessions_store.py`) masks each call's `arguments`
  the same way before sending them (JSON text kept; text that does not parse is masked whole).
- Desktop: `restHistory.displayRows` attaches the parsed arguments of the row's call;
  `messageStream.historyItems` puts them on the rebuilt `ToolPart`, so `toolSummary` and `ToolRow`
  show what the live row showed.

Tests: `tests/contract/tui_bridge/test_methods_session.py::test_display_projection_gives_tool_rows_their_arguments`,
`tests/contract/web/test_sessions_routes.py::test_tool_call_arguments_are_masked_for_display`,
`src/chat/restHistory.test.ts` "gives tool rows their call's arguments…",
`src/chat/messageStream.test.ts` "rows rebuilt from history carry the live rows' summary and arguments".

### Decisions for the lead
- W47-a-3 (D-16.1032): arguments travel on `HistoryMessage` (an additive protocol field) rather than being
  re-derived by each surface; empty arguments (`"{}"`) come through as `{}` (the row then shows no
  Arguments section, as live).
- W47-a-4 (D-16.1033): the REST messages route now masks tool-call arguments for every client (the web SPA
  included); before, it sent them as stored.
