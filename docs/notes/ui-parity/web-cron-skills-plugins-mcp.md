# Task: Web Cron, Skills, Plugins, MCP (W52-c) Phase: ui-parity Owner: builder w52c

## Status

Done (2026-10-24). Cron, Skills, Plugins and MCP rebuilt on the web foundation (W51-a); data flows
unchanged, no backend change. Re-captured in both looks (clear-blue / deep-teal) and compared beside the
reference screenshots page by page.

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): no reference source opened by this builder;
worked from the lead's spec `w52-specs/web-cron-skills-plugins-mcp.md` (its own Read line names the reference
cron page, schedule builder, blueprints gallery, skills page, toolset drawer, skill editor, plugins page and MCP
page, read for structure only).
Evidence: the owner's screenshots of the running reference, light and dark: `cron`, `cron-create`,
`cron-blueprints`, `skills`, `skills-toolsets`, `skill-editor`, `plugins`, `mcp`, `mcp-catalog`,
`mcp-add-server` (looked at while comparing). None are in the repo.

What it does (own words): the four pages keep their data flows (our REST routes) and take the reference look:
a full-width Jobs/Blueprints strip and job cards with icon actions on Cron, a create/edit panel with a schedule
mode picker that writes our grammar directly; a filter panel (All / Toolsets / Browse hub + categories) beside a
skills card on Skills; runtime-provider and install cards plus an installed list on Plugins; a servers section and
a catalogue section with an Add server panel on MCP. Where our backend has no data (MCP and plugin catalogues,
skill create, toolset configuration, chat prefill) the control is shown as a placeholder, never wired to an
invented endpoint.

Inputs → outputs: `GET /api/cron/jobs[?profile=]`, `/api/cron/delivery-targets`, `/api/cron/blueprints`,
`/api/profiles`, `/api/skills`, `/api/tools/toolsets`, hub routes, `/api/plugins`, `/api/memory`,
`/api/config`, `/api/mcp/servers` → the pages; writes go to the existing routes only.

Edge cases / failure modes: one profile only (the filter still lists it); a profile request failing in the
"All profiles" fan-out (its jobs are skipped, an error notice shows); schedules our builder cannot parse back
(edit opens in Custom with the raw text); unavailable delivery targets (labelled, still selectable as before is
not allowed: kept disabled so the server is never asked for a target that is not set up); empty catalogues.

## Decisions for the lead

- W52-c-1 dialog ink: the foundation `Modal` uses the theme ink for title and body; kept (no second ink).
  Both reference modal styles (panel over a washed page, dialog over a dark scrim) use the one foundation
  `Modal` (blurred scrim over the whole app, sidebar included).
- W52-c-2 "Set up" on a blueprint card stays on one line.
- W52-c-3 Cron "All profiles" fans out: one `GET /api/cron/jobs?profile=<name>` per profile from
  `/api/profiles`, merged and tagged; a single profile is one request. Row actions and the job panel
  carry the row's profile (`?profile=`); a profile that fails is skipped unless all fail.
- W52-c-4 "Once" writes a local ISO date-time (`2026-11-02T08:30`), which our grammar reads as a one-shot in
  the agent's zone; "in 2h" style one-shots stay available under Custom. Weekly and Monthly write a
  five-field cron line; Daily writes `daily at HH:MM`; Every interval writes `every <N><m|h|d>`. Edit reads
  a schedule back into its mode, anything else opens as Custom with the raw text.
- W52-c-5 skills on edit: the backend's `JobUpdate` accepts `skills`, so the panel shows the checklist on
  edit too and always sends the list (empty clears); a new job leaves `skills` out when none is ticked.
- W52-c-6 no hover pencil on skill rows (no skill read/write endpoint).
- W52-c-7 the New skill editor composes SKILL.md and "Create skill" copies it to the clipboard with a toast
  naming the path (`~/.indus/skills/[<category>/]<name>/SKILL.md`); Learn a skill shows its fields with the
  start button disabled and a one-line note.
- W52-c-8 plugin install accepts `owner/repo[/path]` as the backend does, keeps our Commit SHA field (pinning),
  sends `force`, and "Enable after install" enables the plugin when the reply names it.
- W52-c-9 MCP test result: one line "Tools: a, b" (each name carries its description as a tooltip) or a red
  failure line; env values are summarised as "N env vars" with the redacted `KEY=value` lines in the tooltip.
