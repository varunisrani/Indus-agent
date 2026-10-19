# Task: Monitor mode (P14-16) Phase: 14 Owner: wave-17 builder C3

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `cron/monitor.py` (all). Research
§3.15.4 step 4. Closed before writing.

What it does (own words): a job may name a cheap source — a script in `<home>/scripts/` or an
http(s) URL — that runs before the agent on every tick. Its exact output bytes are hashed; same
hash as last time → the run is skipped (`no_change`, saved as such, no model call). New hash (or no
hash yet) → the prompt gets a block with the new output (≤ 8,000 chars) and, after the first time,
a unified diff against the last output (≤ 4,000 chars). A failing source is an error for that run,
never a change, and leaves the stored hash and snapshot alone. URL sources go through the P05 URL
policy (SSRF floor, private ranges refused unless allowed), time out after 30 s and must not exceed
256 KiB (a bigger body is an error, not truncated).

Inputs → outputs: `check_monitor(scope, job, run_script=…, client_factory=…)` →
`MonitorOutcome{status: first|changed|no_change|error, block, error, state}`; `state` is the new
`monitor_state {hash, last_output_path}` for the caller to store (C1 field); the snapshot file
`<home>/cron/output/<job_id>/monitor_last_output.txt` is written here, atomically, before the agent
runs. `validate_monitor(monitor_script, monitor_url, no_agent)` → error text or None (used at
create/update by the tool and the CLI).

Files: `src/indus/cron/monitor.py`, `tests/unit/cron/test_monitor.py`.

Edge cases / failure modes: both script and URL → refused at create; with `no_agent` → refused;
non-http(s) URL → refused; redirect into a private address → refused by the safe transport on that
hop; body exactly 256 KiB accepted, one byte more refused; non-UTF-8 body hashed as bytes, shown
with replacement characters; the snapshot missing while a hash exists → diff against empty text;
script runner raising → error.

Our design differences / decisions: our block wording; the hash covers the raw response bytes for
URLs (text for scripts); the script source is run by C2's script runner (P14-09) through the
`run_script` port so the folder confinement and env rules are the same as for pre-run scripts;
oversize URL bodies are refused (row says error) rather than truncated.

Tests we will write: MockTransport URL: same body twice → second suppressed (fake model never
called); changed body → diff ≤ 4,000 and output ≤ 8,000; 300 KiB → error; private URL refused;
failing script leaves hash and snapshot unchanged; validation table.

Compare checklist (reopened `cron/monitor.py`):
- [x] exact-bytes hash; unchanged → no run (`tests/unit/cron/test_monitor.py::test_same_body_twice_suppresses_the_second_run`)
- [x] first run gets the output only, later changes a diff ≤ 4,000 plus output ≤ 8,000 (`::test_changed_body_injects_a_bounded_diff`)
- [x] snapshot and state written at detection time, before the agent runs; nothing written on failure (`::test_failing_script_leaves_hash_and_snapshot`)
- [x] URL: http(s) only, 30 s, 256 KiB (`::test_oversized_body_is_an_error`), P05 URL policy (`::test_private_url_is_refused`), HTTP error is an error (`::test_http_error_is_an_error`)
- [x] script and URL exclusive, not with `no_agent` (`::test_validation`)
- Deliberate differences: the reference truncates a body over 256 KiB; the row says error, so we
  refuse it. The reference fetches with a plain URL opener; ours goes through `safe_client`
  (re-checked on every redirect hop). Block wording is ours; state key `hash` (row) instead of
  the reference's name; no `last_changed_at` (not in the row).

## Status

Done (2026-10-19). `src/indus/cron/monitor.py`, `tests/unit/cron/test_monitor.py`. Integration: set
`monitor.RUN_SCRIPT` to C2's script runner (`(scope, script_name, job) -> (ok, stdout|error)`),
call `check_monitor` before prompt assembly, store `outcome.state` on the job, record
`no_change` runs without a model call and pass `outcome.block` to P14-10's change-block slot.
