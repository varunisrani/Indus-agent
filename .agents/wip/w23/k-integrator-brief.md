# Integrate P17 wave 1: K1 (P17-01..04) + K2 (P17-05..07)

Worktree: /tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/verify6 (from main). Do NOT commit.
The lead commits and lands. Never write under the real ~ or /root. No network.
Running in parallel (do not touch their areas): wave-22 integrator (gateway adapters telegram/discord/slack, Demo 3,
gateway outbound/extras/delivery), P16 builders (gateway/adapters/<matrix,mattermost,signal,email>), the lead landing
P16-01 (gateway/adapters/_http.py, _identity.py).

Patches (scratchpad/patches/, base 78be7b3): w23-k1.patch then w23-k2.patch.
Read .agents/wip/w23/README.md (K1 and K2 sections) in full first, the seven task notes in docs/notes/P17/,
docs/plan/phases/P17-daemon-shared-backend.md (rows 01–07, Traps, Acceptance, Run until green), AGENTS.md, HANDOFF.md,
LEAD-NOTES "P15 waves 2–3 landing notes (lead)".

Steps:
1. `git apply --3way` K1, then K2. Resolve as unions.
2. Reconcile K2's stand-in with K1's real admission service: delete tests/unit/service/_stubs_k2.py and make
   test_fences.py use K1's real `service/admissions.py` (or a tiny test double of K1's *real* interface if a real
   store makes the test unwieldy — but the fence must be exercised against the real receipt/cancel semantics at least
   once). K2 assumed: `receipt(admission_id) -> Receipt|None` (admission_id, state queued/started/terminal/unknown,
   generation once started) and `cancel(admission_id) -> Receipt`. If K1's names/states differ, adapt K2's fences
   (not K1) and note it. Wire the documented seam where both slices are already present: the admission worker calls
   `fences.begin(session, generation)` at claim, `adopt(...)` once the agent exists, `release(...)` at settle — only if
   that does not require P17-08's session host; otherwise record it as wiring for P17-08.
   `grep -rln '^# STAND-IN' src tests` must be empty.
3. Check consistency between the two: error codes (foundation/errors.py runtime.* vs K2's StaleGeneration/
   RequestUnsupported), state names, generation types, event names (`request.settled`, `session.replay_gap`) — one
   vocabulary. Layer rules: state/ below service/; nothing in service imports gateway or cli.
4. Security/robustness review: unknown-outcome turns never resubmitted (restart test); lease holder identity checks
   (pid + start time + namespace + host) cannot be spoofed to steal a lease from a live holder; generation fences
   prevent a stale client from interrupting/steering/answering a newer turn; event-log memory caps hold; request
   re-offer never shows a request to a viewer that cannot answer it; refusal detail text is cleaned (no secrets/paths).
5. D-ids: number this wave's decisions D-16.360 onward (max D-16.379) in docs/plan/16-what-we-change.md §2.7 (+ §3.1
   range row). Include K1's and K2's "Decisions for the lead" (e.g. interrupt of a finished turn = stale with
   current None, discard settles `discarded`, lease timings constants). Add "P17 wave 1 landing notes (integrator)"
   to LEAD-NOTES.md with the wiring still owed to P17-08/13/14/18/22/25.
6. Full gate, all green, no extras installed (`uv sync --locked`):
   uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run lint-imports
   INDUS_STRESS_SECONDS=10 uv run pytest -q -p no:cacheprovider -rf -m "not perf"
   uv run pytest -q -p no:cacheprovider -m perf     # alone, after
   uv run python -m tooling.checks --require-ref
   CI=true pnpm test --maxWorkers=1
   Other integrations may run gates at the same time; a failure that passes alone must pass alone 3x AND be
   explained; never skip or mark tests. The multiprocess lease test must be stable (run it 5x).
7. Credential-literal scan of the diff, no stray processes, no writes under /root or ~.
8. Usage: the lead may message you to wrap up; then stop, reach a consistent state, report.
Stage the final tree with `git add -A`.
Report: files changed beyond the patches, reconciliation done, D-ids, security findings/fixes, gate counts, follow-ups.
