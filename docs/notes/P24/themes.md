# Task: Themes and locale plumbing (P24-33) Phase: 24 Owner: wave-39 builder U1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/src/themes/` (file
list; `types.ts` head and the preset names in `presets.ts` for structure only; `vscode.ts` the
token keys it reads, for the flow), `apps/desktop/src/app/command-palette/marketplace-theme-page.tsx`
(148 lines: flow only). Research 08 §3.25. Closed before writing.
Evidence: none.

What it does (own words): the desktop gets twelve colour presets of our own, each with a light and
a dark side, picked in Settings → Appearance together with the mode (light / dark / system) and an
optional accent hue. Every preset passes WCAG AA for text and UI pairs in both modes — checked in
tests, and an accent override is nudged until it still passes. People can add their own themes,
and can import a VS Code colour theme: search the Marketplace, download the extension, read its
theme file, convert its colours to our tokens (deterministically), and install it as a user theme.
The chosen preset can be synced to the backend so the TUI uses the same polarity and accent
(`tui.theme`), and each profile may carry its own preset, chat font and terminal font. Locales:
only English ships (D-16.51), but the runtime pack mechanism is built — packs from plugins or the
backend are layered over English, key by key, with placeholders checked — and is tested with a
test-only pseudo-locale.

Inputs → outputs:
- `presets` (12 × {id, label, light, dark}) → `presetTheme(id, mode, accentHue?)` → a
  `SemanticTheme`; `presetCss(id)` → CSS variables for both modes.
- VS Code theme JSON (JSONC allowed) → `convertVscodeTheme(json)` → `{name, mode, theme}` →
  installed into the user theme list; Marketplace: search text → results; extension → VSIX bytes →
  theme JSON (through main: `indus:themes:marketplace`, hosts allow-listed, 5 MiB cap).
- `LocalePack {locale, origin: "backend" | "plugin", messages}` → `layerPacks(en, packs)` →
  catalog; unknown keys dropped, placeholder mismatch → that key falls back to English.

Files: `packages/ui/src/tokens/presets.ts`, `packages/ui/src/tokens/presets.test.ts`,
`apps/desktop/src/theme/vscodeImport.ts`, `apps/desktop/src/theme/vscodeImport.test.ts`,
`apps/desktop/src/theme/themeStore.ts` (new), `apps/desktop/src/theme/AppearancePage.tsx` (new),
`apps/desktop/src/theme/strings.en.ts` (new), `apps/desktop/electron/theme/marketplace.ts` (+ test,
new), `apps/desktop/src/i18n/packs.ts`, `apps/desktop/src/i18n/packs.test.ts`,
`tests/fixtures/desktop/vscode_theme.json`. Shared: `packages/ui/src/index.ts` (exports),
`apps/desktop/src/app/theme.tsx` (preset CSS), settings custom pages, IPC table, `main.ts`, `en.ts`.

Edge cases / failure modes:
- a preset id that does not exist → the default preset; a user theme with a bad colour → refused
  with the key named;
- VS Code theme without `colors` / `type` → mode guessed from the background's lightness; missing
  tokens derived from the background and foreground; translucent `#rrggbbaa` colours composited
  over the background; colours that fail contrast are nudged in lightness (deterministic);
- JSONC with comments and trailing commas; a VSIX with several themes → the first listed in the
  manifest unless one is chosen; a VSIX over 5 MiB or a host not on the allow-list → refused;
- a locale pack with an unknown key, a non-string value, a wrong placeholder set → that entry is
  ignored; a later pack wins over an earlier one; English is the floor for every key.

Our design differences / decisions:
- Preset names are our own (R09.22): `indus`, `monsoon`, `laterite`, `saffron`, `indigo`,
  `basalt`, `lotus`, `mangrove`, `dune`, `harbour`, `terrace`, `jade`. Each preset is a hue recipe
  over OKLCH (accent hue, neutral hue and tint), so light and dark come from one definition.
- Accent overrides keep AA by moving lightness only.
- Theme state lives in the renderer's storage (per device), with a per-profile map; the TUI sync
  writes `tui.theme` as `light` / `dark` (its colour payload is a follow-up).

Tests we will write:
- every preset passes the WCAG AA contrast pairs in both modes; ids unique and not reference names;
  accent overrides still pass;
- the VS Code fixture (written by us) converts to the same tokens every time (snapshot of values);
  JSONC parse; translucent colours; missing keys derived; VSIX reader on a zip built in the test;
- pseudo-locale pack overlays English and falls back per key; placeholder mismatch falls back;
  plugin pack over backend pack order; unknown keys dropped.

## Decisions for the lead
- Marketplace calls go through one main-process channel with an allow-list
  (`marketplace.visualstudio.com`, `*.gallery.vsassets.io`, `*.gallerycdn.vsassets.io`).
- TUI colour sync beyond polarity is a follow-up (the TUI config takes a theme name).

Compare checklist (filled after the rewrite; reopened the files in `Read:`):
- [x] twelve presets, light / dark / system, accent override, user themes (tests: `packages/ui/src/tokens/presets.test.ts`, `apps/desktop/src/theme/themeStore.test.ts`)
- [x] every preset and every accent hue passes AA in both modes (tests: `presets.test.ts::<id> <mode> passes WCAG AA`, `::accent overrides keep AA for every hue`)
- [x] VS Code: background / foreground / accent (brand tokens before focus) / border / sidebar keys read with fallbacks; translucent colours composited; button text kept and the accent moved until it reads (test: `vscodeImport.test.ts::converts the fixture deterministically`, `::every converted theme passes the contrast pairs`)
- [x] terminal ANSI colours carried when the theme has the full set (same test)
- [x] Marketplace: empty query = most installed, already-installed re-activates (tests: `electron/theme/marketplace.test.ts`, `themeStore.test.ts::user themes from VS Code install once, re-activate, and can be removed`)
- [x] locale packs over English, per-key fallback, placeholder parity (test: `apps/desktop/src/i18n/packs.test.ts`)
- [x] per-profile theme and fonts live in a `record` storage family, so a profile rename moves them (`storageFamilies.test.ts`)
- Deliberate differences: presets are OKLCH recipes with fitted lightness; search runs on submit (no live debounce); imported themes are stored as user themes per device.
- Not taken: font stylesheet URLs (no remote fonts), glass / translucency, the TUI colour payload (polarity only), the terminal pane reading `--indus-terminal-font` (follow-up: TerminalPane takes xterm font options).

## Status

Done (2026-10-21). Shared: `packages/ui/src/index.ts` (exports), `apps/desktop/src/app/theme.tsx` (paints a chosen preset), `settings/customPages.tsx` (ThemePage mounts `AppearancePresets`), `profiles/storageFamilies.ts` (two keys), `App.tsx` (profile style), IPC table + `main.ts` (marketplace channels), `en.ts`, `tests/fixtures/desktop/vscode_theme.json` (ours).
