# Task: Outbound sender, final filters, dead targets (P15-17) Phase: 15 Owner: wave-1 builder G3

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/response_filters.py` (all),
`gateway/dead_targets.py` (all), `gateway/platforms/base.py` (`EphemeralReply`). Research
`06-gateway.md` §3.13 (threading, silence tokens, ephemeral notices, redaction), §3.15 (dead
targets), traps R06.14, R07.14. Closed before writing.
Evidence: none.

What it does (own words): every user-facing final reply leaves through one path. It masks secrets
first (P05 redactor, forced on), then applies the silence rule: when the whole reply is a silence
token (`[SILENT]`, `SILENT`, `NO_REPLY`, `NO REPLY`; whitespace, case and stray edge punctuation
ignored) nothing is sent but the turn stays in the transcript; a person who wrote to us and
expected an answer gets a short notice instead; a failed turn is never silenced; a reply that only
mentions a token is delivered. Then media tags are pulled out (P15-18), the text is formatted for
the platform (P15-15) and chunked (P15-14), and the chunks are sent with `reply_to_mode` deciding
which of them thread to the user's message (`off`, `first` default, `all`) and the thread/topic id
carried in the send metadata. Each sent piece is recorded in the sent-message index (P15-03). A
send that proves the whole chat gone (forbidden, chat-level not found) marks the target dead; later
unsolicited deliveries (cron, notices) skip it until any send there succeeds. Ephemeral system
notices delete themselves after `display.ephemeral_system_ttl` where the platform can delete.

Inputs → outputs: `OutboundSender(port, *, scope, platform, dead_targets, sent_index=None,
media=None, reply_to_mode="first", ephemeral_ttl=0.0)`; `await sender.deliver(chat_id,
FinalReply(text, failed, reply_expected, internal, reply_to, thread_id, unsolicited, ephemeral))
-> DeliveryReport { outcome: sent/silent/skipped_dead/failed, message_ids, failure, remainder,
notices }`; `aclose()` cancels pending ephemeral deletes. `filters`: `is_silence(text)`,
`could_become_silence(text)`, `decide_silence(text, *, failed, reply_expected, internal)`,
`redact_final(text, scope)`, `SILENT_NOTICE`. `dead_targets`: `DeadTargets(path)` with
`is_dead`, `mark`, `clear`, `note(platform, chat_id, result)`; `DeadTargets.for_scope(scope)`
(`<home>/gateway/dead_targets.json`, 0600 atomic writes, corrupt file → empty, write errors kept
in memory only).

Files: `src/indus/gateway/outbound/sender.py`, `src/indus/gateway/outbound/filters.py`,
`src/indus/gateway/outbound/dead_targets.py`, `tests/unit/gateway/outbound/test_sender.py`,
`tests/unit/gateway/outbound/test_filters.py`.

Edge cases / failure modes:
- empty reply → not silence (the empty-reply path is the runner's); nothing sent.
- silence token longer than 64 characters with padding → not a token.
- a chunk refused as bad format → that chunk is resent once as plain text.
- a chunk fails otherwise → stop; the report carries the failure and the undelivered remainder
  (source text from that chunk on) for the ledger (P15-19); earlier chunks are never re-sent.
- the dead-target file corrupt or unwritable → in-memory only, delivery never breaks.
- replies to an inbound message ignore the dead flag (the chat just wrote to us) and clear it on
  success; unsolicited deliveries respect it.
- ephemeral notice on a platform without delete → stays.

Our design differences / decisions:
- Redaction is forced (`force=True`) for chat finals, even with `security.redact_secrets` off.
- Silence tokens: the four English ones (the reference's Chinese renderings are left out with the
  non-English locales, D-16.51); edge punctuation tolerance kept.
- Notice text is ours: "Nothing further to add."
- Dead-target registry keyed `platform:chat_id`, stores reason (≤200 chars) and time.

Tests we will write: silence table (token alone, cased/spaced/punctuated, in a sentence, failed
turn, reply expected/not, internal wake); dead target marked by forbidden and chat-level not found,
not by thread-level not found; skipped on unsolicited delivery then revived by a success;
reply_to_mode table (off/first/all over three chunks) and thread id in metadata; redaction applied
before chunking (a secret straddling a chunk border is masked); bad-format chunk resent plain once;
partial failure reports the remainder; sent index recorded; ephemeral delete with fake clock.

## Decisions for the lead
- Chat finals always force redaction. Change: `redact_final(force=...)`.
- Unsolicited vs reply decides whether the dead flag is honoured (FinalReply.unsolicited).

Compare checklist (filled after the rewrite; reopened `gateway/response_filters.py`,
`gateway/dead_targets.py`, `EphemeralReply`):
- [x] whole-message silence only, edge punctuation tolerated, ≤64 chars (`tests/unit/gateway/outbound/test_filters.py::test_silence_table`)
- [x] streamed prefixes held (`::test_streamed_prefixes_of_a_token_are_held`)
- [x] failed turns never silenced; notice for a person, silence for internal / no-reply-expected (`::test_failed_turn_is_never_silenced`, `::test_decide_silence`, `test_sender.py::test_silence_rules`)
- [x] dead targets: only whole-chat failures, persist, self-heal (`test_sender.py::test_dead_target_skipped_then_revived`, `::test_only_whole_chat_failures_mark_a_target`, `::test_corrupt_dead_target_file_starts_empty`, `::test_unwritable_dead_target_file_keeps_memory`)
- [x] reply_to_mode and thread metadata (`::test_reply_to_mode_table`)
- [x] redaction before chunking (`::test_redaction_applies_before_chunking`)
- [x] partial failure reports only undelivered pieces (`::test_partial_failure_reports_only_the_undelivered_pieces`)
- [x] ephemeral TTL (`::test_ephemeral_notice_is_deleted_after_ttl`, `::test_ephemeral_needs_delete_support_and_a_ttl`)
- Deliberate differences: forced redaction; English tokens only; dead flag honoured only for
  unsolicited deliveries (a reply proves the chat alive and clears it on success).
- Not taken: the autonomous-lane loose silence matcher (cron, P15-21), display-kind persistence
  (the runner passes `internal` / `reply_expected`).

## Status

Done (2026-10-19). R06.14 / R07.14 / ACC-K27-1 / ACC-K29-1 (dead-target half) rows.
