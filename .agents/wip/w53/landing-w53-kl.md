# Landing notes — wave 53 INT-KL (desktop slices k + l)

- **Worktree.** `$S/int-kl`, detached at `f3f7772` (main + w51b + w53-desk). node_modules hard-linked from
  `$S/base-w51b`, `.venv` by `uv sync --locked`, Electron 44.5.1 unpacked into the tree's own electron package.
  Everything uncommitted (the patches' `git apply --3way` staged their files; integrator edits are unstaged on top).
- **Apply.** `w53k.patch` then `w53l.patch`, both `git apply --3way`: no textual conflicts (both "fell back to direct
  application" cleanly, including Shell.tsx — k's LeftSidebar body and l's footer / palette hunks do not overlap).
- **Shell merge.** k had moved the SESSIONS | BOTS strip out of the `<nav aria-label="Main">` and dropped the nav when
  the BOTS panel shows (zero navs then). Now one "Main" nav in both panels: it holds the strip, and in the sessions
  panel New session + page links + cron + contributed links; the roster and the vertical "Scheduled jobs" edge tab
  sit after it; the edge tab opens l's cron overlay (`openOverlay("cron")`). `data-panel="bots"` drops the nav's 4px
  bottom pad so the strip sits on the roster. l's footer More menu (webhooks, learning map, session import, profiles,
  agents), command-center house button (overlay) and palette overlay entries kept as built. routes.ts / i18n spreads:
  union (applied as is; `/bots` stays a page for deep links; webhooks / command-center / learning-map /
  session-import are overlays).
- **Dedupes (one copy each).** `app/overlay/kit.tsx` + `kit.css` moved to `app/kit.tsx` + `app/kit.css` (shared by the
  pages and the overlay panels); every l import repointed.
  - TextTabs: one component (`options` API, raised `<sup>` count, `null` = loading bar); `look="page"` = k's 13px
    header row (Capabilities; Messaging's hand-made status-filter tabs now use it too); default = l's 12px panel row.
    k's `TextTabs` + `.md-tab*` CSS deleted.
  - Search: one `QuietSearch` (on the foundation `.desk-search`), new `fill` / `className`; used by Capabilities and
    Messaging headers (k's `PageSearch` deleted), Connectors, the Skills Hub search (its bare input replaced), the
    command center / artifacts / session import, and the foundation `ListDetail` list search (its inline copy removed).
  - Pills: one `PanelPill` (tones muted, category, accent, info, good, warn, bad; `dot`); k's `MdPill` and the raw
    `span.md-pill` in PluginsTab replaced; l's tones remapped (soft-primary "good" → `info`, green "ok" → `good`).
  - Meta rows: one `MetaGrid` with `framed` (k's `MetaBox` inset look, spec's inset-panel colours); `MetaBox` deleted.
  - Detail title: one `DetailHead` (+ `leading`, `description`, `level`); k's `DetailHeader` deleted.
  - Text actions: one `TextAction` (l's: strong / title / disabled); k's copy deleted, BottomSection uses it.
  - Empty / error panels: k's `EmptyPanel` deleted; the foundation `EmptyState look="panel"` gained `fill` and `role`.
  - Kept apart on purpose: `masterDetail.tsx` frame (MasterDetail with seam vs foundation ListDetail — different
    frames), `CapRow` vs `PanelRow` (switch rows vs menu rows), `DetailSection` (messaging's 11px/600 section title,
    spec'd differently from l's 10px `CapsLabel`), foundation `TextLink` (inline link, inherits size; used by settings).
- **Other fixes.** "1 tools" → `toolsets.toolsOne` via `toolCount()`; also `connectors.testOkOne`, `manage.agents.toolsOne`.
  k's `plugins.rescan` collided with Settings › Plugins' key (PLUGIN_STRINGS spread later, so Capabilities showed
  "Check the folder again") → `plugins.rescanAll`. Webhooks banner no longer runs under the card's close X
  (pre-existing in l's capture). Follow-up done: `contrib/host.ts` `pluginSourceFor()` implements
  `DesktopPluginSource.setEnabled` over the running `PluginLoader` (the stored choice Settings › Plugins applies;
  renderer-local, no IPC; no-op before plugins start) + 2 host tests; the Capabilities › Plugins Desktop switch is live.
  Bots surface still not a Desktop row (not in the loader's inventory).
- **Gate (final tree).** biome `check apps packages tooling scripts`: 946 files, 0 errors, 5 warnings (the same
  `noDescendingSpecificity` ones INT-D reported; 2 interim ones from the merged kit.css fixed); `biome format apps
  packages` clean (935 files); `tsc --noEmit -p apps/desktop` clean; `check-ipc.mjs` ok; `gen-bridge-types.mjs
  --check` ok (exit 0). Desktop vitest full (`--maxWorkers=2`, xvfb, through `flock $S/fullgate.lock`, started after
  the last code edit): **178 files, 1756 tests passed**. Electron E2E whole lane (through the lock, xvfb): **8 files
  passed, 1 skipped; 43 passed, 2 skipped** (chat 9, security 12, surfaces 6, security-audit 5 + 1 skipped, lifecycle
  4, remote 1, packaged 2, boot 4; startup-budget skipped — no packaged build). `uv run python -m tooling.checks
  --require-ref`: ok, exit 0 (16 checks; pins and deps_audit warn as on main). Logs: `$S/int-kl-vitest1.log`,
  `$S/int-kl-e2e1.log`, `$S/int-kl-checks2.log`. No Python changed (no pytest).
- **Captures.** Kit copies `$S/int-kl-kit/{capabilities.ts,cc.ts}` (copied from `$S/w53k-kit`, `$S/w53l-kit`, not
  edited), SCENES/WORK `$S/t-intkl` → `$S/int-kl-shots/k` (7 pairs, 0 problems) and `$S/int-kl-shots/l` (15 pairs,
  0 problems), bots-tab and webhooks re-captured after the CSS fixes. Looked at skills, tools, mcp, plugins,
  messaging, bots-tab, command center (sessions, system), cron, cron dialog, profiles detail, session import,
  artifacts, webhooks in light and/or dark: no broken page; tabs / search / pills / meta box render as before.
- **D-ids.** D-16.1250 – D-16.1269 in §2.7 after D-16.1229 (W52-k-1..7 = 1250–1256, W52-l-1..11 = 1257–1267,
  integration 1268 control kit / one nav / rescan key, 1269 live Desktop switch + singular counts; none unused) and a
  §3.1 range row after the 1210–1229 one. Both task notes got an "Integration (wave 53, desktop k + l)" paragraph.
  Settings/* untouched (INT-S owns D-16.1230–1249).
- **Follow-ups.** Bots row in the Plugins Desktop column; k's list (toolset write route, backend readiness probe,
  skill catalog / SKILL.md routes, plugin uninstall + catalog, live bot presence, create-room RPC, messaging
  `docs_url`); l's list (usage counts, curator / memory reset / audit routes, export save path, import paging, cron
  per-job model, profile facts, layout grip chips + custom templates, markdown import preview, artifacts pager);
  CapRow/PanelRow could share one row base; artifacts header could take `look="page"` like Capabilities (same
  page shell in the spec) — left as l built it.
- **Environment.** ENV RULE in every call (HOME etc. under `$S/int-kl-home`); TMPDIR unset for Electron; kit temp under
  `$S/t-intkl`; full lanes through `flock $S/fullgate.lock`; nothing written outside `$S` except the e2e fixtures'
  own `/tmp/indus-e2e-*` dirs (not cleaned globally); no process left running.
