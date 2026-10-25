# Task: Data the pages showed empty although the backend held it (W55-F2) Phase: ui-parity Owner: builder w55f2

Status: Built (2026-10-25, wave 55 builder F2, base main a06a405). Uncommitted, for the integrator.

Read: the wave 55 and wave 54 briefs, `.agents/wip/w54/README.md` (follow-ups), the wave 54 gap list
(web cron-blueprints, mcp, mcp-catalog; desktop cron, profiles, mcp) and our own sources:
`src/indus/cron/blueprints.py`, `src/indus/web/routes/{cron,profiles,mcp}.py`,
`src/indus/cli/commands/mcp.py`, `src/indus/learning/skills/{resolver.py,hub/}`,
`src/indus/plugins/packs.py`. Reference source: not opened (the gap list names the missing data).
Evidence ids: none.

What it does (own words): three pages were empty because a route did not pass on data the backend
already had. Each one is now served by the route that owns it and shown by both clients.

- Cron blueprints. `GET /api/cron/blueprints` listed only skills whose front matter carries a
  plain `blueprint` block. It now lists `cron.blueprints.list_blueprints` first (the four starter
  blueprints, then the skills' slotted blueprints), each prefilled from its slot defaults by the
  new `blueprints.prefill` (a slot with no default stays `{name}` in the prompt for the person to
  fill; the schedule is null when a time slot has no default), with the slots as `fields`. The
  plain front-matter blocks follow as before, except for a skill that already has a slotted
  blueprint. A skill turned off in settings offers neither kind (the module's own list reads the
  skill files without the settings, so the route filters it). Every row now carries `key` (unique), `title`, `source` and a nullable `skill`. Web
  Cron › Blueprints and the desktop cron list key and label rows by `key` / `title`; a starter
  blueprint sets up a job with its title as the name and no skill.
- Profile skill count. `GET /api/profiles` rows carry `skill_count`: the skills that profile has
  turned on, counted the way `/api/skills` lists them (loadable, not disabled, plugin skills
  aside), through the skills catalogue whose parse is cached per file. Null when the profile's
  settings cannot be read. Desktop Profiles shows it in place of "—".
- MCP catalogue. The backend has no hosted MCP or connector catalogue: the skills hub sources
  (official, GitHub, URL, ClawHub, LobeHub, browse.sh) serve skills only, and plugin packs say a
  bare name "needs a plugin catalogue" that does not exist. What it does have is the preset list
  of `indus mcp add --preset` (filesystem, memory, fetch, git). That list moved from the CLI into
  `indus.mcp.presets` (the web layer may not import the CLI) with a one-line description each,
  and `GET /api/mcp/catalog` serves it with `installed` per profile. Web MCP › Catalog lists the
  entries with Install (the normal `POST /api/mcp/servers`); the desktop Connectors tab adds a
  Catalog group of the presets not added yet, with an Add button, and no longer shows its empty
  state while the catalogue has entries.

Decisions: W55-F2-1 the blueprint route serves the starter catalogue and the slotted skill
blueprints first, prefilled from defaults, and keeps the plain front-matter rows (one row per
skill); W55-F2-2 `skill_count` counts what `/api/skills` lists as enabled, null on an unreadable
profile; W55-F2-3 the only catalogue our backend has is the MCP preset list, served as
`/api/mcp/catalog` (moved to `indus.mcp.presets`), no hosted catalogue invented.

Follow-ups: a hosted MCP / connector catalogue needs a source (a registry client in `indus.mcp`
with its own trust rules, cache and offline answer) before the catalogue can grow past the four
presets; the web and desktop job forms could render a blueprint's `fields` instead of the
prefilled text; the web Profiles card still counts skills with one `/api/skills` call per profile
and could read `skill_count` instead.

Compare checklist:
- [x] Written from our own sources; no reference code, CSS or assets.
- [x] Only data our backend already holds is served (no invented feature).
- [x] Python: ruff, pyright, import layers, targeted and full pytest.
- [x] Clients: biome, tsc, vitest for the touched projects.
