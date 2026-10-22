# Task: Slack adapter (P15-35) Phase: 15 Owner: wave-22 builder S

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `plugins/platforms/slack/adapter.py`
(class constants and `__init__`, handler registration, `connect` / `disconnect`, the workspace and
DM-conversation helpers, `send` / `_post_chunks`, private notice, `edit_message`, `delete_message`,
`send_typing` / `stop_typing`, `_resolve_thread_ts`, upload helper, the inbound path
`_handle_slack_message*` / `_prefilter_inbound` / `_build_message_event`, inbound file kinds, the
interactive prompt senders and the approval / clarify action handlers), `plugins/platforms/slack/
block_kit.py` (limits, `_split_text`, `sanitize_blocks`). Phase row P15-35, research 06, LEAD-NOTES
P15 waves 1–3 (open seams: send-only open, `register_fetcher`, Slack `typing_status_text`).
Evidence: none.

What it does (own words): connects the gateway to one Slack workspace over Socket Mode. At connect it
checks the bot token with `auth.test` (learning the bot user id, bot id and workspace id), registers
listeners for `message`, `app_mention` and our button actions on a Bolt app, and opens the Socket
Mode connection with the app token. Every inbound message becomes one `InboundEvent`: DMs (`im`) are
`dm`, channels and group DMs are `group`, replies inside a thread are `thread` with the thread's root
`ts` as thread id; a top-level channel message names its own `ts` as the prospective thread (our
replies open a thread there), so it and its in-thread follow-ups share one session key. The
workspace id rides as `scope_id`, so the same channel id in two workspaces gives two keys. `message`
and `app_mention` for one message share a `ts` and produce one event (dedupe by workspace, channel
and `ts`, bounded); our own posts are dropped; other bots' posts (`bot_message` subtype or a
`bot_id`) are passed on marked `is_bot` for the gateway's bot rules; housekeeping subtypes are
dropped; an edit of a message we already took is dropped, an edit of one we never took is passed on
marked `edited`. Our own mention is stripped and reported as `metadata["mentioned"]`; replies in a
thread we were summoned into (or posted in) count as addressed. Files become attachments (Slack's
private URL), with a fetcher that downloads them with the bot token for media intake.

Outbound: text goes to `chat.postMessage` (already mrkdwn from the sender; `meta["plain"]` turns
markup off), threaded under the metadata thread id or, with `reply_in_thread` (default on), under
the message we reply to; a user id target is opened as a DM first (`conversations.open`, cache
≤5,000). Cap 39,000 code points; longer text is refused as too long before it reaches Slack (the
sender chunks). Edits (`chat.update`, which also drops any old blocks so button cards lose their
buttons), deletes, ephemeral private notices (`chat.postEphemeral`), the typing indicator as
`assistant.threads.setStatus` in a thread (configurable text; turned off for the process when the
app lacks the scope), media through `files.uploadV2`, approvals and clarify questions as Block Kit
action buttons whose action ids are our callback data. Ignored channels: no inbound, outbound
refused. Errors come back classified (Slack error codes, `Retry-After` for rate limits).

Send-only open (`indus send`): `auth.test` and the web client only — no listeners, no Socket Mode
connection, so no event is consumed.

Inputs → outputs: `AdapterConfig` (credentials `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`; proxy;
`platforms.slack.*` extra: `ignored_channels`, `reply_in_thread`, `typing_status_text`; new
`send_only`) → a connected adapter; Events API payloads → `InboundEvent`s; adapter calls →
Web API calls and `SendResult`s.

Files: `src/indus/gateway/adapters/slack/__init__.py`, `src/indus/gateway/adapters/slack/
adapter.py`, `src/indus/gateway/adapters/slack/blocks.py`,
`tests/unit/gateway/adapters/slack/test_adapter.py` (+ `__init__.py`),
`tests/contract/gateway/slack_case.py` (new). Shared: `docs/internals/acceptance-map.md`, `tests/fakes/platform/slack_wire.py`
(ts-shaped ids, regex action matching, `conversations_open` / `conversations_info`, a platform that
finds messages by `ts`), `tests/contract/gateway/conftest.py` (registers the case),
`src/indus/gateway/config.py` (`AdapterConfig.send_only`), `src/indus/gateway/delivery/
send_command.py` (builds send-only adapters), `pyproject.toml` (extra `slack`),
`THIRD_PARTY_NOTICES.md`.

Edge cases / failure modes:
- `message` then `app_mention` (same ts) → one event; Socket Mode redelivery → dropped by the same
  dedupe; dedupe memory bounded (oldest forgotten).
- Own posts (bot user id or bot id) never come back as input; other bots are marked, not dropped.
- `message_changed` of a taken message: dropped (no second turn); `message_deleted`, joins, topic
  changes: dropped.
- Missing library: connect fails non-retryable with the install hint; bad token (`invalid_auth`,
  `not_authed`, `token_revoked`, `account_inactive`): non-retryable; network/timeouts retryable.
