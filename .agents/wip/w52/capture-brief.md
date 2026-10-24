# Side-by-side UI capture brief (owner request 2026-10-24)

Goal: screenshots of EVERY page and every notable component (dialogs, menus, pickers, panes, cards, empty states) of
two apps — the original Hermes (reference) and our rebuild Indus — in BOTH light and dark theme, so the lead can lay
them side by side in a PDF. Four capture agents run in parallel:
- H-desktop: Hermes desktop app   → $SP/compare/hermes/desktop/
- H-web:     Hermes web dashboard → $SP/compare/hermes/web/
- I-desktop: Indus desktop app    → $SP/compare/indus/desktop/
- I-web:     Indus web dashboard  → $SP/compare/indus/web/
($SP = /tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad)

File names: `<slug>--light.png` and `<slug>--dark.png`, viewport 1440x900 (device scale 1), same state in both themes.
Use the canonical slugs below whenever the page/component exists in your app, so pairs line up; for things only your
app has, invent a short kebab-case slug. Skip slugs your app does not have (list them as missing in your manifest).
Also write `manifest.json`: [{"slug","title","group","note","light":"file","dark":"file"}] in capture order, plus
"missing": [slugs]. `group` is a short section name (e.g. "Chat", "Settings", "Pages").

Canonical desktop slugs:
onboarding, onboarding-api-key, new-chat, chat-conversation, chat-streaming, tool-row, tool-row-expanded,
approval-card, slash-menu, mention-menu, model-picker, session-switcher, session-sidebar, command-palette,
context-menu, files-pane, review-pane, right-sidebar, settings-general, settings-appearance, settings-models,
settings-providers, settings-keys, settings-shortcuts, settings-advanced, settings-about, settings-licences,
command-center, capabilities, skills, tools, mcp, messaging, webhooks, artifacts, cron, profiles, agents, starmap,
session-import, quick-entry, hud, notifications-toast, error-state, empty-state.
Canonical web slugs:
login, sessions, session-detail, sessions-search, chat, chat-exchange, analytics, channels, config, cron, docs, env,
files, logs, mcp, models, pairing, plugins, profile-builder, profiles, skills, system, webhooks, theme-menu,
language-menu, empty-state, error-state.

Data: only fake/mock backends and a scripted fake model. Never contact a real model provider, never use or create real
credentials (no credential-shaped literals; if a page needs a key, show the empty/placeholder state or build an obviously
fake value at runtime like "demo-" + "key"). Seed a little realistic demo data (2–3 sessions, a short exchange with a tool
row) so pages are not all empty, using each app's own test fixtures/mock servers.

Rules: work only under $SP (your own folder $SP/compare/work-<agent>/ for builds, HOME and caches); never write under
/root, the real ~, /, /root/reference (read-only) or /home/user/indus-agent (do not edit or commit the repo). Chromium is
at /opt/pw-browsers (PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers; never `playwright install`). Electron under xvfb is fine.
Never pkill by pattern (kill only PIDs you started); clean up processes and large build output when done (keep the PNGs
and manifest). TIME BOX about 75 minutes. Report: count of pairs captured, missing slugs and why, anything that looked
broken in the app (that is useful information, capture it), and how you ran it (so it can be repeated).
