# Task: Pairing store and `indus pairing` (P15-09) Phase: 15 Owner: wave-18 builder G2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/pairing.py` (store,
constants, allowlist mirroring), `hermes_cli/pairing.py` (commands). Research 06 §3.5, trap R06.9.
Closed before writing.
Evidence: none.

What it does (own words): a stranger who DMs the bot (when the platform is in `pair` mode) gets a
one-time code; the owner approves it from the CLI and the stranger is let in from then on.
- code: 8 characters from a 32-letter alphabet without 0/O/1/I, drawn with `secrets`;
- only a salted SHA-256 (16-byte salt) is stored, under a random 16-hex request id; the code is
  never logged and never shown again;
- a code lives 3,600 s; at most 3 pending per platform; one request per user per 600 s;
- 5 failed code approvals lock the platform for 3,600 s (a success resets the streak); the lockout
  is checked before any lookup, so even a correct code is refused while locked out;
- typed codes have all whitespace removed and are upper-cased, compared in constant time;
- an admin approving by request id never sees the code, is not gated by the lockout and does not
  count toward it;
- approval also adds the user to the platform's allowlist when one is configured (the operator's
  list stays the visible truth); revoke removes it there and tells live adapters to drop it;
- files are 0600 in a 0700 directory resolved from the scope on every call (R06.9);
- a rate-limited user gets one "try later" reply, then silence;
- decline stamps (P15-08 `decline` mode) live here too: one per sender per 24 h.
CLI: `indus pairing list`, `approve <platform> <code|request-id>`, `revoke <platform> <user>`,
`clear-pending`.

Inputs → outputs: `PairingStore(scope=None)` (None = the current scope at each call):
`request_code(platform, user_id, user_name) -> CodeRequest{status, code, request_id, notify}`;
`approve(platform, value)`, `approve_code`, `approve_request` -> `Approval{status, user_id,
user_name, lockout_seconds}`; `is_approved`; `list_pending` (no codes); `list_approved`; `revoke`;
`clear_pending`; `lockout_remaining`; `has_recent_decline`; `record_decline`.
`add_revoke_listener(fn)` for live adapters' in-memory lists.

Files: `src/indus/state/pairing.py` (moved from `gateway/core/` at integration),
`src/indus/cli/commands/pairing.py`, `src/indus/cli/app.py` (shared: one REGISTRY row),
`tests/unit/state/test_pairing.py`,
`tests/unit/cli/commands/test_pairing.py`.

Edge cases / failure modes:
- platform names are checked (`[a-z0-9_]`, ≤32): a name can never escape the directory.
- malformed or unreadable files read as empty (a PermissionError is logged once with the fix).
- expired pending rows are pruned on every operation.
- concurrency: gateway and CLI are separate processes; every read-modify-write runs under a
  cross-process file lock in the directory plus a thread lock.
- the allowlist mirror only edits a list that already has entries (never creates one, which would
  flip unknown DMs to `ignore`); `*` lists are left alone.

Our design differences / decisions:
- directory `<home>/platforms/pairing/`; files `<platform>-pending.json`,
  `<platform>-approved.json`, `_limits.json`, `_declined.json`.
- the allowlist mirror writes the profile's `.env` (`INDUS_<PLATFORM>_ALLOWED_USERS`) through the
  P01 env-file writer.
- `request_code` returns a typed status instead of None, so the caller knows whether to send the
  one-time "try later" reply.
- the CLI prints our own copy; `--json` on `list` for scripts.

Tests we will write:
- alphabet property (hypothesis over many draws); stored files hold no code; request-id shape.
- " ab cd 12 34 " approves `ABCD1234`-style input; TTL expiry with fake clock; pending cap; rate
  limit per user; lockout after 5 wrong and the 6th correct still refused; request-id approval
  while locked out works and leaves the streak alone; success resets the streak.
- per-call directory: a store built with no scope follows the current scope (two homes), and a
  code issued in one "process" (scope A, env order X) is approved by a store resolved later with a
  different INDUS_HOME resolution order.
- file modes 0600 / 0700; allowlist mirror add/remove only when configured; revoke listeners.
- CLI: list empty / pending / approved, approve by code and by request id, locked-out message,
  unknown code, revoke, clear-pending, non-TTY output.

## Decisions for the lead
- `indus pairing` row added to `cli/app.py` REGISTRY (P15-28 owns the other gateway groups).
- `clear-pending` kept (an operator's escape hatch); not in the phase row.

Compare checklist (filled after the rewrite; reopened `gateway/pairing.py`, `hermes_cli/pairing.py`):
- [x] alphabet, length, TTL, pending cap, request interval, failure count, lockout length (tests in `test_pairing.py`)
- [x] salted hash under a random 16-hex id; nothing readable back (test: `test_files_hold_no_code_and_modes`)
- [x] whitespace stripped, upper-cased, constant-time compare (test: `test_spaced_lower_case_code_approves`)
- [x] lockout before lookup; 6th correct code refused (test: `test_lockout_after_five_wrong_refuses_a_correct_code`)
- [x] request-id approval neither gated nor counted (test: `test_request_id_approval_ignores_lockout_and_streak`)
- [x] success resets the streak (test: `test_success_resets_the_failure_streak`)
- [x] allowlist mirror only for an existing list, `*` untouched, revoke purges live adapters
  (tests: `test_allowlist_mirror_only_when_configured`, `test_list_approved_and_revoke_with_listener`)
- [x] directory per call (test: `test_directory_resolves_per_call`); 0600 / 0700 modes
- [x] CLI list / approve (code or id) / revoke / clear-pending with a distinct lockout message (`tests/unit/cli/commands/test_pairing.py`)
- Deliberate differences: `request_code` returns a typed status and whether to send the one-time
  "try later" (the reference returns None and the runner decides); platform names are validated
  (path safety); no legacy-layout migration (no older layout exists); user id aliasing (WhatsApp)
  is P16.
- Not taken: printing the lockout to stdout (we log a warning).

## Status

Done (2026-10-19). Shared: `src/indus/cli/app.py` (one REGISTRY row), `pyproject.toml` (one
narrow `ignore_imports` entry: `indus.cli.commands.pairing -> indus.gateway.core.pairing`;
cli and gateway are independent siblings in the layer contract — lead to decide the rule P15-28
will also need).

Integration (lead, D-16.246): the exemption is gone. The store moved to `indus.state.pairing`
(below cli and gateway); `allowlist_env_name` keeps the registry's `INDUS_<PLATFORM>_ALLOWED_USERS`
rule (a test pins that both agree). Rule for P15-28: what top-layer siblings share lives in
`state/` or `service/`; never an `ignore_imports` entry.
