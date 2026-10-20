# Task: WhatsApp Node bridge sidecar (P16-02) Phase: 16 Owner: wave-27 builder E13

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `scripts/whatsapp-bridge/bridge.js`
(endpoint list, the Host-header check, the queued event shape, `/health` fields and the pairing
event names only), `scripts/whatsapp-bridge/owner_message_gate.js` (whole file),
`scripts/whatsapp-bridge/outbound_ids.js` (whole file), `scripts/whatsapp-bridge/package.json`.
Evidence: none.

Vendor API: WhatsApp Web multi-device, reached only through our own `WaSocket` interface
(`src/provider.ts`). No WhatsApp Web library ships with the bridge and none is a dependency.

**Provider (lead decision, 2026-10-20):** the provider is operator-supplied through the WaSocket
interface; no provider ships; Baileys pending an owner decision (copyleft deps). No dependency is
added and nothing is fetched by `pnpm install`.

What it does (own words): a small Node program, run by the Python adapter (P16-03), that holds the
WhatsApp Web session and offers it to the adapter over a loopback HTTP API. At start it loads the
provider module the operator names with `INDUS_WA_PROVIDER` (an absolute module path or a package
name); the module must export `createWaSocket` (a `WaProvider`). Without a provider the bridge
fails closed: one `{"event":"error","error":"no_provider"}` line and exit 2 before binding anything
(an unloadable module or one without `createWaSocket`: `"provider"`, also exit 2); the adapter
reports that as a non-retryable `whatsapp_provider_missing`. Tests use a fake socket provider.

Inputs → outputs (all from env, never argv; the adapter builds the env):
- `INDUS_WA_TOKEN` (required, ≥ 32 characters): the shared secret every request must carry as
  `Authorization: Bearer <token>`; compared in constant time (SHA-256 of both sides, then
  `timingSafeEqual`, so lengths leak nothing).
- `INDUS_WA_HOST` (default `127.0.0.1`; only `127.0.0.1`, `::1` or `localhost` are accepted, any
  other value exits with code 2 before binding), `INDUS_WA_PORT` (1–65535, required).
- `INDUS_WA_AUTH_DIR`: auth state folder (created 0700; the process umask is 0077 so every file the
  library writes there is 0600).
