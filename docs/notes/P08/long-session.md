# Task: Long-session soak, M2 demo (P08-18) Phase: 08 Owner: P08 wave 2 slice B builder

Read: none in the reference (our scenario). Plan: P08 "Done when", LEAD-NOTES "P08 wave 1 landing
notes" seam (18): drive a real `Agent` so usage, cost and compaction counts take the real path.

What it does: one integration test drives a real `Agent` (real loop, real `BoundCaller` /
`RecordingCaller` / `UsageLedger`, real compression engine and prompt lifecycle, an in-memory
store) against `FakeModelServer` on a `FakeClock` for a 300-turn coding session spread over two
simulated hours (24 s between user turns). Each user turn makes the model call a scripted tool that
returns a large output (6,000 characters), then answer. The script is generated from
`tests/fixtures/model_scripts/long_session.yaml` (the task, the turn shape and the sizes; the
fake server needs one turn per request, so the YAML describes the pattern and the test expands
it; usage figures grow with the real transcript so the engine sees provider-shaped numbers).

Asserted:
- compression fires, and every compaction happened at a request whose reported prompt tokens were
  at or above the trigger computed by `compression.threshold.trigger_tokens` for the window, and
  never below it;
- the final answer references the task stated in turn 1 (the summary keeps the user's first
  message, the model's last answer quotes the goal it was given in the final request);
- the stored transcript passes `check_alternation` after the run;
- the system prompt is rebuilt only at compactions: build count = 1 + number of compactions;
- cost and usage totals in the ledger equal the sum of the fake usage the server sent (calls,
  input, output) and the summary calls are recorded under the `compression` tag;
- mid-run a forced 429 storm: the primary's key is rotated (the second key is seen on the wire),
  then the session falls back to the second route (its host gets the request), and the next turn
  goes back to the primary.

The 429 storm segment runs through the real P08-12 pool and P08-13 fallback chain (the
builder's stand-in `_stubs_b.py` was deleted at integration, see below).

Marked `slow`; the `live` variant (`INDUS_LIVE=1` and a real key in `INDUS_LIVE_API_KEY` /
`INDUS_LIVE_PROVIDER` / `INDUS_LIVE_MODEL`) runs 5 short turns against a real provider and skips
cleanly without them.

Files: `tests/integration/core/test_long_session.py`, `tests/fixtures/model_scripts/long_session.yaml`.

Edge cases: the fake clock must move only through the driver (no wall-clock sleeps); the engine's
summary goes to the same fake server (no aux client) so its usage is a `compression` side call and
its turn is part of the script; a 429 storm with `Retry-After` uses the fake clock.

## Decisions for the lead
- `slow` was registered already; a `live` marker is added to `pyproject.toml` (strict markers),
  used by the live variant.
- The soak uses a 64,000-token window (the smallest P03 accepts) so compaction happens often
  (every ~26 turns) without megabyte request bodies; tool output is 6,000 characters per turn.
- Manual two-hour live session (Done when) is an owner item; record its result here.

Results (2026-10-18, this worktree, non-live): 300 turns, 7,200 simulated seconds, 11 compactions
(turns 34, 60, 86, … 294), every one at the post-tool gate with an anchored figure of 55.1–55.4K
tokens against the computed 54,400-token trigger (64K window); prompt built 12 times (1 + 11);
600 main calls and 11 summary calls recorded; cost equals the fake usage at the test price table;
the storm turn sent 4 requests with the first key, 4 with the second, then answered from the
backup host, and the next turn went back to the primary with the first key. Runs in ~17 s.
Live variant: not run here (no key); it skips cleanly.

## Integration (lead)

- `tests/integration/core/_stubs_b.py` deleted. The soak now builds a vendor-family test profile
  (`soakvendor`, the fake host, `context_length_hint` = the 64K window because vendor profiles do
  not read `/models`), puts both keys in its credential pool (P07 rows via `edit_pool` + secrets
  in the profile's secret store) and writes `fallback_providers: [{provider: custom, model:
  backup-model-1, base_url: https://backup.invalid/v1}]` to the profile's `config.yaml`, loaded
  over the defaults. The wire assertions are unchanged (first request key 1, key 2 seen, last
  request on the backup host, next turn back on the primary with key 1); the stand-in's event list
  is replaced by the chain's two `Notice(key="fallback")` texts ("Model fallback: …", "Main model
  restored …").
- Fixture change: the storm's `Retry-After` went from 1 s to 10 s. With 1 s the provider reset is
  the pool cooldown, and the driven fake clock moves ≥ 1 s while the bench is written on a worker
  thread, so the first key was already back before the rotation and the second key was never
  leased (`revert_id` brings the original key back first). 10 s keeps the provider-named reset
  semantics while letting the rotation be observed; with real time the 1 s case still rotates.
- Fallback provider is `custom` (P03 resolves it from the entry's `base_url`; the old `backup`
  name is not a provider P03 knows).
- Result (2026-10-18, verify worktree): 300 turns, 600 main calls, 11 compactions (turns 34, 60,
  …, 294), prompt built 12 times; storm turn: 14 refused requests on the primary alternating the
  two keys (2 per key per lease: P03's same-key retry, then rotation; the 10 s cooldowns lapse
  between rotations so the pool cycles until its per-call budget of 7 runs out), then 2 requests
  on the backup host (tool call + answer); next turn's first request on the primary with key 1.
  ~16 s.
- Follow-up: two-hour manual live session is an owner item (not run). The pool cycling a short
  provider reset until its budget is by design (P08-12 budget) — noted for P09/P10 cost tuning.
