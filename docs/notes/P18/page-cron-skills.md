# Task: Cron, Skills, Plugins and MCP pages (P18-22) Phase: 18 Owner: builder F1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `web/src/pages/CronPage.tsx`,
`SkillsPage.tsx`, `PluginsPage.tsx`, `McpPage.tsx` (all skimmed by search: form fields, views,
transport switch, test result), REST interfaces in `web/src/lib/api.ts` (`CronJob`, `SkillInfo`,
`ToolsetInfo`, `SkillHubScan`, `McpServer`, `McpServerCreate`, `McpTestResult`). Research spec 09 §3.2
and §3.4; our grammar `src/indus/cron/grammar.py` (P14-01). Evidence: none.

What it does (own words): Cron lists jobs with a state badge and last/next run, and creates or edits
them in a modal: name, prompt, schedule and delivery target. The schedule field takes our grammar
(`every 30m`, `in 2h`, `daily at 9am`, a five-field cron line) and a small builder writes the common
shapes for you. Each job can be paused/resumed, triggered now, edited or deleted (confirm). Skills has
three views: installed skills with on/off switches, toolsets (read-only list with tools), and the hub
(search, install, update, uninstall) showing each result's scanner verdict; a `dangerous` result
cannot be installed and the server's 409 verdict is shown. Plugins lists installed plugins with
enable/disable and installs from a git URL plus a full 40-character commit SHA (checked in the browser
before any request). MCP lists servers (env values arrive redacted) with an add form for stdio
(command, args, env) or HTTP (URL, auth), and per-server Test (renders the tool list), enable switch
and Remove (confirm).

Inputs → outputs: `/api/cron/jobs[...]`, `/api/cron/delivery-targets`, `/api/skills`,
`/api/skills/toggle`, `/api/tools/toolsets`, `/api/skills/hub/{search,install,update,uninstall}`,
`/api/plugins[...]`, `/api/mcp/servers[...]` → lists, modals, toasts.

Files: `apps/web/src/pages/{CronPage,SkillsPage,PluginsPage,McpPage}.tsx`,
`apps/web/src/pages/{CronPage,McpPage}.test.tsx`, `apps/web/src/pages/cron/scheduleBuilder.ts`,
`apps/web/src/pages/PluginsPage.test.tsx`, `apps/web/src/pages/SkillsPage.test.tsx`.

Edge cases / failure modes:
- A schedule the server refuses → its message shown in the modal, modal stays open.
- Short / non-hex SHA → inline error, no request.
- MCP test failure → error text instead of tools; test bounded server-side (15 s).
- Stdio vs HTTP: switching transport clears the other transport's fields.

Our design differences / decisions:
- Builder emits only our kept grammar strings (`every <n><unit>`, `in <n><unit>`, `daily at HH:MM`,
  `weekdays at HH:MM`) or a raw cron line.
- No MCP curated catalogue and no dashboard-plugin hub (not in the row; plugins are agent plugins).

Tests we will write:
- Builder emits `every 30m` / `in 2h` and the mock's validator accepts them; create sends the
  schedule; pause/resume/trigger call through; plugin install refuses a short SHA without a
  request; MCP test result renders the tool list; MCP add sends the stdio shape; skill install of a
  dangerous result shows the verdict.

## Decisions for the lead
- Plugin routes: `GET /api/plugins`, `POST /api/plugins/install {url, sha}`,
  `POST /api/plugins/:name/{enable,disable}` (P18-14 to match).

Compare checklist (filled after the rewrite; reopened the four pages by search):
- [x] builder strings accepted by the grammar (test: `apps/web/src/pages/CronPage.test.tsx::emits our grammar strings, which the store accepts`)
- [x] short SHA refused client-side (test: `apps/web/src/pages/PluginsPage.test.tsx::refuses a short SHA without sending anything`)
- [x] MCP test renders tools (test: `apps/web/src/pages/McpPage.test.tsx::renders the tool list from a test`)
- [x] dangerous hub result blocked, server 409 shown (tests: `apps/web/src/pages/SkillsPage.test.tsx::shows verdicts, blocks dangerous installs and installs safe ones`, `::shows the server's verdict when it refuses an install`)
- Not taken: cron blueprints, per-job skills/model/script fields, MCP catalogue and OAuth flow UI, dashboard-plugin hub (later rows or L17).

## Status

Done (2026-10-20).
