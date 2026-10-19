# Task: Telegram adapter (P15-33) Phase: 15 Owner: builder T (wave 22)

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `plugins/platforms/telegram/adapter.py`
(constants block lines 1–520, `connect`, `_start_webhook_mode`, `_start_polling_mode`, `send` /
`_send_text_locked` / `_send_chunk_with_retries` / `_classify_send_exception` / `_send_chunks` /
`_with_partial_send`, `edit_message` and `_edit_overflow_split`, `_register_command_menu`,
`_handle_callback_query` / `_callback_authorized` / `_is_callback_user_authorized`,
`_effective_message_thread_id`, `_should_process_message`, `_handle_text_message` /
`_handle_command`, `_build_message_event`, `_reply_context`, reactions block),
`plugins/platforms/telegram/update_admission.py` (all), `plugins/platforms/telegram/telegram_network.py`
(skim: keepalive, fallback transport, DoH), `gateway/run_topics.py` (topic lobby, lines 70–135),
`gateway/run_inbound.py` (lines 1255–1275, where the lobby reminder is sent).
Evidence: none (source-only task).

Interop pin: Telegram Bot API 9.x as documented at core.telegram.org/bots/api (method and field names
are interop facts). No library: python-telegram-bot is LGPL-3.0 and not on `licenses_allow.json`, so
we ship our own small async client over httpx (already a core dependency, BSD-3-Clause). No
`telegram` extra exists or is needed.

What it does (own words): connects one bot to Telegram. At connect it asks `getMe` who the bot is,
clears any webhook and long-polls `getUpdates` (or, when a webhook URL is configured, registers the
webhook with a secret and serves it; no secret = refuse to start). Each update is admitted once
(a bounded receipt set of recent update ids, kept 24 h and saved under the profile so a restart does
not replay updates Telegram re-sends), handed to a dispatcher that runs at most 32 at a time,
first-come-first-served within one chat, and normalised into an `InboundEvent` (DM / group /
channel / forum topic, reply context, mention of the bot, media as `tg:` file refs, button presses).
Outbound: text arrives already formatted as MarkdownV2 by the sender (P15-15/17); the adapter sends
it with `parse_mode=MarkdownV2` (plain when the sender asks for the plain fallback), splits what is
over 4,096 UTF-16 units itself, threads replies with `reply_parameters` and topics with
`message_thread_id`, and classifies every failure (flood waits come back typed with their wait and
are never slept here; the base's retry policy sleeps at most 5 s). Edits, deletes, typing (chat
action), reactions, media uploads per kind, approval and clarify cards with inline keyboards
(callback presses answered with `answerCallbackQuery` at once), a command menu from the slash
registry (`setMyCommands`), and a media fetcher (`getFile` + file download) for voice notes, photos
and documents.

Inputs → outputs: `AdapterConfig` (credential `TELEGRAM_BOT_TOKEN`, proxy, `platforms.telegram.*`
extra keys: `base_url`, `webhook_url`, `webhook_host`, `webhook_port`, `max_concurrent_updates`,
`drop_pending_on_cold_boot`, `reactions`, `require_topics`, `poll_timeout`; secret
`TELEGRAM_WEBHOOK_SECRET`) → `TelegramAdapter`; Bot API updates → `InboundEvent`s through `emit`;
adapter calls → Bot API requests → `SendResult`s.

