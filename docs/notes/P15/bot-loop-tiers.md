# Task: Bot-loop guard, concurrency cap, slash admin tiers (P15-10) Phase: 15 Owner: wave-18 builder G2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/bot_loop_guard.py` (all),
`gateway/slash_access.py` (all). Research 06 §3.5 (tiers), §3.6, trap R06.10. Closed before writing.
Evidence: none.

What it does (own words):
- Bot-loop guard: when bots are admitted, two agents in one chat can answer each other forever.
  The guard keeps one sliding window per conversation `(profile, platform, chat_id)` — never per
  sender pair, so N bots share one budget. Defaults: on, 20 bot-authored messages per 300 s; the
  21st trips a 600 s cooldown and clears the window. `admit()` counts one message and answers
  `ok` / `tripped` / `cooldown` / `disabled`; it is called once per inbound message (admission),
  while `blocked()` only reads and may be asked by every authorization pass. Idle windows are swept
  at most once per window length. Settings are read on every call; bad values fall back to the
  defaults.
- Concurrency cap: optional `gateway.max_concurrent_sessions` (positive int); a new session beyond
  it is refused with a short message; a session already running is never counted twice.
- Slash admin tiers, per platform and per scope (DM vs group): `allow_admin_from` /
  `group_allow_admin_from` list admins (every command, plugin ones included);
  `user_allowed_commands` / `group_user_allowed_commands` list what others may run; the floor for
  non-admins is `help` and `whoami`; a scope with no admin list has tiers off (everyone runs
  everything). Plain chat is never gated. `/help` lists only what the caller may run.

Inputs → outputs:
- `BotLoopSettings.from_mapping(raw)`; `BotLoopGuard(settings, monotonic)`:
  `admit(conversation, message_id=None) -> AdmitState`, `blocked(conversation) -> bool`;
  `conversation_of(source, profile)`; `settings_reader(config_get)`.
- `ConcurrencyCap(limit)`: `try_claim(key) -> bool`, `release(key)`, `active`; `REFUSAL_TEXT`.
- `SlashPolicy`, `policy_from_extra(extra, scope)`, `policy_for(extra, chat_type)`,
  `runnable(policy, user_id, names)`, `tier_label(policy, user_id)`.

Files: `src/indus/gateway/core/bot_loop.py`, `src/indus/gateway/core/slash_access.py`,
`tests/unit/gateway/core/test_bot_loop.py`, `tests/unit/gateway/core/test_slash_access.py`.

Edge cases / failure modes:
- the same message id admitted twice counts once (belt and braces for "count once").
- `True` is not a number for the integer settings; `0`, negatives, `nan`, strings → default.
- a cap of `None`/missing/invalid → no cap.
- command names compared lower-case with a leading `/` removed; ids compared as strings.
- an empty or unknown chat type resolves to whichever scope is gated (fail toward gating).

Our design differences / decisions:
- setting names: `gateway.bot_loop_guard.{enabled, max_messages, window_seconds,
  cooldown_seconds}` (ours) and `gateway.max_concurrent_sessions`.
- the guard takes an injected monotonic clock (fake clock in tests).
- DM and group command lists are independent (the reference lets the DM scope borrow the group
  list when its own is empty; we keep the two scopes separate so a group setting never changes DM
  behaviour).

Tests we will write:
- two bots in one chat share one budget (21st trips), cooldown expiry with fake clock, window
  slides, repeated `blocked()` never counts, duplicate message id counted once, disabled state,
  settings re-read each call, invalid values → defaults, sweep bounded.
- cap: refuse beyond the limit, re-claim of a running key, release frees, no cap when unset.
- tiers table: no admin list → full access; admin runs plugin command; non-admin floor; listed
  command; group vs DM scopes; blank chat type; `/help` list filtered; tier labels.

## Decisions for the lead
- Config rows needed (P15-02 owns the schema): `gateway.bot_loop_guard` block (enabled true,
  max_messages 20, window_seconds 300, cooldown_seconds 600), `gateway.max_concurrent_sessions:
  int | None = None`; per-platform `extra` keys `allow_admin_from`, `user_allowed_commands`,
  `group_allow_admin_from`, `group_user_allowed_commands`.

Compare checklist (filled after the rewrite; reopened `gateway/bot_loop_guard.py`,
`gateway/slash_access.py`):
- [x] budget per (profile, platform, chat), 20 / 300 s, cooldown 600 s, window cleared on trip (tests in `test_bot_loop.py`)
- [x] `admit` states ok / tripped / cooldown / disabled; `blocked` read-only (test: `test_blocked_is_read_only`)
- [x] sweep at most once per window (test: `test_idle_conversations_swept_once_per_window`)
- [x] settings re-read per call; bools, non-integers and non-positive values fall back (tests: `test_disabled_and_reread_every_call`, `test_settings_fall_back_to_defaults`)
- [x] tiers: admin list per scope, floor help / whoami, no admin list → off, blank chat type → gated scope (`test_slash_access.py`)
- Deliberate differences: a repeated message id is counted once (belt and braces for "count once");
  `nan` / `inf` fall back to defaults; DM and group command lists are independent (the reference
  lets the DM scope borrow the group list); the concurrency cap is a small claim/release object.
- Not taken: multiplex per-profile policy lookup (P24).

## Status

Done (2026-10-19). Shared: none.
