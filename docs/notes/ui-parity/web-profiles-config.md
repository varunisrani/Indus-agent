# Task: Web Profiles, Profile builder, Config (W52-e) Phase: ui-parity Owner: builder w52e

## Status

Done (2026-10-24): all five slugs (profiles, profile-actions, profile-builder, config, config-yaml); none remain. Profiles, the builder and Config are rebuilt on the wave-51 tokens and
components; re-captured in Clear Blue and Deep Teal and set beside the reference shots
(`$SP/w52e/compare.pdf`, not in the repo). Shared files edited (additive): `apps/web/src/ui/Menu.tsx`
(new foundation component), `apps/web/src/ui/controls.tsx` (Badge `secondary` tone),
`apps/web/src/testing/mockApi.ts` (`configDefaults` state and its route), `apps/web/src/lib/api.ts`
(`statusOf`, `config.defaults`, `hubInstall(…, profile)`), `apps/web/src/shell/routes.ts` (builder
title), `apps/web/e2e/dashboard.spec.ts` (Config save opens the Agent section first).

Read: the wave-52 spec `w52-specs/web-profiles-config.md` (lead's Read/Note output; it names the
reference files `web/src/pages/ProfilesPage.tsx`, `ProfileBuilderPage.tsx`, `ConfigPage.tsx`,
`components/AutoField.tsx` at `ab3fe9dffb0a89d534cb795e2a2940e5f6542056` — this builder did not open
them), the foundation note `docs/notes/ui-parity/web-foundation.md`, and the owner's screenshot set of
the running reference web dashboard, slugs `profiles`, `profile-actions`, `profile-builder`,
`config`, `config-yaml` (light and dark). None of them are in the repo.

What it does (own words): three pages get the reference look on the wave-51 tokens and components.
Profiles is a grid of square cards with state badges, a daemon line, a description and a meta block,
each with a three-dot actions menu; one editor dialog per action (model, description, persona);
CREATE and IMPORT open modals and BUILD goes to the builder. The builder is a centred five-step
flow with rounded step pills. Config is a filter column of sections beside one section card of
fields, with a header search, a toolbar (export/import/reset icons, YAML, Effective, Save) and a
read-only YAML view built from the file-sourced effective settings.

Inputs → outputs: `/api/profiles`, `/api/status?profile=`, `/api/skills?profile=`,
`/api/model/options`, `/api/skills/hub/search` → cards, menus and the builder; `/api/config`,
`/api/config/schema`, `/api/config/defaults`, `/api/config/effective` → form, reset, YAML and
Effective views. Writes are unchanged except the hub install after a builder create (now aimed at
the new profile with `?profile=`).

Edge cases / failure modes:
- per-card status or skills query fails → that line is hidden (no invented data);
- default profile → no Rename/Delete items; the profile this dashboard serves → both disabled with
  the locked reason as the title;
- a builder pill after the first is disabled while the name is invalid; a provider without a model
  blocks Next;
- Config: Save stays disabled while nothing changed (saving the full tree untouched could write
  defaults into the file); YAML view never writes; a file with no keys shows a comment line;
- search with no hit → "No fields match '<q>'".

## Decisions for the lead

(from the spec, kept) W52-pc-1 … W52-pc-10 as listed in the spec §11; builder decisions:
- W52-e-1: the dropdown menu the foundation lacked is added to the foundation as `src/ui/Menu.tsx`
  (`Menu`, `MenuItem`, `MenuDivider`), on `useDismiss`; Badge gets a `secondary` tone.
- W52-e-2: the builder's skills step keeps `skillsOff` only when "Keep every bundled skill" is
  unticked and no-skills is off; ticking it back clears nothing (the list is just hidden and ignored).
- W52-e-3: chosen hub skills install after create, one follow-up row each in the outcome list.
- W52-e-4: the section→icon table is our own grouping (icon → sections) covering our category ids;
  the builder's step list is an id order plus a title map (the clean-room similarity guard flagged
  the first shapes as too close to the reference's literals).
- W52-e-5: the builder's MCP draft starts on HTTP/SSE (the first segment); `auth` is sent only when
  it is not "none" (the backend default).
