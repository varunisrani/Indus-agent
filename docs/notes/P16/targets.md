# Task: cron and send target parsers per platform (P16-32) Phase: 16 Owner: wave-27 builder E11

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/platforms/send_targets.py`
(the parser contract: explicit `(chat, thread)`, fall through, or "not explicit"),
`plugins/platforms/matrix/send_targets.py`, `plugins/platforms/feishu/send_targets.py`,
`plugins/platforms/buzz/send_targets.py`, `plugins/platforms/slack/send_targets.py` (shape only), plus
`plugins/platforms/discord/send_targets.py` (shape only) and a grep of which other adapters declare a
parser (signal, ntfy, wecom, telegram, email, weixin, whatsapp, photon, yuanbao: names only).
Evidence: none. Our own sources read: the builders' notes in `docs/notes/P16/` (chat id shapes),
`src/indus/cron/targets.py` (P14 grammar and the `register_platform(splitter=)` seam), the router and
channel directory (P15-20), and each landed adapter's chat-id handling.

What it does (own words): a `deliver` value or an `indus send` target is `platform:address`. P14's
default split cuts the address at its first `:` into chat and thread, which is wrong for every
platform whose ids contain `:` (Matrix `!room:server`, Teams `19:x@thread.tacv2`, QQ `group:<id>`,
SimpleX `group:<id>`, DingTalk `user:<staffId>`) and accepts nonsense for the rest. This task gives
every landed platform its own rule: the shapes its adapter really sends to, how a thread is written,
and a clear message for anything else. Each rule is a splitter registered with P14's seam, so the one
grammar (cron jobs, `indus send`, webhook routes, Graph forwards) parses `matrix:!ops:example.org:$ev`
as room `!ops:example.org`, thread `$ev`, and refuses `sms:12345` before a job is ever saved.

A chat written as `#name` is a friendly name: it stays as written and the router resolves it through
the channel directory at send time (P15-20), on every platform. On Matrix `#alias:server` is kept
whole (an alias contains `:`), so `matrix:#ops:example.org` reaches the directory as one name. Names
on Mattermost written without `#` (`mattermost:town-square`) are turned into the `#` form because
the adapter only sends to channel ids.

Inputs → outputs: `parse_target(platform, address) -> (chat, thread)` or `TargetError` (a
`ValueError`) naming the accepted forms; `validate_target(platform, chat, thread) -> problem | None`
for already split values (home channels, a stored origin); `render_target(platform, chat, thread)`
(the canonical token, equal to P14's `DeliveryTarget.render()`); `install() -> undo` registers every
rule with `cron.targets.register_platform(name, splitter=...)` (idempotent); `RULES` (platform →
rule with its help text and examples). Round trip: for every accepted address,
`parse(render(parse(a))) == parse(a)`.

Rules (chat; thread):
- telegram: integer chat id (negative for groups) or `@channelname`; thread = forum topic number.
- discord: channel snowflake (15–22 digits); thread = thread snowflake.
- slack: conversation `C…`/`G…`/`D…` or user `U…`/`W…` (opened as a DM), `<@U…>` accepted; thread =
  message ts (`1700000000.000100`).
- signal: E.164 number, account UUID, or `group.<base64 id>`; no thread.
- matrix: `!room:server`, `#alias:server`, `@user:server` (server may carry a port); thread = an
  event id `$…`, after the last `:$`.
- mattermost: 26-character channel id, or a channel name (→ `#name`); thread = root post id.
- email: one address, lower-cased; no thread, no display names, no lists.
- whatsapp_cloud: the customer's phone number (`+` and separators dropped, 6–15 digits); no thread.
- sms: E.164 (`+` then 7–15 digits); no thread.
- line: `U`/`C`/`R` + 32 hex; no thread.
- ntfy: topic `[-_A-Za-z0-9]{1,64}`; no thread.
- irc: `#channel` / `&channel` or a nick; no thread.
- teams: a conversation id (`19:…@thread.tacv2`, `a:…`, `29:…`, `8:orgid:…`); thread = the root
  message id, written `;messageid=<n>` (as Teams does) or `:<n>` (as P14 renders it) when what is
  left still contains `:`.
- google_chat: `spaces/<id>`; thread = `spaces/<id>/threads/<id>` or just the thread's own id (made
  full); `spaces/A/threads/B` alone means space A, thread B; a thread of another space is refused.
- qqbot: a user openid (C2C), `group:<openid>`, `channel:<id>`, `dm:<guild id>` (`c2c:<openid>` is
  written bare); no thread.
- dingtalk: an open conversation id (`cid…`) or `user:<staffId>`; no thread.
- wecom: a user id or a group chat id; no thread. wecom_callback: a member user id; no thread.
- feishu: `oc_` / `ou_` / `on_` / `chat_` / `open_` ids with an optional thread id; document comment
  chats `cmt_<type>_<token>` with the comment id (or `whole`) as thread.
- bluebubbles: a chat GUID (`iMessage;-;+15551234567`, `iMessage;+;chat…`) or a phone number / email
  address the adapter looks up; no thread.
- simplex: a contact id (digits) or `group:<digits>`; no thread.
- buzz: a channel UUID with an optional thread (64-hex event id), or a member's hex pubkey / `npub1…`
  (a DM; npub checked with the bech32 checksum from `_identity`); no thread on a DM.

Not targets (no rule, never registered): `api_server`, `webhook`, `msgraph` (D-16.443).

