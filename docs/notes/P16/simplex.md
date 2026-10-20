# Task: SimpleX adapter (P16-28) Phase: 16 Owner: wave-25 builder E10

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `plugins/platforms/simplex/adapter.py`
(whole file: settings, connect, WebSocket listener and backoff, health monitor, event handlers,
contact requests, chat items, file transfers, commands and correlation ids, sends, channel list),
`plugins/platforms/simplex/plugin.yaml`, `website/docs/user-guide/messaging/simplex.md`. Research
06 §3.3 (SimpleX row).
Evidence: none.

Vendor API: the SimpleX Chat CLI's WebSocket API (`simplex-chat -p 5225`, local daemon). Frames
out: `{"corrId": "<ours>", "cmd": "<command>"}`; frames in: `{"corrId"?, "resp": {"type": …}}`
(some builds put the response fields at the top level; both read). Commands we send: `/user`
(connect probe: the active profile), `/_send @<contactId> json [<composed>]`,
`/_send #<groupId> json [<composed>]` (composed = `{msgContent, quotedItemId?, fileSource?}`),
`/_accept <contactRequestId>`, `/freceive <fileId>`, `/contacts`, `/groups`. Events we read:
`newChatItems` (and the older singular `newChatItem`), `chatItemUpdated`, `contactRequest` /
`receivedContactRequest`, `rcvFileComplete`, `chatCmdError` / `chatError`. The CLI is never
bundled, imported or started by us: the user installs and runs it (licence rule, AGPL-3.0).
WebSocket client: httpx-ws 0.9.0 (MIT, already locked through `daytona`/`mattermost`) in a new
`simplex` extra, imported lazily; the contract suite runs over a fake opener without it.

Capability flags: `max_len` 8,000 code points, `splits_natively` true, `markup` markdown (SimpleX
renders a small markdown), `supports_edit` false, `supports_buttons` false, `supports_delete`
false, `owns_access_policy` false, `auth_upstream` false, `dm_is_private` true, `push_capable` true.

What it does (own words): the user runs the SimpleX Chat CLI as a daemon with its WebSocket port on
loopback. The adapter opens one WebSocket, checks the daemon answers `/user`, then reads events:
received messages in direct chats (chat id = the numeric contact id) and in allowed groups (chat id
`group:<groupId>`; groups are ignored unless listed in `group_allowed_chats` or `*`) become
events; our own messages, other item kinds and groups not allowed are dropped; edits of received
messages become edited events; quoted messages give `reply_to_id`. Replies go out as `/_send … json`
commands addressed by numeric id (never by display name), awaited by correlation id so each piece
gets the item id SimpleX assigned, and errors are classified. A contact request is accepted only
when `auto_accept: true` is set (default off: accepting opens a channel the operator never saw);
otherwise it is logged with the command to accept it. The socket is watched: a dropped socket is
reopened after 2 s, doubling to 60 s (+ up to 20 % jitter); every 30 s the health check looks at
the last frame, and after 300 s of silence it probes the daemon with `/user`, reconnecting when no
answer comes in 10 s. Files: inbound files are fetched lazily for admitted messages through the
media intake (`/freceive <fileId>`, then the path the daemon reports, read only inside the
configured `files_folder`); outbound files go as `fileSource` paths the daemon reads.

Inputs → outputs:
- `SIMPLEX_WS_URL` (secret store, then env; or `platforms.simplex.ws_url`; default
  `ws://127.0.0.1:5225`). Keys: `group_allowed_chats` (+ env `INDUS_SIMPLEX_GROUP_ALLOWED_CHATS`),
  `auto_accept` (false; env `SIMPLEX_AUTO_ACCEPT`), `allow_remote` (false), `files_folder` (env
  `SIMPLEX_FILES_FOLDER`).
- WebSocket frames → `InboundEvent` (contact id / `group:<id>`, member id as user in groups,
  `is_bot` from the profile's `peerType`, `mentioned` from `meta.userMention`, `reply_to_is_own`
  when the quote is ours).
- `send(chat, text)` → item ids.

Files: `src/indus/gateway/adapters/simplex/__init__.py`, `.../simplex/adapter.py`,
`tests/fakes/platform/simplex_wire.py`, `tests/unit/gateway/adapters/simplex/__init__.py`,
`tests/unit/gateway/adapters/simplex/test_adapter.py`, `tests/contract/gateway/simplex_case.py`;
shared: contract `conftest.py` import line, `pyproject.toml` extra `simplex`, the
`platforms.simplex` schema row + its test line, THIRD_PARTY_NOTICES (httpx-ws/wsproto rows name
`simplex`).

Edge cases / failure modes:
- non-loopback `ws_url` host refused at connect (fatal) unless `allow_remote: true`; `wss://` and
  `ws://` only.
- daemon down at connect → retryable connect error; httpx-ws missing → fatal `missing_dependency`
  with the install hint.
- socket write fails → nothing sent: transient, retried; no answer within 30 s → "maybe
  delivered", never retried; socket drop while waiting → maybe delivered.
