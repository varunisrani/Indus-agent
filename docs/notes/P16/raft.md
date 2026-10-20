# Task: Raft adapter, wake bridge (P16-30) Phase: 16 Owner: wave-27 builder E14

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `plugins/platforms/raft/adapter.py`
(all: constants, wake and activity endpoints, token check, body caps, content-field refusal, the
activity queue and its validation, the bridge child process, send), `raft/plugin.yaml`,
`raft/__init__.py`; test names only in `tests/gateway/test_raft_adapter.py`. Evidence: none.

What it does (own words): lets an Indus Agent profile act as an external agent in a Raft
workspace. Raft's own CLI holds the conversation (cursors and message bodies); a bridge process of
Raft's (`raft agent bridge` with its "wake channel" adapter) only tells us that something may be
waiting. The adapter runs a small HTTP endpoint on 127.0.0.1 (an ephemeral port unless one is
configured), writes where it listens — URL, port and the per-start bridge token — to a port file
(0600, under the profile home, `platforms/raft/bridge.json`), and accepts:
- `POST /wake` (header `x-raft-bridge-token`): a content-free wake notice (JSON object, no field
  named body / content / message / messages / preview / snippet / text anywhere, strings ≤120
  characters, body ≤16 KiB). It is queued (500 at most; the 501st drops the oldest with a log
  line) and turns into an internal turn whose text is our fixed wake prompt: check Raft with its
  CLI. Answers 202, 503 before the gateway is ready.
- `POST /activity`: one activity event (`raft-activity.v1`: event id, session id, hook name,
  status, time, tool name/input/output, truncation flags, error class, duration) for the bridge's
  telemetry; tool input/output cut to 4,096 characters; queued (500, oldest dropped and counted).
- `GET /activity/drain?max=N`: up to N (default 200) queued activity events and how many were
  dropped since the last drain (`raft-activity-drain.v1`).
- `GET /health`: the listener's health.
The agent then reads and answers with the Raft CLI; the adapter's `send` only records the reply
(nothing goes anywhere). Every chat "is a DM" but is really the bridge: `dm_is_private` false, so
pairing codes and login links never go there (R06.24).

Inputs → outputs: `AdapterConfig` (`RAFT_PROFILE` from the secret store / env turns it on; optional
`RAFT_CHANNEL_TOKEN` fixes the bridge token, else one is made per start; `platforms.raft.{port,
runtime_session}`) → a listener and a port file; wake POSTs → internal `InboundEvent`s; activity
POSTs → the activity queue; `send` → a recorded reply.

Files: `src/indus/gateway/adapters/raft/__init__.py`, `src/indus/gateway/adapters/raft/adapter.py`,
`tests/unit/gateway/adapters/raft/test_adapter.py`; shared: `foundation/config/schema.py` + its
test (row `platforms.raft`), `cli/wizards/gateway_setup.py` (steps row).

Capability flags: `dm_is_private` false; `owns_access_policy` false; `auth_upstream` false (wake
events are internal: the token check is the authentication); `supports_edit` false;
`push_capable` false (a reply never reaches Raft through us); `splits_natively` true (a recorded
reply is one piece).

Edge cases / failure modes:
- A request whose peer is not a loopback address → 403 before the body is read.
- Missing or wrong token → 401 (constant-time compare, as bytes).
- Body over 16 KiB → 413 before it is read (declared length) or once it passes the cap
  (chunked). A string field over 120 characters → 413. Not JSON / not an object → 400; a
  content-bearing field → 400.
- Wakes before the gateway installed its handler → 503 (the bridge retries).
- Port taken (a configured port) → retryable connect error; the port file is written only after
  the listener is up and removed at stop (only when it is still ours).
- Profile home not writable → connect fails with the reason (no bridge can find us).
- Send-only (`indus send`): no listener, no port file; a send is recorded like any other.

Our design differences / decisions:
- We do not start the `raft agent bridge` process ourselves: the operator runs it (or a service
  manager does) pointed at the URL in the port file; the token is in the same 0600 file or set as
  `RAFT_CHANNEL_TOKEN` in the secret store. Starting and supervising a third-party CLI process is a
  separate sidecar task (ownership, restarts, env builder) — follow-up.
