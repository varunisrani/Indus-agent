# Task: Email adapter (P16-09) Phase: 16 Owner: wave-23 builder E3

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `plugins/platforms/email/adapter.py`
(whole file), `plugins/platforms/email/plugin.yaml`, `plugins/platforms/email/gateway_env.py`,
`website/docs/user-guide/messaging/email.md`; test names only in `tests/gateway/test_email.py`,
`test_email_robustness.py`, `test_email_charset_fallback.py`. Research 06 §3.3 (Email row),
§3.5 (unknown senders), R06.8.
Evidence: none.

Vendor API: IMAP4rev1 (RFC 3501; `UID SEARCH`, `UID FETCH`, `UID STORE`, `BODY.PEEK[HEADER.FIELDS]`
with a partial range, optional `IDLE` RFC 2177 and `ID` RFC 2971) and SMTP (RFC 5321, STARTTLS
RFC 3207, AUTH), through the Python standard library only (`imaplib`, `smtplib`, `email`,
`html.parser`, `ssl`). Sender authentication reads the receiving server's
`Authentication-Results` header (RFC 8601) and DMARC relaxed alignment (RFC 7489). No third-party
package, so no extra and nothing to pin. `IMAP4.idle()` exists from Python 3.14; on 3.13 the
adapter polls.

Capability flags: `max_len` 50,000 code points, `splits_natively` false, `supports_edit` false,
`supports_delete` false, `supports_buttons` false, `owns_access_policy` true (own DM policy:
`allowlist`, or `pairing` when opted in), `auth_upstream` false, `dm_is_private` true,
`push_capable` true, `markup` plain.

What it does (own words): people write to the agent's mailbox and get replies by mail. One IMAP
session stays logged in; new mail is noticed with IDLE when both the server and Python offer it,
else by a check every 60 s (`poll_seconds`). Mail already in the inbox when the adapter first starts
is remembered as seen and never answered. For every new message the adapter first downloads only a
few headers (at most 64 KiB): From (at most 2,048 characters, else dropped), the
Authentication-Results header, the auto-reply and list headers and the size. It drops the message —
without downloading the body, and marking it read — when it is from our own address, from an
automated sender (`Auto-Submitted` other than `no`, `Precedence: bulk/list/junk/auto_reply`,
`List-Id`, `List-Unsubscribe`, or a noreply/mailer-daemon/postmaster/bounce-style local part), when the sender is unknown and email pairing was not opted into, or when the sender's
From domain is not authenticated by our receiving server (see below). Only then is the full message
fetched (which marks it read) and parsed: the first plain-text part, else the HTML part turned into
text (tags dropped, script/style dropped, entities decoded; nothing remote is ever loaded), the
subject in front of the text unless it is a reply, and attachments held in memory for the intake.

Unknown senders (R06.8): email's answer to a stranger is always "ignore" unless
`platforms.email.extra.unauthorized_dm_behavior` is `pair` or `decline`. A sender is "known" when it
is on `INDUS_EMAIL_ALLOWED_USERS`, `INDUS_GATEWAY_ALLOWED_USERS` or `platforms.email.extra.allow_from`
(whole addresses, case-insensitive; a bare local part never matches), when an allow-all switch is on
(`INDUS_EMAIL_ALLOW_ALL_USERS`, `INDUS_GATEWAY_ALLOW_ALL_USERS`), or when a pairing approval exists.
The adapter's own DM policy is `allowlist` (so the core's unknown-sender mode resolves to ignore),
`pairing` when opted in.

Sender authentication: From is written by the sender, and every grant and every reply keys on it,
so a forged From would put a stranger's text into an allowlisted person's conversation. The adapter
trusts From only when the topmost `Authentication-Results` header names exactly our pinned
authserv-id (`platforms.email.extra.authserv_id` or `EMAIL_AUTHSERV_ID`) and reports DMARC pass
(with a matching `header.from`), or exactly one SPF pass whose `smtp.mailfrom` domain aligns, or a
DKIM pass whose `header.d` aligns with the From domain (relaxed alignment: equal or one is a
sub-domain of the other). Comments and quoted strings are handled so text inside them cannot fake
a verdict. Without a pin every message fails (logged once per adapter with the fix); operators whose
server stamps no such header opt out with `platforms.email.extra.require_authenticated_sender:
false`.