- W52-e-6: a list field holding objects keeps one JSON value per line; a bad line is reported under
  the field and not applied. Comma lists keep the typed text while editing.

## Compare checklist (spec §10; ticked after re-capture in both themes, side by side)

profiles
- [x] header: IMPORT, BUILD (outlined) and CREATE (primary), 28px tall, 22px from the right
- [x] the active banner is 40px tall with a green check and the "Active profile: <name>"
- [x] the "Profiles (N)" heading has the users icon, wide tracking and secondary colour
- [x] 3-column grid with 12px gaps; square cards, 1px border, 16px padding
- [x] badges are square, 25px tall, condensed text; active is green and default is secondary
- [x] the daemon dot line, italic faint "No description", and the 12px meta block pinned to the bottom
- [x] the three-dot trigger is flush top-right
- [x] loading spinner, empty card, error state

profile-actions
- [x] 200px menu with card fill, border and shadow, aligned to the trigger's right edge, 4px below it
- [x] 30px uppercase 12px items with 16px icons, in the order set-active (when not sticky), model, description, persona, skills, copy CLI, export, divider, rename, delete (danger)
- [x] closes on an outside click, on Escape and after an item runs; one menu open at a time
- [x] the editor dialog has a header rule, a "· name" suffix, a right-aligned SAVE, and the persona textarea in mono at 280px

profile-builder
- [x] the page header reads "Profiles/new"
- [x] centred 690px column, ~40px bold "New profile", tracked `Cancel` ghost on the right
- [x] 5 rounded pills (current: fg fill; visited: muted; future: faint), disabled while the name is invalid
- [x] the card has 20px padding, label-serif uppercase labels and 33px mono inputs with placeholders "coder" and the description text
- [x] Back and Next are 37px tall, bold and tracked; disabled shows the muted fill
- [x] steps 2–5 match §4 (filter list, skills checkbox and hub, MCP segmented controls, review rows)

config
- [x] header search, 300×30, with the icon and "Search..."
- [x] the toolbar has the sliders icon, the mono strip, the download, upload and reset icons, a divider, YAML (outlined) and SAVE
- [x] the 210px filter column has the FILTERS and SECTIONS captions, 23px items with icon, name and count, and the active fill
- [x] the section card has a 48px header with the icon, an uppercase expanded title and the "N fields" badge
- [x] each field has a serif uppercase label, a mono key path and a secondary description, with 34px inputs
- [x] the square 34×19 switch is right-aligned; sub-section dividers have a line
- [x] search results group fields by category; the no-match line
- [x] reset confirm → defaults; SAVE gated by dirty state (muted when clean)
- [x] Effective: the source pill does not wrap

config-yaml
- [x] the button reads `Form` and is filled; the reset icon is hidden
- [x] the card is titled "RAW YAML CONFIGURATION", with a 600px mono 13px/21px textarea and 2-space indents
- [x] it is read-only, shows only the file-sourced keys, and SAVE is disabled with an explaining title

Follow-ups (backend): `path` in `/api/profiles` rows; per-profile daemon state and skill count in
`/api/profiles`; a per-provider model list in `/api/model/options`; `GET/PUT /api/config/raw` with
the config path; an optional describe endpoint.

Deliberate differences (classified): header and toolbar buttons use the foundation's typewriter
button face in mixed case (the reference's are uppercase; a foundation-wide choice, W51-a); the cards
have no path line (no API field) and say "Daemon" where the reference says "Gateway"; the sidebar
shows our profile switcher; the menu has our EXPORT item and the header our IMPORT button; the
Config strip reads `config.yaml · <profile>` (no path API); the YAML view is read-only; Config has
our EFFECTIVE toggle; the builder keeps "Start from" on step 1 and our outcome screen after Create;
the create modal has no "Auto-generate" description (no endpoint).

## Integration (wave 53)

W52-pc-1..10 are D-16.1201, W52-e-1..6 are D-16.1202. Badge `secondary` uses `bg-selected` (one copy with slices c and d, D-16.1206); the foundation `Button` now sets its label in capitals (this note's request, D-16.1208).
