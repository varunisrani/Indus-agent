# Task: Approval policy pipeline (P05-08) Phase: 05 Owner: wave-2 builder B

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/approval.py` (the combined
command guard, the floor function, unattended contexts, result shapes — behaviour only),
`tools/approval_floors.py` (whole: deny globs, hardline/sudo block wording intent, allowlist shell
operator check), `tools/approval_context.py` (contexts, mode normalisation, timeout reader),
`tools/approval_yolo.py` (whole: per-session toggle, persist, restore). Research `02-tools.md`
§3.13, traps R02.4, R02.6, R02.23, R02.27. Closed before writing.
Evidence: none.

What it does (own words): before a shell command runs, one function decides whether it may run
now, must never run, or needs a person. The order is fixed and the floors come first, so no
switch (auto-approve, `mode: off`, cron approve mode, a permanent allowlist entry) can let a
catastrophic command through. Without the fixed order an "approve everything" setting quietly
disables the hardline set (R02.4), and a container with a host mount is treated as harmless
(R02.6).

Inputs → outputs: `await decide(command, ctx) -> ApprovalDecision`.
`ApprovalContext{scope, session_id, turn_id, run_context: interactive|cron|single_query|unattended,
broker, isolated, cwd, state}`. `ApprovalDecision{approved, status: approved|blocked|pending,
pattern_key?, description?, user_approved, deny_reason?, user_summary?, message?, outcome, step}`.
Settings read through the scope on every call: `approvals.mode` (manual|smart|off, default
`manual`), `approvals.deny` (globs), `approvals.cron_mode`, `approvals.single_query_mode`,
`approvals.unattended_mode` (deny|approve, default deny), `command_allowlist`, `approvals.timeout`;
env `INDUS_YOLO_MODE` from the scope's env view.

Order:
1. Isolated backend (`ctx.isolated`, i.e. the env's `BackendFlags.isolated`, false whenever a host
   path is mounted): hardline floor and deny globs only, everything else approved.
2. Floors: hardline (incl. `malformed`/`parser_limit` → fail closed), deleting the interpreter or
   venv Indus runs from, agent-written `sudo -S`/`--stdin` with no configured password, deny globs.
3. A decision prepared for this exact command by the agent's batching (consumed once).
4. Auto-approve (session switch or `INDUS_YOLO_MODE`) or `mode: off` → approved.
5. `command_allowlist`: exact text, or a glob for a single simple command (no operators,
   substitutions or redirections).
6. No answering surface (cron, single query, unattended, or no broker): that context's policy —
   `deny` blocks a dangerous command at once, `approve` lets it run.
7. Detection: not dangerous, or its pattern approved for this session → approved.
8. Human (P05-09).

Files: `src/indus/safety/approvals/policy.py`, `src/indus/safety/approvals/floors.py`,
`src/indus/safety/approvals/decision.py` (new: shared types and per-session state, so `human.py`
and `policy.py` do not import each other), `tests/unit/safety/approvals/test_policy.py`,
`tests/unit/safety/approvals/test_floors.py`; shared: `src/indus/safety/approvals/__init__.py`
(exports), `src/indus/foundation/config/schema.py` (approval keys, `command_allowlist`).

Edge cases / failure modes:
- No scope (unresolved) → blocked, never "approved because nothing was configured" (R02.8).
- `mode: off` arrives from YAML 1.1 as `False`; unknown mode strings fall back to `manual`.
- Deny globs are matched case-insensitively against several spellings (as typed, normalised,
  each simple command, each simple command with leading wrappers and assignments removed) so
  quoting tricks do not dodge them.
- Allowlist globs never match a compound command (`git *` must not cover `git log; rm -rf ~`).
- Interpreter delete: targets equal to, above, or (for globs) matching the running prefix or the
  interpreter file; relative paths are resolved only when the caller passes `cwd`.
- Unattended deny is instant: no request is raised and nothing waits (R02.27).

Our design differences / decisions:
- Detection is our own (P05-07); no regex list is reused.
- On an isolated backend we keep the hardline floor too (phase "Done when": a hardline command is
  refused on an isolated fake backend), not only deny globs as the reference does.
- A surface-less context that is none of cron / single query falls under `unattended_mode`
  (default deny) instead of the reference's silent auto-approve for local non-interactive runs.
- `always` stores the exact command (P05-09); category-wide approvals stay per session.
- `smart` is accepted and behaves like `manual` until P08.

Tests we will write:
- Order table: hardline + auto-approve → blocked; hardline + mode off / cron approve / allowlist →
  blocked; deny glob on isolated → blocked; isolated + dangerous → approved without asking;
  allowlist exact and glob hits; compound command misses a glob; cron/single-query/unattended deny
  → blocked with no request raised; approve modes → approved; mode change mid-session; session
  pattern approval; prepared decision consumed once; no scope → blocked.
- Floors: interpreter/venv delete (exact, ancestor, glob, relative with cwd), `sudo -S` with and
  without a configured password, deny-glob variants.

## Decisions for the lead
- Isolated backends keep the hardline floor (see above). Change: drop `_hardline` from the
  isolated branch in `policy.decide`.
- Surface-less, non-cron, non-single-query contexts use `approvals.unattended_mode` (default deny).
- The config key is `approvals.timeout` (existing schema name), not `approvals.timeout_s`.
- New keys: `approvals.cron_mode`, `approvals.single_query_mode`, `approvals.unattended_mode`
  (deny|approve, default deny), top-level `command_allowlist` (list of strings).

Compare checklist (filled after the rewrite; reopened `tools/approval.py`,
`tools/approval_floors.py`, `tools/approval_context.py`, `tools/approval_yolo.py`):
- [x] Floors before yolo, `mode: off`, cron approve and the allowlist (test:
  `tests/unit/safety/approvals/test_policy.py::test_hardline_beats_every_bypass`)
- [x] Deny globs apply on isolated backends (test: `test_policy.py::test_deny_glob_blocks_on_isolated_backend`)
- [x] Host-mounted container is not isolated: flag-driven only (test:
  `test_policy.py::test_isolation_comes_from_the_flag_only`)
- [x] Unparseable quoting fails closed (test: `test_policy.py::test_malformed_command_fails_closed`)
- [x] Unattended deny is instant, no request (test: `test_policy.py::test_unattended_contexts_deny_instantly`)
- [x] `mode: off` as YAML boolean (test: `test_policy.py::test_mode_spellings`)
- [x] Allowlist refuses compound commands for globs (test: `test_policy.py::test_allowlist_glob_skips_compound_commands`)
- [x] Deny glob sees through quoting and wrappers (test: `test_floors.py::test_deny_glob_variants`)
- Deliberate differences: hardline kept on isolated backends; surface-less runs default to deny;
  no plugin hooks around approvals yet (P13); no pending-approval queue (the broker parks the
  request instead). `status: pending` exists in the type but nothing in P05 returns it: an
  interactive context without a broker falls under `approvals.unattended_mode` (fail closed
  instead of a card nobody can answer).
- Not taken: the parser-limit "saved script" recovery (the model gets advice to write a script
  file instead); smart pre-judge and its denial breaker (P08); hosted-room policy overrides.

## Status

Done (2026-10-18). Shared edits: `src/indus/foundation/config/schema.py`,
`src/indus/safety/approvals/__init__.py`. The terminal tool (P05-11) calls `decide` with an
`ApprovalContext` built from its tool context and the environment's flags.

## Integration (lead)

Merged in the P05 wave 2 integration (2026-10-18, lead's verify tree).
- `terminal.pipeline_gate` builds a real `ApprovalContext` and maps `run_context` through
  `terminal.run_context_of` (`interactive`, `cron`, `single_query`, `unattended`; anything else is
  `interactive`). Real-pipeline check: `tests/unit/tools/builtin/test_terminal_plan.py::test_the_real_pipeline_blocks_a_hardline_command_unattended`.
- P06 seam: the host must offer `extras["run_context"]` (cron, one-shot, unattended surfaces);
  until then every terminal call is judged as interactive (the pipeline still treats a call with
  no broker as unattended).