Replies: one SMTP connection per send (connect timeout 30 s; implicit TLS on port 465, STARTTLS
otherwise, plain only when asked; certificates verified unless `smtp_tls_verify: false`, which
warns for non-loopback hosts), login, one plain-text UTF-8 message: To the chat id (one address,
validated), Subject `Re: <last subject from that sender>` (no doubled `Re:`; "Indus Agent" when
none is known), `In-Reply-To` = the message replied to (the `reply_to` id, else the last message
from that sender), `References` = that message's references plus its id (at most 20, only
well-formed `<id>` tokens), a fresh `Message-ID` `<indus-<hex>@<our domain>>` which is the send's
message id. Session key per sender address (chat id = the lower-cased address, chat type `dm`).
Files go out as attachments (`send_document`, `send_image`, … each one mail with the caption as
body).

Inputs → outputs: `EMAIL_ADDRESS`, `EMAIL_PASSWORD`, `EMAIL_IMAP_HOST`, `EMAIL_SMTP_HOST`
(secret store, then env; address and hosts may also come from `platforms.email.extra.address`,
`imap_host`, `smtp_host`), `EMAIL_IMAP_PORT` (993), `EMAIL_SMTP_PORT` (587), `EMAIL_AUTHSERV_ID`,
`platforms.email.extra` (`imap_security`, `smtp_security` = `tls` | `starttls` | `plain`,
`imap_tls_verify`, `smtp_tls_verify`, `poll_seconds`, `authserv_id`,
`require_authenticated_sender`, `skip_attachments`, `allow_from`, `unauthorized_dm_behavior`)
→ `EmailAdapter`; IMAP messages → `InboundEvent`s; sends → `SendResult` with the Message-ID.

Files: `src/indus/gateway/adapters/email/__init__.py`, `src/indus/gateway/adapters/email/adapter.py`,
`src/indus/gateway/adapters/email/parsing.py` (header, body and authentication parsing),
`tests/fakes/platform/email_wire.py`, `tests/unit/gateway/adapters/email/__init__.py`,
`tests/unit/gateway/adapters/email/test_adapter.py`, `tests/unit/gateway/adapters/email/test_parsing.py`,
`tests/contract/gateway/email_case.py`. Shared: `tests/contract/gateway/conftest.py`,
`tests/contract/gateway/test_adapter_contract.py`, `tests/contract/gateway/test_vendor_names.py`
(wire registration), traps ledger row R06.8.

Edge cases / failure modes:
- missing address/password/host → fatal config error naming the missing names (never values);
- IMAP login refused for credentials → fatal (not retried); other IMAP/SMTP failures → retryable;
  an IMAP error while polling drops the session and reconnects on the next cycle;
- unknown or garbage charsets, malformed RFC 2047 words → decoded with replacement characters,
  never an exception; one unparseable message never stops the batch;
- headers over 64 KiB or From over 2,048 characters → dropped unread;
- a message larger than 50 MiB (declared size) → dropped with a log line;
- header injection: CR/LF in subject or ids is removed; To must be one plain address;
- a send that fails after the message was handed to the server (timeout, disconnect mid-DATA) is
  "maybe delivered", never re-sent (R06.4); refusals (5xx recipient) are definite.
- the password never appears in logs, errors or reprs.

Our design differences / decisions:
- One persistent IMAP session (with IDLE where available) instead of a fresh login every 15 s;
  60 s poll default (plan) instead of 15 s.
- A reply over 50,000 characters: the P15 sender splits it into ≤ 50,000-character mails with
  `(i/n)` markers, all threaded; the plan's "longer → attachment" needs a core switch (the sender
  splits before the adapter sees the text) — proposed below, not built.
- Inbound attachments are kept in memory (bounded: 32 per message, 64 MiB in all, 30 min) and
  handed to the intake through `fetch_attachment` (registered with `register_fetcher`); audio
  attachments make the message a voice message so the intake transcribes them.
- No IPv4 fallback for SMTP and no NetEase-specific workaround beyond sending IMAP `ID` when the
  server advertises it.
- No standalone sender: `indus send` builds the adapter with a send-only open.

