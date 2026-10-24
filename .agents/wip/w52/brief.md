# Wave 52 brief: page builders (UI parity)

Base: main after wave 51 (web + desktop foundations). Rules, inputs and checks: .agents/wip/w51/brief.md (all of it applies;
TIME BOX 90 min). Before building read: your spec (below), lead decisions
(.agents/wip/w52/lead-decisions.md), and the foundation note for your surface (docs/notes/ui-parity/web-foundation.md or
desktop-foundation.md). Use the foundation's tokens and components; never create a second copy of a shared component — if
something is missing, add it to the foundation's own files in the foundation's style and say so in your report.
The spec is your note's starting point: copy its checklist into docs/notes/ui-parity/<slice>.md and tick it in Compare.
Rewrite with the reference closed (you may look at the reference SCREENSHOTS while comparing, not its source while writing).

Shared files several slices touch — additive edits only, in your own blocks: web App routes/nav, desktop routes.ts,
settings/pages.ts, strings.en.ts, the settings search index. Say in your report exactly which shared files you edited.

Slices (builder id: spec file → area):
- a: web-sessions-chat.md        → apps/web Sessions, session detail, search, prune, chat page, model picker dialog
- b: web-analytics-models-logs.md → Analytics, Models, Logs
- c: web-cron-skills-plugins-mcp.md → Cron, Skills, Plugins, MCP
- d: web-channels-webhooks-pairing-env.md → Channels, Webhooks, Pairing, Keys/Env
- e: web-profiles-config.md       → Profiles, Profile builder, Config (+ YAML view)
- f: web-system-files-docs-login.md → System, Console, Files, Docs, Login, error state, language menu
- g: desktop-transcript.md        → transcript, tool rows, approval/error cards, toasts, no-provider state
- h: desktop-composer-menus.md    → slash/mention/attach/help menus, model + effort pickers, palette, switcher, sidebar rows, row menu
- i: desktop-settings-a.md        → settings pages: models, chat, general, appearance, workspace, safety, browser, passwords
- j: desktop-settings-b.md        → settings pages: memory, voice, advanced, notifications, providers, gateways, shortcuts, tools, sessions, plugins, about
- k: desktop-capabilities.md      → capabilities, skills, tools, connectors, plugins, messaging, bots tab
- l: desktop-command-center.md    → command center, cron, profiles, agents, artifacts, webhooks, learning map, session import, layout editor
- m: desktop-panes-onboarding.md  → files/review/terminal panes, right layout, HUD, quick entry, onboarding
Specs are in $SP/w52-specs/ ($SP = /tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad).

Base and node_modules (this run):
- WEB builders a–f start now from local base `0a7f30a` = main `1388ccd`-era main + w51a (web foundation), not yet on main.
  Your patch is relative to that base. node_modules: `cp -al` from $SP/base-w51a (root, apps/*, packages/*, apps/desktop/e2e)
  — it has the new web packages (lucide-react, @fontsource*). Do not use verify26's tree for apps/web.
- DESKTOP builders g–m start after the desktop foundation (w51b) lands; their base will be given then.
- The web foundation's API is docs/notes/ui-parity/web-foundation.md in your worktree.
- DESKTOP builders g–m: local base `993f0e9` = main + w51b (desktop foundation). node_modules: `cp -al` from
  $SP/base-w51b (has @fontsource/inter and abril-fatface). Foundation API: docs/notes/ui-parity/desktop-foundation.md
  (overlay frames in app/overlay/OverlayFrame.tsx, settings primitives in settings/primitives.tsx). The capture kit's
  pane steps must find the band's icon buttons by aria-label ("Open Terminal" etc.) — fix that in your copy of the kit.
  Batch 1 now: g, h, m. Batch 2 (i, j, k, l) after the usage window resets.
