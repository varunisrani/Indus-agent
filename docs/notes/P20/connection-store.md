# Task: Gateway connection store (P20-17) Phase: 20 Owner: wave-34 builder R

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/src/store/gateway.ts`
(constants, secondary liveness probe and re-probe, activation path), `apps/shared/src/json-rpc-gateway.ts`
(timeouts, replay hold and flush, replay epochs, cursor cap), `apps/desktop/src/api/canonical-protocol.ts`
(constants only), `apps/desktop/src/lib/gateway-liveness-policy.ts` (streak policy),
`apps/desktop/src/store/session-states.ts` (watchdog constants only). Closed before writing.
Evidence: none.

What it does (own words): the renderer keeps one live WebSocket per (connection, profile). Each
`GatewayConnection` asks main for a single-use ticket (`indusDesktop.connection.ticket`), dials the
daemon with `@indus/client`'s `WsTransport` (ticket only as the second subprotocol, never in a URL
or a log), and drives an `IndusClient` (channel, replay cursors, server requests). When the link
drops it redials on its own ladder: full jitter, base 300 ms, cap 15,000 ms; the ladder resets only
after a socket stayed open for 5,000 ms (a socket that flaps open/closed keeps backing off). After
300,000 ms of continuous failure the state carries `warning: true` (the shell shows a non-blocking
toast); the transcript stays on screen and the composer stays usable for drafting. After every
reconnect each watched session is subscribed again from its cursor (≤ 256 cursors, each call
10,000 ms): the daemon replays the missed events once (duplicates are dropped by the cursor) and
re-offers still-open server requests marked `replayed`. Server-request answers are first-wins by
id; a re-offered card that is already on screen stays one card; a "decline" is only sent to a
daemon that advertises counting declines (`request.decline`), otherwise the plain refusal.
Liveness: while a turn runs, one unanswered probe schedules one bounded re-probe (3,000 ms later);
only a second consecutive miss closes the socket (the normal ladder then rebuilds it); with no turn
running a missed probe closes at once. Turn watchdog: a session whose turn produced no event for
5 minutes is probed with `events.since` (15,000 ms); an answer with nothing new marks the session
stalled (never "finished"); no answer goes to the liveness path. Every async result carries the
connection generation it started under and is dropped when a newer transport exists.

Inputs → outputs:
- bridge `connection.get/ticket/onChanged` + `facts.profile` → `ConnectionState` snapshots.
- daemon frames → `onEvent` / `onSessionEvent` listeners (deduplicated), `ServerRequestStore` cards.
- `request(method, params)` → typed result, with per-method default timeouts (below).

Timeouts (`src/gateway/timing.ts`): default 120,000 ms; startup (dial → ready) 60,000 ms;
`prompt.submit` 1,800,000 ms; approval/server-request answer 330,000 ms (daemon window 300 s);
replay per session 10,000 ms; watchdog silence 300,000 ms, watchdog probe 15,000 ms; liveness
re-probe delay 3,000 ms, streak 2; reconnect base 300 / cap 15,000 / reset after 5,000 ms open;
warning after 300,000 ms of failure.

Files: `apps/desktop/src/gateway/{connectionStore,replay,serverRequests,timing,react}.ts(x)`,
`connectionStore.test.ts`, `serverRequests.test.ts`. Shared (minimal): `packages/client/src/backoff.ts`
(`DESKTOP_BACKOFF` profile), `packages/client/src/channel.ts` + `client.ts` (`answerTimeoutMs`
option so answers wait 330 s instead of the call default).

Edge cases / failure modes:
- ticket mint fails (main not ready, stale socket) → counts as a failed attempt, ladder continues;
- terminal close (4403) → phase `failed`, no redial until `start()` is called again;
- no `gateway.ready` within 60 s of dialling → transport closed, failed attempt;
- reconnect while a replay of the old connection is still in flight → its results are dropped;
- the same server request delivered live and again by replay → one card; answered twice → one
  frame on the wire; withdrawn (`request.settled` / `server_request.cancelled`) → card removed;
- connection descriptor changes (main re-homed the backend) → the socket is rebuilt at once
  (generation bump), cursors kept (the daemon decides with the epoch whether they still hold).

Our design differences / decisions:
- Built on `@indus/client` (`IndusClient`, `WsTransport`, `ReplayCursors`, `RpcChannel`); the
  store adds only the desktop policy (ticket per dial, ladder reset rule, liveness, watchdog).
- One class per connection, React reads it through `useSyncExternalStore` (no state library).
- The exponent cap of 32 in the row is reached by the 15,000 ms cap long before; `backoffDelay`
  caps its exponent at 30 (same result).

Tests we will write: fake server (in-memory `FakeServer`) through a fake bridge:
drop + reconnect resubscribes `since` the cursor and duplicates render once; replayed approval
answered once on the wire; open ≥ 5 s resets the ladder, a flapping socket does not; warning only
at 300 s (fake clock); liveness streak; watchdog marks stalled; stale replay result after a newer
connection is ignored; decline sent only with the capability.

## Contract for slice C

All names are final for this wave (R ships them; C may stand them in).

`src/gateway/connectionStore.ts`
```ts
export type ConnectionPhase = "idle" | "connecting" | "open" | "reconnecting" | "failed" | "closed";
export interface ConnectionState {
  readonly key: string;                     // profile name or "default"
  readonly profile: string | null;
  readonly phase: ConnectionPhase;
  readonly ready: boolean;                  // phase === "open" and gateway.ready seen
  readonly descriptor: IndusConnectionDescriptor | null;
  readonly capabilities: readonly string[]; // from gateway.ready
  readonly countsDeclines: boolean;         // capabilities include "request.decline"
  readonly attempt: number;                 // reconnect attempt (0 when healthy)
  readonly failingSince: number | null;     // ms timestamp of the first failure in a streak
  readonly warning: boolean;                // ≥ 300 s of continuous failure (toast)
  readonly lastError: string | null;
  readonly stalledSessions: readonly string[];
  readonly generation: number;              // bumps on every new transport
}
export class GatewayConnection {
  readonly key: string;
  readonly mode: "local" | "remote" | null;   // from the descriptor (getter)
  readonly codeVersion: string | null;        // from the descriptor (getter)
  reconnect(): void;                          // drop the socket and dial again now
  readonly profile: string | null;
  readonly serverRequests: ServerRequestStore;
  getState(): ConnectionState;
  subscribe(listener: () => void): () => void;           // state changes
  start(): void;                                         // idempotent
  close(): Promise<void>;
  request<M extends MethodName>(method: M, params: MethodTypes[M]["params"],
    options?: CallOptions): Promise<MethodTypes[M]["result"]>;   // per-method default timeouts
  onEvent(listener: (event: ClientEvent) => void): () => void;   // every event, deduplicated
  onSessionEvent(sessionId: string, listener: (event: ClientEvent) => void): () => void;
  watchSession(sessionId: string): Promise<void>;        // session.subscribe from cursor; kept across reconnects
  unwatchSession(sessionId: string): void;
  whenReady(timeoutMs?: number): Promise<void>;          // rejects on timeout / close
}
export class ConnectionStore {
  constructor(options?: ConnectionStoreOptions);         // bridge, clock, random, transport factory
  connection(profile?: string | null): GatewayConnection; // one per (connection, profile), created lazily
  primary(): GatewayConnection;                          // the window's profile (facts.profile)
  closeAll(): Promise<void>;
}
```
`src/gateway/react.tsx`
```ts
export function ConnectionStoreProvider(props: { store: ConnectionStore; children: ReactNode }): JSX.Element;
export function useConnectionStore(): ConnectionStore;
export function usePrimaryConnection(): GatewayConnection;
export function useConnectionState(connection: GatewayConnection): ConnectionState;
export function useServerRequests(store: ServerRequestStore, sessionId?: string): readonly PendingServerRequest[];
```
`src/gateway/serverRequests.ts`
```ts
export interface PendingServerRequest {
  readonly id: string;                      // request_id
  readonly kind: ServerRequestKind;         // "approval" | "clarify" | "sudo" | "secret"
  readonly sessionId: string;
  readonly request: ServerRequest;          // the typed payload (ApprovalRequest, ...)
  readonly replayed: boolean;               // re-offered after a reconnect
  readonly receivedAt: number;
}
export class ServerRequestStore {
  list(sessionId?: string): readonly PendingServerRequest[];   // arrival order
  get(id: string): PendingServerRequest | undefined;
  subscribe(listener: () => void): () => void;
  answer(id: string, answer: ServerRequestAnswer): boolean;     // first answer wins; false if already answered/unknown
  decline(id: string): boolean;             // "not shown here"; decline frame only if the daemon counts declines
  isAnswered(id: string): boolean;
}
```
`src/gateway/timing.ts`: `RPC_DEFAULT_TIMEOUT_MS`, `RPC_STARTUP_TIMEOUT_MS`, `PROMPT_SUBMIT_TIMEOUT_MS`,
`SERVER_REQUEST_ANSWER_TIMEOUT_MS`, `methodTimeoutMs(method)` and the rest listed above.

Transcript names for C are in `docs/notes/P20/transcript.md` ("Contract for slice C").

## Decisions for the lead
- "cap 256 per request" read as: at most 256 sessions keep a replay cursor (the client's
  `REPLAY_CURSOR_CAPACITY`), each resubscribe call bounded by 10,000 ms; the daemon sizes each
  replay itself (the protocol has no per-call event limit).
- Replay uses `session.subscribe` with the cursor (the daemon replays ahead of its answer and
  re-offers open requests); `events.since` is used by the turn watchdog's probe.
- Remote sockets: `WsTransport` (P17-20) accepted only the gateway family; it now also accepts
  `indus-dashboard-v1` + `indus-dashboard-ticket.<id>` (same "protocol, then at most one ticket
  of the same family" rule) and requires the server to echo the first one offered.
- `en.ts` stays one flat record; slice C's `CHAT_STRINGS` (`src/chat/strings.en.ts`) is spread into
  it at merge (one line); `t` is exported beside `useT`/`translate`.
- The store runs its own reconnect ladder (`IndusClient` with `reconnect: false`) because the
  client's ladder resets on ready, and the row asks for a reset only after 5 s open.

Compare checklist (filled after the rewrite; reopened the files in `Read:`):
- [x] Ticket per dial, offered exactly as main returns it; never in URL/state/log (test: `src/gateway/connectionStore.test.ts::dials with a fresh single-use ticket…`, `::offers exactly the subprotocols…`).
- [x] Drop + reconnect resubscribes from the cursor, duplicates passed on once (test: `::after a drop, resubscribes since the cursor…`); fixture `testing/fixtures/streams/reconnect-splice.json` through the reducer.
- [x] Full jitter 300 ms / 15 s cap bounds; ladder resets only after 5 s open, a flapping socket keeps backing off (tests: `::backs off with full jitter…`, `::resets the ladder only after…`).
- [x] Warning only after 300 s of continuous failure (test: `::raises the warning only after 300 s…`); reference `RECONNECT_ESCALATE_AFTER_MS` same value.
- [x] Liveness streak policy (probe 5 s, re-probe 3 s, streak 2; an error frame counts as alive) as the reference's liveness policy (tests: `::during a turn, one missed probe re-probes…`, `::closes at once on a missed probe when no turn runs`).
- [x] Watchdog 5 min / 15 s probe, marks stalled, never "finished" (test: `::marks a silent turn stalled…`); same values as the reference.
- [x] Timeouts: default 120 s, replay 10 s, connect 15 s (WsTransport), submit 30 min; answer 330 s (reference 300 s; ours follows the row's 330 s) (test: `::uses each method's own deadline`, `packages/client/src/channel.test.ts::sends answers with their own deadline…`).
- [x] Replayed approval: one card, one answer on the wire; withdrawn closes the card; decline only with `request.decline` (tests: `src/gateway/serverRequests.test.ts`, fixture `replayed-requests.json`).
- [x] Gap found: the reference forgets a cursor when the owner answers "session gone" — fixed: a `SESSION_NOT_FOUND` replay failure unwatches and forgets the cursor.
- [x] Generation fencing: a replay answer for an older socket changes nothing (test: `::drops a replay result that belongs to an older connection`).
- Deliberate differences: the reference parks live frames while a replay is in flight and flushes them after; our daemon (P17) sends the replay ahead of the subscribe answer on the same socket, so the cursor's seq order alone keeps it once and in order. Remote sockets use the `indus-dashboard-v1` family (WsTransport now accepts both families, shared change).
- Not taken: the reference's wake-reconnect coalescing hold-off (15 s) and secondary-socket budget; we probe on `online` / visibility instead of redialling.

