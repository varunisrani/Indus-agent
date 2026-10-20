# Task: ntfy adapter (P16-26) Phase: 16 Owner: wave-24 builder E5

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `plugins/platforms/ntfy/adapter.py`
(whole file: stream loop, fatal statuses, dedupe, echo tag, publish), `plugins/platforms/ntfy/
plugin.yaml` (env names), `tests/gateway/test_ntfy_plugin.py` (test names only). Evidence: none.
Interop (ntfy server ≥ 2.x, its documented HTTP API): subscribe `GET /<topic>[,<topic>…]/json`
(newline-delimited JSON; events `open`, `keepalive`, `message`, `poll_request`; fields `id`, `time`,
`event`, `topic`, `message`, `title`, `tags`, `priority`; `since=<message id>` resumes after a
message); publish `POST /<topic>` with the message as the body and headers `X-Title`, `X-Tags`,
`X-Priority` (1–5), `X-Markdown: yes`; the answer is the stored message as JSON (`id`);
`Authorization: Bearer <access token>` or `Basic base64(user:pass)`; topic names
`[-_A-Za-z0-9]{1,64}`; default keepalive every 45 s; message bodies over 4,096 bytes become
attachments. No SDK: our own client over httpx.

What it does (own words): ntfy is a publish/subscribe push service. The adapter keeps one HTTP
streaming subscription to the configured topics and turns each `message` event into a DM-typed
`InboundEvent` whose chat id **and** user id are the topic: ntfy has no authenticated senders (the
title is whatever the publisher wrote), so nothing in a message decides who sent it. A topic is a
broadcast surface — anyone who knows its name can read it — so the adapter says
`dm_is_private=false` and the authorization chain never sends a pairing code or login link there
(R06.24). The stream reconnects after a drop or after 90 s without any line (keepalives included),
2 s doubling to 60 s, resuming with `since=<last id>` so nothing published during the gap is lost;
ids seen in the last 300 s (at most 1,000) are dropped as repeats. Our own replies carry a tag and
are ignored when they come back on a subscribed topic. Replies are POSTed to the chat's topic (or
the configured reply topic) with optional title, tags and priority from the send metadata; text
over 4,096 bytes is refused as too long (the sender splits before). A 401/403 at connect is a fatal
configuration problem; while running it reports the platform fatal (no retry loop on a dead
token); a 404 topic is fatal too.

Inputs → outputs:
- `create_adapter(AdapterConfig)`: `NTFY_TOPIC` (one or more, comma-separated; the credential that
  enables the platform), `NTFY_TOKEN` (optional; `user:pass` → Basic, else Bearer), `NTFY_SERVER_URL`
  (default `https://ntfy.sh`); `platforms.ntfy` keys `server`, `topics`, `reply_topic`, `markdown`
  (false), `stream_timeout_seconds` (90).
- `send(topic, text, meta={title, tags, priority})` → `SendResult` with ntfy's message id.
- stream lines → `InboundEvent(kind=text, chat_type="dm", chat_id=user_id=topic)`.

Files: `src/indus/gateway/adapters/ntfy/__init__.py`, `ntfy/adapter.py`,
`tests/fakes/platform/ntfy_wire.py`, `tests/unit/gateway/adapters/ntfy/test_adapter.py`,
`tests/contract/gateway/ntfy_case.py`.

Edge cases / failure modes:
- topic names outside `[-_A-Za-z0-9]{1,64}` refused on send and at build (no path tricks via a
  chat id such as `../x`).
- empty or whitespace message, a non-`message` event, a line that is not JSON → skipped.
- stream stall (no keepalive) → reconnect at 90 s; drop → 2/4/8…60 s; after a stream that lived
  ≥60 s the backoff starts again at 2 s.
