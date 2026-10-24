## Wave 53 landing notes (desktop integrator, INT-D) — w52g transcript / tool rows / cards / toasts, w52h composer menus / palette / switcher / sidebar rows, w52m panes / HUD / quick entry / onboarding, on the desktop foundation (w51b)

- **Worktree / base.** `$S/int-desk`, detached at local base **e402468** (= main 5115691 + w51b, never pushed). Patches
  `.agents/wip/w52/w52{g,h,m}.patch` (built on 993f0e9; same tree content) applied in that order with `git apply --3way`.
  Everything is uncommitted (resolved conflict files and the patches' changes staged, integration edits unstaged).
  Full diff against e402468: `$S/int-desk-final.diff` (`git diff HEAD --binary`, 89 files, +9542 / −1679; 19 new files).
  node_modules hard-linked from `$S/base-w51b`; `.venv` by `uv sync --locked`; Electron 44.5.1 unzipped into the tree's
  own `electron/dist`. No package added, no lockfile / notices change (w51b's are already in the base).
- **Conflicts (all resolved by reading both sides).** w52h onto g: `app/Shell.tsx` (import lines: both `SessionSwitcher`
  and `Toasts`), `chat/composer/Composer.tsx` (lucide imports: g's `History` for the restored-draft `NoticeStrip` kept,
  `Plus` dropped — h's `AttachMenu` replaced g's plain attach button), `ui/ui.css` (h's disabled-row rules + its
  danger rule that also tints the icon). w52m onto g+h: `ui/components.tsx` (h's menu family / HUD / tooltip /
  `SmallDialog` kept, m's `PanelLabel` appended), `ui/ui.css` (h's popover / HUD / tooltip / dialog blocks kept, m's
  panel label + dither appended). The other shared files merged clean and were read for semantic overlap:
  `i18n/en.ts` (spreads `COMPOSER_MENU_STRINGS` and `TRANSCRIPT_STRINGS`; no duplicate keys across the packs),
  `styles.css` (h's palette rules, g's toast / tool rules moved out), Composer (g's `NoticeStrip` + h's drawer, help,
  attach menu, `current` model facts), Shell (g's `ToastRegion` + connection-warning toast, h's palette entries and
  switcher, m's Alt+B right group), strings packs (own blocks each). No conflict markers left; tsc clean.
- **Integration change (m's own follow-up, W52-m-6).** The foundation now has g's toast region and h's `SmallDialog`,
  so the review pane uses them: notices (committed / pushed / failed / PR opened) → `notify({id: "review-notice"})`
  (success auto-closes, errors stay) with a visually hidden `role=status` mirror; the revert confirm → `SmallDialog`
  with a new optional `role` prop (`alertdialog`, title "Revert", paths in mono, Cancel / "Discard" as foundation
  Buttons). Removed the now-unused `.review-notice` / `.review-dialog*` rules (`review-dialog-path` kept).
  `ReviewPane.test.tsx` unchanged and green.
- **Electron E2E — run in full (the builders had not).** First run on the merged tree: 6 failed / 37 passed / 2 skipped.
  Root causes: (1) `chat.electron.test.ts` "Settings › Providers re-runs onboarding" (the case behind "Other
  providers"): the e2e filled `.onboarding-form input.settings-input`, which after w52m's endpoint form is the
  endpoint's optional **key** field (a `SecretInput`), so the base URL stayed empty and "Test and continue" stayed
  disabled; the onboarding overlay then stayed open over the window and the next four cases in the file (chat
  approval "Run", panes, transcript-once, sidebar) failed behind it — a cascade, not four bugs. Fixed the selector to
  `input[data-ui="onboarding-endpoint-url"]` (the hook w52m added). (2) `surfaces.electron.test.ts` "HUD: the
  sidebar's pop-out": the row's hover pop-out button is gone (W52-h-5); the e2e now opens the row menu
  (`contextmenu` on the row) and clicks "Open in window". No product code changed for either. After the fixes:
  chat + surfaces 15/15, then the whole lane again on the final tree (below).
- **Gate (final tree, all through `flock $S/fullgate.lock`, xvfb).** biome `check apps packages tooling scripts`:
  915 files, 0 errors, 5 warnings (`noDescendingSpecificity`: 3 on the base, 2 from w52h's drawer / HUD `> svg`
  rules); biome over `.` reports 52 errors / 7 warnings, all 52 errors in the committed `.agents/wip/w52/kit-*`
  capture scripts (same 52 on the base, not product code); biome format `apps packages` clean. `tsc --noEmit -p
  apps/desktop` clean. `check-ipc.mjs` ok; `gen-bridge-types.mjs --check` ok. **Desktop vitest (full, maxWorkers=2):
  174 files, 1730 tests passed** (incl. `testing/electron/wiredApp.electron.test.ts` clicking "Run" under xvfb).
  **Electron E2E (whole lane): 8 files passed, 1 skipped; 43 tests passed, 2 skipped** — lifecycle 4, chat 9 (incl. the
  "Other providers" custom-endpoint case), surfaces 6, security 12, remote 1, packaged 2, boot 4, security-audit 5
  (+1 skipped: fuses of `build/release` — no packaged build there), startup-budget skipped (same reason).
  `python -m tooling.checks --require-ref`: ok, exit 0 (16 checks; pins and deps_audit warn as on main: certifi
  exception to 2026-11-08, pypdf advisories). Python untouched (no pytest needed).
- **Decisions.** D-16.1210 – D-16.1229 in §2.7 (W51-b 1210–1215, W52-g 1216–1221, W52-h 1222–1226 with m's shared
  parts, W52-m 1227–1228, integration e2e 1229) + §3.1 range row; each note got an "Integration (wave 53, desktop)"
  paragraph mapping its labels to the ids. INT-W owns D-16.1190 – D-16.1209 (rows go between D-16.1180–1189 and mine).
- **Per patch.** w52g: done (its "Remaining" list stands: running / approval dot colours on tab and row, the
  composer's single round Stop — Steer/Queue still show —, prompt clamp expand, swipe-to-reject, busy spinner on
  approval buttons). w52h: done (10/10 slugs; its late fixes were never re-captured). w52m: done (review pane moved
  onto foundation toast / dialog at integration). No re-capture / compare PDF in this role.
- **Follow-ups.** The three remaining-lists above; HUD sheet should stay up while an approval waits (m); quick-entry
  targets need `target` on `indus:quick:submit` (m); folder picker for the "No folder" state (m); `BootOverlay`'s
  `.toast boot-restarted` still renders its own toast rather than `notify()` (g did not migrate it); tidy the 5 CSS
  order warnings; re-capture g/h/m in both themes with the kit (not done here).
- **Environment.** ENV RULE in every call (HOME etc. under `$S/int-desk-home`); TMPDIR unset for Electron; nothing
  written outside `$S`; no global `/tmp/indus-e2e-*` cleanup; no process left running.
