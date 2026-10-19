# Task: Discord adapter (P15-34) Phase: 15 Owner: wave-22 builder D

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `plugins/platforms/discord/adapter.py`
(intents and `connect`, startup error classification, message admission, `_handle_message` with
auto-threads and history backfill, `_auto_create_thread`, `_build_allowed_mentions`,
`_is_allowed_user` / role lookup, `send` and its reply references, slash registration
`_register_slash_commands`, post-connect sync and the diffing safe sync, the component view base),
`plugins/platforms/discord/adapter_component_auth.py` (all),
`plugins/platforms/discord/adapter_slash_auth.py` (all). Research 06 (Discord rows), R06.7.
Evidence: none. Vendor: discord.py 2.7.1 source (MIT) for the `Client`, `Typing`, `View.stop`,
`HTTPClient` command routes and `RateLimited` shapes.

Pinned library: **discord.py 2.7.1** (uploaded 2026-03-03, MIT; Discord HTTP API v10 + gateway v10).
New transitive package: `audioop-lts` 0.2.2 (PSF-2.0, Python ≥ 3.13 only). `aiohttp` and its
dependencies are already locked (P21) at the versions discord.py accepts (`aiohttp<4,>=3.7.4`).

What it does (own words): the Discord platform for the gateway. One discord.py `Client` per profile
with the `message_content`, `dm_messages` and `guild_messages` intents (plus `members` only when a
role allowlist is set, since role checks need member data). Messages become `InboundEvent`s: DMs,
guild channel messages (chat type `group`), thread messages keyed on the parent channel plus the
thread id, bot authors marked, edits marked, attachments listed by their CDN URL with a kind
(voice note, audio, photo, video, document). The admission pipeline does authorization and mention
gating; the adapter tells it whether we were mentioned (an explicit mention, or any message inside a
thread we opened or posted in) and whether the sender holds an allowed role (`role_authorized`).

