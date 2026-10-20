# Task: IRC adapter (P16-27) Phase: 16 Owner: wave-24 builder E5

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `plugins/platforms/irc/adapter.py`
(whole file: connect/registration, line handling, splitting, standalone send),
`plugins/platforms/irc/plugin.yaml` (env names), `tests/gateway/test_irc_adapter.py` and
`tests/plugins/test_irc_inbound_identity.py` (test names and the same-millisecond id case).
Evidence: none.
Interop: RFC 1459 / RFC 2812 (512-byte lines with CRLF, `PASS`, `NICK`, `USER`, `JOIN`, `PRIVMSG`,
`PING`/`PONG`, `QUIT`, `ERROR`; numerics 001, 366, 401, 403, 404, 417, 432, 433, 436, 437, 464, 465,
471, 473–475); IRCv3 capability negotiation (`CAP LS 302` with `*` continuation lines, `CAP REQ`,
`ACK`/`NAK`, `CAP END`), `sasl` (`AUTHENTICATE PLAIN`, `+`, base64 in 400-byte pieces, 900/903 success,
902/904/905/906 failure), `message-tags` (`@k=v;k2` with `\:` `\s` `\\` `\r` `\n` escapes),
`server-time` (`time` tag), `echo-message` (our own messages come back; with `msgid`), `msgid`
(message-ids), `bot` tag (bot-mode), standard replies (`FAIL <command> <code> [context…]
:<description>`), CTCP `ACTION`. No library: asyncio streams from the standard library.

