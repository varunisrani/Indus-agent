# Task: <name> (<task id>) Phase: <nn> Owner: <builder role>

<!-- Copy to docs/notes/<phase>/<slug>.md. Fill everything above "Compare checklist" before writing
code, in your own words. -->

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `<path>` (<which part>), `<path>`.
Evidence: <evidence ids, or none>.
<!-- Nothing read? Say so and why, e.g. "none (product-only fix)". -->

What it does (own words): <a few lines on the behaviour a user or caller sees, the rules behind it,
and what breaks without it.>

Inputs → outputs: <commands, arguments, messages, settings> → <results, events, files, screen state>.

Files: <files this task creates or changes>.

Edge cases / failure modes:
- <empty, oversized, duplicate or missing input>
- <ordering and races: arrives early, retried, interrupted halfway>
- <platform: paths, permissions, case-insensitive file systems, Windows>
- <what the user sees on each failure>

Our design differences / decisions:
- <our names>
- <simplifications or improvements and why; a user-visible one gets a D-id in what-we-change>
- <what we leave out and why>

Tests we will write:
- <one line per group of cases>

## Decisions for the lead
- <a decision the task needed but did not give, the choice taken, and how to change it>

Compare checklist (filled after the rewrite; reopened `<files>`):
- [ ] <case> (test: `<file>::<test>`)
- [ ] <gap found> — fixed by <change>, pinned by `<test>`
- Deliberate differences: <the reference does X; we do Y because ...>
- Not taken: <behaviour left out, and why>

## Status

<Done | Partial | Blocked> (<date>). <What is finished, anything changed outside the task's files,
what is handed on and to whom.>