Tests we will write:
- contract suite over in-process IMAP/SMTP fakes (`email_case.py`);
- auto-reply, bulk, list and noreply mail dropped without a body fetch and marked read;
- unknown sender ignored by default (no body fetch, no event), paired when opted in;
- forged From (no or untrusted Authentication-Results) dropped; pinned DMARC/SPF/DKIM verdicts;
- reply carries `In-Reply-To`, `References`, `Re:` subject, our Message-ID;
- first connect skips existing mail; reconnect keeps the seen set; IDLE wakes a fetch;
- HTML-only body converted; charset fallbacks; 64 KiB header cap; From length cap;
- SMTP error classification (refused recipient, policy refusal, rate limit wait, timeout);
- send-only open never touches IMAP; secrets never in errors or reprs.

## Decisions for the lead
- Registry row (core, not edited): `email` entry with `check_fn=lambda: True` (stdlib),
  `required_env=("EMAIL_ADDRESS", "EMAIL_PASSWORD", "EMAIL_IMAP_HOST", "EMAIL_SMTP_HOST")`,
  `max_len=50000`, plus `BUILTIN_CREDENTIALS["email"]` with the same four names. Until then the
  platform starts with `platforms.email.enabled: true` and the adapter reads the names itself.
- Core follow-up for R06.8: `Authorizer.unknown_dm_mode` lets a non-default
  `gateway.unauthorized_dm_behavior` (e.g. `decline`) win over an adapter's own DM policy. The
  email adapter already drops unknown senders before they become events, so strangers never get a
  decline mail; the core order should still put "email → ignore" before the global setting.
- Core follow-up: a `Capabilities` switch for "long text as one message with an attachment" so the
  sender does not split email replies.
- Send-only open: `adapter.inbound_enabled = False` before `connect()`; fetcher registration as for
  Signal.
- Allowlist entries are compared lower-cased by the adapter's pre-check, but the core chain
  compares exactly: write addresses lower-case in `INDUS_EMAIL_ALLOWED_USERS`.

Compare checklist (filled after the rewrite; reopened `plugins/platforms/email/adapter.py`):
- [x] missing settings named, never retried (test: `test_adapter.py::test_connect_errors_and_secrets`)
- [x] IMAP login refusal fatal, other failures retryable; SMTP probe at connect, its refusal fatal
  and the mailbox closed again (same test)
- [x] IMAP `ID` only when advertised (contract run; fake advertises `ID`)
- [x] first start marks existing mail seen; a reconnect keeps the list, also across the fresh
  adapter the gateway builds per reconnect (tests: `::test_mail_already_in_the_inbox_at_first_start_is_never_answered`,
  `::test_a_reconnect_keeps_the_seen_list_and_reads_outage_mail`,
  `::test_a_fresh_adapter_built_for_a_reconnect_reads_outage_mail`) — gap found: the gateway
  builds a new adapter per reconnect, so the seen list lives in a per-process snapshot.
- [x] seen list capped (2,000, keep the newest half); a renumbered mailbox clears it (test:
  `::test_a_renumbered_mailbox_clears_the_seen_list`)
- [x] screening on a bounded header fetch before any body download; dropped mail marked read
  (tests: `::test_automated_mail_is_dropped_unread_and_marked_read`,
  `::test_noreply_senders_and_our_own_address_are_dropped`, `::test_header_and_from_size_caps`)
- [x] unknown senders ignored unless pair/decline opted in; lists, allow-all switches and pairing
  approvals (tests: `::test_unknown_senders_are_ignored_by_default_without_a_body_fetch`,
  `::test_unknown_senders_reach_the_gateway_when_pairing_is_opted_in`,
  `::test_senders_known_through_env_lists_allow_all_or_pairing`,
  `::test_the_global_allow_all_switch_counts_only_without_env_lists`)
- [x] sender authentication: topmost header only, exact pin, DMARC / single SPF / DKIM alignment,
  comments and quotes cannot smuggle verdicts, missing pin logged once (tests:
  `test_parsing.py::test_sender_authentication`, `test_adapter.py::test_a_forged_from_is_dropped`,
  `::test_without_a_pin_nothing_is_trusted_and_the_fix_is_logged_once`,
  `::test_the_authentication_check_can_be_switched_off`)
