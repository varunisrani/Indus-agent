# Wave 53 (lead: Agent 2) — UI parity integration + w52 batch 2

## w53-desk.patch — desktop integration (INT-D), base e402468 = main 8233c54-content + w51b (local), 89 files
- = w51b (desktop foundation, NOT included — apply `.agents/wip/w51/w51b.patch` first) + w52g + w52h + w52m merged.
  So land: w51b.patch, then w53-desk.patch (its base is main + w51b).
- Conflicts resolved: Shell.tsx imports, Composer.tsx lucide imports (h's attach menu replaces g's Plus), ui.css /
  ui/components.tsx (h's menu/HUD/tooltip/SmallDialog + m's PanelLabel). Review pane moved onto notify() toasts and
  SmallDialog (role prop, alertdialog). E2E-only fixes: chat.electron onboarding selector `data-ui="onboarding-endpoint-url"`;
  surfaces HUD pop-out via row context menu "Open in window".
- Gate (INT-D): biome 915 files 0 errors (5 CSS-order warnings); format clean; tsc desktop clean; check-ipc + bridge types ok;
  desktop vitest 174 files / 1730 passed; Electron E2E 43 passed / 2 skipped (packaged-build only); tooling.checks ok.
- D-16.1210 – 1229 (§2.7). Landing notes: landing-w53-desk.md. Follow-ups listed there (g's leftovers, HUD approval,
  quick-entry target, BootOverlay toast, CSS order, re-capture g/h/m).
