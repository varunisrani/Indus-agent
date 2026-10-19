# Task: Inbound event model and normalisation (P15-03) Phase: 15 Owner: wave-18 builder G1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/platforms/event.py` (all),
`gateway/platforms/base.py` (`coerce_plaintext_gateway_command` only), `gateway/rich_sent_store.py`
(all). Research 06 §3.4. Evidence: none.

What it does (own words): every adapter turns a platform message into one platform-neutral
`InboundEvent` whose `Source` says where it came from (platform, chat, chat type, sender, thread,
workspace scope, ...). Everything after the adapter (authorization, session keys, busy guards,
slash dispatch) reads only these two models. Command detection lives on the event: leading
attachment-reference lines (`@image:`, `@file:`, `@url:`, any case) are skipped, then a leading
`/` makes a command when the event allows control (`allow_control`, false for untrusted proactive
payloads). The name is the first word without the slash, lower-cased, with an `@botname` suffix
dropped; a name containing `/` (`/etc/hosts`) or an empty name is plain text. Phone keyboards turn
`--` into an em dash and `-` into an en dash, so the arguments get them back. Reply context: when a
user replies to a message, the replied-to text is prepended to the model text; platforms that do not
echo rich content back get it from a bounded in-memory index of what we sent (`SentIndex`), and a
reply to one of our own messages is marked.

Inputs → outputs: adapter fields → `InboundEvent` / `Source`; event → `command`, `command_args`,
`is_command`; (event, `SentIndex`) → event with reply context filled; event → model text with the
quote line.

Files: `src/indus/gateway/core/__init__.py`, `src/indus/gateway/core/events.py`,
`src/indus/gateway/core/sent_index.py`, `tests/unit/gateway/core/test_events.py`,
`tests/unit/gateway/core/test_sent_index.py`.

## API (G2 and later waves build on this; names are final unless the lead says otherwise)

`indus.gateway.core.events`:
- `ChatType = Literal["dm", "group", "channel", "thread", "forum"]`
- `class MessageKind(StrEnum)`: `TEXT, LOCATION, PHOTO, VIDEO, AUDIO, VOICE, DOCUMENT, STICKER, COMMAND`
  (values are the lower-case names).
- `class Source(BaseModel)` (frozen, extra keys refused): `platform: str`, `chat_id: str`,
  `chat_type: ChatType = "dm"`, `chat_name: str | None`, `user_id: str | None`,
  `user_name: str | None`, `thread_id: str | None`, `user_id_alt: str | None`, `is_bot: bool = False`,
  `scope_id: str | None` (Slack workspace), `guild_id: str | None`, `parent_chat_id: str | None`,
  `prospective_thread_id: str | None` (Discord auto-thread, P15-07).
- `class Attachment(BaseModel)` (frozen): `ref: str` (local path or URL), `mime: str = ""`,
  `inlined: bool | None = None` (text already inlined into the event text; None = unknown).
- `class InboundEvent(BaseModel)` (frozen; change with `model_copy(update=...)`): `source: Source`,
  `text: str = ""`, `kind: MessageKind = TEXT`, `message_id: str | None`, `update_id: int | None`,
  `media: tuple[Attachment, ...] = ()`, `reply_to_id: str | None`, `reply_to_text: str | None`,
  `reply_to_author_id: str | None`, `reply_to_author_name: str | None`,
  `reply_to_is_own: bool = False`, `internal: bool = False`, `allow_control: bool = True`,
  `reply_expected: bool | None = None`, `metadata: dict[str, Any] = {}`,
  `timestamp: datetime | None = None`.
  Properties: `user_id`, `user_name` (from `source`), `is_command`, `command` (`str | None`),
  `command_args` (`""` when not a command).
- `parse_command(text) -> ParsedCommand | None` (`ParsedCommand(name, args)`), ignoring
  `allow_control`; `strip_attachment_refs(text) -> str`; `normalise_dashes(text) -> str`.
- `with_reply_context(event, index) -> InboundEvent` fills `reply_to_text` from the index when the
  platform gave none and sets `reply_to_is_own` when the index knows the id;
  `model_text(event) -> str` = quote line + text (quote capped at `REPLY_QUOTE_MAX_BYTES` 4 KiB).

`indus.gateway.core.sent_index`:
- `class SentIndex(per_chat=2000, max_text_bytes=4096, max_chats=1000)`:
  `record(platform, chat_id, message_id, text)`, `lookup(platform, chat_id, message_id) -> str | None`,
  `is_own(platform, chat_id, message_id) -> bool`, `__len__`, `chats()`.