- [x] From parsing: display names with brackets, folding, unquoted commas, size and nesting caps
  (test: `test_parsing.py::test_sender_address`) — gap found: a From with two `From` fields;
  dropped now.
- [x] charsets never raise; RFC 2047 decoding (tests: `test_parsing.py::test_decoding_never_raises`,
  `::test_broken_charset_body_does_not_raise`, `test_adapter.py::test_a_poison_message_does_not_stop_the_batch`)
- [x] plain body first, HTML converted, attachments and inline parts, `skip_attachments` (tests:
  `test_parsing.py::test_bodies`, `::test_html_becomes_text_without_scripts_styles_or_remote_content`,
  `test_adapter.py::test_subject_html_and_attachments`, `::test_skip_attachments`)
- [x] subject in front unless a reply (test: `::test_subject_html_and_attachments`)
- [x] a mid-batch failure still hands on what was fetched; the rest stays unread (test:
  `::test_mail_fetched_before_a_mid_batch_failure_is_still_delivered`)
- [x] a failed check drops the session and logs in again (test: `::test_a_failed_check_drops_the_session_and_logs_in_again`)
- [x] reply threading: `In-Reply-To`, `References`, `Re:` without doubling, our Message-ID
  domain (test: `::test_a_reply_is_threaded_under_the_message_it_answers`)
- [x] files as attachments; a missing file fails cleanly (test: `::test_files_go_out_as_attachments`)
- [x] TLS modes and verification switches with warnings (factories; `::test_create_adapter_reads_secrets_then_config`)
- [x] SMTP connection always released (quit, else close) (`_deliver_now`, `_smtp_probe`)
- [x] IDLE wakes a fetch (test: `::test_idle_wakes_a_fetch_when_mail_arrives`)
- [x] contract suite (`tests/contract/gateway/test_adapter_contract.py`, case `email`)
- Deliberate differences: see "Our design differences" above; also: SMTP failures are
  classified (recipient refused → chat gone, 5.7.x → forbidden, 4xx → retry or rate limit with a
  stated wait, 552 → too long, a break after the message was handed over → maybe delivered); a
  refused SMTP login stops the retry loop; replies carry `Auto-Submitted: auto-replied`
  (RFC 3834) so other robots do not answer them; an email with no subject, body or attachment is
  ignored (the reference turns it into "(empty email)"); `X-Auto-Response-Suppress` alone does not
  drop a message (Exchange sets it on ordinary mail).
- Not taken: SMTP IPv4 fallback, the standalone sender, multi-image mails, the reference's
  allowlist local-part guard (our chain compares whole entries), poll-failure escalation to the
  gateway (no adapter-side hook in the core yet; the adapter retries with back-off and logs).

## Decisions for the lead (after compare)
- Runtime failure signal: the core has no way for an adapter to report "my mailbox keeps
  failing" after connect (the reference raises a fatal error into its reconnect watcher). Proposed:
  an `Adapter.report_failure(code, message, retryable)` hook the daemon turns into a platform state.
- Token lock: all four credentials joined key the lock; keying on `EMAIL_ADDRESS` + IMAP host
  would be enough.

## Status

Done (2026-10-19). Adapter, parsing module, wire fakes, unit tests and contract case are in; the
contract suite passes for `email`. Outside the task's files (shared): `tests/contract/gateway/conftest.py`,
`tests/contract/gateway/test_adapter_contract.py`, `tests/contract/gateway/test_vendor_names.py`,
traps ledger R06.8. Registry row, credentials row, send-only switch, fetcher registration and the
R06.8 core ordering are handed to the lead (core files).

Landed (integrator, 2026-10-20; D-16.381..383, D-16.387..391, D-16.393): `inbound_enabled`
replaced by `send_only` read from `AdapterConfig.send_only`; `register_fetchers(seat)` registers
`fetch_attachment`; the proposed `report_failure` became the base `report_fatal`: a login the IMAP
server refuses after connect reports `email_auth` and stops the mailbox loop; registry row and
`BUILTIN_CREDENTIALS["email"] = ("EMAIL_ADDRESS", "EMAIL_PASSWORD")` added (hosts are not
credentials); the lock keys on address + password (address + IMAP host is an open seam); the
contract case is `tests/contract/gateway/email_case.py`. Still open: R06.8 core ordering, the
"long text as an attachment" capability.
