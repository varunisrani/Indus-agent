# Task: Prompt builder tiers (P04-05) Phase: 04 Owner: wave-5 builder C

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/system_prompt.py` (the
tier assembly function, the gate resolver, the platform-hint override resolver, the timestamp line
and zone bits, the active-profile line, the guidance-part and identity-part helpers),
`agent/prompt_builder.py` (gate model lists, the platform-hint table keys, execution-guidance line
gating, the runtime heading/end constants and the local host hints — structure only, no prose),
`website/docs/developer-guide/prompt-assembly.md` (tier description, platform-hint overrides).
Reference tests skimmed for edge cases: `tests/agent/test_system_prompt.py` (gate on/off cases,
same-day vs later-day date line, surrogate zone name, tier order). Closed before writing.
Research: `docs/plan/research/01-agent-core.md` §3.4. Evidence: none.

What it does (own words): `PromptBuilder.build(ctx)` renders the system prompt once per
conversation as three tiers joined by blank lines, most stable first so a longest-prefix provider
cache reuses as much as possible:

- **stable** — identity (the persona text from P04-06, else our built-in identity); a short pointer
  to Indus's own help (a variant naming the help skill only when `skill_view` is loaded and the
  skills index lists that skill); with tools: task-completion and parallel-tool guidance (each
  behind its own config switch), guidance for tools that are actually loaded (memory, session
  search, skill writing), the steer-trust note, model-family guidance behind two gates
  (`prompt.tool_use_enforcement`, `prompt.execution_guidance`: `auto | true | false | [substrings]`)
  plus an extra block for Google-family models; then, when there is **no** workspace snapshot, the
  workspace guidance and the platform hint.
- **context** — the caller's system text, the project context files (P04-06), then the workspace
  snapshot and, after it, the workspace guidance and platform hint (worktree-specific text always
  after repo-wide text).
- **volatile** — skills index, memory snapshot, user-profile snapshot (all empty slots until P09),
  the active-profile line (names the home path), the date line block, then the runtime block
  (host, home, working directory; or the backend's description) between our heading and our end
  marker. The runtime block always ends the prompt so lifecycle (P04-07) can find it.

The date line has no time of day: weekday + date of the conversation start, IANA zone, abbreviation
(when it differs from the key) and `UTC±hh:mm`. When the build happens on a later day than the
conversation start, a second line gives the rebuild date. Then `Session ID:` (only when the caller
asks), `Model:`, `Provider:`, `Platform:` lines, each omitted when empty.

Platform hints exist for `cli, tui, desktop, cron, api_server`. Config `prompt.platform_hints.<p>`
may be a bare string (append), or a mapping with `replace` and/or `append`; malformed entries are
ignored and the built-in hint stays.

Inputs → outputs: `BuildContext{scope, model, provider, platform, session_id, show_session_id,
tool_names, system_message, cwd, context_length, context_files, persona_without_context_files,
workspace_snapshot, skills_index, memory_snapshot, profile_snapshot, started_at, runtime}` →
`PromptParts{stable, context, volatile, text, warnings, persona_loaded}`.

Files: `src/indus/core/prompt/__init__.py`, `builder.py`, `texts.py`,
`tests/unit/core/prompt/test_builder.py`; shared: `src/indus/foundation/config/schema.py` (new
`prompt` section), `tests/unit/foundation/config/test_schema.py` (pinned defaults).

Edge cases / failure modes:
- no tools → no task/parallel/tool-aware/steer/model-family/workspace guidance at all.
- gate value `True/False`, the words `true/yes/on/always` and `false/no/off/never` in any case, a
  list of substrings (case-insensitive; non-strings in it ignored), anything else = auto.
- zone names that are lone surrogates (locale zones on Windows) go through `safe_strftime`.
- `Model:`/`Provider:` lines are omitted when empty, never printed blank.
- embedder text in the backend description that contains our runtime heading is quoted with `> `
  so only the renderer's own heading ends the prompt.
- unknown platform: no built-in hint; a config `append` alone still yields text.
- config missing (`scope.config is None`) → all defaults.

Our design differences / decisions:
- All prose is ours (D-16.23); family lists are ours (D-16.66): enforcement
  `gpt, codex, gemini, gemma, grok, qwen, glm, deepseek, kimi`; execution discipline
  `gpt, codex, grok, deepseek, kimi, qwen, glm, minimax, mistral`.
- `replace` and `append` together: the reference's code applies both (replacement text, then the
  appended text); its docs say "replace wins". We follow the code (D-16.24) — `replace` wins over
  the built-in hint, `append` is still added.
- No vendor identity line for a provider that misreports its model, no bot-mode protocol, no
  pinned-skills auto-load, no plugin prompt sections, no environment probe line, no desktop
  embedded-pane clarifier, no cron delivery-destination hint (cron delivery is P14). Each slot can
  be added later without moving the tiers.
- The conversation start date comes from `ctx.started_at` (the session row's `started_at`, passed
  by lifecycle) instead of parsing a timestamp out of the session id.
- The builder is `async`: context-file and persona reads run off the loop with a deadline.
- `developer` vs `system` role is a P03 route property; the builder emits text only.

Tests we will write (`tests/unit/core/prompt/test_builder.py`, 53 cases): tier order and separators; tools-absent build has no tool guidance; each gate
on/off by model substring and by config (bool, words, list, auto); Google extra block; tool-aware
guidance only for loaded tools; help-pointer variant; date line has no time of day and carries zone,
abbreviation, offset; two days on an injected clock → second line; session id line only when asked;
platform hint append / replace / both / malformed / unknown platform; workspace snapshot moves the
trailing guidance from stable to context; runtime block last and heading in backend text quoted;
persona replaces built-in identity.

## Decisions for the lead
- Config keys live in a new `prompt` section (`task_completion_guidance`,
  `parallel_tool_guidance`, `tool_use_enforcement`, `execution_guidance`,
  `context_file_max_chars`, `context_file_read_timeout`, `platform_hints`) instead of the
  reference's mix of top-level and `agent.*` keys; P07's config migration maps them.
- The steer marker strings (`STEER_OPEN`, `STEER_CLOSE`) live in `core/prompt/texts.py` so the
  prompt note and P04-19's steer rows share one source; P04-19 imports them from there.
- The help pointer names no docs URL (docs domain is owner-open); it points at `indus --help`.

Compare checklist (filled after the rewrite; reopened `agent/system_prompt.py`,
`agent/prompt_builder.py` gate/hint/runtime parts, the developer guide):
- [x] tier order stable → context → volatile, blank-line join, empty parts dropped (test:
  `tests/unit/core/prompt/test_builder.py::test_tiers_in_order_and_joined_by_blank_lines`)
- [x] trailing workspace guidance + platform hint in stable without a snapshot, after the snapshot
  in context with one (test: `::test_workspace_snapshot_moves_trailing_blocks_to_context`)
- [x] caller system text before project context files (test: `::test_context_tier_order`)
- [x] skills index leads the volatile tier, profile line then date block then runtime block (test:
  `::test_volatile_tier_order`)
- [x] gate resolution: bools, words, list substrings, auto (test: `::test_gate_resolution`)
- [x] execution guidance independent of the enforcement gate (test:
  `::test_execution_guidance_is_its_own_gate`)
- [x] steer note only with tools (test: `::test_no_tools_means_no_tool_guidance`)
- [x] date-only line, abbreviation omitted when equal to the key, `UTC±hh:mm` offset (test:
  `::test_date_line_has_zone_and_no_time`)
- [x] later-day rebuild adds the second line, same day keeps one (test:
  `::test_rebuild_on_later_day_adds_second_line`)
- [x] empty trailer values omitted; session id only when configured (test:
  `::test_identity_lines`)
- [x] platform hint: bare string = append, replace, both, malformed ignored (test:
  `::test_platform_hint_overrides`)
- [x] embedder text quoting the runtime heading is defused (test:
  `::test_backend_description_cannot_fake_the_runtime_heading`)
- [x] help pointer variant depends on `skill_view` and the help skill in the index (test:
  `::test_help_pointer_variant`)
- [x] gap found: a list gate holding an empty string would match every model — we skip blank
  items, pinned by `::test_gate_resolution`
- Deliberate differences: a platform-hint mapping with a non-string `replace`/`append` is treated
  as malformed (built-in hint kept) instead of reading that key as empty; date from `started_at` instead of the session-id stamp and lineage root
  (P02 rows carry `started_at`; lineage-root date can be passed in by the caller); tool-aware
  guidance blocks are separate paragraphs, not one space-joined run; the memory block does not
  vary with which memory stores are on (P09 can add that).
- Not taken: vendor identity line, bot-mode protocol and timeless prompt, auto-loaded skills,
  plugin sections, environment probe line, desktop pane clarifier, cron delivery hint, Telegram
  rich-message hint, scratch-dir line and Windows bash hint in the runtime block (P05 owns the
  terminal environment), kanban guidance.

## Status

Done (2026-10-18). Cross-slice names: none (tool names arrive as a plain set; no stand-in).
Shared edits: `src/indus/foundation/config/schema.py` (new `prompt` section),
`tests/unit/foundation/config/test_schema.py` (pinned `prompt` defaults).