A mention in a mention-gated guild channel opens a conversation thread: the message is keyed on the
thread it will open (`prospective_thread_id` = the message id, since a thread started from a message
takes that message's id), so the first message and every in-thread follow-up produce the same
session key. The thread itself is created lazily, on the first outbound operation for it (typing,
reply, card), with `auto_archive_duration` 1440 and a name taken from the message — so a message the
admission pipeline refuses never creates a thread. The same mention also pulls the last 20 channel
messages (oldest first, stopping at our own last message) into the event text as context.

Outbound: text is sent as is (the sender has already formatted Markdown and cut it to 2,000 code
points); anything still longer is split natively with the shared chunker and only the undelivered
remainder is reported on a mid-split failure. Every send carries `allowed_mentions` "none" unless
config opts in. Edits, deletes, typing (one trigger per refresh), files, approval cards as buttons
(≤ 80-character labels, ≤ 5 per row, ≤ 5 rows) and clarify questions as buttons, or a select menu
(≤ 25 options) when there are too many choices. Component views are sent "stopped" so discord.py
never dispatches them; every press arrives through `on_interaction`, is acknowledged, and is emitted
as a button event. With a role allowlist configured, a press from someone with neither an allowed
role, nor a place on a user allowlist, nor a pairing approval, is refused at the adapter with a
private notice and never emitted.

Application commands: the chat commands from the slash registry (gateway surface) plus any extra
names the host supplies, capped at 100 (names ≤ 32, lower case; descriptions ≤ 100; optional
`args` string option ≤ 100); over the cap the extra ones are dropped with one warning. Sync is a
diff against the commands Discord already has: deletes first (to stay under the cap), then creates
and edits, with 4.5 s between mutations; unchanged commands cost nothing. It runs in the background
after connect and is cancelled by disconnect. An invocation is acknowledged privately and emitted as
the text `/name args`.

`indus send` opens the adapter send-only: `login` (REST) without the gateway websocket, no handlers,
no command sync — nothing inbound is consumed.

Media download: `fetch_attachment(attachment)` streams a Discord CDN URL (https on Discord's CDN
hosts only, no redirects) and `register_fetchers(intake)` hands it to the media intake.

Inputs → outputs: `AdapterConfig` (credential `DISCORD_BOT_TOKEN`, `platforms.discord.*` extra:
`require_mention`, `free_response_channels`, `auto_thread`, `no_thread_channels`, `allowed_roles`,
`allowed_mentions`, `history_backfill`, `command_sync`; env `INDUS_DISCORD_ALLOWED_ROLES`) →
`DiscordAdapter`; discord.py events → `InboundEvent`s; `send/edit/delete/...` → `SendResult`s.

Files: `src/indus/gateway/adapters/discord/__init__.py`, `adapter.py`, `inbound.py` (normalisation,
kept apart to keep the adapter small), `commands.py`; `tests/unit/gateway/adapters/discord/test_adapter.py`,
`tests/contract/gateway/discord_case.py`. Shared: `tests/fakes/platform/discord_wire.py` (extended
with its SURFACE), `tests/contract/gateway/conftest.py` (one import line), `pyproject.toml` (extra
`discord`), `THIRD_PARTY_NOTICES.md` (two rows), `src/indus/gateway/config.py`
(`AdapterConfig.send_only`), `src/indus/gateway/delivery/send_command.py` (passes `send_only=True`).

Edge cases / failure modes:
- our own messages and system messages (pins, thread-created) are ignored; edits whose text did not
  change are ignored; a redelivered message id is ignored (bounded memory).
- a command typed with a mention is answered where it was typed (no thread per `/status`).
- replies (`message.reference`) do not open a thread; neither do free-response channels,
  `no_thread_channels`, DMs or messages already in a thread.
- thread creation fails → the reply goes to the parent channel as a reply to the message; the
  thread id is remembered as "parent" so later operations of that turn follow it.
- the starter message is never used as the reply reference inside its own thread.
- a 429 longer than discord.py's 30 s internal wait surfaces as `RateLimited`: a typed rate-limit
  failure carrying `retry_after` (never slept here).
- startup: a rejected token (`LoginFailure`) or missing privileged intents
  (`PrivilegedIntentsRequired`) is fatal (not retried); anything else is retryable.
- the token never appears in errors (the base scrubs every error text with the credentials).
- edit text over the cap: the first piece edits the message, the rest are new messages.
- DMs never get a role grant (roles are guild-scoped).

Our design differences / decisions:
- `allowed_mentions` none by default (the reference allows user and reply pings); config opt-in.
- the auto-thread is created lazily from the outbound side (the reference creates it before
  dispatch, before authorization has run).
- history backfill also runs for a mention that opens a thread (the reference skips it there; a new
  thread has no history of its own, so the channel's recent messages are the only context).
- the backfill is put in front of the event text (the event model has no context field).
- no forum-channel posts, voice, reactions for processing state, missed-message recovery scan,
  thread renaming, skill command group, owner-only visibility, channel allow/ignore lists — not in
  the P15 row (voice is P3, K45).
- no `CommandTree`: commands are registered and invocations read straight from the interaction, so
  discord.py never answers "unknown command" on its own.

Tests we will write:
- contract suite case `discord` over the wire fake (no discord.py needed).
- command sync: 101 commands → 100 synced and one warning; pacing 4.5 s on the fake clock; deletes
  before creates; unchanged commands not touched; disconnect cancels a running sync.
- a mention that spawns a thread and its in-thread follow-up share one session key; the thread is
  created on first reply with archive 1440; a refused (never answered) mention creates no thread.
- component press by a member without an allowed role refused (private notice, nothing emitted);
  with the role it is emitted with the button data; role grant on messages.
- history backfill of 20 messages on a mention; none in a free-response channel.
- send-only open through `indus send discord:<id>`: no gateway session, no handlers, inbound left
  alone.
- media fetcher: streams a CDN URL, refuses other hosts; registered with the intake.
- intents (members only with roles), allowed mentions none, fatal vs retryable connect errors,
  token scrubbed from errors.

## Decisions for the lead
- **send-only seam**: `AdapterConfig.send_only: bool = False` (new field) and
  `send_command.build_adapter` passes `send_only=True`; adapters open REST-only when set. The
  Telegram and Slack builders need the same flag: keep one copy at merge.
- **fetcher wiring**: adapters expose `register_fetchers(intake) -> undo`; the daemon has no
  adapter → intake hook yet. Suggested: `ExtrasPart` subscribes to `daemon.on_platform_up` and calls
  `adapter.register_fetchers(extras.intake)` when present (undo on down).
- **extra commands**: `DiscordAdapter.extra_commands` (plugin/skill command names for the picker)
  is empty until the daemon passes the plugin host's commands; the cap logic already reserves no
  slot for them beyond the 100 limit.
- discord.py is loaded with `importlib.import_module("discord")` on first connect (no import
  statement, so no pyright ignore is needed); a missing library is a fatal `missing_dependency`.
- with a role allowlist set, the adapter itself refuses button presses and application commands
  from people with no grant (allow-all, user lists, role, pairing); without one the authorization
  chain is the only gate. Refusal copy is ours (`REFUSED_PRESS`, `REFUSED_COMMAND`).
- a failed auto-thread creation answers in the parent channel (reply to the message) rather than
  dropping the request; change `_create_thread` to raise instead if the lead prefers refusing.
- the fake's `CommandTree` row was replaced by `discord.http:HTTPClient` command routes (the adapter
  does not use a tree); other fake rows added: `ButtonStyle`, `SelectOption`, `File`,
  `MessageReference`, `ui.Select`, `View.stop`, `Client.login`/`guilds`/`http`/`application_id`.

Compare checklist (filled after the rewrite; reopened `plugins/platforms/discord/adapter.py`
connect / admission / `_handle_message` / sync / send, `adapter_component_auth.py`,
`adapter_slash_auth.py`):
- [x] intents, members only with roles (test: `tests/unit/gateway/adapters/discord/test_adapter.py::test_intents_ask_for_members_only_with_a_role_allowlist`)
- [x] fatal vs retryable startup errors by exception type name (test: `::test_connect_errors_are_classified`); token never in the error (`::test_the_token_never_shows_in_a_connect_error`)
- [x] a second live client is never left behind: `_open` shuts any previous client first, a failed start shuts the new one (gap found: added the "shut previous" step)
- [x] own, system, duplicate and unchanged-edit messages ignored (`::test_own_system_duplicate_and_unchanged_edit_messages_are_ignored`)
- [x] bare mention without text, media or context dropped; with context kept as "mentioned without writing anything" (gap found; `::test_a_bare_mention_counts_only_with_context_around_it`)
- [x] threads we opened stay addressed (no mention needed) — also after a restart, through the thread's `owner_id` (gap found; `::test_threads_the_bot_opened_stay_addressed_after_a_restart`)
- [x] auto-thread key = thread key, thread archive 1440, name from the text without mention tokens (`::test_thread_spawned_by_a_mention_shares_the_session_with_follow_ups`, `::test_when_a_mention_opens_a_thread`)
- [x] component press gate: allow-all, user lists, role, pairing; DMs never get a role grant (`::test_component_press_by_a_member_without_an_allowed_role_is_refused`, `::test_a_paired_or_listed_user_may_press_without_the_role`, `::test_role_grant_on_guild_messages_only`)
- [x] application commands under a role allowlist go through the same gate, refused privately (gap found; `::test_an_application_command_by_a_member_without_the_role_is_refused`)
- [x] 100-command cap with one warning, deletes before creates, unchanged untouched, 4.5 s pacing (`::test_command_sync_caps_at_100_with_one_warning`, `::test_command_sync_paces_mutations_and_deletes_first`)
- [x] edit overflow continues in new messages (`::test_an_edit_past_the_cap_continues_in_new_messages`)
- [x] history backfill: last 20, oldest first, stops at our own message, skips system messages (`::test_history_backfill_on_a_mention_in_a_busy_channel`)
- Deliberate differences: `allowed_mentions` none by default (row); lazy thread creation after
  admission; backfill also for a thread-opening mention; failed thread creation answers in the
  channel instead of refusing the message; no slash-command gate of our own when no role allowlist
  is set (the authorization chain is the one gate, and it also covers pairing and allowlists);
  commands synced by difference on every connect (no fingerprint state file); bots are admitted or
  not by the chain's allow-bots setting, not by the adapter.
- Not taken (outside the P15 row): forum-channel posts, forwarded-message snapshots and attachments
  of the replied-to message, the per-reply chunk cap, voice (P3, K45), reactions for processing
  state, the missed-message recovery scan, thread renaming after the first turn, the `/skill`
  command group and owner-only visibility, channel allow/ignore lists, username → id resolution of
  allowlist entries (`resolved_allowlist_ids` stays empty), liveness probe of the websocket.

## Status

Done (2026-10-19). All four row files plus `inbound.py`; contract case `discord` passes the shared
suite over the wire fake without discord.py; the drift check passes against discord.py 2.7.1 when
installed (31 rows). Shared edits: `tests/fakes/platform/discord_wire.py` (+ SURFACE),
`tests/contract/gateway/conftest.py` (one import), `pyproject.toml` (extra `discord`),
`THIRD_PARTY_NOTICES.md` (discord.py, audioop-lts rows; aiohttp row names the extra),
`docs/plan/phases/00-layout-and-conventions.md` (pin row), `src/indus/gateway/config.py`
(`AdapterConfig.send_only`), `src/indus/gateway/delivery/send_command.py` (passes it),
`docs/internals/acceptance-map.md` (ACC-K6-1 tests), `docs/internals/traps-ledger.md` (R06.7 row).
`uv.lock` left to the lead: a trial `uv lock` in the worktree added only `discord-py 2.7.1` and
`audioop-lts 0.2.2` (no version moved); the worktree's lock is restored.

Landed (integrator, wave 22): `AdapterConfig.send_only` kept as the one send-only shape (D-16.334); `register_fetchers` is the base adapter's seam and `ExtrasPart` calls it on platform up/down (D-16.335); `extra_commands` stays empty (the daemon's chat commands have no plugin/skill command source yet: open seam).
