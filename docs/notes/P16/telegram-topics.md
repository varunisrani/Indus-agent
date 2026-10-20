# Task: Telegram DM topics and sticker descriptions (P16-34) Phase: 16 Owner: wave-28 builder E16

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `plugins/platforms/telegram/adapter.py`
(topic and sticker parts only: DM topic creation and its errors, the configured topic list,
`_handle_sticker`), `gateway/sticker_cache.py` (skimmed: cache file, entry fields, injection text),
`gateway/run_topics.py` (skimmed: method list only), `hermes_cli/commands.py` (the `/topic` row).
Research 03 §3.20 (topic tables), 06 §3.7 and §3.9 (stickers). Evidence: none.
Interop (vendor): Telegram Bot API — `getMe` reports `has_topics_enabled` when the bot's
"Threaded Mode" (BotFather → Bot Settings → Threads Settings) is on; messages inside a private-chat
topic carry `message_thread_id`; `createForumTopic(chat_id, name ≤128)` → `ForumTopic`
(`message_thread_id`, `name`) also works in a private chat with that mode on; `sendMessage` and
`sendChatAction` take `message_thread_id` there; `Sticker` has `file_id`, `file_unique_id` (stable
across bots), `emoji`, `set_name`, `is_animated`, `is_video`; static stickers download as WEBP
through `getFile`. Checked against the changelog as we know it (Bot API 9.x); live check owed.

What it does (own words): (1) DM topics. When `platforms.telegram.dm_topics` is on and the bot has
Threaded Mode, every topic of a private chat is its own conversation: the topic id becomes the
event's thread, so the session key gets a thread part and two topics are two sessions (the root
of the chat stays one more). `/topic [name]` in a private chat opens a new topic (named, else "New
chat") and greets in it; the person continues there. Without Threaded Mode the command says how
to turn it on. (2) Sticker descriptions. A static sticker is described once by the vision
describer the media intake uses (the side model) and remembered by its `file_unique_id` (LRU of
2,000, saved as JSON under the profile), so the same sticker again costs nothing; the agent gets a
one-line note with the emoji, the set and the description. Animated and video stickers get an
emoji-only note. Without a describer, or over the fresh-description budget, the note says the
sticker was not described.

Inputs → outputs: Telegram updates (private chat + `message_thread_id`; `sticker`) and settings
`platforms.telegram.dm_topics` (default false), `platforms.telegram.sticker_descriptions` (default
true) → events with `source.thread_id` set / sticker text; `/topic` → `createForumTopic` + a greeting
in the topic; `<home>/gateway/telegram_stickers.json` (0600 in 0700).

Files: `src/indus/gateway/adapters/telegram/topics.py`, `.../telegram/stickers.py`,
`tests/unit/gateway/adapters/telegram/test_topics.py`; hook points in `telegram/adapter.py`,
`telegram/__init__.py`, `telegram/bot_api.py` (`create_forum_topic`, `User.has_topics_enabled`,
sticker fields); shared: `telegram_wire.py`, `test_vendor_names.py`, traps / ACC rows (K24-3, K46-1).

Edge cases / failure modes:
- topic mode off in config, or on without Threaded Mode → a DM thread id is ignored (one session),
  `/topic` passes to the core (off) or answers with the BotFather hint (no Threaded Mode);
- forum supergroups keep their own topic rule (unchanged); General topic semantics untouched;
- `/topic` again within 10 s in one chat → refused politely (no topic spam); name cut to 128 chars;
  createForumTopic refused → a short "could not open a topic" reply, nothing else;
- a thread that Telegram deleted: the existing "thread not found" retry sends to the chat;
- sticker cache: same unique id twice → one describer call; concurrent arrivals of one sticker →
  one call; a corrupt or unreadable cache file → start empty (logged); a write failure is logged;
  2,001st entry evicts the least recently used; entries persisted in LRU order;
- describer failure → the fallback note (not cached, so a later arrival tries again);
- animated / video stickers never downloaded; a sticker over the 20 MB download cap is refused.

Our design differences / decisions:
- our names (`DmTopics`, `StickerNotes`, `StickerCache`); topic opening is an adapter command
  handled before the event reaches the core (no registry row: the slash registry is core), added
  to the private-chat command menu when the mode is active;
- no topic tables, bindings, renames, restore or lobby for DM roots (the reference's richer topic
  slice is research 03 §3.20; recorded as a follow-up): the thread id in the key is the binding;
- the describer is the intake's own (borrowed when the adapter registers its downloads), so the
  same side model and question are used; no separate vision prompt of ours beyond the intake's;
- descriptions run in the adapter, before the core's authorization (the reference also describes
  in the adapter, after its own allowlist check, which our core owns): bounded by the cache, by
  "DMs, or group messages addressed to the bot", and by a fresh-description budget per sender
  (10 an hour); the clean place is a core intake hook (proposed below).

Tests we will write: two DM topics → two session keys (and the root a third), mode off → one;
`/topic` opens a topic and greets in it, hint without Threaded Mode, cooldown; same sticker twice
→ one describer call, persisted and reloaded, LRU eviction, animated sticker never described,
describer failure not cached, budget per sender, menu entry.

## Decisions for the lead
- **Core seam proposed:** describe stickers after authorization — the media intake asks the
  platform for a sticker note (`FetcherSeat`-style registration `register_sticker_notes(platform,
  fn)` called from `enrich` for `MessageKind.STICKER`), so strangers never trigger a side-model
  call. Until then the adapter describes inline under the bounds above.
- `/topic` as a registry command routed to an adapter seam (`open_topic(source, name)`) after the
  authorization chain would replace the adapter-level command.
- DM root lobby, topic renames from session titles and the topic binding tables are not built.

Compare checklist (filled after the rewrite; reopened the files on the Read line):
- [x] a topic per DM thread in the session key (test: `test_topics.py::test_two_dm_topics_are_two_sessions`,
  `::test_without_the_mode_a_private_chat_stays_one_session`)
- [x] topic creation and its errors: Threaded Mode off → the BotFather hint, other refusals
  reported (`::test_without_threaded_mode_the_command_says_how_to_turn_it_on`,
  `::test_a_refused_topic_creation_is_answered`,
  `::test_another_refusal_is_reported_without_the_threaded_mode_hint`); a duplicate name is just
  another refusal here (the reference maps it from later messages)
- [x] stickers: cached by unique id (`::test_the_same_sticker_twice_costs_one_vision_call`),
  animated/video placeholder (`::test_animated_stickers_are_never_downloaded`), describe failure
  fallback not cached (`::test_a_failed_description_is_not_remembered`), LRU + bad file
  (`::test_the_cache_drops_the_least_recently_used_and_survives_bad_files`), concurrent arrivals
  (`::test_concurrent_arrivals_share_one_description`)
- [x] gap found: the reference's cache has no size bound — ours is an LRU of 2,000 (phase row)
- Deliberate differences: no configured topic list (`dm_topics` as a list of named topics with
  persisted thread ids) — the person opens topics with `/topic` or in Telegram itself and the
  thread id keys the session; no reference-style sticker prompt (the intake's describer question is
  used); the note text is ours; the sticker file is removed after describing.
- Not taken: topic bindings/renames/restore (research 03 §3.20), DM root lobby, handoff threads
  (`create_handoff_thread`, P17/P24 territory).

## Status

Done (2026-10-20). Hook points in the Telegram adapter, package, Bot API client and wire fake;
the core seams proposed above are with the lead.

Integration (2026-10-20): landed as built; the two core seams (sticker notes after authorization,
`/topic` as a registry command) are recorded follow-ups, not built (D-16.570).
