# Task: Signal adapter (P16-05) Phase: 16 Owner: wave-23 builder E3

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `plugins/platforms/signal/adapter.py`
(whole file: connect, SSE listener, health monitor, envelope handling, RPC, send, typing,
reactions, attachments), `plugins/platforms/signal/signal_rate_limit.py` (whole file),
`plugins/platforms/signal/plugin.yaml`, `plugins/platforms/signal/gateway_env.py`,
`website/docs/user-guide/messaging/signal.md`; test names only in
`tests/gateway/test_signal.py` and `tests/gateway/test_signal_rate_limit.py`. Research 06 §3.3
(Signal row), §3.5, §3.6.
Evidence: none.

Vendor API: signal-cli daemon in HTTP mode, signal-cli 0.13.x (the floor the plan names; we call
only commands that exist there). Routes: `GET /api/v1/check`, `GET /api/v1/events?account=…`
(server-sent events, one `receive` event per envelope), `POST /api/v1/rpc` (JSON-RPC 2.0).
RPC methods we call: `send`, `sendTyping`, `sendReaction`, `getAttachment`, `listContacts`.
No Python library: the adapter talks HTTP through `httpx` (a core dependency), so the platform has
no extra and nothing to pin. signal-cli is GPL-3.0: it is never imported, bundled or linked; the
user installs and runs it as a separate process.

Capability flags: `max_len` 8,000 UTF-16 units, `splits_natively` true, `supports_edit` false,
`supports_delete` false, `supports_buttons` false, `owns_access_policy` false, `auth_upstream`
false, `dm_is_private` true, `push_capable` true, `markup` plain, `inline_wait_cap` 60 s (base).

What it does (own words): the user links signal-cli to their phone number as a secondary device
and runs it as an HTTP daemon. The adapter checks the daemon is up (`/api/v1/check`), then holds an
event stream open (`/api/v1/events`) and turns each received envelope into an `InboundEvent`;
replies, typing and reactions go out as JSON-RPC calls. The stream is watched: when it drops the
adapter reconnects after 2 s, doubling up to 60 s (a stream that stayed up a while or delivered
something resets the wait); every 30 s a monitor looks at how long the stream has been quiet, and
after 120 s of silence asks `/api/v1/check` whether the daemon is still alive — a failed check
forces a fresh stream, a good one just resets the quiet timer.

Inbound rules:
- an envelope from our own account is dropped (no loops), except "Note to Self": a sync copy of a
  message we sent from the phone to our own number, or into a group, is handled as inbound unless
  its timestamp is one of our own recent sends (echo of an adapter reply; 512 timestamps, 5 min);
- stories are ignored (setting `ignore_stories`, default on); receipts, typing notices and other
  envelopes without text or attachments are ignored;
- an edit (`editMessage`) carries the new text, the id of the message it edits and the `edited`
  mark;
- DMs: chat id = the sender's phone number, or the sender's UUID when the sender hides the number;
  `user_id_alt` = the UUID when both exist (allowlists may name either);
- groups: chat id `group.<groupId>`, chat type `group`; groups are off unless listed in
  `platforms.signal.extra.group_allowed_chats` or `INDUS_SIGNAL_GROUP_ALLOWED_CHATS` (`group.<id>`
  entries or `*`); an unlisted group's messages are dropped before they become events;
- mentions: Signal puts U+FFFC where a mention sits; mentions of us set `metadata["mentioned"]` and
  are removed from the text (so `@bot /help` is the `/help` command), other mentions become
  `@<number or uuid>`;
- quotes become `reply_to_id` (the quoted message's timestamp), `reply_to_text`,
  `reply_to_author_id`, and `reply_to_is_own` when the quote points at one of our sends;
- attachments: each becomes an `Attachment` whose ref is `signal-attachment:<id>` (id
  percent-encoded so the intake never takes it for a path); declared size over 100 MiB is skipped
  with a log line; at most 32 kept. The kind follows the first attachment's content type (voice
  note → voice, audio → audio, image → photo, video → video, else document). Bytes are fetched
  only when the intake asks (`fetch_attachment`, registered with `register_fetcher`), through
  `getAttachment`; the first bytes are sniffed and a file whose bytes contradict its declared
  kind (an "image" that is not an image, "audio" that is HTML or an executable) is refused.

