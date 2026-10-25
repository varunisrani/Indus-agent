# Task: Web Docs, raw YAML save, skill create, provider logins (W58-J2) Phase: ui-parity Owner: builder w58j2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): none (product-only wiring of our own
backend; no reference file opened).
Evidence: the wave-55 web compare set, slugs `docs`, `docs-direct-url`, `config-yaml`, `env`,
`skill-editor` (`$S/compare2/deliver/gaps.md` lines for them). Nothing copied.

What it does (own words):
- **Docs** (W58-J2-1): /docs renders our user guides (docs/user) inside the page: the guides are
  bundled into the dashboard at build time (one lazy chunk each) and listed in the order and with
  the titles of our docs site's navigation (`nav:` of mkdocs.yml). `?page=<slug>` picks a guide and
  `#anchor` a heading (anchors made the way the docs site makes them), so a guide can be linked and
  reloaded. Markdown becomes React elements only (never HTML); a link to another guide opens it in
  the page, an http(s) link opens in a new tab (`noopener noreferrer`), anything else is text.
  Colours come from the theme tokens, so it is themed in light and dark. "Open docs" still links
  the docs site out. No API reference page: the backend keeps the OpenAPI pages off.
- **Config › YAML** (W58-J2-2): the YAML view is now the config file's own settings (new
  `GET /api/config/raw`: no defaults, `${NAME}` unexpanded, credentials masked) and is editable;
  Save sends the text to `PUT /api/config/raw`, which parses it with the loader's safe reader,
  refuses `_`-internal keys and a mask with nothing stored behind it, puts the hidden values back,
  expands references like the loader and validates with the schema. Any problem is a 422
  `config_invalid` listing every one, and nothing is written. A valid text goes through
  `config.save(..., replace=True)` (the one writer: backup, lock, comments kept, templates kept).
- **Skill editor** (W58-J2-3): Create skill calls `POST /api/skills/create {name, content,
  category?}`, which runs one `create` through the `skill_manage` tool's own handler (name, format,
  path and lock checks, the `skills.write_approval` gate, the ledger). A refusal is 400
  `skill_refused` with the tool's reason; a held write answers `staged: true`.
- **Keys › Provider logins** (W58-J2-4): `/api/model/options` rows carry `sign_in` (any documented
  flow in `providers.auth.flows`); the OAuth card lists those providers (today OpenRouter, browser
  PKCE) with `indus auth add <name> --type oauth`. The Login button stays for device flows only (none
  today), since the browser flow needs a localhost callback the dashboard does not run.

Inputs → outputs: `/docs?page=…#…` → guide; YAML text → 422 problems | file rewritten; New skill
dialog → `skills/<category>/<name>/SKILL.md` | held | refusal; options → OAuth rows.

Files: src/indus/web/routes/{config_env,config_view,skills,models}.py; apps/web/src/pages/{DocsPage,
ConfigPage}.tsx, pages/docs/*, pages/config/YamlView.tsx, pages/skills/SkillDialogs.tsx,
pages/EnvPage.tsx, lib/{api,restTypes,yaml}.ts, testing/mockApi.ts, test/routes.generated.ts;
docker/Dockerfile, .dockerignore, nix/packages.nix (the web build gets docs/user + mkdocs.yml).

Edge cases / failure modes:
- YAML: not a mapping, a parse error (line given), a schema error, `_config_version`, a mask moved
  to a new key or list slot: 422, file untouched. Unreadable file: 500; managed install: 403.
- Comments typed in the editor are not kept (the file's own comments are); an integer map key reads
  back as a string key (JSON on the way to the page).
- Skill: a duplicate or bad name, front matter that does not name the skill: the tool's message in
  the dialog. The untouched template follows the typed name so its front matter matches.
- Docs: an unknown `page` says so with a way home; a link outside docs/user is plain text.

## Decisions for the lead
- W58-J2-1: guides are bundled into the web build (no new route); Docker and Nix copy docs/user and
  mkdocs.yml into the web stage. The Markdown reader is a docs variant of the desktop's reader (our
  code), not shared yet: a follow-up could move both into packages/.
- W58-J2-2: a raw save replaces the file's settings (`replace=True`); keys removed in the text are
  removed from the file. Validation refuses on schema errors only; unknown keys are kept.
- W58-J2-3: the dashboard create honours `skills.write_approval` like any skill write (held, then
  `indus skills pending`), rather than treating the person's click as the approval.
- W58-J2-4: only providers with a documented flow are listed (1 today vs the reference's 8).

## Status
- [x] Docs render, nav, links, anchors, unknown page (tests: `pages/DocsPage.test.tsx`,
  `pages/docs/docs.test.tsx`; e2e `dashboard.spec.ts::Docs`)
- [x] Raw YAML get/put, secrets, references, unknown keys, refusals, managed, unreadable, session
  (tests: `tests/contract/web/test_config_env_routes.py::test_raw_*`; `ConfigPage.test.tsx`; e2e
  `dashboard.spec.ts::Config: raw YAML`)
- [x] Skill create, refusals, approval gate, session (tests:
  `tests/contract/web/test_cron_skills_routes.py::test_skill_create_*`; `SkillsPage.test.tsx`; e2e
  `dashboard.spec.ts::Skills`)
- [x] Provider sign-in rows (tests: `test_profiles_models_routes.py::test_model_set_info_and_options`,
  `EnvPage.test.tsx`)
