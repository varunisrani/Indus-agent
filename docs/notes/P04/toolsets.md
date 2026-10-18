# Task: Toolsets + resolver (P04-11) Phase: 04 Owner: wave-5 builder D

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `toolsets.py` (whole: core list,
table helpers, static table, `get_toolset`, `bundle_non_core_tools`, `resolve_toolset` memo,
platform gating), `website/docs/reference/toolsets-reference.md` (first 90 lines), `model_tools.py`
(`_select_tool_names`, `_apply_toolset_selection`, legacy names). Test names of
`tests/tools/test_toolsets.py`. Research `02-tools.md` §3.2.
Closed before writing.
Evidence: none.

What it does (own words): names bundles of tools. A toolset lists tools and may include other
toolsets; a session's selection (`-t web,file`, a platform bundle, `all`) is resolved recursively
to a set of tool names, then disabled toolsets are subtracted at the very end so a composite
cannot bring a disabled tool back. Platform bundles (`indus-cli`, `indus-cron`, `indus-acp`,
`indus-api-server`, `indus-webhook`, `indus-gateway`) all start from one shared core list. A
toolset a plugin registers into (or an unknown registry-only toolset) contributes its registered
members, so a registered tool is never orphaned (R02.25, D-16.74). There is no tool that sends
messages on the user's behalf (R02.26), and webhook sessions only get read-only web, vision and
clarify (R02.27).

Inputs → outputs: `Toolset{description, tools, includes, platforms, posture}`; `TOOLSETS`;
`CORE_TOOLS`; `resolve(name, registry=None, scope=None) -> list[str]`;
`select(enabled, disabled, registry=None, scope=None, platform=None) -> set[str]`;
`toolset_names(registry=None, scope=None)`; `is_known(name, …)`; `persistable(names)` (drops
posture toolsets before a selection is written to config).

Files: `src/indus/tools/toolsets.py`, `tests/unit/tools/test_toolsets.py`,
`tests/contract/tools/test_reachability.py`.

Edge cases / failure modes:
- include cycle A→B→A and diamonds resolve (a revisit contributes nothing).
- unknown toolset name → empty (the selection reports it as unknown).
- `all` / `*` → every toolset except platform-gated ones.
- disabling a platform bundle or posture removes only its non-core tools.
- memo keyed (name, registry generation, profile key), cleared past 256 entries; a new
  registration changes the generation so the memo cannot serve stale members.

Our design differences / decisions:
- Core list = the P05 tools (terminal, process_manage, read_file, write_file, patch,
  search_files, web_search, web_extract, todo_list, clarify); later phases append to it.
- `coding` posture = core minus image/TTS/cron/computer-use names (none exist yet, so = core);
  `posture=True`; `persistable()` strips postures so they are never written to config.
- `indus-gateway` = core plus `indus-webhook` for now; messaging bundles join its includes when
  their phases land.
- `safe` = web + vision (no terminal, no file writes).
- Legacy `*_tools` names and MCP-named toolset aliases are not carried (no old configs to read).

Cross-slice names needed: none.

Tests we will write:
- cycle and diamond; disabled subtracted last (even when a composite re-adds); bundle disable
  keeps core; `all` skips platform-gated; registry members merged into a static set;
  registry-only toolset resolves; memo invalidated by generation; two profiles' overlays resolve
  separately; posture stripped by `persistable`.
- contract: every tool registered by the built-in loader (plus a probe set) is reachable from at
  least one toolset; no `send_message` anywhere; webhook bundle is exactly its floor.

## Decisions for the lead
- Gateway union and `safe` membership as above (smallest choice, easy to extend).

Compare checklist (filled after the rewrite; reopened `toolsets.py`, `model_tools.py`):
- [x] recursion with cycle and diamond tolerance (test: `test_toolsets.py::test_cycle_resolves`, `::test_diamond_resolves`)
- [x] `all`/`*` skip platform-gated sets; platform gating in select (test: `::test_all_skips_platform_gated`, `::test_platform_gating_in_select`)
- [x] disabled subtracted last; bundle / posture disable keeps core (test: `::test_disabled_subtracted_last`, `::test_disabling_a_bundle_keeps_core`, `::test_disabling_the_posture_keeps_core`)
- [x] memo keyed by name, registry generation, profile; cleared past 256 (test: `::test_generation_bump_invalidates_memo`, `::test_profiles_resolve_their_own_overlays`, `::test_memo_is_bounded`)
- [x] registry members merged (R02.25, D-16.74), registry-only toolsets (test: `::test_registry_members_merged_into_static_set`, `::test_registry_only_toolset`)
- [x] webhook floor (R02.27), no send tool (R02.26), every registered tool reachable (test: `tests/contract/tools/test_reachability.py`)
- [x] posture never persisted (test: `::test_posture_never_persists`)
- Deliberate differences: bundles reach registered members through the named sets their core
  tools come from (the reference merges only into the set a tool registered under); the webhook
  floor ignores registry members; `select` reports unknown names instead of printing.
- Not taken: legacy `*_tools` names, MCP alias union, implicit plugin-platform bundles, messaging /
  desktop / kanban sets (their phases add them), custom runtime toolsets.

## Status

Done (2026-10-18). Files: `src/indus/tools/toolsets.py`, `tests/unit/tools/test_toolsets.py`,
`tests/contract/tools/test_reachability.py`. Shared: "Decided in" for D-16.74 in
`docs/plan/16-what-we-change.md`.
