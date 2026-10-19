# Task: Delivery targets and local delivery (P14-13) Phase: 14 Owner: wave-17 builder C3

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `cron/scheduler_delivery.py`
(target tokens and their resolution `_resolve_single_delivery_target`, `_resolve_delivery_targets`,
`_normalize_deliver_value`, the redaction helper `_redact_cron_payload`, the wrapper inside
`_deliver_result`, the unresolved-target outcome, the verification bookkeeping; platform transports,
mirrors, continuable threads, bot-chat and the durable queue skipped), `cron/scheduler_delivery_origin.py`
(relay discriminators only), `cron/delivery_outcome.py` (queued-delivery settling; not taken).
Research: `docs/plan/research/07-learning-skills-plugins-cron.md` §3.15.6. Closed before writing.

What it does (own words): every scheduled run ends with "send this text somewhere". The target
grammar turns a job's `deliver` (and `failure_deliver`) string into a list of `DeliveryTarget`
values; the delivery module looks up one deliverer per platform in a registry and hands each target
a request whose text was already secret-masked and wrapped. This phase registers only `local`, whose
"message" is the output document the runner already saved (plus an optional local notification);
P15 registers the chat platforms through the same `register_deliverer` call and never parses
targets itself. Delivery reports its own status (`delivered`, `delivery_failed{reason}`,
`unverified`, `suppressed`), kept apart from whether the run succeeded, so a broken chat never
counts as a failed job (R07.17).

Inputs → outputs: `parse_targets("origin, telegram:-100123:42, local")` → `ParsedTargets{targets,
problems}`; `deliver(scope, job, text, for_failure=…, output_path=…, attachments=…)` →
`DeliveryResult{status, reason, outcomes}`; `register_deliverer("telegram", fn)` → undo callable.

Files: `src/indus/cron/targets.py`, `src/indus/cron/delivery.py`, `src/indus/cron/ports.py` (the
narrow job/store view C3 needs from C1/C2, see "Decisions"), `tests/unit/cron/test_targets.py`,
`tests/unit/cron/test_delivery.py`, `tests/unit/cron/_stubs_c3.py` (stand-in job + backend, test
only). Shared: `src/indus/foundation/config/schema.py` (+ its test): `cron.wrap_response`,
`cron.local_notify`.

Grammar (ours, platform-neutral, shared with P15):
- tokens split on commas, trimmed, empty pieces dropped; an empty or missing value means `local`;
- `local`, `origin`, `all` are reserved words (any case);
- `<platform>` → that platform's home channel (P15 resolves it; `chat_id` None);
- `<platform>:<chat_id>[:<thread_id>]` → explicit; the platform name is lower-cased, chat and
  thread keep their case; a platform may register its own address splitter (chat ids that contain
  `:`, such as Matrix room ids) — the default splits at the first `:` of the remainder;
- platform names are checked against a known list (the P15/P16 adapters) plus anything registered
  by `register_platform` or `register_deliverer`; an unknown one is a `unknown_platform` problem and
  is never routed to `local`;
- duplicates collapse by (platform, chat, thread), first spelling kept, order kept.

`origin` pinning: a job created from inside a cron run (session id `cron_<job>_<stamp>`, P14-11)
has `origin` replaced at create time by the parent job's first concrete target (its own `origin`
resolved, or an explicit `platform:chat`), else `local` — `pin_origin(deliver, parent_targets)`.

Edge cases / failure modes:
- empty or whitespace payload → `suppressed`, nothing sent (also after redaction);
- the redactor raising → the payload becomes a fixed notice (ours) and that is delivered; the job
  name in the header goes through the same redaction (a name could carry a secret);
- `origin` with no stored origin (CLI-created jobs) → `local`, never an error;
- a platform target with no registered deliverer → `delivery_failed` "platform delivery needs the
  gateway" (before P15); `all` with no `all` deliverer → `delivery_failed`;
- an unknown platform name in the stored value → `delivery_failed{unknown_platform}`;
- a deliverer raising → that target fails with the exception type and a redacted message; others
  still run; a receipt without evidence → `unverified`;
- attachments: only paths the runner hands in as `Path` objects, resolved (symlinks followed) and
  required to sit inside the job's own output folder, then passed through P05
  `path_guards.read_denial`; anything else is dropped and reported. The module never extracts file
  paths from the reply text (model or MCP output), so a path written by the model can never be
  attached.

Our design differences / decisions:
- our wrapper wording: a header line naming the job and its id and a footer saying the agent does
  not see this message and how to manage the job (`indus cron …`); `cron.wrap_response: false`
  sends the bare text;