- W52-c-10 no Authenticate button yet (the client has no OAuth-flow calls; follow-up).
- W52-c-11 delivery targets that are not set up stay disabled in the Deliver to list (and say "set up
  first"), so the server is never asked for a missing target.
- W52-c-12 the empty Cron card's button is named "Create job" so it does not share the header's
  "Create" name.
- W52-c-13 MCP Add server defaults to HTTP/SSE as the reference; "Bearer token" is our `header` mode (the
  key itself lives on the Keys page; the backend refuses literal secrets).

Foundation additions (in the foundation's own files and style, for the integrator): `Badge` tone
`secondary` (filled chip) and `SectionHeading` (spaced H2 with icon) in `ui/controls.tsx`; `Segmented`
gains `fill` (full-width strip); `Modal` gains size `narrow` (30 rem) and an optional `description`.

## Compare checklist (from the spec §13, ticked after re-capture in both themes)

Common
- [x] Header 52px + line; title in display face; actions right; content padding 23/32/22.
- [x] All corners square; borders 1px in the measured colours; no card tint (except filter panel / modals).
- [x] Badges: condensed lowercase text, 24px tall, success/secondary/outline colours as measured.
- [x] Buttons 28px; primary uppercase bold mono with dark 1px bottom-right edge (L).

cron
- [x] Segmented bar full width 28px, "Jobs" active filled.
- [x] H2 "Scheduled Jobs (N)" with clock icon left; PROFILE select 220px right, bottoms aligned.
- [x] Job card: title + state badge + profile badge; prompt line; mono schedule · repeat · Last · Next.
- [x] Four icon actions right: pause (amber), lightning, pencil, trash (red).
- [x] Empty and load-error states.

cron-create
- [x] PanelModal 720px, wash over content, sidebar crisp; header "NEW CRON JOB" + ×.
- [x] Field order Profile, Name, Prompt, Schedule(mode + Every/Unit), "Sends as:" preview, Deliver to, Skills list, Advanced.
- [x] Inputs mono 13px, 34px, tinted fill; name has focus ring.
- [x] Create works for interval/daily/custom; edit variant shows id + Save changes.

cron-blueprints
- [x] 2-column grid, gap 12; wand icon + serif title; serif description; tag chips; "Set up" primary right.
- [x] Set up prefills the create modal (our data path).

skills
- [x] "N/M enabled" after title; search box 300px in header.
- [x] Filter panel 210px tinted: FILTERS header, ALL/TOOLSETS/BROWSE HUB (active filled), CATEGORIES with counts.
- [x] Card header: icon + ALL, "N skills" pill, outlined Learn a skill + New skill.
- [x] Rows: square switch left, mono name, muted description.

skills-toolsets
- [x] 3-column grid of cards; icon + label + active/inactive badge; summary; mono chips; Configure outlined.
- [x] Configure drawer opens (placeholder body).

skill-editor
- [x] Dark scrim over whole app; dialog 720px; NEW SKILL + description; Name | Category row; SKILL.MD textarea with
      template; Cancel + CREATE SKILL footer.

plugins
- [x] Refresh icon after title.
- [x] RUNTIME PROVIDER PLUGINS card: memory (select + active badge + note + save) | context engine (select + save).
- [x] INSTALL FROM GITHUB / GIT URL card: identifier, (sha), two switches, INSTALL, two hints.
- [x] PLUGIN CATALOG heading, hint, search (placeholder data), installed list cards.

mcp / mcp-catalog
- [x] ADD SERVER primary in header.
- [x] "Your MCP servers (N)" H2; empty card with centred text + Browse catalog; server cards with badges + actions.
- [x] "Catalog (N)" H2 + one-liner + empty card; Browse catalog scrolls there.

mcp-add-server
- [x] PanelModal 480px; NAME (focus ring), TRANSPORT HTTP/SSE, URL, AUTHENTICATION; ADD bottom-right.
- [x] stdio shows COMMAND / ARGS / ENVIRONMENT.

Compare result (per slug; what still differs and why):
- cron: same layout and controls; our seed has two jobs (one paused) where the reference has one.
- cron-create: same field order and controls. The panel is the foundation Modal: centred with a blurred
  scrim instead of a top-anchored panel over a pale wash, and its title uses the wide display voice rather
  than the serif. Advanced fields shows one placeholder line (no per-job provider/model/script yet).
- cron-blueprints: same card shape (wand, serif title and description, chips, Set up). Ours lists blueprints
  from installed skills (two seeded for the capture); the reference ships a starter catalogue with inline
  slot forms (backend gap).
- skills: same. Our category names come from our skills' `category` field.
- skills-toolsets: same grid and card; our summary line is the toolset description.
- skill-editor: same dialog; ours adds one serif line saying Create skill copies the file (W52-c-7), and the
  inputs keep the dialog's side padding instead of running edge to edge.
- plugins: same two cards; ours adds the Commit SHA field (pinning rule) and lists no catalogue entries
  (no backend catalogue); the context engine list has only `builtin` plus the configured value.
- mcp / mcp-catalog: same sections; the catalogue is empty (no backend catalogue), so Browse catalog has
  nothing below to scroll to. Seeded shot (`mcp-seeded`) shows server cards with badges, tested tools line,
  Disable/Enable, Test and Remove.
- mcp-add-server: same panel; Bearer token shows our Keys-page hint.

Deliberate differences: no Authenticate button for OAuth servers (W52-c-10), no edit pencil on skill rows
(W52-c-6), no plugin update/remove/visibility actions (no endpoints), Learn a skill cannot start (no chat
prefill route).

Follow-ups (backend gaps, not this slice): MCP catalogue endpoint; plugin catalogue; skill create/read/update
of SKILL.md; toolset provider configuration/toggle; cron blueprints route with the starter catalogue, slot
forms and an instantiate route; per-job model/provider/script fields; chat `?prompt=` prefill for Learn a
skill; context-engine option list; client calls for the existing MCP OAuth routes (then the Authenticate
button).

## Integration (wave 53)

W52-c-1..7, -11, -12 are D-16.1197, W52-c-8..10, -13 are D-16.1198. `SectionHeading` and Badge `secondary` are this slice's versions, shared with slices d and e; `Modal` keeps the optional description beside slice a's class slots (D-16.1206).
