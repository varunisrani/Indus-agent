# The Indus relay connector contract (version 1)

A relay connector is a service that holds chat-platform connections (bot tokens, webhooks) and
fronts them for one or more Indus gateways. The gateway dials **out** to the connector, so a gateway
without a public address can still answer on those platforms. This page is the contract a
connector author implements. It is Indus's own protocol (not compatible with other agents'
relays); the gateway side lives in `src/indus/gateway/relay/`.

## Configuration (gateway side)

```yaml
gateway:
  relay:
    url: https://relay.example.com     # the connector's base URL
    gateway_id: my-gateway              # this gateway's id at the connector
    connect_timeout_seconds: 30         # dial + handshake
    request_timeout_seconds: 30         # one operation's answer
```

The shared secret is stored, never written in config: `indus` reads `INDUS_RELAY_SECRET` from the
profile's secret store (or its environment).

## Transport

- One WebSocket per gateway, dialled to `wss://<host>/relay` (`https` → `wss`, `http` → `ws`).
- Every WebSocket text message is **one JSON object** with a `type` field. Unknown types are
  ignored by both sides; unknown fields are ignored.

## Handshake

1. The gateway's first message is a signed hello:

   ```json
   {"type": "hello", "v": 1, "gateway_id": "my-gateway", "ts": 1767258000,
    "nonce": "<32 hex characters>", "sig": "<hex>"}
   ```

   `sig` = lower-case hex of HMAC-SHA256, keyed with the shared secret, over the UTF-8 text
   `indus-relay-v1\n<gateway_id>\n<ts>\n<nonce>`. `ts` is Unix seconds.

2. The connector checks, in order: version 1, the signature (constant-time compare), `ts` within
   ±300 seconds of its own clock, and that the nonce was not seen inside that window. On any
   failure it sends `{"type": "refused", "reason": "auth"}` and closes. (Other reasons, e.g.
   `"busy"`, are allowed; the gateway retries those.)

3. On success it sends one `descriptor` per platform it fronts for this gateway, then `ready`:

   ```json
   {"type": "descriptor", "descriptor": {
     "contract_version": 1, "platform": "telegram", "label": "Telegram",
     "max_len": 4096, "len_unit": "utf16",
     "supports_draft": false, "supports_edit": true, "supports_threads": true,
     "markdown_dialect": "markdown_v2", "block_formatting": false,
     "ops": ["send", "edit", "typing", "follow_up", "prompt", "react"]}}
   {"type": "ready"}
   ```

### Descriptor fields

| Field | Meaning | If missing or invalid |
| --- | --- | --- |
| `contract_version` | must be `1` | descriptor refused |
| `platform` | lower-case name, `[a-z][a-z0-9_]{0,31}`; the gateway's platform name | refused |
| `label` | display name | the platform name |
| `max_len` | message length cap, in `len_unit` | 0, negative or not a number → 4096 |
| `len_unit` | `chars` (code points) or `utf16` (UTF-16 code units) | `utf16` |
| `supports_edit` | messages can be edited (also needs the `edit` op) | false |
| `supports_draft`, `supports_threads`, `block_formatting` | informational | false |
| `markdown_dialect` | `markdown_v2`, `mrkdwn`, `markdown`, `discord`, `plain`, … | `plain` |
| `ops` | operations implemented for this platform | `send`, `edit`, `typing`, `follow_up` |

A connector may send a new `descriptor` for a platform at any time; the gateway uses its `ops` from
then on.

## Inbound (connector → gateway)

```json
{"type": "event", "platform": "telegram", "event": {...}, "buffer_id": "b17"}
```

`event` fields: `chat_id` (required), `chat_type` (`dm`, `group`, `channel`, `thread`, `forum`),
`chat_name`, `user_id`, `user_name`, `user_id_alt`, `thread_id`, `parent_chat_id`, `scope_id`,
`guild_id`, `is_bot`, `message_id`, `text`, `reply_to`, `reply_to_text`, `reply_to_is_own`,
`mentioned`, `edited`, `button` (the callback data of a pressed button), `media`
(`[{ref, kind, mime, name}]`; `kind` one of `photo`, `voice`, `audio`, `video`, `document`,
`sticker`), `profile` (the gateway profile this chat is routed to, if the connector routes
profiles) and `ts` (Unix seconds).

