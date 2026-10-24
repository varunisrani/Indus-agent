# Landing notes — wave 53 desktop settings integration (INT-S)

- **Worktree:** `$S/int-set` (detached at f3f7772 = main + w51b + w52 g/h/m). Applied with `git apply --3way`:
  `.agents/wip/w53/w53i.patch` (clean), then `w53j.patch` (8 files with conflicts, all i vs j; nothing against g/h/m).
  Everything uncommitted; the index holds the whole result (`git diff --cached`, 44 files, +8620 / -1757; copy at
  `$S/int-set-final.diff`). `fieldCopyA.ts` (from i) is gone.
- **Conflicts and resolution**
  - `i18n/en.ts`: union (both imports and spreads: `SETTINGS_A_STRINGS`, `TRANSCRIPT_STRINGS`, `SETTINGS_B_STRINGS`).
    No duplicate key across the spread string packs (scripted check, 1708 keys).
  - `context.tsx`: union (i's `schema`/`focusField`/`saveField`/`savePatch` + j's `go`; j's bridge groups merged clean).
  - `SettingsOverlay.tsx`: union of the context value; i's rail footer, j's `page.after` block, `pageIcon()` merged clean.
  - `navMeta.tsx`: one `PAGE_ICONS` map (i's model…passwords entries + j's memory…about entries) behind the one
    `pageIcon(view, page)`; imports union; `Plug` dropped (gateways = Globe per spec-b).
  - `pages.ts`: CustomPage union drops both `sessions-archived` (j) and `workspace-desktop` (i); `PageDef.order` (i) and
    `PageDef.after` (j) both kept; VIEW_IDS keep browser/passwords; Workspace › Files keeps i's custom page with
    `code_execution.mode` + `file_read_max_chars` (spec-b: `code_execution.mode` stays with slice A); MANIFEST rows union.
  - `customPages.tsx`: rebuilt from j's version minus i's moved pages (theme, typography, window, workspace-desktop and
    their helpers); `...PAGES_A` spread; Partial table with `?.()`; only the persona page + `Row` remain;
    `SecretInput` re-exported from `pagesB/kit.tsx` for Onboarding.
  - Field copy (`schemaPage.tsx`, `search.ts`): ONE map `settings/fieldCopy.ts` = i's entries + j's entries. Labels,
    descriptions and placeholders are message keys (j's 57 English strings became `settingsB.field.*` keys in
    `stringsB.en.ts`); `description: ""` hides the schema's; `options` (names, shown as is); `suggestions` is a function
    (i's lazy timezone list; j's static lists wrapped). schemaPage: i's comma-list input, then j's option select, then the
    suggestions combo (timezone placeholder "Search…"), then the JSON textarea; search: copy label + key-derived keyword.
- **Field placement check** (backend `schema_form()`, 713 fields; ownership computed in base, i, j and merged trees):
  0 fields that base / i / j put on a page are without an owner after the merge; ownership is unique by construction
  (first claim); the only fields on two pages are the specs' `also` cross-listings (`security.allow_private_urls` on
  Browser › Local & private URLs, `checkpoints.max_snapshots` on Advanced › Output limits). Moves: terminal.backend/timeout
  → Advanced › Terminal, tool_output.max_chars → Advanced › Output, browser.* → Browser, wake word → Voice view.
  `default-directory` stays on Workspace › Projects; j's `sessions-directory` on Sessions edits the same value.
- **Gate (final tree)**
  - biome `check apps packages tooling scripts`: 936 files, 0 errors, 5 warnings (the same `noDescendingSpecificity`
    set as INT-D: settings.css 503, ui.css x4); `biome format apps packages`: 925 files clean.
  - `tsc --noEmit -p apps/desktop`: clean.
  - `check-ipc.mjs`: ok; `gen-bridge-types.mjs --check`: ok (i changed `electron/ipc/schemas.ts`: chatTextScale max 175).
  - Desktop vitest full (`--maxWorkers=2`, through `flock $S/fullgate.lock`, xvfb): **175 files, 1757 tests passed**.
  - Electron E2E whole lane (lock, xvfb): **8 files passed, 1 skipped; 43 tests passed, 2 skipped** (chat 9, security 12,
    surfaces 6, security-audit 5 +1 skipped, lifecycle 4, remote 1, packaged 2, boot 4; startup-budget skipped — no
    packaged build).
  - `uv run python -m tooling.checks --require-ref`: ok, exit 0 (16 checks; pins and deps_audit warn as on main).
- **Captures:** kit copies `$S/int-set-kit/settings-{a,b}.ts` (copied from `$S/w53i-kit`, `$S/w53j-kit`, plus a probe
  counting `[data-row]` / `[data-field]` per page). 51 pages x light/dark = 102 PNGs in `$S/int-set-shots` (contact
  sheets `$S/int-set-sheets`). Probe: no page empty, no duplicated row or field on any page, no renderer errors on the
  A pages, 0 problems on the B pages. Looked at all sheets: no broken page; rail shows both slices' child icons and the
  footer; Voice shows the wake word block under its schema rows, Sessions shows the archived list under retention.
- **D-ids** (§2.7, after D-16.1229; §3.1 range row after the 1210 one): D-16.1230 rail/search (dsa-1/3/4), 1231 no
  Billing (dsa-2, j-1), 1232 model pages (dsa-5/6/7), 1233 rows without a feature (dsa-8/9/11), 1234 chat text 175 %
  (dsa-10), 1235 Safety/Browser (dsa-12/13/14), 1236 Passwords (dsa-15/16), 1237 field moves (i-1, j-3, integration
  check), 1238 default folder (i-2, j-2), 1239 shared hooks (i-3 + j's `after`/`go`), 1240 one field-copy table (j-9 +
  integration), 1241 notifications master (j-4), 1242 no endpoint delete (j-5), 1243 saved connections (j-6), 1244 About
  log viewer (j-7), 1245 local models (j-8), 1246 page table after the slices (integration); 1247 – 1249 unused (range
  row). Both task notes got an "Integration (wave 53, settings)" paragraph mapping labels → ids.
- **Follow-ups:** `voice.record_key` and `security.secret_store` come back secret-masked from `/api/config` (the key
  name looks secret), so Push-to-talk key shows "Stored. Type to replace it." and Secret store a dotted select — backend
  masking rule, not the merge; builders' own lists stand (rail footer wiring, MoA delete, vault API, endpoint delete,
  "Current" pill, provider-card edge, Plugins markup, Screen capture / Tools & Keys › Settings not built); the 5 CSS
  order warnings.
- **Environment:** ENV RULE in every call (HOME etc. under `$S/int-set-home`); node_modules hard-linked from
  `$S/base-w51b`; Electron 44.5.1 unpacked into the worktree's own electron dir (path.txt removed before rewrite so the
  hard link to the base was not written through); TMPDIR unset; kit temp under `$S/t-intset` (removed); a temporary
  placement test file in the worktree was deleted after the run; no global /tmp cleanup; no process left running.
