# Task: Profiles, profile builder and Models pages (P18-24) Phase: 18 Owner: builder P1 (wave 32)

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `web/src/pages/ProfilesPage.tsx` (skimmed by
search: card actions, rename, sticky default, persona editor), `web/src/pages/ProfileBuilderPage.tsx`
(skimmed by search: step list, advance rule), `web/src/pages/ModelsPage.tsx` (skimmed by search:
device-code block, polling). Our side: `src/indus/web/routes/{profiles,models,skills,mcp}.py`,
`src/indus/foundation/profiles.py` (`normalize_id`: the P01 profile id rule),
`apps/web/src/test/routes.generated.ts`, note `rest-profiles-system.md`. Evidence: none.

What it does (own words): **Profiles** shows a card per live profile: name, badges (default, sticky —
the one new CLI runs pick up —, served by this dashboard), model and provider, description and whether
a persona exists. Actions: make sticky, rename (inline; not the default nor the served one), delete
(confirm dialog that wants the name typed; not the default nor the served one), edit model
(provider + model), description and persona (`PERSONA.md`, loaded on open), export (a background
action whose log is tailed; the archive path is shown) and import an archive by path. "New profile"
opens the **builder**: a stepped flow name → description → model → skills → MCP servers → review. The
name step cannot advance until the name passes the profile id rule (1–64 of `a-z 0-9 _ -`, starting
with a letter or digit, not `default`, not an existing profile). Create runs `POST /api/profiles`
(clone from a chosen profile, skills kept or not), then the model (`PUT /api/profiles/{name}/model`),
each skill switched off (`POST /api/skills/toggle?profile=<new>`) and each MCP server added
(`POST /api/mcp/servers?profile=<new>`); a failed follow-up is listed, the profile stays. **Models**
shows the current model and its context window, the provider inventory (configured or not, sign-in
available), a form to set the main model, the side-task (auxiliary) models with one save per task, and
the device-code sign-in: Start shows the code and the verification link, polls the session at the
interval the server gave (never faster than 1 s), and stops on approved / denied / expired / failed or
when the person cancels (`DELETE`).

Inputs → outputs: `GET/POST /api/profiles`, `POST /api/profiles/active`, `PATCH/DELETE
/api/profiles/{name}`, `GET/PUT …/persona`, `PUT …/description`, `PUT …/model`, `POST …/export`,
`POST /api/profiles/import`, `GET /api/actions/profile-export/status?profile=<name>`,
`GET /api/skills`, `POST /api/skills/toggle`, `POST /api/mcp/servers`, `GET /api/model/{info,options,
auxiliary}`, `POST /api/model/set`, `POST /api/providers/login/start`, `GET/DELETE
/api/providers/login/{id}` → cards, steps, forms, a polling sign-in panel.

Files: `apps/web/src/pages/{ProfilesPage,ProfileBuilderPage,ModelsPage}.tsx`,
`apps/web/src/pages/{ProfilesPage,ProfileBuilderPage,ModelsPage}.test.tsx`,
`apps/web/src/pages/profiles/profileName.ts` (the id rule); shared edits in `restTypes.ts`, `api.ts`,
`mockApi.ts`, `shell/routes.ts`, `shell/ProfileScope.tsx` (a `refresh()` so the switcher sees a
created / renamed / deleted profile).

Edge cases / failure modes:
- Server refusals (bad name, exists, in use, the command's own message) shown as the server's sentence.
- Rename / delete of the default or served profile: buttons disabled with the reason.
- Delete confirm needs the exact name typed.
- Builder: going back keeps entries; a name that becomes invalid stops "Next"; the name is normalised to
  lower case before checking (as the server does).
- Builder follow-ups run after the create; a refusal on one does not undo the profile, the review lists
  what failed.
- Device code: a provider without a documented flow answers 400 `no_device_flow` → its sentence; poll
  errors keep polling until a terminal status; unmount stops the timer; cancel sends `DELETE` once.

Our design differences / decisions:
- Persona file is `PERSONA.md` (ours); no "copy CLI command" item; no hub search inside the builder (the
  skills step switches off installed skills, or starts without bundled skills).
- Builder MCP step adds new servers (name + command or URL); it does not copy servers from another
  profile (their env arrives redacted, so a copy would lose values).

Tests we will write:
- Profiles: badges; sticky; rename; delete needs the typed name; edit model / description / persona.
- Builder: Next disabled for invalid names (upper case normalised, bad characters, `default`, existing,
  65 chars); full flow sends create, model, skill toggle and MCP add with `?profile=<new>`.
- Models: set main model; save a side task; device code shows code + URL, polls at the server's interval
  and stops on approved; cancel sends DELETE and stops polling.

## Decisions for the lead
- Builder follow-ups (model, skills, MCP) are separate calls after the create (the create route takes no
  model / skills / MCP fields); a partial result is reported, not rolled back.
- `ProfileScope` gains `refresh()` (refetches the shell's profile list).

Compare checklist (filled after the rewrite; reopened the three pages by search):
- [x] builder name rule (test: `apps/web/src/pages/ProfileBuilderPage.test.tsx::cannot advance with an invalid name`)
- [x] device-code poll stops on success / cancel (tests: `apps/web/src/pages/ModelsPage.test.tsx::polls at the server's interval and stops when approved`, `::stops polling and cancels the session on Cancel`)

- [x] delete needs the typed name; default / served profile locked (tests: `apps/web/src/pages/ProfilesPage.test.tsx::deletes only after the name is typed`, `::shows badges, model and description, and locks the default profile`)
- [x] builder writes model / skills / MCP into the new profile with `?profile=<new>` (test: `ProfileBuilderPage.test.tsx::walks the steps and writes the profile, its model, skills and servers`)
- [x] never polls faster than 1 s (test: `ModelsPage.test.tsx::never polls faster than once a second`)
- Deliberate differences: the reference builder has no description step and offers hub skills; ours follows the row (name → description → model → skills → MCP) and only switches installed skills off. Our persona file is `PERSONA.md`.
- Not taken: "copy CLI command" menu item, per-provider model catalogue (no listing route), clone-all toggle in the builder (the route has it; left for later).

## Status

Done (2026-10-20). Shared edits: `apps/web/src/shell/ProfileScope.tsx` (`refresh()`), `apps/web/src/lib/{api,restTypes}.ts` (incl. an optional `profile` on `api.skills.list/toggle` and `api.mcp.add`), `apps/web/src/testing/mockApi.ts`, `apps/web/src/shell/routes.ts`.

Integration (wave 32, P18c integrator, 2026-10-21): decisions numbered D-16.651.