## Status

Done (2026-10-21). Shared changes: `packages/client/src/{backoff,channel,client,ws-transport,index}.ts`
(+ tests). Slice M's contract applied: `connection.ticket` → `{wsUrl, protocols, expiresInMs}` offered as is;
`connection.get` rejecting with `boot-*` leaves the store `failed` until the boot overlay reports ready.
Aliases for slice C's stand-in names: `GatewayConnection.mode/codeVersion/reconnect()`,
`useGatewayConnection`, `useOpenServerRequests(sessionId?)`.

Integration (wave-34 merge, D-16.674, D-16.675): every new socket first sends `client.capabilities
{features: ["server_requests", "request.decline"], surface: "gui"}` (`CLIENT_FEATURES`), then the
replay — without it the daemon offers the window no cards; the main window starts its primary
connection at launch (`connection.get` starts the boot); a descriptor announced while the first dial
still waits for `connection.get` is not a change. Slice C's names live here and in `react.tsx`:
`ChatConnection` (the part of `GatewayConnection` the chat surfaces use), `REQUEST_TIMEOUTS`,
`RequestOptions`, `SessionEvent`, `ConnectionMode`; `useGatewayConnection()` (an explicit
`GatewayConnectionProvider` value, else the primary connection, else null),
`useOpenServerRequests(sessionId | null)` → `OpenServerRequest` (pending + `answer` bound to
`ServerRequestStore.answer`, `bindAnswers`), `ServerRequestStoreProvider`. `ConnectionBridge` is
now `Pick<IndusDesktopConnection, …>` from the generated bridge types (cast dropped).