- local notification: `cron.local_notify` (default off); when on, the registered local notifier
  (default: one line on stderr when stderr is a terminal) gets the job name and the first line;
- no media extraction from text at all (the reference parses media markers out of the reply);
- no mirror, continuable threads, bot-chat, durable queue (P15/P16, H19 P3).

Tests we will write: parser table (`telegram:-100123:42`, `origin,all`, duplicates, case,
`myspace` → unknown_platform, empty, custom splitter); origin pinning; local delivery writes nothing
new and reports the document; redactor raising → fixed notice; empty payload not sent; wrapper on/off;
platform target without deliverer → failed with the gateway reason; deliverer registry undo;
unverified receipt; attachment outside the output dir dropped; failure lane uses `failure_deliver`.

## Decisions for the lead
- `ports.py` is a narrow Protocol (`CronBackend`) over what C1/C2 provide (store, ledger, ticker,
  notepad). The C3 surfaces (tool, CLI, slash) take it from `ports.backend_for(scope)`; at
  integration set `ports.BACKEND_FACTORY` to an adapter over C1's store and C2's ticker. Jobs are
  read through `ports.field(job, name)` so the pydantic model or a dict both fit.
- Known platform list = the P15/P16 adapter names; plugin platforms call `register_platform`.

Compare checklist (filled after the rewrite; reopened `cron/scheduler_delivery.py` target
resolution, `_redact_cron_payload`, `_deliver_result`, `_unresolved_delivery_outcome`):
- [x] comma tokens, list values flattened, dedup on (platform, chat, thread) (test: `tests/unit/cron/test_targets.py::test_parser_table`)
- [x] `origin` without a stored origin is local, never an error (`tests/unit/cron/test_delivery.py::test_origin_without_a_stored_origin_is_local`)
- [x] origin of a cron-created job pinned to the creating run's target or `local` (`test_targets.py::test_cron_created_job_origin_is_pinned`, `test_tool.py::test_cron_created_job_origin_is_pinned_to_the_parent`)
- [x] masking forced on every lane, job name masked, failure → fixed notice (`test_delivery.py::test_secrets_are_masked_even_with_log_redaction_off`, `::test_redactor_failure_sends_the_fixed_notice`)
- [x] wrapper on by default, `cron.wrap_response: false` sends the bare text (`::test_wrapper_on_and_off`)
- [x] empty payload never sent (`::test_empty_payload_is_never_sent`)
- [x] failure notices use `failure_deliver` when set, else `deliver` (`::test_failure_lane_uses_failure_deliver`)
- [x] delivery status separate from run status; a send without evidence is `unverified` (`::test_deliverer_errors_unverified_and_registry_undo`, `::test_delivery_failure_leaves_the_failure_streak`)
- [x] `all` with nothing to resolve it is a delivery failure (`::test_platform_without_a_deliverer_needs_the_gateway`)
- Deliberate differences: the reference silently skips a target whose platform it cannot resolve
  and logs it; we report `unknown_platform` in the delivery reason (row: never routed to local).
  The reference extracts media markers from the reply text and filters them by a path policy; we
  never read paths out of reply text — attachments come only as `Path` objects from the runner,
  inside the job's output folder, re-checked with the P05 read guard
  (`::test_attachments_only_from_the_job_output_folder`). Wrapper wording is ours.
- Not taken: platform transports, live-adapter/standalone lanes, mirrors and continuable threads,
  bot-chat targets, the durable delivery queue and queued-outcome settling, warning-notification
  suppression per platform (P15/P16; H19 P3).

## Status

Done (2026-10-19). `src/indus/cron/targets.py`, `src/indus/cron/delivery.py`, `src/indus/cron/ports.py`,
`tests/unit/cron/test_targets.py`, `tests/unit/cron/test_delivery.py`, `tests/unit/cron/_stubs_c3.py`
(stand-in). Shared: `foundation/config/schema.py` (`CronSection`: `wrap_response`, `local_notify`
and the C3 rows below) + `tests/unit/foundation/config/test_schema.py`. Integration: the runner (C2)
calls `delivery.deliver(scope, job, text, for_failure=…, output_path=…)` after writing the output
document, and puts the result into `bookkeeping.RunOutcome.delivery`.

## Integration (lead, P14 landing)
Landed with C1/C2/C3 together: one backend `service.cron_host.CronHost` behind `ports.BACKEND_FACTORY` (C2's `set_ports_factory`/`ports_for` were folded into it), stand-ins deleted, tests on the real store (`tests/unit/cron/world.py`). Details and open seams: `docs/plan/phases/LEAD-NOTES.md`, "P14 landing notes (lead)"; differences D-16.220 … D-16.237.
