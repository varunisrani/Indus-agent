# Task: Toolset distributions (P22-02) Phase: 22 Owner: wave-19 builder R1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `toolset_distributions.py` (all).
Research 10 §3.2 (the table of 17). Closed before writing.

What it does (own words): a batch run wants varied training samples, so each prompt gets a random
set of toolsets drawn from a named distribution. Each entry has its own percentage and is rolled on
its own; a `+`-joined group is rolled once so its members always come together. If every roll
misses, the single most likely entry is used so a prompt never runs with no tools.

Inputs → outputs: `DISTRIBUTIONS` (17 `Distribution{name, description, entries}`), `get(name)`
(raises `UnknownDistribution` listing the names), `names()`, `listing(dist)` (entries sorted by
percentage high → low, ties by name), `Sampler(valid, warn).sample(dist, rng)` → our toolset names,
`prompt_rng(seed, index)` → a `random.Random` for one prompt, `TOOLSET_ALIASES` (reference name →
our name) and `our_names(entry)`.

Files: `src/indus/research/distributions.py`, `tests/unit/research/test_distributions.py`.

Edge cases / failure modes:
- an entry whose member is not a toolset in the P04 registry → one warning per (distribution,
  entry) for the sampler's life, entry skipped;
- all rolls miss → the highest-percentage valid entry (first in table order on ties);
- the RNG is injected; a prompt's RNG depends only on the run seed and the prompt index, so a
  resumed run (other workers, other order) samples the same toolsets for the same prompt.

Our design differences / decisions:
- same 17 names, entries and percentages as the research table; descriptions are ours;
- names go through one alias table (D-16.63). Every name the table uses (`web`, `vision`,
  `image_gen`, `terminal`, `file`, `browser`, `search`) already exists under the same name in our
  toolset registry, so today the table is the identity; a rename only touches the table;
- `prompt_rng` seeds `random.Random` with the text `"<seed>:<index>"` (string seeding is stable
  across processes and Python runs).

Tests we will write: 17 distributions; every name resolves in `toolsets.toolset_names()`; the
`browser+search` group co-occurs in exactly every selecting roll over 10,000 seeded rolls; fallback
when all rolls miss; unknown toolset warned once and skipped; unknown distribution lists names;
per-prompt seeding reproducible; listing sorted high → low.

## Decisions for the lead
- Seed mixing `"<seed>:<index>"` (string seed). Change in `prompt_rng`.

Compare checklist (filled after the rewrite; reopened `toolset_distributions.py`):
- [x] all 17 entries and percentages match (test: `test_table_matches_the_research_table`)
- [x] grouped entry rolled once (test: `test_grouped_entry_co_occurs_exactly`)
- [x] fallback to the highest-probability entry (test: `test_all_rolls_missing_falls_back_to_the_top_entry`)
- Deliberate differences: the reference warns on every roll of an invalid entry; we warn once per
  sampler. It uses the global `random`; we inject a seeded RNG per prompt.
- Not taken: nothing.

## Status

Done (2026-10-19).