- Our own wake prompt text; the Raft CLI command it names is an interop fact.
- Queued wakes go to the core one at a time; several wakes waiting at once are folded into one
  internal event (they carry no content, one check covers them all).
- Agent hook telemetry (session start, tool calls…) is not fed into the activity queue yet: the
  adapter exposes `report_activity(event)`; the hook bus wiring is a core follow-up.
- Raft is not a send or cron target (a delivery would only be recorded): not in the target rules.

Tests we will write:
- non-loopback request refused (403, nothing parsed, no event);
- 501st queued wake drops the oldest with a log line;
- oversized field 413; oversized body 413 without reading; content field 400; bad token 401;
- wake → one internal event with our prompt, `allow_control` false; before ready 503;
- port file: ephemeral port, 0600, token, removed at stop; send records only;
- activity: validation, 4,096 cut with flags, drain and dropped count;
- `dm_is_private` false; no pairing code ever reaches Raft (authz `dm_is_private`).

## Decisions for the lead
- Registry row (`dm_is_private=False`, `lock_env=("RAFT_PROFILE",)`), `BUILTIN_PUBLIC_ENV`
  `raft: ("RAFT_PROFILE",)` and `raft` in `send_command._NOT_TARGETS` / the channel directory's
  `_NOT_TARGETS`: in the report.
- `RAFT_CHANNEL_TOKEN` is read as a secret (masked) but is not a credential that enables Raft.

Compare checklist (filled after the rewrite; reopened `plugins/platforms/raft/adapter.py`):
- [x] wake: token header (bytes, constant time), 16 KiB cap, content-field refusal at any depth,
  not-ready 503, wake id keys, internal event (test: `test_adapter.py::test_the_bridge_token_is_required`,
  `::test_an_oversized_body_is_413_without_being_read`,
  `::test_content_bearing_and_malformed_wakes_are_refused`,
  `::test_wakes_before_the_gateway_is_ready_get_503`, `::test_a_wake_becomes_one_internal_turn_with_our_prompt`)
- [x] activity: schema, allowed fields, safe scalars, 4,096 cut with flags, queue cap with a
  dropped count, drain (`::test_activity_events_are_checked_cut_and_drained`,
  `::test_the_activity_queue_drops_the_oldest_and_counts_them`)
- [x] send records only; `dm_is_private` false (`::test_send_only_records_replies_and_opens_nothing`,
  `::test_raft_never_gets_pairing_codes`)
- [x] row's Verify: non-loopback refused, 501st wake drops the oldest with a log line, oversized
  field 413 (`::test_a_non_loopback_request_is_refused_before_anything_is_read`,
  `::test_the_501st_queued_wake_drops_the_oldest_with_a_log_line`, `::test_an_oversized_field_is_413`)
- Deliberate differences: loopback only, always (no host setting; non-loopback peers refused
  before the body); a string field over 120 characters anywhere in a wake is 413 (the reference
  checked only that wakes carry no content); wakes have their own bounded queue and waiting wakes
  fold into one turn; a port file (0600) replaces passing the URL and token to a child process;
  an overlong activity scalar is 413, other invalid activity 400; the drain is capped at 500.
- Not taken: starting `raft agent bridge` ourselves (operator-run; follow-up sidecar task), the
  hook-bus feed of agent activity (`report_activity` is the seam), the activity summary in
  `/health` (the kit's health answer is used).

## Status

Done (2026-10-20). Shared: `foundation/config/schema.py` + `test_schema.py` (`platforms.raft`),
`cli/wizards/gateway_setup.py` (steps). Registry row, `BUILTIN_PUBLIC_ENV`, lock key and the
`_NOT_TARGETS` additions are for the lead (report and `_stubs_e14.py`).

Landed at integration (wave 27): registry row, `BUILTIN_PUBLIC_ENV`, lock key and Raft in both `_NOT_TARGETS` sets (D-16.536, D-16.537); `_stubs_e14.py` removed.
