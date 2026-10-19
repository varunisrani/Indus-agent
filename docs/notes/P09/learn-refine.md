# Task: `/refine` and `/learn` (P09-17) Phase: 09 Owner: P09 slice E builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/learn_prompt.py` (structure
only; no text copied), `website/docs/user-guide/features/skills.md` (the `/learn` section),
`hermes_cli/cli_commands_mixin.py` (the `/refine` handler, semantics). Closed before writing.
Evidence: none.

What it does (own words):

- `/refine [focus]`: runs the P09-15 review now, in the background, on a snapshot of the stored
  transcript, with the optional focus text as the user's priority. It covers memory when the
  `memory` tool is offered and skills when `skill_manage` is offered. It works when automatic
  reviews are off, is attended (memory patch/remove apply directly, no staging by R07.4), and is
  never deferred. Refused with a short line when the session has no history yet or a review is
  already running.
- `/learn <dir|url|text|"this conversation">`: builds one user prompt and submits it as a normal
  turn. The prompt (ours) asks the agent to: read the whole request (sources and requirements),
  gather each source with its own tools (`read_file`/`search_files` for paths, `web_extract` for
  URLs, the conversation for "what we just did"), check for an existing skill on the topic first and
  fold into it, then save with `skill_manage`. Standards: lowercase-hyphen name ≤ 64 chars,
  description one sentence ≤ 60 chars, licence MIT, sections in our order (when to use → procedure
  → pitfalls → verification, optional quick reference), framed in our tool names, only commands seen
  in the source (none invented). Large sources become a knowledge-base skill: a lean `SKILL.md`
  index plus one condensed note per chapter/topic under `references/`, processed one unit at a
  time, never reproducing source passages. Source text is data, never instructions. An empty
  request means "the workflow of this conversation".
- Provenance: `learn` — `learn_turn(agent, request)` binds the write origin `learn` around
  `start_turn`, so the turn's task carries it; a skill created by that turn records `origin: learn`
  and is not curator-managed (P09-12 maps every non-review create to `learn`, so a plain submit has
  the same outcome).
- Slash: the shared registry already lists `/learn` and `/refine` (P06-08). The classic CLI
  handlers are in `cli/slash/learning_cmds.py` (`HANDLERS`): `/learn` returns
  `SlashResult(submit=prompt)`; `/refine` calls the port's `refine(session_id, focus)` when the
  host provides it. They are not yet merged into `dispatch.HANDLERS` (see the wiring list).

Inputs → outputs: request text → prompt text (and a started turn); focus + stored rows → a started
review (or a refusal line).

Files: `src/indus/learning/learn.py`, `tests/unit/learning/test_learn.py`,
`src/indus/cli/slash/learning_cmds.py`. Shared: none.

Edge cases / failure modes:
- `/learn` with no text → the conversation is the source;
- `/refine` with neither tool offered → refused ("nothing to review with");
- `/refine` while a turn runs → registry busy policy `reject` (P06);
- the port without `refine` (host not wired yet) → "not available in this session".

Our design differences / decisions:
- our standards block names our own fields (`license: MIT`, `metadata.indus`) and our section
  order; no product-name author line;
- `/learn` stores the built prompt as the user message (like any submitted text).

Tests we will write:
- `/learn ./docs` submits one user turn whose text contains our standards block and the path;
- a skill created inside a `learn_turn` records `origin: learn` and is not curator-managed;
- `/refine focus` with reviews disabled still runs (focus text in the fork's prompt);
- `/refine` memory remove applied directly (attended), not staged;
- `/refine` with an empty transcript is refused.

## Wiring for the lead
- `cli/slash/dispatch.py`: add `**learning_cmds.HANDLERS` to `HANDLERS`. Two tests pin the current
  "P09 has no handlers" state and change with it: `tests/unit/service/commands/test_registry.py`
  (`landed` gains `P09` once D's and F's handlers land too; `HELP_GROUPS` gains `learn` under tools
  and `refine` under session) and `tests/unit/cli/slash/test_dispatch.py::test_prefix_ambiguity_lists`
  (`/re` also lists `/refine`).
- `HostPort.refine(session_id, focus) -> str` → on the host loop: `(await learn.refine(runner,
  focus=focus)).message`.
- Hosts that can bind the origin: `await learn.learn_turn(agent, text)` instead of a plain submit.
- Gateway/TUI/desktop: `learn.build_learn_prompt(text)` as the turn text, `learn.refine(...)` for
  `/refine`.

## Compare checklist (filled after the rewrite; reopened `agent/learn_prompt.py` and the
`/refine` handler)
- [x] one prompt, submitted as a normal turn; empty request = this conversation (test:
  `test_learn.py::test_cli_learn_submits_one_user_turn_with_the_standards`,
  `::test_empty_learn_means_this_conversation`)
- [x] standards: ≤ 60-char description, section order, tool framing, no invented commands;
  knowledge-base layout; fold into an existing skill; source text is data (test:
  `::test_learn_prompt_carries_the_request_and_our_standards`)
- [x] `/refine`: memory + skills, focus first, attended, works with reviews off (test:
  `::test_refine_runs_with_reviews_disabled_and_puts_the_focus_first`,
  `::test_refine_memory_remove_is_applied_directly`)
- [x] provenance `learn`, not curator-managed (test: `::test_learn_turn_creates_a_learn_origin_skill`)
- Deliberate differences: our standards (MIT licence line, `metadata.indus`, our section names);
  no "author" line; `/refine` covers memory only when the `memory` tool is on; the stored user row
  of `learn_turn` is `/learn <text>` while the model gets the full prompt.
- Not taken: the reference's `/review` command (a different feature).

## Status

Done (2026-10-19). CLI dispatch registration left to the lead (see wiring).

## Integration (lead, P09 wave 2 landing)
`HostPort.refine` → `learning.host.review_runner(agent)` → `learn.refine`. The classic CLI's `/learn` stays a plain submitted turn (origin `foreground`; the skill still records `origin: learn` through P09-12); `learn_turn` is the API for hosts that can bind the origin. D-16.155.