What it does (own words): one TLS connection (by default; port 6697, or 6667 when TLS is off) to
an IRC server, our nick registered, the configured channels joined. Capability negotiation asks for
`sasl` (only when an account is configured), `message-tags`, `server-time`, `echo-message`; SASL
PLAIN runs before registration ends. Passwords (server `PASS`, SASL, NickServ `IDENTIFY`) are sent
only over TLS unless `allow_plaintext_auth: true` is set; otherwise connecting fails as a fatal
configuration problem, and they never reach a log line or an error text. A taken nick gets `_`
appended, up to three retries. PRIVMSGs to our nick are DMs (chat id = the sender's nick); channel
messages are groups, with `mentioned` set (and the `nick:` / `nick,` prefix cut) when addressed to
us — whether unaddressed lines are answered is the gateway's mention policy. A `bot` tag marks
bot-authored lines; CTCP ACTION becomes `* nick text`, other CTCP is dropped; our own echoed
lines are never inbound. Outbound text is plain (markdown stripped by the sender), split per
line and then on UTF-8 boundaries so each payload is at most 450 bytes after `PRIVMSG <target> :`
(and never over the 512-byte line), with CR, LF and NUL removed. One line per 0.5 s per target
(the chat's send slot spacing). Each line is confirmed before the next: with `echo-message` the
server echoes it back (carrying its `msgid`), otherwise a `PING` after it is answered; an error
numeric or `FAIL` before that is the line's refusal (401/403 → chat not found, 404 → cannot send,
417 → too long, `FAIL … INVALID_UTF8` → bad format, `FAIL … RATE_LIMITED [seconds]` → rate limited,
anything else read by its text). No answer within 15 s → "maybe delivered" (never resent, R06.4).
A lost connection reconnects inside the adapter (2 s doubling to 60 s); a ban (`465`, `ERROR`
naming a K/G-line) is reported fatal. The token-lock key is server + nick (R06.16): two gateways
must never sit on one nick.

Inputs → outputs:
- `create_adapter(AdapterConfig)`: `IRC_SERVER`, `IRC_NICKNAME` (the lock credentials),
  `IRC_CHANNEL` (comma-separated) or `platforms.irc.channels`; optional secrets
  `IRC_SERVER_PASSWORD`, `IRC_SASL_PASSWORD` (+ `sasl_username`, default the nick),
  `IRC_NICKSERV_PASSWORD`; keys `port`, `use_tls` (true), `allow_plaintext_auth` (false),
  `realname`.
- `send(target, text)` → one or more PRIVMSG lines → `SendResult` with one id per line (`msgid`
  when the server gives one, else ours).
- server lines → `InboundEvent(chat_type dm|group, user_id=nick, metadata mentioned)`.

Files: `src/indus/gateway/adapters/irc/__init__.py`, `irc/adapter.py`, `irc/protocol.py` (line
parsing, tags, splitting), `tests/fakes/platform/irc_wire.py` (in-process fake server, no socket),
`tests/unit/gateway/adapters/irc/test_adapter.py`, `tests/contract/gateway/irc_case.py`.

Edge cases / failure modes:
- multibyte text split exactly at UTF-8 boundaries, never inside a character; a line that is only
  whitespace is skipped; a target with a space, comma, CR, LF or NUL is refused.
- nick in use 3 times → retried as `nick_`, `nick__`, `nick___`; a fourth refusal → retryable
  connect error.
- registration timeout 30 s → retryable; SASL failure → fatal; `464` (bad server password) fatal.
- server `PING` answered at once; nothing heard for 240 s → our `PING`; still nothing → reconnect.
- same text twice in one millisecond → two events with distinct ids.
- the password never in `received` logs; plaintext without opt-in → fatal before connecting.

Our design differences / decisions:
- Our names (`IrcAdapter`, `IrcLine`, `split_payload`).
- Per-line confirmation (echo or PING) gives real delivery ids and real failures; the reference
  writes and assumes success.
- SASL PLAIN and IRCv3 tags (the reference only has PASS and NickServ).
- Unaddressed channel lines reach the gateway with `mentioned` false (the core mention policy
  decides); the reference drops them in the adapter.
- Reconnect inside the adapter; the reference marks the platform failed on a lost connection.
- No standalone one-shot connection for `indus send`: `send_only` connects, registers with the
  configured nick + `-send`, joins nothing but the target channel, and quits after the send.

Tests we will write:
- contract suite (`irc_case.py`, lacks thread/edit/media) against the fake server.
- multibyte split at byte boundaries; 450-byte payload cap; CRLF injection removed.
- nick collision recovery (three `_`), fourth fails; pacing one line per 0.5 s per target with the
  fake clock; two targets interleave.
- SASL PLAIN exchange; password refused over plain TCP without opt-in; never in logs/errors.
- echo/PING confirmation; error numerics classified; silent server → maybe delivered.
- reconnect after a dropped connection; ban → fatal.

## Decisions for the lead
- Registry row: `_chat_entry("irc", "IRC", sdk=("ssl",), extra_name="irc",
  required_env=("IRC_SERVER", "IRC_NICKNAME"), max_len=450, ...)`, install hint "no extra";
  `BUILTIN_CREDENTIALS["irc"] = ("IRC_NICKNAME", "IRC_SERVER")`: the daemon joins the sorted
  credential values, so the lock key is nick + server (R06.16's canonical example). The channel is
  not a credential.
- `IRC_SASL_PASSWORD` is a new env name of ours (the reference has none).

Compare checklist (filled after the rewrite; reopened the files on the Read line):
- [x] payloads cut on UTF-8 boundaries within the per-target budget (Verify column; test:
  `test_adapter.py::test_multibyte_text_splits_at_byte_boundaries`,
  `::test_multibyte_lines_reach_the_server_whole`)
- [x] nick collision recovery (Verify column; test: `::test_nick_collision_recovers_with_underscores`)
- [x] pacing with the fake clock, other targets not held (Verify column; test:
  `::test_one_line_per_half_second_per_target`)
- [x] CR/LF/NUL never start a command; bad targets refused (test:
  `::test_line_breaks_and_control_characters_never_start_a_command`)
- [x] PASS / NickServ IDENTIFY after welcome, SASL PLAIN, password only over TLS unless opted in
  (test: `::test_sasl_plain_over_tls_and_the_password_stays_out_of_logs`,
  `::test_a_password_is_never_sent_over_a_plain_connection`)
- [x] PING answered, CTCP ACTION, other CTCP dropped, own messages dropped, addressed channel lines
  (`nick:`, `nick,`, `nick `), same text twice → two ids (test: `::test_inbound_lines_become_events`)
- [x] token-lock key server + nick (R06.16; test: `::test_the_token_lock_key_is_server_and_nick`)
- [x] standalone send = our send-only open: own nick, joins only the target, QUITs (test:
  `::test_indus_send_uses_its_own_nick_and_joins_only_the_target`)
- [x] gaps found at compare: the reference accepts `nick ` (space) as an address and lower-cases
  allowlisted nicks — fixed by the third address form and `user_id_alt` = the lower-case nick,
  pinned by `::test_inbound_lines_become_events`; `IRC_PORT` / `IRC_USE_TLS` from the environment
  now work too (`::test_create_adapter_reads_the_settings`)
- Deliberate differences: per-line confirmation (echo or PING) instead of fire-and-forget;
  unaddressed channel lines reach the core's mention policy instead of being dropped in the adapter;
  nick retries stop after three (phase row) where the reference counts up without end; a lost
  connection is reopened inside the adapter; 0.5 s per line per target (phase row) instead of a
  global 0.3 s sleep; the line budget counts the 512 bytes with CRLF (the reference uses 510);
  SASL PLAIN added; the password-over-plaintext refusal is ours.
- Not taken: a default nick (the nick is a credential here), the reference's interactive setup
  (P16-31), IRCv3 `account-tag` identity (follow-up: a services account would be a better
  `user_id_alt` than the lower-case nick).

## Status

Done (2026-10-20). New files only (plus the shared contract changes listed in `line.md` and the
R06.11 / R06.16 trap rows). No new dependency (standard library only), no schema rows, no pyproject
extra.