Files: `src/indus/gateway/adapters/telegram/{__init__,bot_api,adapter,inbound,webhook}.py`,
`tests/unit/gateway/adapters/telegram/{__init__,test_adapter,test_bot_api}.py`. Shared (minimal):
`tests/fakes/platform/telegram_wire.py` (+ its SURFACE; now built on our client's types and errors),
`tests/contract/gateway/conftest.py` (Telegram case registered),
`tests/contract/gateway/test_vendor_names.py` (Telegram checked against our client and the
documented method names), `tests/contract/gateway/test_adapter_contract.py` (chunk indicator regex
also accepts the MarkdownV2-escaped form), `tests/unit/fakes/test_platform_wires.py` (imports),
`src/indus/gateway/adapters/base.py` (`connect(send_only=True)`),
`src/indus/gateway/delivery/send_command.py` (uses it), `src/indus/gateway/extras.py` (registers an
adapter's `fetch_media` with the intake when the platform comes up), `src/indus/gateway/registry.py`
(Telegram probe = httpx, hint without an extra), traps / acceptance rows.

Edge cases / failure modes:
- The token is in every request path. It must never reach a log line, an exception text, a repr or
  argv: requests are built with a placeholder path and the real token is put in by our transport at
  the last moment (httpx's own "HTTP Request" log line shows the placeholder); every error text is
  scrubbed again; httpx exceptions are re-raised `from None` with their class name.
- Empty poll returning at once (fake, proxies) must not spin: a short pause after a quick empty poll.
- Poll errors: network → backoff (1 s doubling to 60 s); 409 conflict → backoff and a warning;
  401 → stop polling, log once (the token was revoked).
- Duplicate update id (re-delivery after a lost acknowledgement, or after a restart): dropped.
- Our own echoed messages dropped; a `/cmd@otherbot` command in a group dropped.
- Forum: a message in a topic is `chat_type="forum"`, `thread_id=<topic>`; General (no thread id)
  is `"1"`; sends never pass thread id 1 (the API refuses it), typing does.
- Thread gone on send: retried once without the topic. Reply anchor gone: `allow_sending_without_reply`.
- Over-cap text: split with UTF-16 counting; a failure after some chunks landed reports the delivered
  ids and (unless the failure was an ambiguous timeout) the undelivered source remainder.
- Final edit over the cap: first piece edited in, the rest sent as replies; "message is not
  modified" counts as success; interim edit over the cap shows the first piece only.
- Callback presses: answered at once (stops the client spinner) whoever pressed; the pipeline's
  authorization decides (an unauthorized press changes nothing).
- Send-only open (`indus send`): `getMe` only — no webhook change, no polling, no command menu.
- Webhook mode without `TELEGRAM_WEBHOOK_SECRET`: fatal connect error (forged updates otherwise); a
  request with a wrong secret header gets 401 and is not processed.
- Media: uploads over 50 MB refused before any request; downloads over 20 MB refused (Bot API
  limits); captions cut at 1,024 UTF-16 units.

Our design differences / decisions:
- Own client (`bot_api.BotApi`), snake_case methods named after the documented camelCase methods,
  typed results (frozen dataclasses), errors `RetryAfter`, `BadRequest`, `Forbidden`,
  `Unauthorized`, `Conflict`, `TimedOut`, `NetworkError`.
- No direct-IP fallback transport, no DoH discovery, no polling-stall watchdog ladder: the daemon's
  reconnect watcher (P15-29) owns recovery; the poll loop backs off by itself.
- No local per-chat flood cooldown: the delivery ledger owns long waits (R06.3).
- Rich messages (Bot API 10.1), native drafts, DM topics, sticker vision: not here (P3 / later).
- Reactions: opt-in (`platforms.telegram.reactions: true`), exposed as `processing_started` /
  `processing_finished` on the adapter; the turn runner calls them (follow-up for the lead).
- Topic-root reminder: opt-in `platforms.telegram.require_topics: true`; plain text in a forum's
  General topic gets a reminder (once per 30 s per chat) instead of a turn; commands still pass.

Tests we will write:
- contract suite (Telegram case over the wire fake);
- 4,096 UTF-16 units of emoji: one message; one more unit: split, every piece ≤ cap, nothing lost;
- `RetryAfter(97*60)` → typed flood failure, fake clock not advanced, one API call;
- duplicate update id ignored (and after a restart via the receipt file);
- unauthorized button press refused through the real admission pipeline, callback still answered;
- forum topic → session key equals the golden `forum_topic_shared` key;
- token never in caplog / exception text / repr (real client over `httpx.MockTransport`);
- send-only open over the wire fake through `indus send telegram:<chat>`: no `get_updates`;
- webhook: refuses without a secret; wrong secret header 401; right one dispatches;
- command menu names and descriptions; `/reload_mcp` → `/reload-mcp`; fetcher downloads `tg:` refs.

## Decisions for the lead
- No `telegram` extra: the client is ours over httpx (core). `registry.py` probes `httpx` for the
  Telegram row and its hint names no extra. Change: restore an extra if a library is ever adopted.
- Webhook secret name `TELEGRAM_WEBHOOK_SECRET` (secret store, then env), URL `TELEGRAM_WEBHOOK_URL`
  or `platforms.telegram.webhook_url`; listener `webhook_host` (default 0.0.0.0; 127.0.0.1 at landing, D-16.330) /
  `webhook_port` (8443). Our own small FastAPI app under uvicorn, not the P15-32 receiver.
- Cold boot drops the server-side update backlog (`deleteWebhook(drop_pending_updates)`) unless
  `drop_pending_on_cold_boot: false`; a reconnect keeps it.
- Update receipts saved at `<home>/gateway/telegram_receipts_<bot id>.json`.
- `Adapter.connect(send_only=True)` is the send-only open; `indus send` uses it (Discord/Slack can
  read `self.send_only` in `_open` the same way).
- `extras.ExtrasPart` registers `adapter.fetch_media` with the intake on platform up (any adapter
  that defines it, through `extras.attach_fetcher`), and drops it on down.
- The command menu is cut at a text budget (names + descriptions ≤3,500 characters; today 50
  commands are ~4.5 KB of JSON, so the tail of the registry is left out of the menu but still
  works typed). Change: `inbound.MENU_TEXT_BUDGET`.
- The `require_topics` reminder is sent by the adapter, before authorization (it reveals only
  that the bot is in the group, which its members can see). Move it behind admission if wanted.
- Inbound edits (`edited_message`) are not turned into events (an edit would start a second turn);
  `allowed_updates` asks only for messages, channel posts and button presses.
- The shared contract's chunk-indicator pattern accepts the MarkdownV2-escaped `\(i/n\)`.

Compare checklist (filled after the rewrite; reopened `adapter.py` thread/topic helpers, cold-boot
drop, `send_typing`, `_send_voice_bubble` / `send_voice`, `_register_command_menu` /
`_ensure_forum_commands`, `edit_message`; `update_admission.py`):
- [x] Connect: `getMe`, refused token fatal (test: `tests/unit/gateway/adapters/telegram/test_adapter.py::test_a_refused_token_is_a_fatal_connect_error`);
  a failed open closes the client (`::test_a_failed_connect_closes_the_client`).
- [x] Cold boot drops the queued backlog, a reconnect keeps it; the decision is logged
  (`::test_connect_drops_the_backlog_on_a_cold_boot_and_keeps_it_on_a_reconnect`).
- [x] Webhook only with its secret; secret checked before parsing
  (`::test_webhook_mode_needs_its_secret`, `::test_webhook_mode_accepts_only_pushes_with_the_secret`,
  `::test_the_webhook_listener_serves_on_loopback`).
- [x] Update admission: ≤4,096 receipts, 24 h, saved per bot, reloaded after a restart
  (`::test_a_duplicate_update_id_is_handled_once_also_after_a_restart`,
  `::test_receipts_are_bounded_and_expire_after_a_day`); per-chat FIFO, ≤32 at once
  (`::test_updates_of_one_chat_run_in_order_and_at_most_the_limit_at_once`).
- [x] Sends: UTF-16 cap, own split, escaped markers, partial-split remainder only when certain
  (`::test_4096_utf16_units_of_emoji_fit_one_message_and_one_more_unit_splits`, contract
  `no-whole-resend`); flood waits over 5 s typed, never slept
  (`::test_a_97_minute_flood_wait_comes_back_typed_without_sleeping`); deleted topic retried once
  without the topic, General (1) left out of sends (`::test_topic_replies_threads_and_a_deleted_topic`).
- [x] Gap found: the reference puts thread 1 on the chat action (sendChatAction needs it for
  General while sendMessage refuses it) — fixed by `_typing_thread`, pinned by
  `::test_general_topic_typing_saturated_previews_and_forum_menus`.
- [x] Gap found: a saturated interim overflow preview was re-sent on every frame — fixed by the
  preview memory in `_edit` (same test).
- [x] Gap found: forum topics show only a chat-scoped command menu — fixed by `_forum_menu` (once
  per forum chat, same test); menu also capped by a text budget (Telegram refuses large menus,
  `::test_the_command_menu_stays_within_its_text_budget`).
- [x] Gap found: voice files that are neither Ogg/Opus nor MP3/M4A went to sendAudio — now a
  document (same test).
- [x] Edits: not-modified = ok, markup refusal → plain once, final overflow split into replies
  (`::test_edits_not_modified_markup_fallback_and_overflow`).
- [x] Buttons: answered at once, refused upstream for strangers
  (`::test_a_button_press_from_an_unauthorized_user_is_refused`); cards fall back to plain markup
  (`::test_approval_cards_have_inline_keyboards_and_fall_back_to_plain_markup`).
- [x] Reactions 👀 / 👍 / 👎 / cleared, opt-in; menu in the three scopes
  (`::test_typing_reactions_and_the_command_menu`).
- [x] Token never in logs, error texts or reprs (`tests/unit/gateway/adapters/telegram/test_bot_api.py::test_the_token_reaches_the_wire_but_no_log_repr_or_error`).
- Deliberate differences: the reference answers a refused button press with a toast; we answer
  every press with an empty answer and let the admission pipeline refuse it (no adapter-level
  allowlist copy). The reference retries short floods and network errors inside the adapter; we
  return the classified result and the shared retry policy (P15-04) retries. Webhook listener
  binds 0.0.0.0 by default (reference: all interfaces incl. IPv6); set `webhook_host` for `::`. (Changed at landing: loopback by default, D-16.330.)
- Not taken: direct-IP fallback transport and DoH discovery, the polling-stall watchdog ladder
  (the reconnect watcher owns recovery), rich messages (Bot API 10.1), native drafts, DM topics,
  sticker vision, the model/choice pickers, voice transcoding to Ogg/Opus, video geometry probes,
  the bot status indicator, group observation of unmentioned messages, per-chat local flood
  cooldown.

## Status

Done (2026-10-19). All files above; shared edits listed under Files. Follow-ups for the lead:
call `processing_started` / `processing_finished` from the turn runner; surface
`polling_stopped == "auth"` to the daemon as a fatal platform state (today it is logged);
Discord/Slack can read `self.send_only` in `_open` and use `extras.attach_fetcher` through
`fetch_media`.

Landed (integrator, wave 22): send-only is `AdapterConfig.send_only` (D-16.334; `Adapter.connect` has no send-only argument), media downloads go through `register_fetchers` (D-16.335; `fetch_media` is the registered function), the webhook listener binds 127.0.0.1 unless `webhook_host` is set (D-16.330), and a revoked token is reported with `report_fatal` so the daemon parks Telegram as fatal (D-16.333).
