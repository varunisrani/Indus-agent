# Task: Session key builder and routing index (P15-07) Phase: 15 Owner: wave-18 builder G2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/session.py`
(`_session_key_namespace`, `_canonical_participant`, `build_session_key`, `SessionEntry` fields
only). Research 06 §3.7, trap R06.7, glossary (e)6. Closed before writing.
Evidence: none.

What it does (own words): every chat maps to one conversation slot by a deterministic string, the
session key. One function builds it, and nothing else may assemble a key by hand (R06.7). The key
is persisted, so its byte layout is a compatibility surface: a change orphans every existing
conversation. Layout `<ns>:<platform>:<chat_type>[:<scope_id>][:<chat_id>][…]`:
- namespace `@default` for the default profile, the profile id otherwise (`@` is outside the
  profile-id alphabet, so a profile can never collide with the default namespace);
- DM: key on the chat id; without one, on the sender id; without both, one per platform. In a DM
  the user part comes before the thread part;
- group: append the participant (alt id first, else user id) when `group_sessions_per_user`
  (default true) and not in a thread; threads are shared unless `thread_sessions_per_user`. In a
  group the thread part comes before the user part;
- Discord auto-threads: a channel message that will open a thread is keyed on the future thread
  (`prospective_thread_id`) with the chat-type slot `thread`, so follow-ups inside the thread give
  the same bytes; a real thread id always wins; DMs never use the prospective id;
- Slack puts the workspace (`scope_id`) before the chat id; a Discord guild id is never in a key.
The routing index (store table `gateway_routes`) maps key → current session id plus the origin
source, display name, a session model override, `resume_pending` (+ reason), the durable
active-turn marker, and the previous session id after a rotation. Conversations never reset on
idle or day boundaries: only `/new` rotates a key to a fresh session.

Inputs → outputs:
- `build_session_key(source, *, profile, group_sessions_per_user=True,
  thread_sessions_per_user=False) -> str`; `key_namespace(profile) -> str`;
  `origin_of(source) -> dict` (JSON-safe copy of the source for the route row).
- `indus.state.gateway_routes`: `RouteEntry`, `get`, `get_or_create`, `rotate`, `set_display_name`,
  `set_model_override` (model/provider/base_url only; credentials dropped), `mark_resume_pending`,
  `clear_resume_pending`, `begin_turn(token)`, `end_turn(token) -> bool` (compare-and-clear),
  `list_routes`, `routes_for_session`, `delete`.

Files: `src/indus/gateway/core/session_keys.py`, `src/indus/state/gateway_routes.py`,
`src/indus/state/schema.py` (shared: `gateway_routes` table + index),
`tests/unit/gateway/core/test_session_keys.py`, `tests/unit/state/test_gateway_routes.py`,
`tests/fixtures/gateway/session_keys.golden.json`.

Edge cases / failure modes:
- chat ids that contain `:` (Matrix-style) are not escaped: the reference does not either and the
  key is only matched whole, never split.
- an enum-valued platform or chat type is reduced to its value, so G1's model can use either.
- empty strings count as missing (no `::` holes in a key).
- `end_turn` only clears the marker it set (a displaced turn cannot clear its successor's).
- route rows survive any amount of idle time; a model override never stores api keys or tokens.

Our design differences / decisions:
- namespace literal `@default` / `<profile>` (glossary (e)6) instead of the reference's
  `agent:main` family; no `~` marker needed (a profile id cannot contain `@`, and `default` is a
  reserved profile id).
- no FK from `gateway_routes.session_id` to `sessions`: the route may be written before the
  service creates the session row, and an ended/deleted session is detected by the caller.
- WhatsApp identity canonicalisation is a P16 concern; `canonical_participant` is the hook.
- token counters / cost columns of the reference entry are not copied: usage lives in the
  `sessions` table already (P02).

Tests we will write:
- golden file of 24 source shapes (DM, DM without chat id, DM without any id, DM thread, group
  per-user on/off, thread shared / per-user, Slack scope, two Slack workspaces, Discord prospective
  vs real thread, prospective in a DM ignored, alt id, forum, channel, profile `default` and a
  named profile, enum-valued fields).
- properties: the default namespace never equals a valid profile id's namespace; Discord guild id
  never appears; the follow-up in a real thread equals the prospective key.
- routes: round trip, get_or_create idempotent, 30 fake days idle keeps the session id, rotate
  keeps the previous id, override sanitised, active-turn compare-and-clear, resume flags.

## Decisions for the lead
- Schema: `gateway_routes` table declared in `state/schema.py` (shared with P15-19's ledger table).
- Config rows needed (P15-02 owns the schema): `gateway.group_sessions_per_user: bool = True`,
  `gateway.thread_sessions_per_user: bool = False`.

Compare checklist (filled after the rewrite; reopened `gateway/session.py` key builder and entry):
- [x] DM: user part before thread part; groups: thread before user (golden: `dm_no_chat_id_thread`, `thread_per_user`)
- [x] DM without chat id falls back to the sender, then one per platform (golden: `dm_no_chat_id`, `dm_no_ids`)
- [x] group per-user default on, off inside a shared thread, `thread_sessions_per_user` isolates (golden rows)
- [x] prospective thread id → slot `thread`, real thread wins, DMs ignore it (test: `test_discord_follow_up_in_thread_matches_the_spawning_message`)
- [x] Slack scope before chat id; Discord guild never in the key (tests: `test_two_slack_workspaces_same_channel_differ`, `test_guild_id_never_in_any_key`)
- [x] participant = alternate id first (golden: `group_alt_id`, `dm_alt_id_no_chat`)
- [x] route entry keeps origin, display name, model override (no credentials), resume flag and reason,
  active-turn marker (tests in `tests/unit/state/test_gateway_routes.py`)
- Deliberate differences: namespace `@default` / profile id (glossary (e)6), no `~` marker needed;
  the route row drops token / cost counters (usage lives in `sessions`), the reset bookkeeping
  fields (we never auto-reset), the JSON mirror, `/yolo` and prompt-pin fields (later tasks add
  columns through the declarative reconcile when they need them).
- Not taken: WhatsApp identity folding (P16); multiplexed transport profile (P24).

## Status

Done (2026-10-19). Shared: `src/indus/state/schema.py` (`gateway_routes` table + index).
Follow-up for the lead: deleting a session (P02 `deletion.py`) leaves routes pointing at it;
P15-11 should treat a route whose session is gone as "rotate", or `deletion.py` can drop
`gateway_routes` rows by `session_id`.