Outbound rules:
- text: split with the P15 chunker at 8,000 UTF-16 units (markers `(i/n)`); each piece is one
  `send`; the message id is the send's timestamp (Signal identifies messages by author +
  timestamp); a split payload that fails part way returns the delivered ids and the proven
  remainder (R06.4);
- reply quoting: when `reply_to` names an inbound message we still remember (author known), the
  first piece quotes it;
- recipient: a phone number is upgraded to the UUID signal-cli knows for it (from envelopes and
  one `listContacts` per 10 minutes at most); groups go by `groupId`;
- per-recipient results: a DM fails when its one result is not `SUCCESS` (unregistered → chat not
  found, identity/safety-number → forbidden, rate limit → rate limited with the stated wait,
  network → transient, captcha proof → unknown); a group send counts as sent when any member got
  it (the rest are logged), so a partly failed group send is never repeated to everyone;
- attachments: files up to 100 MiB, at most 32 per `send` (`send_files` batches 33 into 32 + 1);
  a rate-limit reply on an attachment batch is retried exactly once after the server's stated
  refill (default 4 s) when that wait fits the inline cap; a longer wait is returned as the typed
  flood failure for the delivery ledger (R06.3); text sends are never retried here (the P15 retry
  policy owns that);
- typing: `sendTyping` (and `stop: true` when typing ends); after three failures in a row the chat
  skips typing for 16 s, then 32 s, capped at 60 s; a success resets it;
- reactions: `send_reaction(chat, author, timestamp, emoji, remove=False)` for processing-state
  markers (no core caller yet).
- errors: JSON-RPC errors and transport errors become classified `SendResult`s; connect timeouts
  are transient, read timeouts are "maybe delivered" (never re-sent), rate limits carry the wait
  (`retryAfterSeconds` in the error data or "Retry after N seconds" in the text; default 4 s).

Inputs → outputs: `SIGNAL_HTTP_URL`, `SIGNAL_ACCOUNT` (secret store, then env, then
`platforms.signal.extra.http_url` / `account`), `platforms.signal.extra` (`ignore_stories`,
`group_allowed_chats`) → `SignalAdapter`; SSE envelopes → `InboundEvent`s; sends → `SendResult`.

Files: `src/indus/gateway/adapters/signal/__init__.py`, `src/indus/gateway/adapters/signal/adapter.py`,
`tests/fakes/platform/signal_wire.py`, `tests/unit/gateway/adapters/signal/__init__.py`,
`tests/unit/gateway/adapters/signal/test_adapter.py`, `tests/contract/gateway/signal_case.py`
(contract case). Shared: `tests/contract/gateway/conftest.py` (case import, `AdapterCase.lacks`),
`tests/contract/gateway/test_adapter_contract.py` (inbound check honours `lacks`).

Edge cases / failure modes:
- daemon down at connect → retryable connect error; missing URL or account → fatal (config);
- SSE drop, SSE never connects, SSE quiet for 120 s with a dead daemon → reconnect with backoff;
- invalid JSON in an event → logged at debug, stream continues;
- an event for another account on a multi-account daemon → ignored;
- `+` in the account must be percent-encoded in the events URL;
- envelope without a sender, contentless envelopes, our own echoes → dropped;
- attachment over 100 MiB declared size → skipped; getAttachment answer as a bare base64 string or
  as `{"data": …}`; empty or undecodable data → refused;
- typing failures never surface; disconnect during a send → classified failure.
- phone numbers are personal data: logs show them masked (`+155****4567`).

Our design differences / decisions:
- Formatting is plain text (markdown stripped by the P15 plain formatter); the reference converts
  markdown to Signal text-style ranges. Native styles are a follow-up (they need range offsets kept
  through the chunker).
- Message id = the send's timestamp (the reference returns no id); the base already refuses edits.
- No process-wide attachment token-bucket model: the server's reply is the authority, and a rate
  limit gets one retry after its stated refill, or goes to the ledger (R06.3).