- `chatCmdError` texts classified (`contactNotFound`/`groupNotFound` → chat not found, …).
- frames that are not JSON or not objects ignored; unknown event types ignored.
- contact request with auto-accept off → no command sent, one log line per request id.
- send-only (`indus send`): opens the socket and probes, never handles events or contact requests,
  no health monitor.
- file paths from the daemon outside `files_folder` (or none configured) → the fetch is refused.

Our design differences / decisions:
- Contact requests are not auto-accepted by default (the reference accepts every request).
- Loopback only unless `allow_remote: true` (the reference connects anywhere).
- Sends wait for the daemon's answer (ids, errors) instead of fire-and-forget.
- Stale socket: probed and reopened (the reference only logs idleness).
- No `MEDIA:` tag parsing in text (the core delivers media through the media methods), no image
  conversion or thumbnails (images go as files), no text batching (the core merges busy input).
- Group senders are keyed by the member id, DMs by the contact id (as the reference).

Tests we will write:
- contract suite through the fake WebSocket (lacks `thread`).
- contact request not auto-accepted by default; accepted with `auto_accept: true`.
- own messages, unknown items, groups not allowed ignored; edits; quotes; mentions.
- error classification from `chatCmdError`; read timeout maybe delivered; write failure transient.
- reconnect backoff 2/4/8…60 on the fake clock; stale probe reconnects.
- loopback rule, missing dependency, send-only does no inbound work.
- file fetch: `/freceive`, path inside `files_folder` only.

## Decisions for the lead
- `auto_accept` default false; "only when pairing allows" read as "only when the operator opts in"
  (the gateway's allowlist/pairing still gates every message from an accepted contact).
- Registry row: name `simplex`, label `SimpleX Chat`, probe `httpx_ws` (extra `simplex`),
  `required_env` `SIMPLEX_WS_URL`, max_len 8,000, `group_chats_env`
  `INDUS_SIMPLEX_GROUP_ALLOWED_CHATS`, install hint "pip install 'indus-agent[simplex]' and run
  `simplex-chat -p 5225`"; `BUILTIN_CREDENTIALS["simplex"] = ("SIMPLEX_WS_URL",)` (the URL is the
  only enabling value; it is not secret, so masking it is harmless).

Compare checklist (filled after the rewrite; reopened the files on the Read line):
- [x] retry 2 → 60 s with up to 20 % jitter (test:
  `test_adapter.py::test_a_dropped_socket_reopens_after_2_4_8_doubling_to_60_seconds`)
- [x] health check every 30 s, stale after 300 s — the reference only logs; ours probes and reopens
  a mute socket (test: `::test_a_silent_socket_is_probed_and_reopened_when_mute`)
- [x] correlation ids: answers resolved, our late answers never read as events (test:
  `::test_frames_that_are_not_events_are_ignored`)
- [x] contact requests (both event names) — not accepted by default, accepted with `auto_accept`
  (tests: `::test_a_contact_request_is_not_auto_accepted_by_default`,
  `::test_contact_requests_are_accepted_when_the_operator_opts_in`)
- [x] files: `/freceive` and `rcvFileComplete`; the reference starts every transfer at once, ours
  when the intake asks (test: `::test_files_are_fetched_lazily_from_the_files_folder`)
- [x] direct vs group chats, group allowlist with `*`, member ids as group senders, own items and
  non-message items skipped (test: `::test_direct_and_allowed_group_messages_become_events`)
- [x] `/_send` by numeric id with json items (test: `::test_sends_address_by_id_and_return_item_ids`)
- [x] channel list from `/contacts` and `/groups` (test: `::test_list_channels_names_contacts_and_allowed_groups`)
- [x] item time from `itemTs`, else `createdAt` (gap closed after the compare)
- [x] Verify column: contract suite; contact request not auto-accepted by default
- Deliberate differences: no auto-accept by default; loopback only unless `allow_remote`; sends
  wait for the daemon's answer (ids, errors) instead of fire-and-forget; stale sockets probed and
  reopened; DM targets are contact ids (the reference's channel list offers display names, which
  `/_send` cannot address); edits of received items become edited events; `peerType: bot` marks
  bots; `meta.userMention` marks mentions.
- Not taken: `MEDIA:` tags in reply text, image conversion and thumbnails (images go as files),
  text batching (the core merges busy input), the setup wizard (P16-31).

## Status

Done (2026-10-20). Files: the adapter package, the wire fake, its unit tests and contract case.
Shared: contract `conftest.py` import line, `pyproject.toml` extra `simplex`, THIRD_PARTY_NOTICES
(httpx-ws / wsproto rows name `simplex`), `platforms.simplex` schema row and its test line,
ACC-K12-9. Not done here (P15 core, for the lead): the registry row and `BUILTIN_CREDENTIALS`
entry above. The production opener was smoke-tested once by hand against a local WebSocket
server (uvicorn + wsproto, scratch script, not committed).