- `INDUS_WA_PROFILE`: the profile id `/health` reports (the adapter refuses another profile's bridge).
- `INDUS_WA_PARENT_PID`: the bridge exits when that process is gone (checked every 500 ms; exit
  within 2 s), and also when its own parent pid changes.
- `INDUS_WA_SELF_CHAT` (`1`/`0`): pass the owner's messages in their own chat ("message yourself").
- `INDUS_WA_MODE`: `serve` (default) or `pair` (print pairing events, exit 0 once linked).
- `INDUS_WA_PAIR_PHONE`: in pair mode, ask for an 8-character pairing code for this number instead
  of a QR code.
- stdout: one JSON object per line for pairing/lifecycle events (`{"event":"qr","qr":…}`,
  `{"event":"pairing_code","code":…}`, `{"event":"connected"}`, `{"event":"logged_out"}`,
  `{"event":"listening","port":…}`, `{"event":"error","error":<code>}`); nothing else is printed,
  and no event carries message text or an unmasked number.

HTTP API (JSON in and out; every route needs the token; `Host` must name a loopback host, against
DNS rebinding; bodies over 64 KiB are 413 before parsing; unknown routes 404, wrong method 405):
- `GET /health` → `{status, profile, epoch, self: {jid, lid} | null, queued, version}`; `status` is
  `starting | connecting | open | pairing | logged_out | closed`. `self` ids are full JIDs (the
  adapter needs them for mention gating); only the token holder ever sees them.
- `GET /events?after=<cursor>&wait=<s>` → `{epoch, cursor, events: [...]}`: long-poll (default and
  maximum 25 s); events with a sequence number above `after`; a cursor from another epoch (another
  bridge run) starts from the oldest kept event. The ring keeps the last 1,000 events.
- `POST /send {chat, text, replyTo?}` → `{id}`.
- `POST /send-media {chat, path, kind, caption?, fileName?, mime?, replyTo?}` → `{id}`; `kind` is
  `image | video | audio | voice | document`; the path must be absolute, a regular file (not a
  symlink), at most 100 MiB and outside the auth folder (the bridge never sends its own keys).
- `POST /typing {chat, state: "composing"|"paused"}`, `POST /react {chat, id, emoji, fromMe?,
  participant?}`, `POST /read {chat, id, participant?}` → `{ok: true}`.
- `GET /media?ref=<ref>` → the bytes of one inbound attachment (`application/octet-stream`); refs
  are 24 hex characters the bridge handed out in an event (kept for the last 500 media messages,
  one hour); a declared size over 100 MiB is 413.
- Errors: `{error: <code>}` with 400 `bad_host` (Host not loopback), `bad_request`, 401
  `unauthorized`, 404 `not_found` (route), `chat_not_found`, `message_not_found` (also an expired
  media ref), 405 `method_not_allowed`, 403 `forbidden` (also a path inside the auth folder), 413
  `too_large`, 429 `rate_limited` (+ `Retry-After`), 503 `not_connected` (+ `Retry-After: 5` while
  reconnecting), 504 `timeout` (the library did not answer in 60 s: the message may be out, the
  adapter never resends), 502 `failed`.

Event shape (ours): `{seq, id, chat, sender, senderAlt, senderName, group, chatName, text,
timestamp, fromMe, mentions, mentionsMe, quoted: {id, sender, text, fromMe} | null, media:
[{ref, kind, mime, name, size}], edited}`.

Inbound rules (in `session.ts`):
- status broadcasts (`status@broadcast`), other `@broadcast` lists and `@newsletter` channels are
  dropped; protocol messages other than edits, reactions, receipts and empty messages are dropped;
- `fromMe` messages: our own sends (ids remembered in a bounded set of 512, first in first out) are
  echoes and dropped; anything else the owner typed is dropped unless self-chat is on and the chat is
  the owner's own chat (phone JID or LID); then it passes with `fromMe: true`;
- an edit carries the edited message's id and `edited: true`;
- device suffixes are stripped from every JID (`123:4@s.whatsapp.net` → `123@s.whatsapp.net`); the
  sender's other form (phone ↔ LID) is `senderAlt` when the key carries it;
- `mentionsMe` when a mentioned JID is one of our own ids; `quoted.fromMe` when the quoted
  participant is us.

Edge cases / failure modes:
- missing/short token, bad port, non-loopback host, no provider or an unreadable one: exit code 2
  with one `{"event":"error"}` line, before binding anything;
- a request without the token, with a wrong one, or with a non-loopback `Host` (DNS rebinding): 401 /
  400, nothing done;
- library closes (not logged out): reconnect after 1 s, doubling to 30 s; logged out: status
  `logged_out`, no reconnect (the adapter reports the fatal state); in `pair` mode a QR or code is
  printed, `connected` printed and exit 0 once linked;
- send while not connected: 503 `not_connected`, never queued;
- parent gone: stop the server, close the socket, exit 0 within 2 s; SIGTERM/SIGINT the same;
- a long-poll is answered early when an event arrives and at once when the server stops.

Our design differences / decisions:
- our env names (`INDUS_WA_*`), our routes: `/events` with a cursor instead of a drain-on-read
  queue (a response lost in transit loses nothing), media fetched lazily through `/media` instead of
  downloaded for every message (nothing is fetched for a message the gateway refuses; the Python
  side saves it through the intake's cache, 0600), `/send-media` takes a path with our own checks;
- every route needs the per-start token (the reference had none); a `Host` allow-list as well;
- no `/edit`, `/send-poll`, `/send-location`, `/chat/:id` (the adapter has no edit, approvals are
  text, P16-03 does not use them);
- no reply prefix, no allowlist logic in the bridge: access policy is the adapter's (P16-03), so the
  bridge forwards every message that is not an echo, a broadcast or the owner's.

Files: `apps/whatsapp-bridge/package.json`, `tsconfig.json`, `src/main.ts`, `src/http.ts`,
`src/session.ts`, `src/provider.ts` (the `WaSocket` seam and provider loading), `src/jid.ts`,
`src/testing/fakeSocket.ts` (the fake library socket, also usable as a provider module), tests
`src/http.test.ts`, `src/session.test.ts`, `src/main.test.ts`, `src/provider.test.ts`. The workspace
already lists `apps/*`, so `pnpm-workspace.yaml` needs no change; the lock file does not change (no
dependencies).

Tests we will write (vitest, fake socket, real loopback server on an ephemeral port):
- `http.test.ts`: no token / wrong token 401 (compare spied), wrong Host 400, oversize body 413,
  unknown route 404, send/send-media/typing/react/read forwarded, error mapping, long-poll wakes and
  times out, non-loopback host refused by `listen`;
- `session.test.ts`: own outbound ids filtered, owner gate (self-chat off/on), broadcasts dropped,
  device suffix stripped, mention/quote flags, edits, media refs fetched, reconnect/back-off,
  logged-out state, ring and cursor/epoch;
- `main.test.ts`: env validation (non-loopback exit 2), no provider → `no_provider` exit 2, parent
  death exits within 2 s (a real child process watching a short-lived parent);
- `provider.test.ts`: provider loading (empty name refused, path vs package name, a module without
  `createWaSocket` refused) and `WaError`.

## Decisions for the lead
- **Provider**: see the provider note at the top (lead decision); the bridge fails closed without
  one.
- Token: per start (fresh 32 random bytes each spawn), written 0600 next to the pidfile so a
  restarted adapter can adopt its own running bridge; this is the "per-install shared secret" the
  brief asks for, rotated on every start.
- Node: `.nvmrc` says 24.21.0; the code also runs on Node 22.18+ (type stripping) for this sandbox.

Compare checklist (filled after the rewrite; reopened `bridge.js` endpoint list, its
`messages.upsert` entry and Host check, `owner_message_gate.js`, `outbound_ids.js`):
- [x] endpoints the adapter needs exist (health, events ≙ `/messages`, send, send-media, typing,
  read) (test: `http.test.ts` "sends text, media, typing, reactions and read receipts")
- [x] own outbound ids filtered, bounded FIFO 512, re-remembering is a no-op (test:
  `session.test.ts` "OutboundIds", "drops our own outbound messages when they come back")
- [x] owner gate: owner-typed messages dropped unless self-chat in the owner's own chat (test:
  `session.test.ts` "drops what the owner types unless self-chat is on")
- [x] status / broadcast / newsletter never forwarded (test: `session.test.ts` "never forwards
  status updates")
- [x] loopback only, Host header allow-list (tests: `http.test.ts` "refuses a Host", "binds
  loopback only"; `main.test.ts` "exits 2 on a non-loopback host")
- [x] an echo that arrives before its send returned its id is held until the send returns (our
  own race, not in the reference) (test: `session.test.ts` "decides on an own message only after…")
- Deliberate differences: token on every route (the reference had none); cursor long-poll instead
  of a drain-on-read queue; media fetched lazily; profile id instead of the session path in
  `/health`; no script-hash staleness check (the bridge exits with its parent, so a bridge from an
  older install never outlives the gateway that started it); no DM/group policy in the bridge.
- Not taken: polls (`/send-poll`) and poll-update events, locations, `/edit`, `/chat/:id`, reply
  prefix, owner-message forwarding in bot mode, quoted-media cache, debug event stream, the
  read-receipt env flag (the adapter asks through `/read`), `npm install` from the adapter.

## Status

Done (2026-10-20), with the provider left to the operator (see "Decisions for the lead"); the
library mapping was removed at integration (lead decision) and provider tests rewritten. Tests:
vitest cases in 4 files; `tsc --noEmit` and biome clean; `CI=true pnpm test --maxWorkers=1`
green. Nothing changed outside `apps/whatsapp-bridge/` for this task.
