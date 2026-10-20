# Task: Yuanbao platform tools (P16-22) Phase: 16 Owner: wave-27 builder E14

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `plugins/platforms/yuanbao/yuanbao_sticker.py`
(all: the sticker catalogue shape, lookup by name / id, fuzzy search scoring, the face element),
`plugins/platforms/yuanbao/register.py` (tool list, platform hint), `tools/yuanbao_tools.py` (the
five tools, their arguments and answers, recipient resolution for DMs). Evidence: none.

What it does (own words): five model tools for Yuanbao sessions, in the `yuanbao` toolset that
only Yuanbao chats get (bundle `indus-yuanbao`, R02.25):
- `yuanbao_query_group_info`: the current group's name, owner and member count;
- `yuanbao_query_group_members`: the current group's members (list, find by name, list bots/admins),
  a page at a time;
- `yuanbao_send_dm`: a private text message to one member of the current group, found by user id or
  name; refused for anyone outside that group, checked by the gateway's own authorization chain
  (the recipient must be someone the gateway would accept a DM from), and only after a person
  approves it (R02.26 spirit: the model never gets a general send tool);
- `yuanbao_search_sticker`: search the built-in sticker catalogue by keyword (Chinese or English);
- `yuanbao_send_sticker`: send one catalogue sticker (by name or id; empty = a random one) to the
  current chat.
They act through the running Yuanbao adapter of the same profile (Yuanbao needs the live signed
socket; there is no REST client to build), so they answer "not connected" when it is down. Group
names, nicknames and name cards are written by other people: answers are fenced as untrusted
data, each field and each answer is size-capped.

Inputs → outputs: tool arguments + the current chat (the gateway identity bound for the turn:
platform, chat id, user, session) → JSON text answers (fenced for the reading tools); DMs and
stickers through the adapter's normal send path.

Files: `src/indus/gateway/adapters/yuanbao/tools.py`, `src/indus/gateway/adapters/yuanbao/stickers.py`
(shared with P16-21), `tests/unit/gateway/adapters/yuanbao/test_tools.py`.

Edge cases / failure modes:
- Not in a Yuanbao chat (CLI, another platform) → every tool answers an error, nothing is sent.
- Group tools in a DM → "only in a Yuanbao group chat".
- `yuanbao_send_dm`: empty text, text over 10,000 characters, a name matching nobody or several
  members (the candidates are listed, nothing sent), a user id not in the current group, a
  recipient the authorization chain refuses, approval denied / timed out / nobody to ask (cron)
  → refused with the reason and nothing sent. Credential-shaped text in the message is masked.
- `yuanbao_send_sticker`: unknown sticker → refused with a hint to search first.
- Member lists larger than a page: `offset` / `next_offset`; answers trimmed to the cap with a
  "more not shown" note.

Our design differences / decisions:
- Names spelled out (`yuanbao_*`), per the glossary.
- Every tool works on the current chat only (no `chat_id` / `group_code` argument): a group tool
  never reads another group the bot is in, and the sticker tool cannot post elsewhere.
- `yuanbao_send_dm` is text-only (no local files: sending files from this machine to a person is
  an exfiltration path) and needs a person's approval each time unless approved for the session
  (pattern key per recipient).
- The sticker catalogue holds the vendor's sticker ids and names (interop facts) with our own
  keyword lists; our search ranks exact, prefix, substring and shared-character matches.

Tests we will write:
- tools only in their bundle (absent from every other bundle, present in `indus-yuanbao`, gated to
  Yuanbao sessions even with every toolset selected); registration in the daemon registry
  (stand-in rows until the lead lands them);
- DM to a user outside the group refused; recipient refused by the authorizer; approval requested
  (a broker sees one request) and a denial sends nothing; an approved DM reaches the fake as C2C
  with the group code;
- reading tools fenced and capped; sticker search and send; not-connected answers.

## Decisions for the lead
- Toolset rows (`yuanbao`, `indus-yuanbao`, `indus-yuanbao` in `indus-gateway`), the
  `yuanbao_` untrusted prefix and the `gateway_tool_registry` line are core edits: exact rows in
  the report (stand-ins in `_stubs_e14.py`).
- The current chat comes from the gateway identity (`core.admission.current_identity`), bound
  before the turn starts; if P17-22 moves turns onto the session host in a task that does not
  inherit that context, the tools need a session-id → route lookup instead.
- The tools build the gateway's `Authorizer` for the profile the same way the daemon does
  (registry policy, secret store/env, `platforms.yuanbao` extra, pairing store, the live adapter);
  the daemon could pass its own instance instead (`YuanbaoTools(authorizer_for=...)`).

Compare checklist (filled after the rewrite; reopened `tools/yuanbao_tools.py`,
`plugins/platforms/yuanbao/yuanbao_sticker.py`, `register.py`):
- [x] the five tools and their arguments (names spelled out; group tools act on the current
  group) (test: `test_tools.py::test_reading_tools_are_fenced_capped_and_for_the_current_group`)
- [x] DM recipient by id or partial name, several matches → candidates, nothing sent
  (`::test_a_dm_asks_for_approval_and_a_denial_sends_nothing`)
- [x] the reference refused DMs to users its access policy would not admit — here the gateway's
  own `Authorizer` (`::test_a_recipient_the_gateway_would_not_accept_gets_nothing`)
- [x] sticker lookup by id / name / keyword, random sticker, face element data
  (`::test_sticker_search_and_send_to_the_current_chat`, `test_adapter.py::test_stickers_go_as_face_elements`)
- [x] not connected → a clear answer (`::test_tools_say_so_when_the_bot_is_not_connected`)
- Deliberate differences: no `chat_id` / `group_code` arguments (current chat only); DMs need a
  member of the current group, the gateway's authorization and a person's approval; text only (no
  media files); members are listed as member / admin / owner (the vendor's role field) instead of
  user kinds; our own sticker keywords and ranking.
- Not taken: the reference's "list bots" action (the member answer has no reliable bot flag),
  `reply_to` on stickers.

## Status

Done (2026-10-20). Core rows (toolsets, untrusted prefix, daemon registry line) are for the lead:
report and `tests/unit/gateway/adapters/yuanbao/_stubs_e14.py` (`install_rows`).

Landed at integration (wave 27): toolsets `yuanbao` / `indus-yuanbao` (in `indus-gateway`), the `yuanbao_` untrusted prefix and the daemon registration are in the core files (D-16.535); `_stubs_e14.py` removed.
