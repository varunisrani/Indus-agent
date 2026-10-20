# P16 · More gateway platforms

**Weeks:** 3 (calendar weeks 28–30, beside P17; adapter files only) · **Tracks:** backend (Python), plus two small Node sidecars · **Depends on:** P15 (adapter contract, registry, fake platform and shared contract suite, chunker, formatters, router, intake) · **Status:** planned

## Goal
Every remaining chat adapter in the reference's catalogue runs on the P15 gateway core behind the
same adapter contract: WhatsApp (local bridge and Business Cloud), Signal, Matrix, Mattermost, Email,
SMS, Microsoft Teams and Graph webhooks, Google Chat, Feishu/Lark, DingTalk, WeCom (bot and
callback), Weixin, QQ Bot, Yuanbao, LINE, BlueBubbles, Photon, ntfy, IRC, SimpleX, Buzz and Raft. Each
adapter passes the shared fake-platform contract suite with its own wire fake, so no test talks to a
real service. The phase also finishes the richer parts of the first three adapters that P15 left
out (native draft streaming, Telegram DM topics and sticker descriptions, Discord voice channels),
per-platform setup helpers and cron target parsers. A2A and the relay connector are P24.

The work is embarrassingly parallel: each task owns only its adapter folder, its wire fake and its
tests. No task edits a P15 core module; if one needs a core change, the builder stops and the lead
lands it as a P15 follow-up first.

## Interop note
Every adapter speaks a vendor protocol. Method, event, field and header names are interop facts:
check each against the vendor's own documentation or schema at the pinned version; read the
reference only for behaviour and edge cases. Pins follow D-16.52 (exact, newest release at least 14
days old at lock time; record the exact version and the vendor API version in the task note). Floors
below are the versions known to work at the reference pin.

| Platform | Library / API | Pin (floor) | Licence check |
|---|---|---|---|
| WhatsApp bridge | WhatsApp Web multi-device through an operator-supplied provider behind our own `WaSocket` interface, run in our own Node sidecar (no provider ships, D-16.531) | — | nothing shipped |
| WhatsApp Cloud | Meta Graph API, WhatsApp Cloud API messages + webhooks | Graph `v21.0` or newer stable; webhook `X-Hub-Signature-256` with the app secret | — |
| Signal | signal-cli daemon, HTTP mode: `GET /api/v1/check`, SSE `/api/v1/events`, JSON-RPC 2.0 `/api/v1/rpc` (`send`, `sendTyping`, `sendReaction`, `getAttachment`, `listContacts`, `getContact`) | signal-cli 0.13.x | run as a separate process only (GPL-3.0; never imported or bundled) |
| Matrix | `mautrix[encryption]` (client-server API, `m.thread`, Olm/Megolm) | 0.21.1, Linux-only extra | MPL-2.0 (dependency use only) |
| Mattermost | REST API v4 + WebSocket events | server ≥ 9 | — |
| Email | IMAP4rev1 (RFC 3501) + SMTP (RFC 5321) via stdlib `imaplib`/`smtplib` in the thread pool | — | — |
| SMS | Twilio Programmable Messaging REST `2010-04-01`, `X-Twilio-Signature` | — | — |
| Teams | Bot Framework REST v3 (activities, JWT from the Bot Connector OpenID metadata) via `microsoft-teams-apps` | 2.0.13.4 | MIT |
| Graph webhooks | Microsoft Graph v1.0 change notifications (`validationToken`, `clientState`) | — | — |
| Google Chat | Chat API v1 (HTTP endpoint events with Google-signed ID tokens) or Cloud Pub/Sub pull via `google-cloud-pubsub` | 2.39.2 | Apache-2.0 |
| Feishu/Lark | `lark-oapi` (WS long connection or event webhook) | 1.6.8 | MIT |
| DingTalk | `dingtalk-stream` (Stream Mode) + robot REST | 0.24.3 | MIT |
| WeCom | smart-robot WebSocket API; callback mode with WXBizMsgCrypt-style AES-256-CBC + SHA1 signature | — | — |
| Weixin | iLink Bot HTTP (`getupdates` long poll, `context_token`, AES-128-ECB media) | — | — |
| QQ Bot | QQ Bot API v2 (WebSocket gateway + REST, chunked media upload) | — | — |
| Yuanbao | Yuanbao bot WebSocket with an HMAC-signed token | — | — |
| LINE | Messaging API v2 (reply/push, `X-Line-Signature` HMAC-SHA256) | — | — |
| BlueBubbles | BlueBubbles server REST API v1 + webhooks | server ≥ 1.9 | — |
| Photon | Spectrum (`spectrum-ts`) in our own Node sidecar | newest stable ≥14 days old | check before use; block on a non-allow-listed licence |
| ntfy | HTTP JSON stream subscribe (`/<topic>/json`), POST publish | server ≥ 2.x | — |
| IRC | RFC 1459/2812 + IRCv3 `sasl`, `server-time`, `message-tags` over TLS | — | — |
| SimpleX | SimpleX Chat CLI WebSocket API (local daemon) | — | run as a separate process |
| Buzz | Nostr NIP-01 events, NIP-19 bech32 `npub` | — | — |
| Raft | loopback wake bridge (local HTTP) | — | — |

Drift check: each adapter adds its vendor method/route list to `tests/contract/gateway/test_vendor_names.py`
through its `vendor_names` declaration (P15-05); a library bump that renames one fails CI.