- 429 → rate limited with the server's wait; 413 → too long; read timeout → maybe delivered.
- `send_only` (`indus send`) opens no subscription.
- the token never appears in errors, logs or reprs (masked by the adapter's scrub).

Our design differences / decisions:
- Our names (`NtfyAdapter`, `NtfyApi`); resuming with `since=` (the reference reconnects without it
  and loses messages published during the gap).
- Several topics in one subscription; `reply_topic` replaces the reply destination only for the
  subscribed topics (an explicit `indus send ntfy:<topic>` goes to that topic).
- Markdown: `markdown: true` sends standard Markdown with `X-Markdown: yes` (a markdown-markup
  variant of the adapter); plain text otherwise.
- Length cap in bytes (ntfy's limit is bytes); longer text is refused, never truncated.
- Attachments on inbound messages are not taken (text only); recorded as a follow-up.

Tests we will write:
- contract suite (`ntfy_case.py`, lacks group/thread/bot/edit/media).
- unknown sender on ntfy never receives a pairing code (real authorizer + our adapter).
- stream stall at 90 s reconnects (fake clock); drop backoff sequence; `since` resume.
- echo tag dropped, dedupe window, bad topic names, auth header forms, title/tags/priority.
- 401 at connect fatal; 401 while running reports fatal once.
- `indus send` opens no stream.

## Decisions for the lead
- Registry row: `_chat_entry("ntfy", "ntfy", sdk=("httpx",), extra_name="ntfy",
  required_env=("NTFY_TOPIC",), max_len=4096, ...)` with `dm_is_private=False` (replace on the
  entry); `BUILTIN_CREDENTIALS["ntfy"] = ("NTFY_TOPIC",)` (token lock on the topic list, so two
  gateways never answer one topic).
- Topic is the user id: allowlists (`INDUS_NTFY_ALLOWED_USERS`) list topic names.

Compare checklist (filled after the rewrite; reopened the files on the Read line):
- [x] every topic reports "dm" but `dm_is_private` is false; an unknown sender gets no pairing code
  (Verify column; test: `test_adapter.py::test_unknown_sender_on_ntfy_never_gets_a_pairing_code`)
- [x] stream stall at 90 s reconnects; keepalives keep it alive (Verify column; test:
  `::test_a_stalled_stream_reconnects_at_90_seconds`)
- [x] reconnect backoff, reset after a stable stream (test:
  `::test_drops_back_off_and_messages_from_the_gap_still_arrive`)
- [x] 401/403/404 stop the loop: fatal at connect, reported fatal later, no retry loop (test:
  `::test_a_refused_credential_is_fatal_at_connect_and_while_running`)
- [x] echo tag, dedupe window, empty message, non-message events; identity from the topic, never
  the title (test: `::test_echoes_repeats_and_odd_lines_are_dropped`)
- [x] Bearer token / `user:pass` Basic, tags, title, priority, markdown flag (test:
  `::test_publish_parameters_and_credentials`, `::test_create_adapter_reads_topics_server_and_markdown`)
- [x] publish topic: the reference has `publish_topic` (default the subscribe topic) and a
  per-send override; ours is `reply_topic` for the subscribed topics, the chat id otherwise (test:
  `::test_topic_names_are_checked_and_replies_can_go_to_a_reply_topic`)
- [x] out-of-gateway sends (the reference's standalone sender) = our send-only open (test:
  `::test_indus_send_opens_no_subscription`)
- Deliberate differences: reconnects resume with `since=<last id>` (the reference loses what was
  published during the gap); text over 4,096 bytes is refused as too long rather than truncated
  (the sender splits first); 403 is fatal like 401; topic names are validated; a credential is
  never sent to a plain-http server off this machine; backoff doubles 2→60 s instead of the
  reference's fixed ladder.
- Not taken: attachments on inbound ntfy messages (follow-up), the reference's per-send
  `publish_topic` metadata.

## Status

Done (2026-10-20). New files only (plus the shared contract changes listed in `line.md` and one
R06.24 trap row). No new dependency, no schema rows, no pyproject extra.
