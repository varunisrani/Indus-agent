# Task: Suggested jobs and blueprints (P24-46) Phase: 24 Owner: wave-39 builder B4 (note begun by wave-38 S1)

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/suggestions_cmd.py`
(all), `hermes_cli/blueprint_cmd.py` (all) — read again by B4 (wave 39) before writing. Closed
before writing; reopened for Compare. Research:
`docs/plan/research/07-learning-skills-plugins-cron.md` §3.15.11. Evidence: none.

What it does (own words):
- Blueprints: a job template whose recurrence is fixed (`schedule` is a cron template using only
  `{minute}`, `{hour}`, `{dow}`, `{interval}`; an interval template is `every {interval}m`) and
  whose human parts are slots with a schema:
  `time` (`HH:MM`), `weekdays` (`daily`, `weekdays`, `weekends`, or day names / numbers 0–6),
  `choice` (one of `options`), `interval` (minutes, bounded), `text` (a target; length bounded).
  A value outside its slot's schema (weekday 8, `25:00`, an option not listed) is refused; an
  unknown slot name is refused; a missing slot takes its default or is refused. A filled blueprint
  is a job spec (cron expression, prompt with `{slot}` placeholders filled, name, deliver). The same
  blueprint renders as a form description (dashboard/desktop), a pre-filled `/cron add` line and an
  agent seed prompt (asks for each slot, then creates the job through the cron tool).
  Sources: a small starter catalogue written by us, and `blueprint` blocks in skills' frontmatter
  (`metadata.indus.blueprints` list or `blueprint` mapping; key shown as `<skill>:<key>`).
- Suggestions: a per-profile store `<home>/cron/suggestions.json` of offered jobs, each with a
  dedup key, title, description, source (`catalogue`, `skill`, `usage` for the P09 review fork,
  `integration`) and the stored spec. `refresh` offers starter-catalogue and skill blueprints that
  fill from defaults alone; anything resolved (accepted or dismissed) is latched by its dedup key
  and never offered again, whatever later refreshes find. Accept = the normal P14 job creation
  (`CronBackend.create`) with the stored spec, exactly once; dismiss = latch. Nothing schedules a
  job by itself. At most 20 pending.
- Surfaces: `indus cron suggestions [list|accept <n|id>|dismiss <n|id>|refresh]`,
  `indus blueprint list|show <key>|use <key> [slot=value …] [--dry-run]`, `/suggestions`,
  `/blueprint` (CLI; `/blueprint <key>` without values hands the agent the seed prompt).

Files: `src/indus/cron/suggestions.py`, `src/indus/cron/blueprints.py`,
`src/indus/cli/commands/blueprint.py`, tests `tests/unit/cron/test_suggestions.py`,
`test_blueprints.py`; shared: `cli/commands/cron.py` (adds the `suggestions` group),
`cli/app.py` (`blueprint` row), slash registry + dispatch (rows `suggestions`, `blueprint`), CLI
contract tables.

Edge cases / failure modes:
- concurrent accept from two processes: the store lock makes the second see no pending entry.
- accept whose creation fails (bad spec, store busy) keeps the suggestion pending and says why.
- a skill blueprint with a schedule using other placeholders, or a fixed field that is not a
  cron field, is ignored with a warning (never offered).
- name matching for `/blueprint`: exact key, then prefix of key/title words, then substring,
  then a close spelling of a key (difflib, 0.6); several matches are listed; no match names the
  nearest keys (0.4).
- `/blueprint <name> word` (a word without `=`) is refused with a hint (the reference ignores it).

Our design differences / decisions:
- our catalogue content and our seed wording; `{interval}` instead of `{interval_min}`.
- cron is a sibling of `learning` (layer rule), so skill blueprints are read straight from
  `<home>/skills/**/SKILL.md` frontmatter rather than through the skill resolver.
- no `/suggestions clear`; `refresh` instead of `catalog`.

Tests we will write:
- dismissed suggestion never returns after a catalogue refresh; accept creates exactly one job
  with the rendered schedule (twice → still one); nothing scheduled without accept; slot outside
  its schema (weekday 8) refused; time/weekday/choice/interval/text rendering; skill blueprint
  parsing; seed, form and `/cron` line rendering; CLI verbs.

## Decisions for the lead
- Gateway / TUI surfaces of `/suggestions` and `/blueprint` call `indus.cron.suggestions.
  handle_suggestions(scope, args, backend=…, surface="gateway", origin={platform, chat_id,
  thread_id})` and `indus.cron.blueprints.handle_blueprint(…)` (a reply with `seed` set is sent
  to the agent as the user's next turn); the P09 review fork's `usage` suggestions and connected
  integrations call `indus.cron.suggestions.offer(scope, dedup_key=…, title=…, spec=…,
  source="usage"|"integration")` — wiring owed. The registry rows are offered on every surface
  (owner P24); the classic CLI has handlers.
- Blueprint jobs deliver to `origin` by default (local when the chat has none).
- Accept runs the cron prompt scan and the deliver check before `CronBackend.create`; a refused
  accept keeps the suggestion pending.
- Desktop / dashboard forms: `blueprints.form(bp)` is the field list; no route added here.

Compare checklist (filled after the rewrite; reopened both reference files):
- [x] `/suggestions` list / accept / dismiss wording and the per-surface follow-up hint; accept
  aliases (`add`, `schedule`) and dismiss aliases (`no`, `reject`) — added after Compare (plus
  `yes`).
- [x] `catalog` → our `refresh` (catalogue + skill blueprints that fill from defaults); `clear`
  not taken (resolved records are the latch).
- [x] accept uses the chat origin when the surface passes it (reference reads session env).
- [x] `/blueprint`: no args lists; name alone → seed for the agent; name + values → direct create;
  fill error → message plus "run without values" hint; matching passes incl. fuzzy (added after
  Compare); "did you mean" on no match (added after Compare).
- [x] seed contents: title, key, description, each slot with options / default / optional / help,
  the schedule template and how each placeholder fills, the prompt template, confirm first.
- Deliberate differences: see above (`{interval}`, `refresh`, stricter leftovers, deliver
  `origin` default, prompt scan on accept).
- Not taken: `/suggestions clear`; plugin-declared blueprints (only skills declare them here).

## Status

Done (2026-10-21, wave 39 B4): `cron/blueprints.py`, `cron/suggestions.py`,
`cli/commands/blueprint.py`, `indus cron suggestions` verbs, `/suggestions` and `/blueprint`
(registry rows + classic CLI handlers); tests `tests/unit/cron/test_blueprints.py`,
`tests/unit/cron/test_suggestions.py`. Gateway / TUI / desktop surfaces owed (see Decisions).

## Integration (P24 integrator, 2026-10-21)

Wired: `/suggestions` and `/blueprint` are gateway chat commands (idle only): accepted jobs deliver
to that chat, `/blueprint <name>` sends the blueprint's questions as the next turn (`Rewrite`). The
desktop slash catalogue and the ACP golden frames were regenerated for the two new commands. Owed:
`cron.suggestions.offer` from the review fork and integrations; dashboard / desktop forms over
`blueprints.form`.