## Tasks (36)
| ID | Task | Read in reference | New files | Verify |
|---|---|---|---|---|
| P16-01 | **Inbound HTTP listener kit and identity helpers.** A small shared helper (inside the adapters package, not core) for adapters that receive vendor callbacks: one uvicorn server per adapter port, bind 127.0.0.1 by default (public bind only by explicit config), body-size cap enforced before parsing, constant-time signature helpers (HMAC-SHA256 hex/base64, SHA1 sorted-param signature), replay window helper, `/health` per listener, graceful stop within the 5 s adapter disconnect budget. Identity normalisers: WhatsApp JID/LID canonical form (strip device suffix, map LID aliases to one id; non-user suffixes `@g.us`, `@broadcast`, `@newsletter` never treated as users), Nostr `npub` → hex (bech32 decode with checksum), Signal UUID alt id. Plus an HTTP-callback wire fake base for the contract suite. | `gateway/platforms/whatsapp_common.py` [309: identity parts]; `gateway/authz_mixin.py` [758: bech32 helpers 103–160]; `gateway/profile_routing.py` [192: WhatsApp suffix constants] | `src/indus/gateway/adapters/_http.py`, `src/indus/gateway/adapters/_identity.py`, `tests/fakes/platform/http_wire.py`, `tests/unit/gateway/adapters/test_http.py`, `tests/unit/gateway/adapters/test_identity.py` | Oversized body rejected without reading it fully; bad signature 401 in constant time (timing not asserted, compare function spied); bech32 test vectors from NIP-19; JID with device suffix and its LID alias normalise to one id |
| P16-02 | **WhatsApp Node bridge (sidecar).** Our own small Node TypeScript program (Node version from `.nvmrc`) driving an operator-supplied WhatsApp Web provider (D-16.531): pairs a device (QR or pairing code printed as JSON events to stdout), persists auth state under `<home>/platforms/whatsapp/<profile>/auth` (0700), and exposes a loopback HTTP API to the Python adapter: `GET /health`, `GET /events` (long-poll, 25 s, returns normalised inbound messages with media saved under the profile cache), `POST /send`, `POST /send-media`, `POST /typing`, `POST /react`, `POST /read`. Bound to 127.0.0.1 only, port 3000 for the default profile and 3001–3999 for others (first free), a per-start random bearer token passed via env from the Python side. It keeps an outbound-id set so our own echoed messages are never re-ingested and drops messages from the account owner unless self-chat is enabled. Exits when its parent pid dies. All code ours; no reference JS read beyond the endpoint list. No WhatsApp library ships (D-16.531); without a provider the bridge fails closed. | `scripts/whatsapp-bridge/bridge.js` [1179: endpoint list and event shapes only]; `scripts/whatsapp-bridge/owner_message_gate.js` [56] | `apps/whatsapp-bridge/package.json`, `apps/whatsapp-bridge/src/main.ts`, `apps/whatsapp-bridge/src/http.ts`, `apps/whatsapp-bridge/src/session.ts`, `apps/whatsapp-bridge/src/http.test.ts`, `apps/whatsapp-bridge/src/session.test.ts` | vitest with a fake provider socket: own outbound ids filtered; request without the token 401; parent death exits within 2 s; non-loopback bind refused |
| P16-03 | **WhatsApp bridge adapter.** Python adapter that spawns and owns the sidecar (pidfile with pid + start time under the profile home; refuses to adopt a sidecar owned by another profile; restarts it with capped backoff 2→60 s), polls `/events`, sends through the HTTP API. Own DM/group policy (`open`, `allowlist`, `disabled`, `pairing`) with `owns_access_policy=true`; JID/LID normalised via P16-01; mention gating in groups; text-like documents ≤100 KiB inlined; voice notes to intake. No editing (`supports_edit=false`), so streaming falls back to segments. Setup step in P16-31 prints the pairing QR. | `plugins/platforms/whatsapp/adapter.py` [1272: connect, ownership, policy]; `whatsapp/bridge_ownership.py` [≈140] | `src/indus/gateway/adapters/whatsapp/__init__.py`, `src/indus/gateway/adapters/whatsapp/adapter.py`, `src/indus/gateway/adapters/whatsapp/sidecar.py`, `tests/fakes/platform/whatsapp_wire.py`, `tests/unit/gateway/adapters/whatsapp/test_adapter.py` | Shared contract suite against the fake bridge HTTP; a second profile cannot adopt the first profile's sidecar; sidecar crash restarts with backoff (fake clock); group message without mention ignored |
| P16-04 | **WhatsApp Business Cloud adapter.** Graph API sends (text, media by upload id, interactive buttons for approvals — max 3 reply buttons, else a list or text fallback), webhook receiver on port 8090 (configurable) via P16-01: body ≤3 MiB, verify `X-Hub-Signature-256` with the app secret, GET verification handshake with the verify token; `wamid` dedupe cache 5,000; interactive state cache 1,000; 24-hour customer-service window: outside it only template messages are allowed, so a send outside the window returns a typed `forbidden` with a clear reason instead of retrying. | `plugins/platforms/whatsapp_cloud/adapter.py` [1123: webhook, send, interactive] | `src/indus/gateway/adapters/whatsapp_cloud/__init__.py`, `src/indus/gateway/adapters/whatsapp_cloud/adapter.py`, `tests/fakes/platform/whatsapp_cloud_wire.py`, `tests/unit/gateway/adapters/whatsapp_cloud/test_adapter.py` | Contract suite; duplicate wamid ignored; bad signature 401; out-of-window send classified forbidden, not retried; 4 approval choices render as a list |
| P16-05 | **Signal adapter.** Talks to a user-run signal-cli daemon in HTTP mode: health `GET /api/v1/check` every 30 s, inbound SSE `/api/v1/events` with reconnect 2 s doubling to 60 s and a stale threshold of 120 s without events, outbound JSON-RPC (`send`, `sendTyping`, `sendReaction`, `getAttachment`, `listContacts`); cap 8,000; attachments ≤100 MiB and ≤32 per message; attachment type sniffed from magic bytes; server rate-limit replies retried once after the stated refill (default 4 s); UUID alt id honoured; groups keyed by group id. signal-cli (GPL-3.0) is never bundled, imported or linked; it runs only as a separate process the user installs (licence rule). | `plugins/platforms/signal/adapter.py` [983: RPC and SSE]; `signal/signal_rate_limit.py` [≈120] | `src/indus/gateway/adapters/signal/__init__.py`, `src/indus/gateway/adapters/signal/adapter.py`, `tests/fakes/platform/signal_wire.py`, `tests/unit/gateway/adapters/signal/test_adapter.py` | Contract suite; SSE drop reconnects on 2/4/8…60 s (fake clock); 33 attachments split across two sends; rate-limit reply retried exactly once |
| P16-06 | **Matrix adapter (plain).** mautrix client: login by access token, sync loop, ignore events older than 5 s before startup, rooms as groups (DMs detected by the `m.direct` account data), `m.thread` relations as threads, replies via `m.in_reply_to`, edits via `m.replace` (streaming supported), cap 15,000 chars and 45,000 bytes of content, HTML formatted body from markdown with a plain body fallback, typing notifications, reactions for processing state, room alias resolution for cron targets. | `plugins/platforms/matrix/adapter.py` [3276: sync, send, edit, thread]; `matrix/voice_mention.py` [skim] | `src/indus/gateway/adapters/matrix/__init__.py`, `src/indus/gateway/adapters/matrix/adapter.py`, `tests/fakes/platform/matrix_wire.py`, `tests/unit/gateway/adapters/matrix/test_adapter.py` | Contract suite; backlog event at startup ignored; thread reply keys match the thread; 46,000-byte reply split by bytes as well as chars |
| P16-07 | **Matrix end-to-end encryption.** Optional, Linux-only extra: Olm/Megolm through mautrix's crypto store under `<home>/platforms/matrix/crypto` (0700, SQLite), cross-signing bootstrap from a recovery key held in the `SecretStore`, automatic key sharing only with verified devices of allow-listed users; undecryptable events produce one "could not decrypt" notice per session and are never passed to the agent as empty text. Clean failure with an install hint when the extra is missing. | `plugins/platforms/matrix/adapter_crypto.py` [skim: store and trust rules] | `src/indus/gateway/adapters/matrix/crypto.py`, `tests/unit/gateway/adapters/matrix/test_crypto.py` | Fake crypto store: unverified device gets no keys; undecryptable event → one notice; missing extra → adapter `fatal` with hint, gateway keeps running |
| P16-08 | **Mattermost adapter.** REST v4 (`/api/v4/posts`, `/users/me`, `/channels`, files) + WebSocket events (`posted`, `typing`), personal access token; cap 4,000; threads via `root_id`; edits supported; channel allowlist from config; reconnect on WS close through the P15 watcher. | `plugins/platforms/mattermost/adapter.py` [746] | `src/indus/gateway/adapters/mattermost/__init__.py`, `src/indus/gateway/adapters/mattermost/adapter.py`, `tests/fakes/platform/mattermost_wire.py`, `tests/unit/gateway/adapters/mattermost/test_adapter.py` | Contract suite; thread replies keyed by root id; channel not in allowlist ignored |
| P16-09 | **Email adapter.** IMAP poll (IDLE when supported, else 60 s poll) and SMTP send (connect timeout 30 s), run in the bounded thread pool. One reply per message, body ≤50,000 chars (longer → attachment). Threading by `Message-ID`/`In-Reply-To`/`References`; session key per sender address. Unknown senders are ignored unless pairing is explicitly enabled (email's unknown-sender default is always `ignore`, R06.8). Pre-auth header fetch capped at 64 KiB and the From header at 2,048 chars; HTML bodies converted to text; auto-replies and mailing-list bulk mail (`Auto-Submitted`, `Precedence: bulk/list`) dropped to avoid loops. | `plugins/platforms/email/adapter.py` [1030] | `src/indus/gateway/adapters/email/__init__.py`, `src/indus/gateway/adapters/email/adapter.py`, `tests/fakes/platform/email_wire.py`, `tests/unit/gateway/adapters/email/test_adapter.py` | Contract suite (fake IMAP/SMTP servers in-process); auto-reply dropped; unknown sender ignored by default and paired when opted in; reply carries correct `In-Reply-To` |
| P16-10 | **SMS adapter (Twilio).** Webhook on port 8080 via P16-01, body ≤64 KiB, `X-Twilio-Signature` validated over the public URL + sorted params; REST send with the account SID/token; cap 1,600 (about 10 segments); markdown stripped with the P15 plain formatter; no edit, no typing; STOP/HELP keywords left to Twilio. | `plugins/platforms/sms/adapter.py` [359] | `src/indus/gateway/adapters/sms/__init__.py`, `src/indus/gateway/adapters/sms/adapter.py`, `tests/fakes/platform/sms_wire.py`, `tests/unit/gateway/adapters/sms/test_adapter.py` | Contract suite; signature over a URL with a query string; 1,601 chars → two messages; markdown removed |
| P16-11 | **Microsoft Teams adapter.** Bot Framework activity endpoint on port 3978 (body ≤1 MiB) validating the inbound JWT against the connector's OpenID metadata (keys cached 24 h, issuer and audience checked); replies via the conversation's `serviceUrl` (only HTTPS hosts on the Microsoft allow-list); cap 28,000; Adaptive Card for approvals with a text fallback; typing activity; conversation reference stored per route so proactive cron delivery works. The meeting-summary pipeline is not in 1.0 (record as a K12 gap). | `plugins/platforms/teams/adapter.py` [859] | `src/indus/gateway/adapters/teams/__init__.py`, `src/indus/gateway/adapters/teams/adapter.py`, `tests/fakes/platform/teams_wire.py`, `tests/unit/gateway/adapters/teams/test_adapter.py` | Contract suite; JWT with wrong audience 401; `serviceUrl` outside the allow-list refused (SSRF guard); proactive send uses the stored reference |
| P16-12 | **Microsoft Graph change-notification receiver.** A core-style HTTP ingress adapter: subscription creation and renewal before expiry (renew at 80 % of lifetime), validation handshake echoing `validationToken` as plain text within 10 s, `clientState` secret check on every notification, dedupe by notification id for 1 h; notifications become internal events delivered to a configured session or target (no chat replies; `dm_is_private=false`). | `gateway/platforms/msgraph_webhook.py` [359] | `src/indus/gateway/adapters/msgraph/__init__.py`, `src/indus/gateway/adapters/msgraph/adapter.py`, `tests/unit/gateway/adapters/msgraph/test_adapter.py` | Handshake echo; wrong clientState dropped; renewal timer fires at 80 % (fake clock) |
| P16-13 | **Google Chat adapter.** Two inbound modes: HTTP endpoint (verify the Google-signed ID token, certs cached 300 s, audience = project number or URL) or Pub/Sub pull subscription; send via Chat API with a service account; cap 4,000; cards v2 for approvals; threads by `thread.name`; attachments only from trusted Google hosts; when the gateway is draining, HTTP events get 503 so Google retries (parked-route semantics) rather than a lost message. | `plugins/platforms/google_chat/adapter.py` [1812: inbound modes, send]; `google_chat/cards.py` [skim] | `src/indus/gateway/adapters/google_chat/__init__.py`, `src/indus/gateway/adapters/google_chat/adapter.py`, `tests/fakes/platform/google_chat_wire.py`, `tests/unit/gateway/adapters/google_chat/test_adapter.py` | Contract suite in both modes; forged token 401; draining → 503; attachment from an untrusted host not fetched |
| P16-14 | **Feishu/Lark adapter.** `lark-oapi` long-connection WS (default) or event webhook (encrypt key + verification token); tenant token refresh; connect attempts 3, send attempts 3; cap 8,000; rich text (`post`) and interactive cards for approvals; text batching per chat (0.6 s window, ≤8 messages, ≤4,000 chars) and media batching (0.8 s); mention gating in groups; `union_id` as alt user id; both Feishu (China) and Lark (international) domains. | `plugins/platforms/feishu/adapter.py` [4500: connect, inbound, send, batching] | `src/indus/gateway/adapters/feishu/__init__.py`, `src/indus/gateway/adapters/feishu/adapter.py`, `tests/fakes/platform/feishu_wire.py`, `tests/unit/gateway/adapters/feishu/test_adapter.py` | Contract suite; 9 rapid messages → two batches; webhook decrypt with the encrypt key; Lark domain selected by config |
| P16-15 | **Feishu document tools and comment intake.** Platform tools in the `indus-feishu` bundle only (C40): `feishu_doc_read` and the four `feishu_drive_*` tools using the adapter's credentials, output budgeted and spilled like any tool; document comment events mentioning the bot become inbound events keyed per document thread. Tools never appear in other bundles (R02.25 toolset membership test). | `plugins/platforms/feishu/feishu_comment.py`, `feishu_comment_rules.py` [skim]; tools registration in `feishu/__init__.py` | `src/indus/gateway/adapters/feishu/tools.py`, `src/indus/gateway/adapters/feishu/comments.py`, `tests/unit/gateway/adapters/feishu/test_tools.py` | Tool absent from `indus-cli` bundle; comment mention creates one event; doc read output budgeted |
| P16-16 | **DingTalk adapter.** Stream Mode client (`dingtalk-stream`) for inbound; replies through the per-conversation session webhook (cache ≤500, expiry honoured) else the robot REST API; cap 20,000; markdown messages; circuit breaker: 5 identical start failures trip a 300 s wait between attempts; repeated SDK log lines collapsed within 300 s. | `plugins/platforms/dingtalk/adapter.py` [866]; `dingtalk/inbound.py` [skim] | `src/indus/gateway/adapters/dingtalk/__init__.py`, `src/indus/gateway/adapters/dingtalk/adapter.py`, `tests/fakes/platform/dingtalk_wire.py`, `tests/unit/gateway/adapters/dingtalk/test_adapter.py` | Contract suite; expired session webhook falls back to REST; breaker trips on the 5th identical failure |
| P16-17 | **WeCom smart-robot adapter.** WebSocket API: connect timeout 20 s, request timeout 15 s, heartbeat 30 s; dedupe 1,000 recent message ids; cap 4,000; native streaming replies (stream-is-the-message: frames prefix-stable, final declared; R06.12) on by default; send queue per chat; media upload. | `plugins/platforms/wecom/adapter.py` [886]; `wecom/streaming.py`, `send_queue.py` [skim] | `src/indus/gateway/adapters/wecom/__init__.py`, `src/indus/gateway/adapters/wecom/adapter.py`, `src/indus/gateway/adapters/wecom/stream.py`, `tests/fakes/platform/wecom_wire.py`, `tests/unit/gateway/adapters/wecom/test_adapter.py` | Contract suite; every stream frame is a prefix of the next; missed heartbeat reconnects; duplicate id ignored |
| P16-18 | **WeCom callback adapter.** Second platform from the same folder: HTTP callback mode (P16-01) with the AES-256-CBC message crypto and SHA1 signature (URL verification echo, encrypted XML parsed with `defusedxml` only), replies through the corp API with an access token cache; cap 4,000. | `plugins/platforms/wecom/callback_adapter.py`, `wecom_crypto.py` [skim crypto steps] | `src/indus/gateway/adapters/wecom/callback.py`, `src/indus/gateway/adapters/wecom/crypto.py`, `tests/unit/gateway/adapters/wecom/test_callback.py` | Known-answer crypto vectors written from the vendor spec; XXE payload rejected; contract suite for the callback platform |
| P16-19 | **Weixin adapter (iLink Bot).** Long poll `getupdates`; echo the per-peer `context_token` on every reply (stored per route, refreshed by each inbound); AES-128-ECB media decrypt/encrypt per the iLink spec; dedupe TTL 300 s; cap 2,000; group messages require mention. | `plugins/platforms/weixin/adapter.py` [1434] | `src/indus/gateway/adapters/weixin/__init__.py`, `src/indus/gateway/adapters/weixin/adapter.py`, `tests/fakes/platform/weixin_wire.py`, `tests/unit/gateway/adapters/weixin/test_adapter.py` | Contract suite; reply without a stored context token refused with a clear error; media round trip with test vectors |
| P16-20 | **QQ Bot adapter.** API v2 WebSocket gateway (identify, heartbeat, resume) + REST send; cap 4,000; C2C, group and guild channels as distinct chat types; keyboards for approvals where allowed, else text; chunked media upload: parts uploaded with concurrency 1 default (max 10), 300 s per part, 2 retries per part, part-finish retry every 1 s up to 120 s; the daily upload limit error is not retried. | `plugins/platforms/qqbot/adapter.py` [1670]; `qqbot/chunked_upload.py` [skim] | `src/indus/gateway/adapters/qqbot/__init__.py`, `src/indus/gateway/adapters/qqbot/adapter.py`, `src/indus/gateway/adapters/qqbot/upload.py`, `tests/fakes/platform/qqbot_wire.py`, `tests/unit/gateway/adapters/qqbot/test_adapter.py` | Contract suite; resume after a WS drop keeps the sequence; daily-limit error not retried; part retry count exact |
| P16-21 | **Yuanbao adapter.** WebSocket with an HMAC-signed auth token (sign over app id + timestamp + nonce); connect 15 s, auth 10 s, heartbeat 30 s, 2 missed pongs → reconnect, ≤100 reconnect attempts before `fatal`; send timeout 30 s; reply-in-progress heartbeat every 2 s while a turn runs; media via the platform's upload flow. | `plugins/platforms/yuanbao/adapter.py` [2820: auth, heartbeat, send] | `src/indus/gateway/adapters/yuanbao/__init__.py`, `src/indus/gateway/adapters/yuanbao/adapter.py`, `tests/fakes/platform/yuanbao_wire.py`, `tests/unit/gateway/adapters/yuanbao/test_adapter.py` | Contract suite; two missed pongs reconnect (fake clock); 101st reconnect gives `fatal` |
| P16-22 | **Yuanbao platform tools.** `yuanbao_query_group_info`, `yuanbao_query_group_members`, `yuanbao_send_dm`, `yuanbao_search_sticker`, `yuanbao_send_sticker` (names spelled out per glossary) in the `indus-yuanbao` bundle only; `yuanbao_send_dm` restricted to users in the same group as the current chat and subject to approval (it messages a person, R02.26 spirit). | `plugins/platforms/yuanbao/yuanbao_sticker.py`, `register.py` [skim tool list] | `src/indus/gateway/adapters/yuanbao/tools.py`, `tests/unit/gateway/adapters/yuanbao/test_tools.py` | Tools only in their bundle; DM to a user outside the group refused; approval requested for `yuanbao_send_dm` |
| P16-23 | **LINE adapter.** Webhook on port 8646 via P16-01 (body ≤1 MiB, `X-Line-Signature` HMAC-SHA256 base64 over the raw body); reply with the reply token when younger than 50 s, else push; ≤5 messages per API call; bubble cap 5,000 with chunking at 4,500; media served to LINE through short-lived signed URLs (token TTL 1,800 s) from the gateway; a "still working" postback button offered after 45 s of a slow turn (0 disables). | `plugins/platforms/line/adapter.py` [998] | `src/indus/gateway/adapters/line/__init__.py`, `src/indus/gateway/adapters/line/adapter.py`, `tests/fakes/platform/line_wire.py`, `tests/unit/gateway/adapters/line/test_adapter.py` | Contract suite; reply token at 51 s falls back to push; 12 chunks sent in 3 calls; media URL expires after TTL |
| P16-24 | **BlueBubbles adapter (iMessage via a macOS server).** REST (`/api/v1/message/text`, attachments, typing) with the server password; webhook on port 8645 (≤1 MiB); cap 4,000; chat GUID resolution cache (LRU 500); tapback reactions for processing state where the server supports the private API. | `plugins/platforms/bluebubbles/adapter.py` [722] | `src/indus/gateway/adapters/bluebubbles/__init__.py`, `src/indus/gateway/adapters/bluebubbles/adapter.py`, `tests/fakes/platform/bluebubbles_wire.py`, `tests/unit/gateway/adapters/bluebubbles/test_adapter.py` | Contract suite; GUID cache eviction; webhook replay ignored |
| P16-25 | **Photon adapter and sidecar.** A small Node sidecar of ours (Spectrum client) on loopback port 8789 with a per-start token, exposing an at-least-once event stream and send endpoints; the Python adapter owns its lifecycle (pidfile, restart backoff, an `npm ci` self-heal bounded to 600 s) and dedupes events (≤4,000 ids, 48 h); cap 8,000; waits up to 15 s for a real attachment after an object-replacement placeholder; rich-link previews suppressed for 30 s after our own sends. If the Spectrum licence fails the allow-list, the task ships the adapter disabled with a D-id. | `plugins/platforms/photon/adapter.py` [1739]; `photon/sidecar/` [list files only] | `apps/photon-sidecar/package.json`, `apps/photon-sidecar/src/main.ts`, `apps/photon-sidecar/src/main.test.ts`, `src/indus/gateway/adapters/photon/__init__.py`, `src/indus/gateway/adapters/photon/adapter.py`, `tests/fakes/platform/photon_wire.py`, `tests/unit/gateway/adapters/photon/test_adapter.py` | Contract suite; replayed event ignored; placeholder then attachment within 15 s merged; sidecar token required |
| P16-26 | **ntfy adapter.** Subscribe with HTTP JSON streaming to configured topics (stream timeout 90 s, reconnect), publish with POST (title, tags, priority from metadata); cap 4,096; dedupe 1,000 ids for 300 s; access token or basic auth. Every topic reports chat type "dm" but is a broadcast surface: `dm_is_private=false`, so pairing codes and login links are never sent there (R06.24). | `plugins/platforms/ntfy/adapter.py` [380] | `src/indus/gateway/adapters/ntfy/__init__.py`, `src/indus/gateway/adapters/ntfy/adapter.py`, `tests/fakes/platform/ntfy_wire.py`, `tests/unit/gateway/adapters/ntfy/test_adapter.py` | Contract suite; unknown sender on ntfy never receives a pairing code; stream stall at 90 s reconnects |
| P16-27 | **IRC adapter.** Raw asyncio socket with TLS by default; registration, SASL PLAIN when configured, PING/PONG, nick collision (append `_`, retry up to 3); channels and private messages; line cap 450 bytes after the `PRIVMSG <target> :` prefix (split on UTF-8 boundaries), markdown stripped, at most 1 line per 0.5 s per target (flood protection); token-lock key = server + nick (the canonical scoped-lock example). | `plugins/platforms/irc/adapter.py` [617] | `src/indus/gateway/adapters/irc/__init__.py`, `src/indus/gateway/adapters/irc/adapter.py`, `tests/fakes/platform/irc_wire.py`, `tests/unit/gateway/adapters/irc/test_adapter.py` | Contract suite (fake IRC server); multibyte text split at byte boundaries; nick collision recovery; pacing with fake clock |
| P16-28 | **SimpleX adapter.** Connects to a user-run SimpleX Chat CLI over its local WebSocket API; retry 2 s doubling to 60 s; health check 30 s, stale after 300 s; cap 8,000; contact and group messages; files via the CLI's file commands; auto-accept of contact requests only when pairing allows. The SimpleX CLI is never bundled; it runs only as a separate user-installed process (licence rule). | `plugins/platforms/simplex/adapter.py` [718] | `src/indus/gateway/adapters/simplex/__init__.py`, `src/indus/gateway/adapters/simplex/adapter.py`, `tests/fakes/platform/simplex_wire.py`, `tests/unit/gateway/adapters/simplex/test_adapter.py` | Contract suite; contact request not auto-accepted by default |
| P16-29 | **Buzz adapter (Nostr community relay).** Poll the relay for kind-9 chat events every 4 s (configurable, floor 1 s), 50 events per poll, per-channel seen set capped at 500; DM discovery every 5 sweeps; member list cache 60 s; publish signed events with the bot key from the `SecretStore`; allowlist entries in `npub` form decoded to hex (P16-01). | `plugins/platforms/buzz/adapter.py` [2028]; `buzz/nostr_auth.py` [skim] | `src/indus/gateway/adapters/buzz/__init__.py`, `src/indus/gateway/adapters/buzz/adapter.py`, `tests/fakes/platform/buzz_wire.py`, `tests/unit/gateway/adapters/buzz/test_adapter.py` | Contract suite; npub allowlist entry authorizes the matching hex pubkey; non-chat kinds ignored; signature verified on inbound events |
| P16-30 | **Raft adapter (wake bridge).** Loopback-only HTTP endpoint (ephemeral port by default, written to a port file) that accepts wake notifications (body ≤16 KiB, scalar fields ≤120 chars, activity content ≤4,096), queues them (cap 500) and starts an internal turn with a fixed wake prompt of ours; the agent then uses an external CLI for I/O, so the adapter's `send` only records the reply. `dm_is_private=false` (R06.24). | `plugins/platforms/raft/adapter.py` [579] | `src/indus/gateway/adapters/raft/__init__.py`, `src/indus/gateway/adapters/raft/adapter.py`, `tests/unit/gateway/adapters/raft/test_adapter.py` | Non-loopback request refused; 501st queued wake drops the oldest with a log line; oversized field 413 |
| P16-31 | **Per-platform setup helpers.** `indus gateway setup [platform]` (K52): a registry-driven wizard step per adapter (P15 and P16 platforms) that asks for credentials (stored in the `SecretStore`), writes the non-secret config through the round-trip writer, shows the access mode that will apply and why (pair/ignore/decline; R06.8), and runs a connection check through the adapter's `check_fn`. Specials: WhatsApp pairing QR from the sidecar; Slack app manifest printed for copy; Telegram QR deep link to the bot. Non-TTY: prints the env and config keys to set and exits 2 (R04.19). Refuses to copy bot tokens into a cloned profile (R04.26). | `hermes_cli/gateway_setup_wizard.py` [skim]; per-platform `setup*.py` / `onboard*.py` [skim names only] | `src/indus/cli/wizards/gateway_setup.py`, `tests/unit/cli/wizards/test_gateway_setup.py` | Scripted prompt run per platform with the fake platform `check_fn`; non-TTY exit 2 with key list; secrets never written to config |
| P16-32 | **Cron and send target parsers per platform.** Each P16 adapter contributes `parse_target`/`validate_target` to the router (P15-20): Matrix `!room:server` and `#alias:server`, Email addresses, Signal phone numbers and `group.<id>`, Mattermost channel names, Teams stored conversation refs, Google Chat `spaces/<id>`, LINE user/group ids, IRC `#channel`, ntfy topics; friendly names via the channel directory. | `plugins/platforms/*/send_targets.py` [≈50–150 each: matrix, feishu, buzz, slack for shape] | `src/indus/gateway/adapters/targets.py`, `tests/unit/gateway/adapters/test_targets.py` | Table test per platform: valid, invalid, ambiguous; `matrix:#ops:example.org` resolves through the directory |
| P16-33 | **Native draft streaming (Telegram, Slack).** Stream-is-the-message mode for adapters that support native drafts: Telegram message drafts and Slack `chat.startStream`/`appendStream`/`stopStream` (standard markdown in streams, mrkdwn elsewhere). Frames prefix-stable (no per-tick fence closing, cursor or format conversion), final declared by the consumer with augmentations riding it, a final beside a sealed stream reconciled by editing (R06.12). Off unless `display.platforms.<p>.native_stream: true`. | `gateway/stream_consumer.py` [976: native stream branch]; `plugins/platforms/slack/adapter.py` [6950: stream calls only] | `src/indus/gateway/adapters/telegram/drafts.py`, `src/indus/gateway/adapters/slack/streams.py`, `tests/unit/gateway/adapters/test_native_streams.py` | Recorded frame sequences: each frame a prefix of the next; no duplicate final after seal; flood during a stream backs off |
| P16-34 | **Telegram DM topics and sticker descriptions.** DM topic mode (K46): with the bot's threaded mode on, each new DM topic is its own session key part, and a "new topic" command creates one; stickers described once by the vision tool and cached by unique file id (LRU 2,000, persisted JSON) so repeats cost nothing. | `plugins/platforms/telegram/adapter.py` [7326: topic and sticker parts only]; `gateway/sticker_cache.py` [skim] | `src/indus/gateway/adapters/telegram/topics.py`, `src/indus/gateway/adapters/telegram/stickers.py`, `tests/unit/gateway/adapters/telegram/test_topics.py` | Two DM topics → two sessions; same sticker twice → one vision call |
| P16-35 | **Discord voice channels.** `/voice join/leave/status` in a guild: join the caller's voice channel, receive audio per speaker, transcribe utterances (silence gap 0.8 s) into turns keyed on the voice channel, speak replies with TTS through a mixer; leaves after 300 s of no speech; requires the `discord.py[voice]` extra and ffmpeg (doctor check); mention-free inside the joined channel only for allowed users. | `plugins/platforms/discord/voice_mixer.py` [225]; `discord/adapter.py` [7408: voice handlers only]; `gateway/run_voice.py` [470: voice channel parts] | `src/indus/gateway/adapters/discord/voice.py`, `tests/unit/gateway/adapters/discord/test_voice.py` | Fake voice stream: two speakers → two turns; idle leave at 300 s (fake clock); missing ffmpeg → clear error, adapter keeps text working |
| P16-36 | **Adapter matrix gate and doctor checks.** CI job that runs the shared contract suite across every registered adapter and fails if any registered platform lacks a wire fake or a contract run (the M5 criterion); `indus doctor` gateway section: per enabled platform credential presence, sidecar/daemon reachability (WhatsApp bridge, signal-cli, SimpleX CLI, BlueBubbles server), port conflicts among webhook listeners (8080, 8090, 8644–8646, 3978), and the access mode summary. | `hermes_cli/doctor*.py` [skim the gateway checks] | `tests/contract/gateway/test_adapter_matrix.py`, `src/indus/cli/doctor/gateway_checks.py`, `tests/unit/cli/test_doctor_gateway.py` | Removing one wire fake fails the matrix test with the platform named; two adapters configured on one port reported |

## Files to create (136)
| Path | What it holds | Task |
|---|---|---|
| `src/indus/gateway/adapters/_http.py` | callback listener kit | P16-01 |
| `src/indus/gateway/adapters/_identity.py` | JID/LID, npub, alt-id normalisers | P16-01 |
| `tests/fakes/platform/http_wire.py` | HTTP-callback wire fake base | P16-01 |
| `tests/unit/gateway/adapters/test_http.py` | caps, signatures | P16-01 |
| `tests/unit/gateway/adapters/test_identity.py` | normaliser vectors | P16-01 |
| `apps/whatsapp-bridge/package.json` | `@indus/whatsapp-bridge` | P16-02 |
| `apps/whatsapp-bridge/src/main.ts` | sidecar entry | P16-02 |
| `apps/whatsapp-bridge/src/http.ts` | loopback HTTP API | P16-02 |
| `apps/whatsapp-bridge/src/session.ts` | WhatsApp Web session (through the provider seam) and event normalisation | P16-02 |
| `apps/whatsapp-bridge/src/http.test.ts` | API tests | P16-02 |
| `apps/whatsapp-bridge/src/session.test.ts` | echo filtering, owner gate | P16-02 |
| `src/indus/gateway/adapters/whatsapp/__init__.py` | registry entry | P16-03 |
| `src/indus/gateway/adapters/whatsapp/adapter.py` | bridge adapter | P16-03 |
| `src/indus/gateway/adapters/whatsapp/sidecar.py` | sidecar ownership | P16-03 |
| `tests/fakes/platform/whatsapp_wire.py` | fake bridge | P16-03 |
| `tests/unit/gateway/adapters/whatsapp/test_adapter.py` | adapter tests | P16-03 |
| `src/indus/gateway/adapters/whatsapp_cloud/__init__.py` | registry entry | P16-04 |
| `src/indus/gateway/adapters/whatsapp_cloud/adapter.py` | Cloud API adapter | P16-04 |
| `tests/fakes/platform/whatsapp_cloud_wire.py` | fake Graph + webhook | P16-04 |
| `tests/unit/gateway/adapters/whatsapp_cloud/test_adapter.py` | adapter tests | P16-04 |
| `src/indus/gateway/adapters/signal/__init__.py` | registry entry | P16-05 |
| `src/indus/gateway/adapters/signal/adapter.py` | Signal adapter | P16-05 |
| `tests/fakes/platform/signal_wire.py` | fake signal-cli | P16-05 |
| `tests/unit/gateway/adapters/signal/test_adapter.py` | adapter tests | P16-05 |
| `src/indus/gateway/adapters/matrix/__init__.py` | registry entry | P16-06 |
| `src/indus/gateway/adapters/matrix/adapter.py` | Matrix adapter | P16-06 |
| `tests/fakes/platform/matrix_wire.py` | fake homeserver surface | P16-06 |
| `tests/unit/gateway/adapters/matrix/test_adapter.py` | adapter tests | P16-06 |
| `src/indus/gateway/adapters/matrix/crypto.py` | E2EE store and trust rules | P16-07 |
| `tests/unit/gateway/adapters/matrix/test_crypto.py` | crypto behaviour | P16-07 |
| `src/indus/gateway/adapters/mattermost/__init__.py` | registry entry | P16-08 |
| `src/indus/gateway/adapters/mattermost/adapter.py` | Mattermost adapter | P16-08 |
| `tests/fakes/platform/mattermost_wire.py` | fake REST + WS | P16-08 |
| `tests/unit/gateway/adapters/mattermost/test_adapter.py` | adapter tests | P16-08 |
| `src/indus/gateway/adapters/email/__init__.py` | registry entry | P16-09 |
| `src/indus/gateway/adapters/email/adapter.py` | Email adapter | P16-09 |
| `tests/fakes/platform/email_wire.py` | in-process IMAP/SMTP fakes | P16-09 |
| `tests/unit/gateway/adapters/email/test_adapter.py` | adapter tests | P16-09 |
| `src/indus/gateway/adapters/sms/__init__.py` | registry entry | P16-10 |
| `src/indus/gateway/adapters/sms/adapter.py` | SMS adapter | P16-10 |
| `tests/fakes/platform/sms_wire.py` | fake Twilio | P16-10 |
| `tests/unit/gateway/adapters/sms/test_adapter.py` | adapter tests | P16-10 |
| `src/indus/gateway/adapters/teams/__init__.py` | registry entry | P16-11 |
| `src/indus/gateway/adapters/teams/adapter.py` | Teams adapter | P16-11 |
| `tests/fakes/platform/teams_wire.py` | fake Bot Connector | P16-11 |
| `tests/unit/gateway/adapters/teams/test_adapter.py` | adapter tests | P16-11 |
| `src/indus/gateway/adapters/msgraph/__init__.py` | registry entry | P16-12 |
| `src/indus/gateway/adapters/msgraph/adapter.py` | Graph notification receiver | P16-12 |
| `tests/unit/gateway/adapters/msgraph/test_adapter.py` | handshake, renewal | P16-12 |
| `src/indus/gateway/adapters/google_chat/__init__.py` | registry entry | P16-13 |
| `src/indus/gateway/adapters/google_chat/adapter.py` | Google Chat adapter | P16-13 |
| `tests/fakes/platform/google_chat_wire.py` | fake Chat API and Pub/Sub | P16-13 |
| `tests/unit/gateway/adapters/google_chat/test_adapter.py` | adapter tests | P16-13 |
| `src/indus/gateway/adapters/feishu/__init__.py` | registry entry | P16-14 |
| `src/indus/gateway/adapters/feishu/adapter.py` | Feishu/Lark adapter | P16-14 |
| `tests/fakes/platform/feishu_wire.py` | fake lark surface | P16-14 |
| `tests/unit/gateway/adapters/feishu/test_adapter.py` | adapter tests | P16-14 |
| `src/indus/gateway/adapters/feishu/tools.py` | Feishu document tools | P16-15 |
| `src/indus/gateway/adapters/feishu/comments.py` | comment intake | P16-15 |
| `tests/unit/gateway/adapters/feishu/test_tools.py` | tools and bundle membership | P16-15 |
| `src/indus/gateway/adapters/dingtalk/__init__.py` | registry entry | P16-16 |
| `src/indus/gateway/adapters/dingtalk/adapter.py` | DingTalk adapter | P16-16 |
| `tests/fakes/platform/dingtalk_wire.py` | fake stream + REST | P16-16 |
| `tests/unit/gateway/adapters/dingtalk/test_adapter.py` | adapter tests | P16-16 |
| `src/indus/gateway/adapters/wecom/__init__.py` | registry entries (two platforms) | P16-17 |
| `src/indus/gateway/adapters/wecom/adapter.py` | smart-robot adapter | P16-17 |
| `src/indus/gateway/adapters/wecom/stream.py` | native streaming replies | P16-17 |
| `tests/fakes/platform/wecom_wire.py` | fake WS | P16-17 |
| `tests/unit/gateway/adapters/wecom/test_adapter.py` | adapter tests | P16-17 |
| `src/indus/gateway/adapters/wecom/callback.py` | callback adapter | P16-18 |
| `src/indus/gateway/adapters/wecom/crypto.py` | message crypto | P16-18 |
| `tests/unit/gateway/adapters/wecom/test_callback.py` | crypto vectors, XXE | P16-18 |
| `src/indus/gateway/adapters/weixin/__init__.py` | registry entry | P16-19 |
| `src/indus/gateway/adapters/weixin/adapter.py` | Weixin adapter | P16-19 |
| `tests/fakes/platform/weixin_wire.py` | fake iLink | P16-19 |
| `tests/unit/gateway/adapters/weixin/test_adapter.py` | adapter tests | P16-19 |
| `src/indus/gateway/adapters/qqbot/__init__.py` | registry entry | P16-20 |
| `src/indus/gateway/adapters/qqbot/adapter.py` | QQ Bot adapter | P16-20 |
| `src/indus/gateway/adapters/qqbot/upload.py` | chunked media upload | P16-20 |
| `tests/fakes/platform/qqbot_wire.py` | fake gateway + REST | P16-20 |
| `tests/unit/gateway/adapters/qqbot/test_adapter.py` | adapter tests | P16-20 |
| `src/indus/gateway/adapters/yuanbao/__init__.py` | registry entry | P16-21 |
| `src/indus/gateway/adapters/yuanbao/adapter.py` | Yuanbao adapter | P16-21 |
| `tests/fakes/platform/yuanbao_wire.py` | fake WS | P16-21 |
| `tests/unit/gateway/adapters/yuanbao/test_adapter.py` | adapter tests | P16-21 |
| `src/indus/gateway/adapters/yuanbao/tools.py` | Yuanbao tools | P16-22 |
| `tests/unit/gateway/adapters/yuanbao/test_tools.py` | tool rules | P16-22 |
| `src/indus/gateway/adapters/line/__init__.py` | registry entry | P16-23 |
| `src/indus/gateway/adapters/line/adapter.py` | LINE adapter | P16-23 |
| `tests/fakes/platform/line_wire.py` | fake Messaging API | P16-23 |
| `tests/unit/gateway/adapters/line/test_adapter.py` | adapter tests | P16-23 |
| `src/indus/gateway/adapters/bluebubbles/__init__.py` | registry entry | P16-24 |
| `src/indus/gateway/adapters/bluebubbles/adapter.py` | BlueBubbles adapter | P16-24 |
| `tests/fakes/platform/bluebubbles_wire.py` | fake server | P16-24 |
| `tests/unit/gateway/adapters/bluebubbles/test_adapter.py` | adapter tests | P16-24 |
| `apps/photon-sidecar/package.json` | `@indus/photon-sidecar` | P16-25 |
| `apps/photon-sidecar/src/main.ts` | Spectrum sidecar | P16-25 |
| `apps/photon-sidecar/src/main.test.ts` | sidecar tests | P16-25 |
| `src/indus/gateway/adapters/photon/__init__.py` | registry entry | P16-25 |
| `src/indus/gateway/adapters/photon/adapter.py` | Photon adapter | P16-25 |
| `tests/fakes/platform/photon_wire.py` | fake sidecar | P16-25 |
| `tests/unit/gateway/adapters/photon/test_adapter.py` | adapter tests | P16-25 |
| `src/indus/gateway/adapters/ntfy/__init__.py` | registry entry | P16-26 |
| `src/indus/gateway/adapters/ntfy/adapter.py` | ntfy adapter | P16-26 |
| `tests/fakes/platform/ntfy_wire.py` | fake ntfy server | P16-26 |
| `tests/unit/gateway/adapters/ntfy/test_adapter.py` | adapter tests | P16-26 |
| `src/indus/gateway/adapters/irc/__init__.py` | registry entry | P16-27 |
| `src/indus/gateway/adapters/irc/adapter.py` | IRC adapter | P16-27 |
| `tests/fakes/platform/irc_wire.py` | fake IRC server | P16-27 |
| `tests/unit/gateway/adapters/irc/test_adapter.py` | adapter tests | P16-27 |
| `src/indus/gateway/adapters/simplex/__init__.py` | registry entry | P16-28 |
| `src/indus/gateway/adapters/simplex/adapter.py` | SimpleX adapter | P16-28 |
| `tests/fakes/platform/simplex_wire.py` | fake CLI WS | P16-28 |
| `tests/unit/gateway/adapters/simplex/test_adapter.py` | adapter tests | P16-28 |
| `src/indus/gateway/adapters/buzz/__init__.py` | registry entry | P16-29 |
| `src/indus/gateway/adapters/buzz/adapter.py` | Buzz adapter | P16-29 |
| `tests/fakes/platform/buzz_wire.py` | fake relay | P16-29 |
| `tests/unit/gateway/adapters/buzz/test_adapter.py` | adapter tests | P16-29 |
| `src/indus/gateway/adapters/raft/__init__.py` | registry entry | P16-30 |
| `src/indus/gateway/adapters/raft/adapter.py` | wake bridge | P16-30 |
| `tests/unit/gateway/adapters/raft/test_adapter.py` | adapter tests | P16-30 |
| `src/indus/cli/wizards/gateway_setup.py` | per-platform setup steps | P16-31 |
| `tests/unit/cli/wizards/test_gateway_setup.py` | wizard runs | P16-31 |
| `src/indus/gateway/adapters/targets.py` | per-platform target parsers | P16-32 |
| `tests/unit/gateway/adapters/test_targets.py` | parser tables | P16-32 |
| `src/indus/gateway/adapters/telegram/drafts.py` | Telegram native drafts | P16-33 |
| `src/indus/gateway/adapters/slack/streams.py` | Slack native streams | P16-33 |
| `tests/unit/gateway/adapters/test_native_streams.py` | prefix stability | P16-33 |
| `src/indus/gateway/adapters/telegram/topics.py` | DM topic mode | P16-34 |
| `src/indus/gateway/adapters/telegram/stickers.py` | sticker description cache | P16-34 |
| `tests/unit/gateway/adapters/telegram/test_topics.py` | topics and stickers | P16-34 |
| `src/indus/gateway/adapters/discord/voice.py` | voice channels | P16-35 |
| `tests/unit/gateway/adapters/discord/test_voice.py` | voice flow | P16-35 |
| `tests/contract/gateway/test_adapter_matrix.py` | every adapter has a wire fake and a contract run | P16-36 |
| `src/indus/cli/doctor/gateway_checks.py` | gateway doctor section | P16-36 |
| `tests/unit/cli/test_doctor_gateway.py` | doctor checks | P16-36 |

## Files to update (6)
| Path | Change | Task |
|---|---|---|
| `pyproject.toml` | one optional extra per platform family (`whatsapp`, `signal`, `matrix`, `email`, `sms`, `teams`, `google-chat`, `feishu`, `dingtalk`, `wecom`, `weixin`, `qqbot`, `yuanbao`, `line`, `bluebubbles`, `photon`, `ntfy`, `irc`, `simplex`, `buzz`, `discord-voice`) with exact pins | each adapter task (lead merges) |
| `pnpm-workspace.yaml` | add `apps/whatsapp-bridge`, `apps/photon-sidecar` | P16-02, P16-25 |
| `src/indus/gateway/adapters/telegram/adapter.py` | call into `drafts.py`, `topics.py`, `stickers.py` behind flags (P15-33 file; small hook points only) | P16-33, P16-34 |
| `src/indus/gateway/adapters/slack/adapter.py` | route native streams to `streams.py` (P15-35 file) | P16-33 |
| `src/indus/gateway/adapters/discord/adapter.py` | register `/voice` and hand voice events to `voice.py` (P15-34 file) | P16-35 |
| `src/indus/cli/commands/doctor.py` | include the gateway checks section (P07 file) | P16-36 |

## Fixtures and fakes (4)
| Path | What it holds | Task |
|---|---|---|
| `tests/fakes/platform/*_wire.py` | one wire fake per adapter (listed above) | P16-01…P16-30 |
| `tests/fixtures/gateway/crypto_vectors/` | WeCom callback, Weixin media and LINE signature known-answer vectors, derived by us from the vendor specs | P16-18, P16-19, P16-23 |
| `tests/fixtures/gateway/nip19_vectors.json` | bech32 `npub` vectors from the NIP-19 spec | P16-01 |
| `tests/fixtures/gateway/native_stream_frames/` | recorded frame sequences for prefix-stability tests (made with our fakes) | P16-33 |

## Task notes (36)
`docs/notes/P16/<slug>.md`, one per task: `http-kit` (01), `whatsapp-bridge-sidecar` (02),
`whatsapp-bridge` (03), `whatsapp-cloud` (04), `signal` (05), `matrix` (06), `matrix-e2ee` (07),
`mattermost` (08), `email` (09), `sms` (10), `teams` (11), `msgraph` (12), `google-chat` (13),
`feishu` (14), `feishu-tools` (15), `dingtalk` (16), `wecom` (17), `wecom-callback` (18),
`weixin` (19), `qqbot` (20), `yuanbao` (21), `yuanbao-tools` (22), `line` (23), `bluebubbles` (24),
`photon` (25), `ntfy` (26), `irc` (27), `simplex` (28), `buzz` (29), `raft` (30),
`gateway-setup` (31), `targets` (32), `native-streams` (33), `telegram-topics` (34),
`discord-voice` (35), `adapter-matrix` (36). Each adapter note records the exact pinned library or
API version, its capability flags (`dm_is_private`, `owns_access_policy`, `auth_upstream`,
`supports_edit`, `push_capable`; research 06 Q4) and its vendor method list.

## Run until green
```bash
cd indus-agent
uv sync --all-extras
uv run ruff check
uv run ruff format --check
uv run pytest tests/unit/gateway/adapters tests/contract/gateway tests/unit/cli -p no:randomly
pnpm install
pnpm --filter @indus/whatsapp-bridge test && pnpm --filter @indus/photon-sidecar test
pnpm --filter @indus/whatsapp-bridge typecheck && pnpm --filter @indus/photon-sidecar typecheck
uv run python -m tooling.checks --require-ref
```
Gated network lanes (owner accounts only): `INDUS_LIVE=1 uv run pytest -m network tests/unit/gateway/adapters -k <platform>`.
On any failure, fix the cause and rerun from `uv sync`. A failing contract case is never skipped for
one platform; the capability flag must say what the adapter cannot do instead.

## Compare checklist (reopen the reference after writing)
- [ ] Every adapter in the reference catalogue (research 06 §3.3) is built here, in P15, in P24 (A2A, relay), or recorded as a gap (Home Assistant is an external catalogue plugin: K12 gap with a D-id; Teams meeting pipeline: K12 gap).
- [ ] Per adapter: cap, length unit, edit/typing/buttons support, dedupe window, reconnect cadence and webhook body limit match research 06 or the note explains the difference.
- [ ] Per adapter: the authorization flags (`owns_access_policy`, `auth_upstream`, `dm_is_private`) match the reference's behaviour (research 06 Q4 answered in the note).
- [ ] Broadcast surfaces (ntfy, Raft, Graph) never receive pairing codes or login links.
- [ ] Every vendor method the reference adapter calls is used, or recorded as "not needed" with the reason.
- [ ] Sidecars bind loopback only and require a per-start token.

## Traps covered
| Trap | Pinned by |
|---|---|
| R06.3 long flood waits not slept inline (Signal, QQ, LINE, Yuanbao rate limits) | P16-05, P16-20, P16-23, P16-21 |
| R06.4 no blind retry after timeouts (all HTTP senders through P15-04) | P16-01 (kit), every adapter contract run |
| R06.8 email always ignores unknown senders | P16-09 |
| R06.11 byte-based caps (Matrix bytes, IRC 450 bytes) | P16-06, P16-27 |
| R06.12 prefix-stable native streams | P16-17, P16-33 |
| R06.16 token locks for unique credentials (IRC server+nick example) | P16-27, contract suite |
| R06.22 media path policy for every outbound attachment | contract suite media cases, P16-02 |
| R06.24 broadcast surfaces reporting "dm" | P16-26, P16-30, P16-12 |
| R02.25 platform tools only in their bundle | P16-15, P16-22 |
| R02.26 no general send tool on the model surface (`yuanbao_send_dm` scoped and approved) | P16-22 |
| R04.19 non-TTY setup behaviour | P16-31 |
| R04.26 cloned profiles never share bot tokens | P16-31 |
| R02.22 sidecar child environments from the env builder only | P16-03, P16-25 |

## Acceptance
| Feature | ACC ids (planned) |
|---|---|
| K8 Signal | ACC-K8-1, ACC-K8-2 |
| K9 WhatsApp bridge → Cloud API | ACC-K9-1 (bridge), ACC-K9-2 (Cloud) |
| K10 Matrix, Mattermost, Email, SMS | ACC-K10-1 … ACC-K10-4 |
| K11 DingTalk, Feishu/Lark, WeCom (+callback), Weixin, QQ Bot, Yuanbao (P3) | ACC-K11-1 … ACC-K11-7 |
| K12 BlueBubbles, Photon, Teams, Graph webhooks, LINE, Google Chat, ntfy, IRC, SimpleX, Buzz, Raft (P3; A2A in P24; Home Assistant gap) | ACC-K12-1 … ACC-K12-11 |
| K28 native drafts (P2 part) | ACC-K28-2 |
| K24 stickers (P2 part) | ACC-K24-3 |
| K45 Discord voice channels (P3) | ACC-K45-1 |
| K46 Telegram DM topics (P3) | ACC-K46-1 |
| K52 `indus gateway setup` per-platform helpers | ACC-K52-1, ACC-K52-2 |
| C40 platform tools (Feishu, Yuanbao) (P3) | ACC-C40-1 |

## Done when
- The adapter matrix test (P16-36) is green: every registered platform has a wire fake and passes
  the shared contract suite (the M5 criterion "every P16 adapter passes the shared fake-platform
  contract suite").
- `indus gateway setup <platform>` configures each P16 platform and its connection check passes
  against the fake; `indus doctor` reports per-platform readiness and port conflicts.
- Both sidecars build and test under pnpm; `tooling.checks --require-ref` is clean.
- Demo (no new milestone): one gateway running Telegram (P15), Signal and Matrix at once, each
  answering, with an approval over Matrix answered by `/approve`.