Edge cases / failure modes:
- empty text, text that is only refs, refs not at the very start (then not stripped), CRLF lines.
- `/` alone, `/@bot`, `//x`, `/etc/hosts`, `/Help@MyBot args`, upper-case names, tabs or newlines
  after the name; `allow_control` false → never a command.
- dashes only in arguments; a run of em dashes becomes one `--`.
- reply to an id the index evicted → no quote, not own; reply text from the platform wins over the
  index; quotes longer than 4 KiB are cut on a character boundary.
- index: text over 4 KiB in UTF-8 cut on a character boundary; per-chat LRU (lookups refresh);
  chats over the cap evict the least recently used chat.

Our design differences / decisions:
- One `media` tuple of `Attachment` instead of three parallel lists.
- Frozen Pydantic models (no in-place mutation; merges build new events).
- No process-local admission receipts, prompt-response, auto-skill, channel prompt/context fields:
  those belong to later tasks (P15-11, P15-13, P15-22); `metadata` carries anything else.
- The plaintext "restart gateway" rewrite is not taken: `indus gateway restart` is not a chat command
  in P15 and P15-25 forbids the agent stopping its own gateway through the guard env instead.
- The sent index is in memory only (no JSON file under the home): reply context after a gateway
  restart falls back to what the platform sends. Easy to persist later behind the same class.
- `timestamp` optional (adapters set the platform's own time); no default read of the wall clock.

Tests we will write:
- seeded property test: text whose first non-ref character is not `/` is never a command; any
  command name is lower-case, contains no `/` or `@`.
- table: `/Help@MyBot args` → `help`/`args`; `/etc/hosts` → text; refs then `/new`; `/` alone; em/en
  dash args; `allow_control` false.
- reply context from platform vs index; own-message mark; quote cap.
- index: LRU eviction per chat, lookup refreshes, text cap at a character boundary, chat cap.

## Decisions for the lead
- Sent index is in memory only (see above). Change: persist under `<home>/state/` in P15-17 if
  reply-after-restart matters.
- A bare `/` (or `/@bot`) is plain text, not an empty command.
- Quote line wording is ours: `[Replying to <who>: "<text>"]` then a blank line; `<who>` is "your
  earlier message" for own messages, else the author name or "a message".

Compare checklist (filled after the rewrite; reopened `gateway/platforms/event.py`,
`gateway/rich_sent_store.py`):
- [x] leading `@image:`/`@file:`/`@url:` lines skipped, any case, paths with spaces (`tests/unit/gateway/core/test_events.py::test_commands_are_detected`)
- [x] `/` + allow-control; lower-case; `@bot` dropped; names with `/` are text (`::test_commands_are_detected`, `::test_plain_text_is_not_a_command`, `::test_untrusted_events_never_run_commands`)
- [x] property: no `/` after stripping → never a command (`::test_property_text_not_starting_with_slash_is_never_a_command`, seeded generator: hypothesis is not installed)
- [x] em/en dashes in arguments (`::test_dash_normalisation`)
- [x] reply context from the platform or the index, own-message mark, quote cap (`::test_reply_context_from_the_index_and_own_mark`, `::test_platform_reply_text_wins_and_unknown_ids_are_not_own`, `::test_quote_is_capped_on_a_character_boundary`)
- [x] index LRU 2,000 per chat, 4 KiB text cap (`tests/unit/gateway/core/test_sent_index.py`)
- Deliberate differences: a bare `/` is text (the reference reports an empty command); `command_args` is `""` for plain text (the reference returns the whole text); a run of em dashes becomes one `--`; one `media` tuple instead of three parallel lists; the index is per chat, in memory, sized by the plan (the reference keeps 1,000 entries for all chats in a JSON file with 2,000-character texts).
- Not taken: the plaintext "restart gateway" rewrite (see above); the media part of the sent index (re-fetching a quoted attachment) — P15-23 can add `record_media` if needed; process-local receipt fields.
- Conventions every adapter follows (contract suite pins them): an edited message → `metadata["edited"] = True`; a button press → `metadata["button"] = <callback data>` with empty text; a mention of the bot → `metadata["mentioned"] = True` (the contract checks the first two).

## Status

Done (2026-10-19). `src/indus/gateway/core/{__init__,events,sent_index}.py`, `tests/unit/gateway/core/test_events.py`, `tests/unit/gateway/core/test_sent_index.py`. `with_reply_context` / `model_text` are ready for P15-11 (admission) and P15-17 (the sender records into `SentIndex`).
