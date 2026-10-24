# Wave 51–52 brief: UI parity with the reference (owner decision 2026-10-24)

Owner (2026-10-24): "the Indus UI must look like Hermes Agent's, same to same — every page and every component of
the web dashboard and the desktop app." This overrides the D-16 palette/theme row (our clay/teal palette, R09.22) for
look and layout: layout, structure, component set, spacing, density, typography style and the light/dark colour
scheme now follow the reference. Recorded as an owner decision (integrator gives it a D-id in §3.2 as decided).

What stays (clean room still applies to CODE and ASSETS, docs/plan/01-clean-room-protocol.md):
- Read → Note → Rewrite (reference closed) → Compare (re-capture ours, put it next to theirs, close gaps by writing
  again ourselves). No copied code of any size, no copied CSS/theme/config files, no reference tests/fixtures.
- No reference assets: logos, the wordmark artwork, icon files, sounds, font files from their repo, images, pet art.
  No `@nous-research/ui` (their private UI kit) and no Hermes/Nous names in product code or UI text. Our product name is
  "Indus Agent"; where they show their wordmark, we show an "INDUS AGENT" wordmark in the same style with an open font.
- UI copy: short labels (1–4 words) may match; longer sentences are written in our words.
- Allowed: the same open-source libraries at exact pins (e.g. lucide-react, @tabler/icons-react, cmdk, clsx,
  tailwind-merge, radix-ui, @fontsource/* OFL fonts). Add with the lead's tooling: `pnpm add --filter <pkg>
  <name>@<exact version>` through the proxy is ALLOWED for these (licence must be MIT/ISC/Apache-2.0/BSD/OFL; the
  `licenses` guard must pass; update THIRD_PARTY_NOTICES via the notices tool). Colours, sizes and spacing may be
  measured from the screenshots and the running reference.
- Behaviour and data flows of our app stay as they are (our protocol, our APIs); this is a visual/structural rebuild.
  Existing tests must pass; update a test only where it pins old markup/text you deliberately changed.

Inputs ($SP = /tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad):
- Reference screenshots (Evidence — never copy into the repo): $SP/compare/hermes/{web,desktop}/<slug>--{light,dark}.png
  with manifest.json (titles/notes). Hermes web light = "Nous Blue" preset, dark = "Hermes Teal" (default).
- Our current screenshots: $SP/compare/indus/{web,desktop}/ (manifest.json).
- Side-by-side PDFs of both: $SP/compare/deliver/*.pdf. Indus UI bugs seen: $SP/compare/indus-ui-bugs.md (fix the ones
  in your area).
- Reference source (Read/Compare only): /root/reference/hermes-agent/web and /root/reference/hermes-agent/apps/desktop.
- Capture kits to re-screenshot OUR app for Compare: $SP/compare/work-i-web/kit (run.sh, stack.ts, capture.ts) and
  $SP/compare/work-i-desktop/kit (run-desktop-all.sh, desktop-all.ts; note its walkTabs flaw). Copy a kit into your own
  $S, never edit the originals. PDF builder: $SP/compare/tools/build_pdf.py.

Wave 51 — foundations (run first, then wave 52 builds on them):
- [a] WEB foundation (apps/web, packages/ui as needed): design tokens for light and dark matching the reference look,
  fonts, the app shell (left nav with icons and the same grouping/labels style, collapse mode — fix the clipped labels
  bug, footer status block, theme menu with presets (light/dark at least), header/page-title pattern), and the core
  components every page uses (buttons, cards, stat tiles, badges/pills, inputs/selects/switches, tabs, tables/list rows,
  dialogs, toasts, empty and error states). Re-skin the Sessions page fully as the reference implementation; other pages
  must still render (they will be rebuilt in wave 52). Write docs/notes/ui-parity/web-foundation.md listing the tokens
  and components so wave-52 builders use them.
- [b] DESKTOP foundation (apps/desktop renderer): tokens light/dark, fonts, window chrome/title bar, the left sidebar
  (new session, nav items, grouped session list, footer), default layout matching the reference (no Preview/Terminal
  panes open by default), the new-chat empty state (wordmark + composer with model and effort pickers), base transcript
  styling (user message, assistant markdown), the settings shell (left category nav + page frame) and core components.
  Fix the desktop bugs in your area (model chip, light-theme terminal colours, double clear button). Write
  docs/notes/ui-parity/desktop-foundation.md listing tokens and components for wave 52.

Wave 52 — pages (after wave 51 lands; slices assigned then): web: sessions/chat, analytics/models/logs,
cron/skills/plugins/mcp, channels/webhooks/pairing/env, profiles/profile-builder/config, system/files/docs/login;
desktop: transcript + tool rows + approval/error cards, composer menus + palette + pickers + switcher, settings pages
(two slices), capabilities/skills/tools/mcp/plugins + messaging + bots, command center + cron + profiles + agents +
artifacts + webhooks + starmap, panes (files/review/terminal) + onboarding + hud + quick entry.

Rules (ENV RULE), builder <x> of wave <w>:
- Worktree $SP/wt-w<w><x> (created, detached at main). `export S=$SP/w<w><x>` as ONE statement, then a SECOND:
  `export HOME=$S/home XDG_CACHE_HOME=$S/home/.cache UV_CACHE_DIR=$SP/uv-cache-shared npm_config_cache=$S/home/.npm
  INDUS_HOME=$S/indus` (write $SP out literally). Repeat in every Bash call. TMPDIR unset. Own .venv
  (`uv sync --locked --offline`). node_modules: `cp -al` from $SP/verify26 (root, apps/*, packages/*), then
  `pnpm install --offline --frozen-lockfile` only if a link is missing; when you add a package (allowed list above) use
  `pnpm add` through the proxy and keep the lockfile change in your diff. Never `pnpm run`, `pnpm exec` or `npx`; use
  ./node_modules/.bin/<tool>.
- No secrets, no credential-shaped literals, no real providers; never pkill by pattern; never git stash; never commit;
  never write under the real ~, /root or /, /root/reference, or /home/user/indus-agent. `rm` only with literal paths inside
  $S or your worktree, never inside `bash -c`. The container may restart; if resumed, check what survived and continue.
- Task note first: docs/notes/ui-parity/<slice>.md (Status at the top, Read line naming the reference files and
  screenshot slugs, Compare checklist). Decisions labelled `W<w>-<x>-<n>`.
- TIME BOX about 120 minutes for wave 51, 90 for wave 52.
Before finishing: biome check/format, tsc for touched projects, vitest for touched projects, the web/desktop e2e where your
change reaches them (Electron E2E under xvfb with the verified Electron zip, web Playwright), `python -m tooling.checks
--require-ref` (similarity, banned, licenses, assets must pass), pytest only if Python changed. Re-capture your pages with
the kit in BOTH themes and build a side-by-side PDF of your slugs (Hermes vs yours) into $S/compare.pdf; look at it page
by page and keep closing gaps until it reads as the same UI. Report: files, what matches and what still differs per slug,
packages added (name@version, licence), checks, decisions, follow-ups, and the path to your compare PDF.
