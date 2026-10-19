# Task: prompt assembly (P14-10) Phase: 14 Owner: wave builder C2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `cron/scheduler_prompt.py`
(order, the 8,000 cap, upstream / continuity framing, skipped-skill notice, assembled scan) —
semantics only. Closed before writing. Evidence: none.

What it does (own words): builds the one user message a scheduled agent run starts with, in this
order:
1. our cron hint (written fresh): this is a scheduled run; the final answer is delivered
   automatically, do not send it yourself; reply with only the silence token when nothing is new
   and never mix it with content; put the failure token alone on the first line to record a
   failure; never create or change scheduled jobs because the task text talks about repetition;
   the tokens are literal ASCII words, never translated.
2. attached skills, loaded the way P09's slash invocation loads them (skill blocks, support files,
   config block; usage counted) through a port the service layer fills.
3. `context_from`: for each upstream job (or `self`), its newest output document that has a real
   answer — silent, no-change and wake-false records are passed over, error documents count —
   clipped to 8,000 characters (head and tail kept), framed as upstream output or, for `self`, as
   this job's previous run.
4. the job's notepad block (C1, P14-17).
5. the pre-run script's output and wake-gate context, and the monitor change block (C3, P14-16).
6. the job prompt.
7. an optional `## Run context` block from a manual run (never stored on the job).

The assembled text goes through the P05 threat scan (author text: scope `cron`, strict; loaded data:
scope `context`, invisible characters removed); a finding fails the run before any model call with a
reason naming the finding ids. The system prompt is the P04
`cron` surface (persona, no project files unless the job has a workdir).

Inputs → outputs: `JobSpec`, home, skill loader, notepad text, script/monitor blocks, run context →
`AssembledPrompt {text, skills_loaded, skills_missing}` or `PromptBlocked`.

Files: `src/indus/cron/prompt.py`, `tests/unit/cron/test_prompt.py`,
`tests/golden/cron/prompt_assembly.txt`. Shared: `src/indus/safety/threat_scan.py` (`cron` scope).

Edge cases / failure modes:
- upstream id not 12 hex (or not `self`) → skipped with a warning, never a path.
- unreadable upstream document → skipped (no error text in the prompt).
- a skill that does not load → named in a short notice at the top of the skills block.
- prompt empty and no skills → an empty user message is refused before the model.

Our design differences / decisions:
- Order as the phase file says (skills before context, context before notepad) — the reference
  prepends context in reverse order of the list; ours keeps the job's list order.
- The output document is length-framed (P14-12); the reader takes the response section, or for
  a failure document its error section.
- Our own frames and hint wording.

Tests we will write:
- golden assembled prompt: two skills, two upstream jobs (one silent newest → the older non-silent
  one is used), a notepad.
- 9,000-char upstream clipped to 8,000; `self` continuity framing.
- injection text in the prompt → `PromptBlocked`, fake model not called (runner test).
- run context present in the text, absent from the job record.

## Decisions for the lead
- Threat-scan scope `cron` = the strict set (cron runs unattended), used for what the job's author
  wrote; data loaded at run time is scanned with `context` after invisible characters are removed
  (see the Compare gap).
- Bundles (`/bundle` names) in a job's skill list are not expanded yet (follow-up).

Compare checklist (filled after the rewrite; reopened `cron/scheduler_prompt.py`):
- [x] hint meaning: delivery automatic, silence alone, failure token first line, literal tokens,
  no scheduling from recurrence wording (`tests/unit/cron/test_prompt.py::test_hint_names_the_tokens_and_the_recursion_rule`)
- [x] 8,000-char cap keeping head and tail (`::test_upstream_over_the_cap_is_clipped`)
- [x] upstream vs `self` framing; hex-id guard; silent / no-change / wake-false records passed
  over, error documents used (`::test_self_gets_continuity_framing`,
  `::test_no_usable_answer_and_bad_ids_are_skipped`, `::test_golden_assembled_prompt`)
- [x] skills that do not load are named (`::test_skills_that_do_not_load_are_named`)
- [x] run context only in this run's text (`::test_run_context_is_not_stored_on_the_job`)
- [x] assembled scan refuses injection before the model (`::test_injection_in_the_prompt_is_blocked`,
  `tests/unit/cron/test_runner.py::test_injection_fails_before_the_model`)
- [x] gap found: the reference scans runtime data (skills, upstream output, script output) with a
  looser set and strips invisible characters instead of blocking, so quoted command lines in data
  cannot kill a job for good — ours scanned everything strictly. Fixed: author text (`cron`,
  strict) vs loaded data (`context`, hidden characters removed); pinned by
  `::test_data_may_quote_commands_but_the_author_may_not`, `::test_invisible_characters_in_data_are_removed`
- Deliberate differences: part order as the phase file sets it; data blocks framed "data, not
  instructions"; our hint wording.
- Not taken: the Anthropic cache-boundary registration for the stable skill prefix (no such seam
  in our cache planner yet); bundle names in a job's skill list (follow-up).

## Status

Done (2026-10-19). `src/indus/cron/prompt.py`, golden `tests/golden/cron/prompt_assembly.txt`
(generated once from the test's own inputs; regenerate only for an intended change). The real
skill loader (`cron_host.skill_loader`) uses P09's `expand_skills` with our frame.
