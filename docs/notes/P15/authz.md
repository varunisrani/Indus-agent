# Task: Authorization chain, unknown-sender behaviour, mention gating (P15-08) Phase: 15 Owner: wave-18 builder G2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/authz_mixin.py`
(check order, unknown-DM resolution, bot-guard hand-off), `gateway/run_inbound_unauthorized.py`
(all). Research 06 §3.5, traps R06.8, R06.24, R06.10 (hand-off only). Closed before writing.
Evidence: none.

What it does (own words): decides whether an inbound message (or a button press) may reach the
agent, and if not, what the stranger sees. The chain is ordered and the first grant wins; anything
that falls through is denied, and any error inside the chain is a denial (fail closed, K14):
1. webhook / `trusted_inbound` platforms (their own signature or key already authenticated it);
2. chat-scoped grants that need no user id: adapters that authorize upstream; a group chat listed in
   the group-chat allowlist; a bot sender admitted by the platform's allow-bots setting
   (`mentions` or `all`);
3. no user id → deny;
4. the platform's allow-all switch;
5. an adapter-verified role grant (Discord role allowlist) carried on the source;
6. a pairing approval;
7. when no env allowlist exists at all: the adapter's own access policy (only an `allowlist`
   policy is a verdict), then config `allow_from` / `group_allow_from`, then the global allow-all;
8. otherwise the union of the platform, group-user and global allowlists plus ids the adapter
   resolved from names; `*` means everyone; group chats listed by id also pass here;
9. default deny.
A bot-authored message that passes is still refused while its conversation's bot-loop guard is
cooling down (P15-10; the guard is only read here, counting happens once in admission).
Unknown DM behaviour (`pair` / `ignore` / `decline`): explicit per-platform setting; else the
global setting when it differs from the default; else the adapter's own DM policy (pairing → pair,
allowlist / disabled → ignore); else `ignore` when any allowlist is configured (R06.8); else `pair`.
Groups and id-less messages are always dropped silently; bots never get a code; `pair` and login
offers need a platform whose DMs are private (R06.24); `ignore` logs the sender id and the fix once
per sender and tells the home channel once per sender; `decline` answers once per sender per 24 h
and stamps before sending. Mention gating: in groups answer only when mentioned unless the channel
is free-response (or the platform turns the requirement off).

Inputs → outputs:
- `PlatformPolicy` (static per platform, built from a registry entry: env names, trusted flag,
  `dm_is_private`) and `AuthzInputs` (env reader, per-platform `extra` reader, global unknown-DM
  setting, pairing check, adapter lookup, optional bot guard + profile).
- `Authorizer.check(source, *, allow_delegation=True) -> AuthzDecision{granted, step, detail}`;
  `unknown_dm_mode(platform) -> UnknownDmMode{mode, why}`; `explain(platform) -> str`.
- `UnauthorizedHandler.respond(source, decision, *, send_reply, notify_home) -> Response{action,
  ...}` with actions `silent`, `ignored`, `declined`, `paired`, `try_later`.
- `MentionPolicy.from_extra(extra)`; `should_answer(source, *, mentioned, policy) -> bool`.

