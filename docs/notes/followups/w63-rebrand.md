# Task: Indus AGI rebrand of the people-facing surfaces (W63, D-16.1285) Phase: followups Owner: Agent 4

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): none (an owner brand decision, not
parity work; the reference was not opened for it).
Evidence: none. The owner's artwork (mark on orange, mark over the stripe gradient, dark lockup)
was supplied in the session and is not committed; the mark was traced from it.

What it does (own words): the owner asked for the web dashboard, the desktop app and the TUI to be
rebranded without changing their layout: the name **Indus AGI Agent**, the owner's mark, and a
colour theme drawn from the artwork. One source, `packages/ui/src/brand.ts` (`@indus/ui`), holds
the name, short name, two-line wordmark (INDUS / AGI), the sampled colours (ember `#f24001`,
emberDeep, amber, gold, sand, cream, black `#0a0a0a`, white), the gradient and the mark as one SVG
path; `packages/ui/brand/*.svg` are the mark, the white mark and the app tile. The login page and
the first-run message are rendered by Python, so `src/indus/web/brand.py` keeps a copy that a test
checks against the TS file.

Per surface:
- Web: presets gain an optional accent (+ text on it); without one the accent is the ink, so the
  eight older presets are unchanged. Indus Ember (dark), Ember Large and Indus Daylight (light) are
  the defaults. The accent marks brand moments only (primary buttons, selected tabs and toggles,
  checkboxes, active nav, focus frames, links, live badge, main-model card, input chart series).
  Sidebar: mark + INDUS / AGI under a 3 px gradient band; mark only when collapsed. SVG favicon
  built from `markSvg` (`apps/web/vite.brand.ts`).
- Desktop: desk tokens repainted (ember accent with black text on dark, `#c23300` with white on
  light; near-black and warm-paper surfaces; attention moved to gold); the built-in look is named
  Indus Ember / Indus Daylight and is the default; `BrandMark` / `BrandLockup` on the new-chat
  screen, About and onboarding; display name in window title, tray, notifications, launcher and
  package copy; app and tray icons redrawn from the mark by `scripts/make-icons.mjs`.
- TUI: seeds from `BRAND_COLORS` (ember accent, brand-black dark background, warm-white text);
  the banner draws the mark in half blocks beside INDUS / AGI and the full name; boot cache
  version 2.
- Python-rendered: the remote-access login page (brand black, ember buttons with black text, the
  mark + wordmark), the "not set up yet" first-run line, the user docs home heading.

Deliberately unchanged: the `indus` command and package, the `INDUS_` env prefix, `~/.indus`, the
desktop productName / appId / userData / protocol / release identity (changing them would orphan
installs and data), legal and publisher names ("Indus Agent contributors"), sentence copy that says
"Indus …", generated catalogues from the backend, and the backend's other CLI strings.

Edge cases / failure modes: white text on ember fails AA (3.8:1), so text on ember is black on dark
themes and the light themes use a deeper ember (`#c23300` / `#d62e02`) with white; an old TUI boot
cache would paint one frame in the old colours (version bump); the older web presets keep their
exact colours (accent falls back to ink).

Tests: web `presets.test.ts` (accent contrast for every preset, brand values pinned),
`ThemeMenu.test.tsx`, `StatusStrip.test.tsx`, `vite.brand.test.ts`; desktop `deskTokens.test.ts`
(accent and state contrast, hue distance, roles from `BRAND_COLORS`), `builderConfig.test.ts`
(icon pixels match the generator), `brandMark.test.ts`, `SettingsOverlay.test.tsx`; TUI
`theme.test.ts`, `Transcript.test.tsx` (banner rows and fallbacks), demo2 goldens regenerated
(banner only); Python `test_login_page.py::test_the_page_carries_the_indus_agi_brand`,
`::test_the_brand_copy_matches_the_shared_brand_file`.

Compare checklist: not a parity task — the look diverges from the reference's captures on purpose
(D-16.1285); layouts, pages and behaviour are unchanged.

## Status

Done (2026-10-25). Checks: web tsc, vitest 454, Playwright e2e 18/18; desktop tsc, vitest 1930,
Electron e2e 42 passed / 2 skipped; TUI tsc, vitest 800, e2e 12 passed / 2 skipped; Python web and
docs tests; biome, ruff, pyright on the changed modules, tooling.checks --require-ref ok.
