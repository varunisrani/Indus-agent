# Task: Trajectory converter, one shared (P22-03) Phase: 22 Owner: wave-19 builder R1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/agent_runtime_helpers.py`
(only the trajectory helpers near the top), `agent/tool_dispatch_helpers.py` (the trajectory
message normaliser and the multimodal summary it calls), `website/docs/developer-guide/
trajectory-format.md`. Research 10 §3.3. Closed before writing.

What it does (own words): turns one stored conversation (provider-shaped messages) into a training
sample: a list of `{from, value}` turns using the open function-calling convention (`<tools>`,
`<tool_call>`, `<tool_response>`, `<think>`). Every writer of samples (batch, the interactive saver
later, the SWE runner later) calls this one function, so the formats never drift (R10.7).

Inputs → outputs: `convert(messages, *, prompt, tools)` → `Conversion{turns, bad_arguments}`;
`system_value(tools)` (turn 0 text); pure, no IO. `tools` is the tool array the run sent
(`{"type": "function", "function": {...}}` entries, or bare `{name, description, parameters}`).

Rules:
- turn 0 `system` = our own preamble + the tool list as compact JSON (`[{name, description,
  parameters}]`) inside `<tools>…</tools>`; turn 1 `human` = the dataset prompt;
- input rows before and including the first user message are skipped (system rows, the prompt
  itself). Prefill never reaches the stored rows (it is added at request time), so it never shows;
- assistant with tool calls → one `gpt` turn: think block, then the text (if any) and a newline,
  then one `<tool_call>\n{"name":…,"arguments":{…}}\n</tool_call>` per call (compact JSON,
  arguments parsed; unparseable or non-object → `{}` and `bad_arguments += 1`);
- think block = `<think>\n<reasoning>\n</think>\n` from the native `reasoning` field; when there is
  none but the text itself opens with a `<think>…</think>` block, that block is lifted out and
  rendered the same way; else an empty `<think>\n</think>\n`. So every gpt turn starts with
  `<think>` and the closing tag is followed by a newline;
- the run of tool messages after it → one `tool` turn of `<tool_response>\n{json}\n</tool_response>`
  blocks joined by `\n`; each JSON is `{tool_call_id, name, content}` (compact); `name` by
  `tool_call_id` against the preceding calls; by position only when ids are missing (response
  without an id, or calls without ids); otherwise `unknown` (R10.6). `content` parsed when its text
  starts with `{` or `[` and parses;
- image-bearing results: a list of parts becomes its text parts joined by newlines, each non-text
  part as `[image]`; a multimodal envelope dict becomes its `text_summary`. No base64 ever;
- assistant without tool calls → `gpt` turn with the think block; later user messages → `human`
  (text parts joined when the content is a list); tool messages with no assistant before them are
  grouped with names by position over an empty call list (`unknown`);
- every value has trailing whitespace trimmed.

Files: `src/indus/research/trajectory.py`, `tests/unit/research/test_trajectory.py`,
`tests/fixtures/research/trajectories/*.json`, `tests/golden/research/trajectories/*.json`,
`docs/user/reference/trajectory-format.md`.

Our design differences / decisions:
- preamble wording is ours (clean room); only the tags are protocol;
- compact JSON everywhere (tool list, calls, responses);
- names by id first (the reference uses position only);
- the reference's scratchpad tag is not recognised (phase file).

Tests we will write: goldens (reordered responses get names by id; missing ids fall back to
position; extra responses → `unknown`; image result replaced; every gpt turn starts with a think
block; unparseable args → `{}` and counted); prefill-shaped rows before the prompt never appear;
the doc's example equals real output (ACC-O3-2 style check).

## Decisions for the lead
- A response whose id matches no call is `unknown` even if its position would match (position only
  when ids are missing). Change in `_names_for`.
- A leading `<think>` block in the assistant text counts as reasoning when the native field is empty.

Compare checklist (filled after the rewrite; reopened the helpers, the normaliser and the doc):
- [x] think padding on every gpt turn (test: `tests/unit/research/test_trajectory.py::test_shape_rules_hold_for_every_case`)
- [x] names by call id, position only without ids, `unknown` beyond (tests:
  `test_reordered_responses_get_their_names_by_id`, `test_missing_ids_fall_back_to_position_and_extras_are_unknown`)
- [x] image results reduced to text (test: `test_image_results_keep_only_text`)
- [x] prefill never in the sample (tests: `test_prefill_rows_before_the_prompt_never_appear`,
  `test_worker.py::test_tool_loop_writes_a_durable_row_and_closes_the_agent`)
- [x] unparseable arguments → `{}`, counted (test: `test_unparseable_arguments_become_empty_and_are_counted`)
- [x] the format doc's example is real output (test: `test_format_doc_example_matches_real_output`)
- Deliberate differences (D-16.59, D-16.31): own preamble; compact JSON; names by id (the reference
  uses position only, R10.6); a leading `<think>` block in the text counts as the reasoning; tool
  messages with no assistant before them still become a `tool` turn (the reference drops them);
  user content given as parts is turned into text; the reference's scratchpad tag is not recognised.
- Not taken: the reference's `required: null` field in the tool list.

## Status

Done (2026-10-19). Goldens: `tests/golden/research/trajectories/` (5 cases); they change only with
an entry here saying why. `.gitignore` gained `!docs/user/reference/` (the `reference/` rule hid
the format doc).