Files: `src/indus/gateway/core/authz.py`, `src/indus/gateway/core/_shapes.py` (structural types
for G1's source and adapter, one place), `tests/unit/gateway/core/test_authz.py`.

Edge cases / failure modes:
- allowlists given as YAML lists, JSON-list strings or comma strings; blanks dropped.
- a source attribute that is not exactly `True` (e.g. a mock) never grants (`is True` checks).
- an adapter policy callback that raises or returns a non-string → no verdict.
- a reply send that raises after the decline stamp: the stamp stays, no second decline in 24 h.
- a pairing code request refused by rate limit: one "try later" reply, then silence.
- the home-channel notice is skipped when the stranger's DM is the home channel itself.
- display names in the owner notice are cut to 64 characters with mention/markup sigils removed.

Our design differences / decisions:
- copy is ours (D-16.23); env names `INDUS_<PLATFORM>_*` and `INDUS_GATEWAY_ALLOWED_USERS`,
  `INDUS_GATEWAY_ALLOW_ALL_USERS`.
- "any allowlist configured" counts every list the chain reads (platform / group-user / group-chat
  / global env lists and config `allow_from` / `group_allow_from` / `group_allowed_chats`): one
  rule an operator can predict; `explain()` names the list that flipped it.
- email "always ignore" is P16 (no email adapter yet).
- local desktop/TUI sources (step 0 of the reference) are not gateway traffic in P15.

Tests we will write:
- table test over the 9-step order (40+ rows) asserting the deciding step.
- unknown-DM table: each rung, allowlist flips to ignore, explain() text.
- handler: group silent, id-less silent, bot never gets a code, `dm_is_private=false` never gets a
  code, decline stamp before send (send raises → no second decline), 24 h window with fake clock,
  ignore logs once and notifies home once, rate-limited user gets one try-later.
- button press source authorized by the same chain; mention gating table; bot cooldown denies.

## Decisions for the lead
- Config rows needed (P15-02 owns the schema): `gateway.unauthorized_dm_behavior: pair|ignore|
  decline = pair`; per-platform `extra` keys read: `allow_from`, `group_allow_from`,
  `group_allowed_chats`, `allow_bots`, `unauthorized_dm_behavior`, `dm_policy`, `require_mention`,
  `free_response_channels`.
- The adapter contract (P15-05) is read structurally: flags `auth_upstream`,
  `owns_access_policy`, `dm_is_private`; optional `access_policy(kind) -> str`,
  `allows_dm(user_id) -> bool`, `resolved_allowlist_ids() -> set[str]`. If G1 names them
  differently, only `_shapes.py` and the three `getattr` reads in `authz.py` change.

Compare checklist (filled after the rewrite; reopened `gateway/authz_mixin.py`,
`gateway/run_inbound_unauthorized.py`):
- [x] order trusted → chat-scoped (upstream, group chat lists env + config, allow-bots) → no user id
  → platform allow-all → role → pairing → (no env list: own policy, config list, global allow-all)
  → union + resolved ids → deny (test: `test_authz.py::test_chain_order`, 56 rows)
- [x] own policy is a verdict only for `allowlist`; DM re-check through the live adapter (rows `own_policy_*`)
- [x] resolved ids only with a platform list (rows `resolved_ids*`)
- [x] `is True` checks on adapter / source flags (rows `upstream_truthy_not_true`, `role_not_true`)
- [x] bot-loop guard read after a grant, never counted here (test: `test_bot_in_cooldown_is_refused_after_a_grant`)
- [x] unknown DM order: platform → non-default global → adapter DM policy → allowlist → pair (test: `test_unknown_dm_mode`)
- [x] owner notice once per sender per process, bounded set, name sanitised, never posted into the
  stranger's own chat (tests: `test_ignore_logs_and_notifies_home_once_per_sender`,
  `test_ignore_never_answers_into_the_strangers_own_chat`, `test_seen_senders_are_bounded`)
- [x] decline stamp before send (test: `test_decline_stamp_written_before_send`)
- Deliberate differences: a `thread` chat type counts as a group (the reference counts only
  group / forum / channel), so thread strangers are dropped silently and `group_allow_from`
  applies in threads; "any allowlist configured" counts group and config lists for every
  platform (the reference counts only env lists, and group lists only on opted-in platforms) —
  one predictable rule, and `explain()` names the list; the chain never raises (errors deny).
- Not taken: local desktop/TUI verdict (not gateway traffic in P15), the email "always ignore" rung
  (P16), WhatsApp / Nostr identity aliasing (P16), the legacy Telegram chat ids in the group-user
  list (no legacy config to read), multiplex scoping (P24), plugin `extra.allowed_users` spelling.

## Status

Done (2026-10-19). Shared: none. The G1 `Source` / adapter models are read structurally through
`gateway/core/_shapes.py`; tests use `tests/unit/gateway/_stubs_g2.py` (stand-in).

Integration (lead): `_shapes.py` now describes G1's real types — `SourceLike` (with
`user_name`, `parent_chat_id`, `role_authorized`, which became a `Source` field) and `AdapterLike`
(`capabilities.auth_upstream / owns_access_policy / dm_is_private`; `access_policy(kind)`,
`allows_dm(user_id)`, `resolved_allowlist_ids()` are base-class methods with safe defaults). The
`getattr` reads are gone; tests use `tests/unit/gateway/builders.py` (real `Source`, a
`PolicyAdapter` on the real base). The pairing store moved to `indus.state.pairing`.
