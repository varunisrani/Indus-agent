# Task: Desktop tool keys, enabled toolsets, palette entries, pet list (W60-L2) Phase: ui-parity Owner: builder w60l2

Status: Done (2026-10-25) for all four items, each only as far as our backend has the data.
Uncommitted in the w60l2 worktree for the lead.

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): no reference source opened; the
third compare's gap list and screenshots were the input.
Evidence: reference screenshots of the third compare (never in the repo), desktop
`settings-tools-and-keys-tools`, `settings-advanced-tool-access`, `command-palette`,
`settings-appearance-pet`, light.

What it does (own words):
- Tool keys (W60-L2-1): the tool registry's own field for this (`requires_secrets`) is empty for
  every built-in tool; the names live on the tool backends. The web providers and the cloud
  browser providers now say which key and address variables they read (`secret_names` /
  `url_names`, the names their code already reads), and so does the Camofox engine (its optional
  bearer key). `indus.tools.key_needs.tool_key_needs()` gathers those, the browser's own-Chrome
  address (`INDUS_BROWSER_CDP_URL`) and, when given a registry, every tool's `requires_secrets`
  (plugin tools). `GET /api/tools/keys` (scoped read) answers them per backend with `is_set` only
  (secret store, the profile's `.env`, the environment) — never a value or a preview. Model
  provider keys (image, speech) stay on Providers. Tools & Keys lists the variables grouped by
  toolset (WEB TOOLS, BROWSER TOOLS), each row "Set" / "Not set"; opening a row shows which
  backend reads it and saves or removes it through `/api/env` (a URL in a plain field, a key in
  the masked one). Keys a person added that no tool declares stay below as "Other keys".
- Enabled toolsets (W60-L2-2): the config has `platform_toolsets` (per platform; the CLI list is
  what new interactive chats get, else the `indus-cli` bundle). It is a mapping, so the schema
  form never showed it. Advanced › Tool access now starts with an "Enabled toolsets" field (comma
  separated). `GET /api/tools/toolsets` adds `selection` (the list, and whether it is saved);
  `PUT /api/tools/toolsets {toolsets}` checks every name against the resolver (a typo is 400
  naming it, an empty list is 400) and saves through the config writer. Settings search finds it.
- Command palette (W60-L2-3): Agents and Learning map were already entries; they now come right
  after Scheduled jobs and Profiles, as in the reference. The Projects group always ends with
  "Manage projects" (Settings › Workspace › Projects, the projects page we have), so the group
  shows even before the first project. No entry for anything we lack.
- Pet (W60-L2-4): the page already lists the store's pets through `pet.list` (P24-19) as a
  picker; "No pets yet" in the capture is the real state of an empty store. A test now pins the
  list and the pick.

Compare checklist:
- [x] tools & keys: per-tool variable list with set state (reference: one long A–Z list with
  "Paste … key" / "Optional" hints; ours groups by toolset and never shows a preview)
- [x] tool access: Enabled toolsets field above Tool-use enforcement
- [x] palette: Agents / Learning map after Profiles; Projects group present
- [x] pet: list when the store has pets; empty state otherwise

Tests: `tests/unit/tools/test_key_needs.py`, `test_cron_skills_routes.py` (selection, save,
refusals, session gate, tool keys set state, no value in the answer, route list),
`apps/desktop/src/settings/toolAccess.test.tsx` (grouping, Set / Not set, save through
`/api/env`, empty state, toolsets field save and refusal, pet list and pick).
