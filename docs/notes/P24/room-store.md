# Task: Group room store (P24-05) Phase: 24 Owner: wave-37 builder R1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `website/docs/user-guide/bot-mode.md`
("Groups and group chats", "Transferring hosted room authority"), `gateway/hosted_rooms.py`
(constants block, validation helpers, schema, create / list / rename / append / claim / disband /
read / prune), `gateway/hosted_rooms_common.py` (all). Research 06 §3.20.
Evidence: none.

What it does (own words): a group room ("group rooms" in the UI) is a durable chat in which several
profiles ("bots") talk. The store keeps, per room, its identity (id, name, members, which gateway is
the room's authority and at which epoch) and an append-only event log. Every event has a
caller-chosen id that is idempotent: sending the same id with the same content again returns the
stored event; the same id with other content is refused, so a retried send can never write twice or
silently change history. Each room gets a per-room sequence number. Only the current authority (its
gateway id and epoch) may append. A room is never really deleted while it is fresh: disbanding
writes a `room.disbanded` event, marks the row and retires the id for good, so the same id can never
be created again ("recreating a same-name group starts a genuinely fresh room"). Disbanded rooms are
purged after 90 days, or earlier when more than 512 are kept or the gateway's event budget is
exhausted; their ids stay retired. Everything is bounded (the limit table below), and a reserve of
64 events / 1 MiB is kept for the control events (`authority.claimed`, `authority.lost`,
`room.disbanded`, `room.stop_requested`) so a full room can still be stopped, disbanded or moved.
Without this store rooms would not survive a closed desktop, and an unbounded log would fill the
disk of the host serving every profile.

Limit table (research 06 §3.20, number for number): protocol version 2; ids ≤ 128 characters; name ≤
200; event kind ≤ 64 matching `^[a-z][a-z0-9._-]*$`; ≤ 128 members, members JSON ≤ 128 KiB; event
JSON ≤ 256 KiB; a log page ≤ 500 events and ≤ 2 MiB; ≤ 256 active rooms; ≤ 512 disbanded tombstones
kept 90 days; ≤ 50,000 events and ≤ 256 MiB per room; ≤ 16 MiB of events per gateway; reserve 64
events / 1 MiB for control events.

Inputs → outputs: `RoomStore(scope)` opens `<root>/rooms.db` (the root shared by every profile of
the install, never a profile's session store). `create_room(room_id, name, members, authority=)`,
`list_rooms(limit, offset, include_disbanded)`, `get_room`, `rename_room(room_id, event_id, name)`,
`append_event(room_id, event_id, kind, actor, payload, authority=, epoch=, expected_latest_seq=)`,
`request_stop(room_id, cancel_id, authority=, epoch=)`, `claim_authority(room_id, expected=,
expected_epoch=, new=, event_id=)`, `record_authority_lost(room_id, observed=, observed_epoch=)`,
`disband_room(room_id, authority=, epoch=)`, `read_events(room_id, since_seq, limit)` →
`LogPage{events, cursor, latest_seq, has_more, authority}`, `prune_disbanded()`. `gateway_id` is
this install's authority id (minted once, kept in the store's meta table).

Files: `src/indus/state/rooms.py`, `tests/unit/state/test_rooms.py`; shared:
`src/indus/foundation/errors.py` (the `rooms.*` codes).

Edge cases / failure modes:
- duplicate event id, same content → the stored event, flagged `idempotent`; other content →
  `RoomEventConflict`; duplicate create with the same name and members → the room, otherwise
  `RoomConflict`; create of a retired id → `RoomConflict` (retired ids never come back).
- stale authority (other gateway or epoch) → `RoomAuthorityConflict`; claim with a stale expected
  epoch → conflict; a repeated identical claim after a newer claim → "superseded".
- full room / full gateway → `RoomFull` with a sentence the UI can show; control kinds draw on the
  reserve; the gateway budget first purges the oldest disbanded rooms before refusing.
- a page never splits an event: the largest prefix whose serialized page fits 2 MiB is returned;
  `since_seq` ahead of the log is refused.
- actor kinds are fixed (`user`, `member`, `gateway`, `system`) and each may append only its own
  event kinds (a member cannot forge `authority.claimed`); a gateway actor must be the authority.
- every write runs through our writer discipline (one writer, `BEGIN IMMEDIATE`, deadline retries;
  R03.8); several gateways of one install share the file.
- clocks are the scope's clock (tests use a fake clock for the 90-day pruning).

Our design differences / decisions:
- Our names: `RoomStore`, `RoomLimits`, `Room`, `RoomEvent`, `LogPage`; file `rooms.db` under the
  install root (the reference shares one root database file with other features; ours is a file of
  its own so a room storm never contends with anything else).
- The limits are one frozen `RoomLimits` value (defaults = the table) so tests can shrink the big
  bounds (50,000 events, 256 MiB) instead of writing gigabytes; a test pins the defaults.
- `record_authority_lost` (demotion) adopts an observed newer lineage only (observed epoch must be
  greater), records `authority.lost`, and is idempotent for the same lineage.
- Left out: legacy-room adoption and the one-time import from an older store (fresh start, §2.3),
  room links / peer reservations / grants / remote-run receipts (cross-gateway room members are
  not in this row; see the bots-and-peers note), attachment retention.

Tests we will write:
- the default limit table equals research 06 §3.20; one test per bound with shrunk limits where the
  real number is too big to fill (events per room, bytes per room, gateway bytes, active rooms,
  tombstones) and the real number where it is cheap (id, name, kind, members, event JSON, page).
- duplicate event id no-op; conflicting duplicate refused; create idempotent / conflict / retired.
- a full room still accepts `room.stop_requested` (and disband) from the reserve, but not a message.
- tombstone pruning at 90 days with a fake clock; ids stay retired; over 512 tombstones the oldest
  go; the gateway budget purges disbanded rooms before refusing.
- authority: stale epoch refused, claim moves the epoch with an event, demotion adopts only a newer
  observed lineage.
- log paging: cursor, has_more, 2 MiB byte cut keeps whole events.

## Decisions for the lead
- Store file `<root>/rooms.db` (install root, shared by all profiles). Change: `ROOMS_FILE`.
- Error codes `rooms.invalid`, `rooms.not_found`, `rooms.conflict`, `rooms.event_conflict`,
  `rooms.authority_conflict`, `rooms.full` added to `ERROR_CODES` (shared file, union merge).
- Backups: `rooms.db` is install-level, not profile-level; the backup rules are not changed here.

Compare checklist (filled after the rewrite; reopened `gateway/hosted_rooms.py` (room_state,
claim, disband, read paging) and the names of its tests, `tests/gateway/test_hosted_rooms.py`):
- [x] limit constants equal number for number (test: `tests/unit/state/test_rooms.py::test_default_limits_match_the_spec_table`)
- [x] event kind pattern and per-actor kinds (test: `::test_actor_kinds_may_only_append_their_own_events`, `::test_event_kind_bound_and_pattern`)
- [x] idempotent event ids / conflicts, actor part of the content (tests: `::test_duplicate_event_id_is_a_no_op`, `::test_duplicate_event_id_with_other_content_is_refused`, `::test_the_actor_is_part_of_an_event_ids_content`)
- [x] control reserve for stop and disband (test: `::test_a_full_room_still_accepts_a_stop_from_the_reserve`, `::test_events_per_room_bound_and_the_control_reserve`)
- [x] tombstones: 90 days, 512 cap, ids retired forever (tests: `::test_tombstones_are_pruned_after_90_days`, `::test_more_than_512_tombstones_drop_the_oldest`)
- [x] gateway budget purges disbanded rooms before refusing (test: `::test_gateway_budget_purges_disbanded_rooms_first`)
- [x] read page ≤ 500 events / 2 MiB, whole events, bytes not characters (tests: `::test_page_is_cut_by_bytes_without_splitting_an_event`, `::test_page_bytes_count_utf8_not_characters`, `::test_page_limit_is_500_events`)
- [x] claim / supersede / stale-epoch rules, one winner under concurrency (tests: `::test_claim_moves_the_epoch_and_fences_the_old_authority`, `::test_a_repeated_claim_after_a_newer_one_reports_superseded`, `::test_concurrent_claims_have_one_winner`)
- [x] one monotonic sequence across writers; log survives a reopen (tests: `::test_concurrent_writers_share_one_sequence`, `::test_the_log_survives_reopening_the_store`)
- [x] gap found: the reference's room state carries the current epoch's claim event — added
  `RoomStore.authority_claim` and `groups.state` shows it (test: `tests/unit/gateway/rooms/test_rpc.py::test_state_shows_the_authority_claim_after_a_promotion`).
- Deliberate differences: one store file of its own (`<root>/rooms.db`); limits injectable; rename
  needs the authority like every other write; demotion (`record_authority_lost`) is a store call;
  our event-kind names for the non-control kinds; no event on create.
- Not taken: legacy adoption and the one-time import (fresh start), room links, grants, peer
  reservations, remote-run receipts, attachment retention, the non-blocking ownership probe
  (no cross-gateway room members or older desktop clients in this row).

## Status

Done (2026-10-21). Store, limits and tests finished; shared: `foundation/errors.py` (`rooms.*`
codes). The desktop/TUI views read through P24-06's `groups.*` methods.