- Inbound attachments are fetched lazily by the intake (only for admitted messages) instead of
  being downloaded and cached for every envelope, so an unauthorized sender cannot make us
  download 100 MiB. No AAC→M4A remux (that needs ffmpeg in a subprocess; the STT layer owns
  format conversion).
- Group mention gating follows the core default (`require_mention` true unless
  `platforms.signal.require_mention: false`); the reference answers every message in an allowed
  group by default.
- Chat info uses the contact list; `getContact` is not called.
- No setup wizard, cron target parser or doctor row here (P16-31, P16-32, P16-36).

Tests we will write:
- contract suite over the fake daemon (`signal_case.py`);
- SSE drop reconnects after 2/4/8/16/32/60/60 s on the fake clock; a long-lived stream resets it;
- stale stream: dead daemon → reconnect; live daemon → no reconnect;
- 33 attachments → two sends (32 + 1); oversized file refused without a call;
- attachment rate limit retried exactly once after the stated wait; a wait over the cap is handed
  back without sleeping; text rate limit not retried;
- inbound: note-to-self handled, own echo dropped, story dropped, unlisted group dropped, listed
  group with mention → command, UUID-only sender, quote of our own message, attachment fetch and
  sniffing guard;
- group partial failure counts as sent; DM unregistered → chat not found;
- recipient upgrade through `listContacts`; typing backoff; send-only open; secrets never in
  errors or reprs; `create_adapter` reads secrets and config.

## Decisions for the lead
- Registry row (core, not edited): `signal` entry with `check_fn=modules_installed("httpx")`,
  `required_env=("SIGNAL_HTTP_URL", "SIGNAL_ACCOUNT")`, `max_len=8000`,
  `group_chats_env="INDUS_SIGNAL_GROUP_ALLOWED_CHATS"`, install hint "install signal-cli and run
  `signal-cli --account <number> daemon --http 127.0.0.1:8080`", plus
  `BUILTIN_CREDENTIALS["signal"] = ("SIGNAL_HTTP_URL", "SIGNAL_ACCOUNT")`. Until then the platform
  starts with `platforms.signal.enabled: true` and the adapter reads the two names itself.
- No pyproject extra: nothing to install on the Python side.
- Send-only open: `adapter.inbound_enabled = False` before `connect()` checks the daemon and starts
  no stream or monitor; `indus send` should set it (core change, P15-20 file).
- Fetcher: the daemon should call `extras.intake.register_fetcher("signal",
  adapter.fetch_attachment)` when it connects the adapter (no core seam for adapters yet).
- Contract: platforms without threads or bot flags declare it on their case (`lacks`); proposed
  as a `Capabilities` flag later.

Compare checklist (filled after the rewrite; reopened `plugins/platforms/signal/adapter.py`,
`signal_rate_limit.py`):
- [x] health check at connect (10 s, non-200 refused) (test: `test_adapter.py::test_connect_errors`)
- [x] events URL with the account percent-encoded (test: `::test_events_url_percent_encodes_the_account`)
- [x] stream backoff 2 → 60 s with jitter and reset (tests: `::test_a_dropped_stream_reopens_after_2_4_8_doubling_to_60_seconds`,
  `::test_streams_that_open_and_close_at_once_also_back_off`, `::test_a_stream_that_delivered_resets_the_wait`)
  — gap found: the reference resets the wait on every successful open, so a stream that opens and
  dies at once retries every 2 s forever; ours resets only after data or 30 s up.
- [x] stale monitor every 30 s, 120 s threshold, check then force a new stream (tests:
  `::test_a_quiet_stream_with_a_dead_daemon_is_reopened`, `::test_a_quiet_stream_with_a_live_daemon_is_left_alone`)
- [x] sync messages: note-to-self and group sync-sents promoted, own echoes dropped by timestamp
  (512, 5 min), other sync events dropped (test: `::test_note_to_self_is_handled_and_our_own_echo_dropped`)
- [x] own messages, stories, contentless envelopes, other accounts (test:
  `::test_own_messages_stories_receipts_and_other_accounts_are_ignored`)
