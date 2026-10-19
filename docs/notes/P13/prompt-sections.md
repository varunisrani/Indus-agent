# Task: Plugin system-prompt sections (P13-18) Phase: 13 Owner: builder N3

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/plugins_dispatch.py`
(prompt section constants, rendering and per-section checks), `agent/system_prompt.py` (only the
frozen-section restore and where the block sits in the volatile tier).
Evidence: none.

What it does (own words): a plugin may add a short block of text to the system prompt of every new
session with `ctx.register_prompt_section(id, text_or_callable)` (P13-16 records the entry). Each
section is rendered once, when a conversation's prompt is first built, from a read-only mapping
`{session_id, model, provider, platform, profile_name, cwd}`. The rendered bytes become part of the
stored session prompt and are never computed again for that conversation: a rebuild after
compaction and a resume in a new process both reuse the bytes cut out of the stored prompt, so a
plugin that now returns different text cannot change a running conversation's prompt (R07.20,
R01.1 cache stability).

Rules: id `^[a-z0-9][a-z0-9._-]{0,127}$`; anchor only `after_memory`; sections sorted by id; each
section's text at most 4,000 chars; at most 32 sections; the whole block (with its delimiters and
frames) at most 8,000 chars — a section that would push past the total is skipped and later ids
may still fit; a section that raises, returns a non-string, returns blank text, is over size or
contains our delimiters is skipped with a warning; every accepted one is logged (id, plugin, chars).

Inputs → outputs: `PluginHost.render_prompt_inputs()` entries + the build context → one block

```
<!-- indus-plugin-sections:start -->
<!-- indus-plugin-section id=<id> chars=<n> -->
<text>

<!-- indus-plugin-section id=<id2> chars=<m> -->
<text2>
<!-- indus-plugin-sections:end -->
```

placed in the volatile tier right after the memory blocks (memory, user profile, external provider
block), before the skills index. No sections → no block, so prompts without plugins are byte-for-byte
unchanged (golden files).

Files: `src/indus/plugins/prompt_sections.py` (render, frame, restore, `FrozenSections`,
`PluginSectionsBuilder`), `tests/unit/plugins/test_prompt_sections.py`; shared:
`src/indus/core/prompt/builder.py` (new `BuildContext.plugin_sections` slot, placed after memory).

Edge cases / failure modes:
- a callable that hangs: rendered on the `hooks` pool with the hook deadline
  (`plugins.hook_callback_timeout`, 30 s; 0 = none); a miss skips the section, the session starts.
- the stored prompt holds a block that does not re-frame byte-for-byte (hand edits, a user file that
  imitates our markers): not trusted, the restore yields no sections rather than half of them.
- a stored prompt exists but has no block: the conversation started without sections; it keeps none
  (no late additions mid-conversation).
- no stored prompt (new session, or the store is unreadable): render now.
- the same agent rebuilds after compaction: the frozen block it already holds is reused.

Our design differences / decisions:
- Our own delimiter and frame text (`indus-plugin-section id=… chars=…`), no heading line.
- Rendering runs on the hooks pool with the hook deadline (the reference calls inline): plugin code
  must not be able to hang session start.
- Placement: directly after the memory blocks (the anchor's name); the reference puts its block
  after the skills index. Both are in the volatile tier, so the cached prefix is the same.

Tests we will write: 4,001-char section rejected; 33rd rejected; total over 8,000 drops later ids;
resume with a plugin that now returns different text restores the stored bytes; compaction rebuild
reuses the frozen block; invalid id / raising / non-string / marker-carrying sections skipped;
callable gets the frozen session info; hanging callable skipped at its deadline (fake clock); no
sections → prompt bytes unchanged.

## Decisions for the lead
- Placement after the memory blocks and before the skills index (see above); one-line change in
  `builder.py` to move it.
- The plugin host is not created by any session host yet, so `PluginSectionsBuilder` is wired by
  `plugins.session.plugin_options` (N3) for the lead to call from `cli/runtime` once plugins load.

Compare checklist (filled after the rewrite; reopened `hermes_cli/plugins_dispatch.py`,
`agent/system_prompt.py`):
- [x] 4,000 per section, 32 sections, 8,000 total counting the container (test:
  `test_prompt_sections.py::test_total_budget_drops_later_ids`)
- [x] over-budget sections are skipped one by one, later ones may still fit (same test)
- [x] reserved markers inside a section refused (`::test_bad_sections_are_skipped`)
- [x] non-string / raising / blank results skipped (`::test_bad_sections_are_skipped`)
- [x] session info is a read-only mapping (`::test_callable_gets_frozen_session_info`)
- [x] restore accepts only an exact canonical container (`::test_tampered_block_is_not_trusted`)
- [x] resumed session never re-runs plugin code (`::test_resume_restores_stored_bytes`)
- Deliberate differences: the reference keeps the previous frozen sections when a re-render fails at
  a rebuild boundary; we never re-render within a conversation at all (the frozen block is held by
  the session's builder and restored from the stored prompt), so that case cannot arise.
- Not taken: per-section `max_chars` below the cap (no API asks for it).

## Status

Done (2026-10-19). Shared file: `src/indus/core/prompt/builder.py` (slot only).
