# Task: Similarity `--closest` report (P25-18) Phase: 25 Owner: wave-41 builder G1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): none by hand — the skill's
verification reference 06 §2 (similarity ranking is read by a person, not pass/fail by score).
The check itself indexes the reference checkout as it always has (P00).
Evidence: none.

What it does (own words): `--closest N` already probed with small windows and ranked single runs.
It now merges runs that sit within 12 lines of each other on both sides of the same pair of files
into **regions**, scores a region by the sum of its runs' scores (run length / gate size), and
prints the top N regions as `ours:start-end ~ ref:start-end [score, runs, detectors] excerpt`
(`data["regions"]` in `--json`). With `--report` it writes `docs/internals/similarity-report.md`:
one row per region with a stable key (both paths + a hash of the strongest run's excerpt), our
excerpt only (never reference text), and empty **Verdict** / **Note** cells for a person. Verdicts
survive regeneration; a row that drops out of the top N moves to a "Reviewed earlier" table with
its verdict and comes back with it. In report mode a top-N region with no verdict, an unknown
verdict, or `fix` is a violation — the release gate
(`python -m tooling.checks similarity --require-ref --closest 20 --report`).

Files: `tooling/checks/similarity.py` (regions, report, gate), `tooling/checks/common.py`
(`Context.report`), `tooling/checks/__main__.py` (`--report`), `docs/internals/similarity-report.md`
(generated), `tooling/checks/tests/test_similarity_closest.py`,
`tooling/checks/tests/fixtures/simrepo/` (invented paired trees).

Tests: ranking order with line ranges; nearby runs merge, distant ones do not; top-N limit;
ranking alone writes nothing and never gates; one report row per region and each fails without
a verdict; verdicts survive regeneration and pass; `fix` and unknown verdicts fail naming the row;
a region leaving the list keeps its verdict.

## Real run (2026-10-21, 178 s)

`docs/internals/similarity-report.md` holds the 20 closest regions; all 20 verdicts are empty, so
the gate fails 20 times until a person reads them. Shape of the list (for the reader, not a
verdict): Nix module and installer option tables (rows 1–3), MCP/ACP schema fixtures and golden
tool arrays generated from public schemas (rows 4–5, 7–8, 10–14, 18), two gateway modules (rows 6,
9), host facts (row 16), and research-spec prose naming reference features (rows 15, 17, 19, 20).

## Decisions for the lead
- Region score = sum of run scores (dense regions rank above a single run of the same length).
- Verdicts are a person's call (the lead or the owner); this builder did not read reference files
  and left every verdict empty.

## Status

Done (2026-10-21) for the tooling; the report awaits verdicts.