- Rate limit: `Retry-After` header (any case) becomes `retry_after`; read timeouts are "maybe
  delivered" (never re-sent).
- Block payload refused (`invalid_blocks`): the same call once without blocks.
- Typing without a thread: nothing (Slack's status needs a thread).
- File download: only `https` on `slack.com` hosts gets the bot token; anything else is refused.
- A button press is acknowledged first, then emitted (Slack wants the ack within 3 s).

Our design differences / decisions:
- One bot token (one workspace install) per adapter; the reference can hold several bot tokens. The
  key still carries the event's workspace id (Slack Connect / Grid channels).
- The library is behind one seam (`SlackSdk`, loaded on first open), so the contract suite runs over
  the wire fake with no SDK installed.
- No `!cmd` → `/cmd` rewrite, no Slack native slash commands, no native streams (P2), no thread
  history hydration, no reactions lifecycle, no assistant-thread titles/suggested prompts, no
  socket watchdog (the daemon's reconnect watcher owns reconnects) — see "Not taken".
- Mention gating, allowlists and the bot rules stay in the gateway core (P15-08/10); the adapter
  only reports `mentioned` / `is_bot`.

Tests we will write:
- Contract suite over the wire fake (all checks + no network).
- `message` + `app_mention` one turn; redelivery; own post dropped; bot post marked; edit rules.
- Two workspaces, same channel id → different session keys; top-level message and its thread
  follow-up → same key.
- Approval press through a real `AdmissionPipeline` + `ChatPrompts` settles the request and the card
  loses its buttons.
- 50-block cap (clarify with many choices, caller blocks); sanitiser clamps; block refusal retry.
- Rate limit / auth failure classification; DM open cache; ignored channels; typing status.
- Send-only open: `indus send slack:C…` over the wire fake registers no listener and opens no socket.
- Fetcher: token only to Slack hosts, bytes streamed.

## Decisions for the lead
- **Send-only seam:** `AdapterConfig.send_only: bool = False`; `send_command.build_adapter` passes
  `True`. Adapters read it in `create_adapter` (Slack: skip listeners and Socket Mode). Telegram and
  Discord builders may have chosen another shape — keep one.
- **Fetchers:** `SlackAdapter.register_fetchers(intake)` calls `intake.register_fetcher("slack",
  …)` and returns the undo; the daemon should call it for each connected adapter that has it (after
  `ExtrasPart.prepare`, e.g. in `_connect`), and undo on disconnect.
- **Outbound threading of later pieces:** `FinalReply.thread_id` comes from `source.thread_id`
  only, so the 2nd+ piece of a reply >39,000 chars to a top-level channel message (reply_to_mode
  `first`) goes to the channel, not the thread. Fix in the core: for Slack, use
  `prospective_thread_id` as the outbound thread when there is no real one (not done here: it would
  change Discord's meaning of the field).
- **Dependencies:** extra `slack = ["slack-bolt==1.30.0", "slack-sdk==3.44.1", "aiohttp==3.14.4"]`
  (aiohttp is what Bolt's async app and Socket Mode handler import; it is an optional extra of
  slack-sdk, so it must be named). Not locked here (lead re-locks). Wave 46 (RM8) moved the pin
  from 3.14.3 to 3.14.4 (advisory fixes, SEC2-3).
- **Typing meta:** `outbound.ports.typing` calls `typing_start(chat_id)` with no metadata; Slack's
  status needs the thread, so the runner should pass `{"thread_id": source.thread_id or
  source.prospective_thread_id}` when it starts feeding `TypingLoop`.
- **Approval / clarify cards in channels** go where `prompts._meta(source)` says: a top-level channel
  conversation has no real thread id, so the card is posted top-level (the replies are threaded).
  Same core fix as above (use the prospective thread for Slack).
- Settings read from `platforms.slack` extra: `ignored_channels` (list or comma string),
  `reply_in_thread` (default true), `typing_status_text` (default "is thinking…").

Compare checklist (filled after the rewrite; reopened `adapter.py`, `block_kit.py`):
- [x] `message` + `app_mention` sharing a ts → one event; redelivery dropped (test:
  `tests/unit/gateway/adapters/slack/test_adapter.py::test_message_and_app_mention_with_one_ts_make_one_turn`,
  `::test_an_app_mention_alone_counts_as_mentioned_and_a_redelivery_is_dropped`)
- [x] workspace id in the key (`::test_two_workspaces_with_one_channel_id_get_different_keys`)
- [x] top-level channel message keyed on its own ts so the thread follow-up matches; thread we
  were summoned into counts as addressed (`::test_a_channel_message_and_its_thread_follow_up_share_one_session`)
- [x] `reply_in_thread: false` → flat (`::test_without_reply_in_thread_a_channel_message_has_no_prospective_thread`)
- [x] own posts dropped, other bots marked (`::test_own_posts_are_dropped_and_other_bots_are_marked`)
- [x] edit of a taken message dropped; housekeeping subtypes dropped (`::test_edits_and_housekeeping`)
- [x] gap found: the reference also treats `bot_profile`, `user_profile.is_bot` and an app post
  without `client_msg_id` as bot-authored, passes `document_mention`, and reads the workspace from the
  envelope's `authorizations` — added, pinned by
  `::test_app_posts_count_as_bots_and_the_team_can_come_from_the_envelope`
- [x] file kinds incl. Slack audio clips (`::test_files_become_attachments_with_their_kinds`)
- [x] user id targets opened as a DM, cached (`::test_a_user_id_target_opens_the_dm_once`)
- [x] Block Kit refusal → once without blocks for plain sends; an approval card fails instead
  (`::test_refused_caller_blocks_are_dropped_once`, `::test_a_refused_card_fails_instead_of_losing_its_buttons`)
- [x] edit replaces the blocks (buttons go) (`::test_an_approval_card_is_buttons_and_a_final_edit_removes_them`)
- [x] 50-block cap, section 3000 / header 150 clamps, empty blocks dropped
  (`::test_caller_blocks_are_capped_at_fifty`, `::test_clarify_with_many_choices_stays_within_the_block_cap`,
  `::test_sanitize_clamps_and_drops`)
- [x] ephemeral private notice (`::test_private_notices_are_ephemeral`); uploads threaded
  (`::test_media_is_uploaded_into_the_thread`)
- [x] typing = thread status; off without the scope; configurable text
  (`::test_typing_is_a_thread_status_and_goes_off_without_the_scope`, `::test_typing_status_text_comes_from_settings`)
- [x] Retry-After → typed flood failure (`::test_rate_limit_wait_comes_from_the_header_in_any_case`)
- [x] refused token non-retryable and never shown; failed socket closed
  (`::test_a_refused_token_is_not_retryable_and_never_shown`, `::test_a_failing_socket_closes_and_hides_the_app_token`)
- [x] approval press through the real intake settles the request; a stranger's press does not
  (`::test_an_approval_press_settles_the_request`)
- [x] send-only open (`::test_send_only_registers_no_listener_and_opens_no_socket`,
  `::test_indus_send_slack_does_no_inbound_work`)
- [x] file download only to Slack hosts, sign-in page refused (`::test_the_fetcher_sends_the_token_only_to_slack`,
  `::test_the_fetcher_refuses_a_sign_in_page`)
- [x] shared contract suite (`tests/contract/gateway/test_adapter_contract.py`, case `slack`); vendor
  names exist in slack-bolt 1.30.0 / slack-sdk 3.44.1 (run with the libraries installed: 8 passed)
- Deliberate differences: one bot token per adapter (the reference loads several workspaces'
  tokens); DMs are one session per DM channel and replies there are not threaded (the reference
  makes each top-level DM its own thread session by default); an edit text over the cap is refused
  as too long (the reference truncates; the stream consumer seals before the cap); the sender
  splits long text (the reference splits inside `send`); mpim is a group (mention needed, like the
  reference's gating) and keyed as a group.
- Not taken (not needed in P15, with the row that brings it later or why): `!cmd` → `/cmd`
  rewrite and Slack native slash commands (need the command registry in the adapter; follow-up);
  native `chat.*Stream` streaming and task cards (P2); `conversations.replies` thread-history
  hydration and restart rehydration; `reactions.add/remove` processing reactions; Assistant-thread
  titles, suggested prompts, app-home / context events; file stubs (`check_file_info` via
  `files.info`); link-unfurl and block-text enrichment of inbound text; `reply_broadcast`; status
  bubbles (`send_or_update_status`); model picker; feedback buttons; the Socket Mode watchdog (the
  daemon's reconnect watcher restarts a dead adapter); the catch-all ack for unhandled events
  (Bolt logs them; worth adding with the manifest work); per-request proxy middleware (the proxy is
  set on the client and the socket handler).

## Status

Done (2026-10-19). Adapter, Block Kit builders, contract case, unit tests and the send-only seam
are in; shared edits: `tests/fakes/platform/slack_wire.py`, `tests/contract/gateway/conftest.py`,
`src/indus/gateway/config.py`, `src/indus/gateway/delivery/send_command.py`, `pyproject.toml`,
`THIRD_PARTY_NOTICES.md`, `docs/internals/acceptance-map.md` (ACC-K7-1, one ACC-K13-1 line). Left for
the lead: re-lock (adds only slack-bolt 1.30.0 and slack-sdk 3.44.1), daemon wiring of
`register_fetchers`, the core seams under "Decisions for the lead", typing meta (the typing port
passes no thread, so Slack shows no status until it does).

Landed (integrator, wave 22): send-only and fetcher seams as D-16.334 / D-16.335; the outbound-thread seam is fixed in the core with `Capabilities.reply_opens_thread` (on for Slack, D-16.336): every reply piece, card and typing status of a top-level channel conversation goes into its thread. The runner still does not feed `TypingLoop` (open seam).