The connector authenticates and admits senders: the gateway treats relay events as authorized
upstream. `buffer_id` is present on replayed events (see "Dormant"); the gateway answers
`{"type": "ack", "buffer_id": "b17"}` after it has taken the event.

`{"type": "interrupt", "platform": ..., "chat_id": ..., "session_key"?: ...}` asks the gateway to
stop the turn running for that chat.

## Operations (gateway → connector)

```json
{"type": "op", "id": "3-41", "platform": "telegram", "action": {"op": "send", ...}}
```

The connector answers each with `{"type": "result", "id": "3-41", "result": {...}}`.

| `op` | Action fields | Result on success |
| --- | --- | --- |
| `send` | `chat_id`, `text`, `reply_to?`, `meta` | `message_id` |
| `edit` | `chat_id`, `message_id`, `text`, `final`, `meta` | — |
| `typing` | `chat_id`, `meta` | — |
| `delete` | `chat_id`, `message_id`, `meta` | — |
| `follow_up` | `chat_id`, `kind`, `text`, `session_key`, `meta` | `message_id?` |
| `send_media` | `chat_id`, `media_kind`, `ref`, `filename`, `caption?`, `reply_to?`, `meta` | `message_id` |
| `prompt` | `chat_id`, `kind` (`approval`/`clarify`), `text`, `options[{id, label, style?, data}]`, `meta` | `message_id` |
| `react` | `chat_id`, `message_id`, `emoji`, `remove`, `meta` | — |
| `thread_create` | `chat_id`, `name`, `anchor?`, `meta` | `thread_id` |
| `thread_rename` | `chat_id`, `thread_id`, `name`, `meta` | — |
| `chat_info` | `chat_id` | `name`, `chat_type` |

The gateway sends an op only when the platform's descriptor lists it. Text is already within
`max_len` (the gateway splits long replies). `prompt` options carry `data` (at most 64 bytes):
render each as a button whose press comes back as an event with `button` set to that `data`.
`meta` carries `thread_id` and, when known for the chat, `profile`, `scope_id` and `user_id`
from earlier events: stamp the same `profile` on later events of that chat.

Result: `{"ok": true, ...}` or `{"ok": false, "code": ..., "error": "...", "retry_after"?: s}`
with `code` one of: `unavailable` (not done, safe to retry), `timeout` (may have been done),
`rate_limited` (+ `retry_after`), `chat_not_found`, `thread_not_found`, `message_not_found`,
`forbidden`, `declined` (this destination is not allowed for this gateway), `bad_format`,
`too_long`, `unsupported`.

The gateway waits `request_timeout_seconds` for a result; with no answer it assumes the op may
have been applied and never repeats it. When the gateway stops it gives operations already sent
up to 2 s to be answered before it closes the socket.

### Media

Files go by reference. The gateway first uploads the bytes:
`POST https://<host>/relay/media` with the file as the body, `content-type`,
`x-indus-relay-filename`, `x-indus-relay-kind`, and the signed tuple of the hello as headers
(`x-indus-relay-gateway`, `x-indus-relay-ts`, `x-indus-relay-nonce`, `x-indus-relay-sig`). The
answer `{"ref": "relay-media:<id>"}` is then used in `send_media`. At most 25 MB per file.
Inbound attachments named `relay-media:<id>` are fetched with `GET https://<host>/relay/media/<id>`
and the same headers.

## Reconnect

After a dropped socket the gateway dials again after 1 s, doubling the wait after every failed
attempt up to 60 s, and back to 1 s after a successful handshake. A `refused` with reason `auth`
is final: the gateway stops and reports the relay as disabled.

## Dormant

When its host goes idle the gateway sends `{"type": "idle"}`. The connector answers
`{"type": "idle_ack"}` once it has started holding new events for this gateway, and the gateway
closes the socket. Held events are replayed after the next successful handshake, in order, each
with a `buffer_id`; the connector drops an event from its buffer only when it is acknowledged, so
nothing is lost and nothing is delivered twice. Until the `idle_ack` arrives the gateway keeps
serving.