- [x] edits through `editMessage` (test: `::test_edits_carry_the_edited_message_id`)
- [x] groups off unless listed, `*` for all (test: `::test_groups_are_off_unless_listed`)
- [x] mention placeholders rendered, own mention stripped and flagged (test:
  `::test_mentions_of_us_set_mentioned_and_others_render_as_handles`) — gap found: offsets are
  UTF-16 units; the reference indexes code points, which shifts mentions after an emoji. Fixed.
- [x] quote → reply id, text, author, own-message detection (test: `::test_quotes_and_reply_context`)
- [x] UUID-only senders and the alt id (test: `::test_uuid_only_sender_and_alt_id`)
- [x] attachments: 100 MiB cap, content type → kind, voice notes (tests:
  `::test_attachments_are_listed_then_fetched_lazily_and_sniffed`, `::test_voice_notes_are_voice_messages`)
- [x] `getAttachment` answer as a string or `{"data": …}`; magic-byte sniffing (test: `::test_sniffing`)
- [x] per-recipient result validation; group partial success (test:
  `::test_group_send_counts_as_sent_when_any_member_got_it`)
- [x] number → UUID upgrade through `listContacts` (test: `::test_numbers_are_upgraded_to_uuids_from_the_contact_list`)
- [x] typing backoff after three failures (16/32/60 s), explicit stop (test: `::test_typing_pauses_after_three_failures_in_a_row`)
- [x] reactions and their removal (test: `::test_reactions`)
- [x] attachment batches of 32, send timeout max(60 s, 5 s per file), one rate-limit retry
  after the stated refill (default 4 s), rate-limit detection by code −5, `[429]`, RetryLater text
  and `retryAfterSeconds` (tests: `::test_33_attachments_go_out_in_two_sends`,
  `::test_an_attachment_rate_limit_is_retried_exactly_once_after_its_refill`,
  `::test_a_long_attachment_wait_is_handed_over_without_sleeping`)
- [x] masked phone numbers in logs, errors and reprs (test: `::test_the_account_is_masked_in_reprs_logs_and_errors`)
- [x] contract suite (`tests/contract/gateway/test_adapter_contract.py`, case `signal`)
- Deliberate differences: see "Our design differences" above; also: errors are classified
  (unregistered → chat gone, safety number → forbidden, network → transient, captcha →
  unknown) where the reference returns one generic failure; message ids are timestamps.
- Not taken: the phone-number token lock (the daemon's token locks own it; see decisions), the
  👀/✅ processing reactions (no core hook yet; `send_reaction` is ready), the pacing notice for
  long attachment waits (no bucket model), AAC remux, markdown → text styles, plugin native
  handlers, stopping typing before every send (the core typing loop sends the stop), a
  non-rate-limit retry of attachment batches (R06.4: no blind retries).

## Decisions for the lead (after compare)
- Token lock: the daemon locks on all of a platform's credentials joined; for Signal the lock
  should key on `SIGNAL_ACCOUNT` alone (two daemons on one number must conflict even with
  different URLs) — choose the credentials row accordingly.
- Contract suite (shared test files): `AdapterCase.lacks` + the inbound check honouring it, and a
  linear `_long_text` (the old loop was quadratic: 6.7 s for a 50,000 cap). Same output.

## Status

Done (2026-10-19). Adapter, wire fake, unit tests and contract case are in; the contract suite
passes for `signal`. Outside the task's files (shared): `tests/contract/gateway/conftest.py`,
`tests/contract/gateway/test_adapter_contract.py`, traps ledger R06.3, acceptance ACC-K8-1.
Registry row, credentials row, send-only switch in `indus send` and fetcher registration are
handed to the lead (core files).

Landed (integrator, 2026-10-20; D-16.380, D-16.387..391, D-16.393, D-16.394): `inbound_enabled`
replaced by `send_only` read from `AdapterConfig.send_only`; `register_fetchers(seat)` registers
`fetch_attachment`; registry row and `BUILTIN_CREDENTIALS["signal"] = ("SIGNAL_ACCOUNT",)` added
(the URL is not a credential; the lock keys on the number); the event-stream request gets its
`account` query inside the transport so httpx's request log never shows the number; the contract
case is `tests/contract/gateway/signal_case.py`.