Files: `src/indus/gateway/adapters/targets.py`, `tests/unit/gateway/adapters/test_targets.py`;
shared: `src/indus/cron/targets.py` (`whatsapp_cloud` added to `KNOWN_PLATFORMS`, as the E4/E8/E10
notes asked).

Edge cases / failure modes:
- empty address after `:` — P14 reports it (`has no chat id`); our rules never see it.
- `#name` with a thread: `discord:#general:123` → (`#general`, `123`); the thread still has to fit
  the platform's thread rule.
- ambiguous shapes, decided and pinned: Teams `a:1234567` has no thread (cutting `:1234567` would
  leave `a`, not a conversation); Matrix `!r:example.org:8448` is a room on port 8448, not a thread;
  a 26-character lower-case Mattermost name is taken as an id; Telegram `@name` is a channel
  username, not a directory name; QQ `foo:bar` is refused (no such prefix, and QQ ids hold no `:`).
- whitespace inside an address, control characters, a comma (P14 splits on commas first) → refused.
- a splitter error never escapes: P14 turns it into a `bad_address` problem with our message.

Our design differences / decisions:
- One table module (`targets.py`) instead of a parser file per adapter folder, as the phase row
  names; the rules depend on nothing but `re` and `_identity`, so importing it never imports an SDK.
- Strict shapes everywhere (the reference falls back to "send it and let the platform decide" for
  unknown shapes); a cron job with a malformed address fails when it is saved, not at 3 a.m.
- Canonical forms (lower-cased email, digits-only WhatsApp numbers, bare QQ C2C ids, full Google
  Chat thread names, Slack `<@U…>` → `U…`) so duplicate targets collapse in P14's dedupe.
- `install()` is called by whoever parses targets in the gateway process (wiring for the lead,
  below); the module does not register itself at import.

Tests we will write: a table per platform (valid → expected (chat, thread); invalid → refused with
the platform's message; ambiguous → the decided reading), the round trip through P14's
`parse_targets` + `render()` for every valid row, `install()` idempotent with a working undo,
P14's default splitter kept for platforms without a rule, `whatsapp_cloud` known without install,
`matrix:#ops:example.org` resolving through the channel directory via `DeliveryRouter.resolve`, and
`validate_target` for split values.

## Decisions for the lead
- **Wiring (core change proposed, not made — P16 rule):** call
  `indus.gateway.adapters.targets.install()` once in the gateway process before any target is parsed.
  Proposed place: `src/indus/gateway/delivery/__init__.py` (imported by `send_command`, `router` and
  `parts`, so `indus send`, the daemon's deliverers and webhook/Graph forwards all see the rules):
  `from indus.gateway.adapters.targets import install as _install_targets` + `_install_targets()`.
  The CLI side (`indus cron create`, the `cronjob_manage` tool) cannot import the gateway (layer rule)
  and keeps P14's default split; the stored token is the text as written, so a job saved there is
  re-parsed correctly by the gateway that delivers it. Validation at save time from the CLI would need
  the rules below the gateway layer (e.g. moved to `indus.state` or `indus.cron`) — lead call.
- **Router follow-up (core):** `DeliveryRouter.resolve` sends every `#…` chat to the directory. For IRC
  (`irc:#ops` is the real channel) and Matrix aliases not yet in the directory this gives
  `unknown_channel`. Proposed: when the directory has no match and `targets.hash_is_native(platform)`
  is true, use the chat as written. `hash_is_native` is provided.
- `PlatformEntry.cron_target_parser` (registry) stays unused: P14's `register_platform` is the one seam.
- The WhatsApp bridge (`whatsapp`, P16-02/03), Weixin, Yuanbao, Photon and Raft are not landed; they
  get their rule with their adapter (`whatsapp` can use `_identity.to_whatsapp_jid`).

Compare checklist (filled after the rewrite; reopened the five `send_targets.py` files and the base):
- [x] Matrix thread after the last `:$`, room ids keep their `:` (test: `test_targets.py::test_valid_rows`)
- [x] Feishu `oc_/ou_/on_/chat_/open_` ids with an optional thread (`::test_valid_rows`)
- [x] Buzz UUID channels are explicit targets (`::test_valid_rows`)
- [x] Slack `C/G/D` ids, thread ts, `U…` users, `<@U…>` mentions (`::test_valid_rows`)
- [x] Discord numeric channel and thread (`::test_valid_rows`)
- [x] names that are not ids go to the channel directory (`#name` on every platform;
  `::test_hash_names_stay_directory_names`, `::test_matrix_alias_resolves_through_the_directory`)
- Deliberate differences: the reference's parsers return "not explicit" and let generic rules or the
  platform decide; ours refuse a shape the adapter cannot send to, with the accepted forms in the
  message. Slack `@handle` → users.list lookup is not taken (our Slack adapter has no name lookup; use
  `#name` through the directory or the `U…` id). Slack thread shorthand kept (`C…:<ts>`).
- Not taken: the reference's standalone-send policies (media routes that bypass the live adapter):
  ours always sends through the adapter's send-only open (P15-20).

## Status

Done (2026-10-20). Rules for all 22 landed chat platforms, `install()`, tests. Shared edit:
`cron/targets.py` `KNOWN_PLATFORMS` gains `whatsapp_cloud`. Wiring of `install()` and the router
change for native `#` ids are proposed to the lead (above).
