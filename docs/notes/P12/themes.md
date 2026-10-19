# Task: themes (P12-25) Phase: 12 Owner: wave-8 builder R1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `ui-tui/src/theme.ts` (the theme
model, seed → derived tone idea, the contrast floors against the background, the light/dark
detection order, the skin merge). Research `05-tui.md` §3.21; `16-what-we-change.md` §2.5,
D-16.23. Evidence: none.

What it does (own words): the TUI paints with a `Theme { color, brand }`. Colours come from a
handful of seeds per polarity (our clay accent, deep teal secondary, slate neutrals from
`@indus/ui`), every secondary tone (muted text, labels, panel fill, selected row, selection) is
derived by mixing seeds with the background, and every foreground is lifted until it clears a
contrast floor against the background the terminal really has. Light or dark is decided once at
start from an ordered list of signals; the result and the last resolved theme are cached on disk
so the next start paints its first frame in the right colours. The bridge's `gateway.ready`
carries a theme name (`auto` = detect); a theme payload (name, colours, brand) can be merged over
the default; `/theme <name>` previews live and commits or reverts.

Detection order (first that decides wins): `INDUS_TUI_LIGHT` (1/true/yes/on, 0/false/no/off) →
`INDUS_TUI_THEME` (`light`, `dark`, or a background hex) → `INDUS_TUI_BACKGROUND` hex hint
(luminance ≥ 0.5 = light) → `COLORFGBG` last field (7 or 15 light, any other 0–15 slot dark and
authoritative) → `TERM_PROGRAM` light-default list (`Apple_Terminal`) → OSC 11 probe with a
timeout (pure `#000000` / `#ffffff` answers are distrusted as "unset"; the answer is cached) →
dark.

Inputs → outputs: env, an optional OSC 11 answer, the cache file `~/.indus/tui/theme-boot.json`
(`INDUS_HOME` respected), a theme name/payload → `Theme`, `themeStore` (live + preview),
`ThemeProvider` / `useTheme()`.

Files: `apps/tui/src/theme/{theme,detect,bootCache,palette}.ts` plus `ThemeProvider` in
`theme.ts`, tests `theme.test.ts`, `detect.test.ts`. `apps/tui/package.json` gains the workspace
dependency `@indus/ui`.

Edge cases / failure modes:
- malformed values (`COLORFGBG='15;'`, `fffgff`) are ignored, never misread as a colour.
- OSC 11 answers in `rgb:RRRR/GGGG/BBBB`, `rgb:RR/GG/BB`, `#rrggbb`; BEL or ST terminator; split
  across reads; no answer within the timeout → unset (the probe never blocks start-up for long).
- a cache file that is missing, corrupt, from another version or unreadable → ignored.
- a payload colour that is not a colour is dropped; a payload that lowers contrast below the floor
  is lifted again.
- truecolor opt-in (`INDUS_TUI_TRUECOLOR=1`) sets `FORCE_COLOR=3` before any colour library loads.

Our design differences / decisions:
- our own seeds and names (`indus-dark`, `indus-light`, aliases `dark`/`light`/`auto`), our own
  prompt glyph `›`, tool prefix `│`, banner text; no reference glyphs or names (D-16.23).
- contrast floors: body text and prompt 4.5:1, every other foreground 3:1; panel fills keep the
  background's polarity and keep body text at 4.5:1 on them.
- no ANSI-256 remap for a limited light terminal (not needed for the default palettes; later).

Tests we will write: one detection row per source (COLORFGBG authoritative over TERM_PROGRAM;
OSC 11 `#000000` ignored), OSC reply parsing, contrast floor for every token pair in both default
palettes and against off-white/near-black backgrounds, payload merge, preview/commit/revert, boot
cache round trip and read-before-first-render, corrupt cache ignored.

## Decisions for the lead
- Our bridge only sends a theme *name*; `applyThemePayload` also takes `{name, colors, brand}` for
  a later `theme.changed` event (protocol gap: no such event yet).
- `Theme.brand` adds `prompt`, `tool`, `welcome`, `goodbye` to the contract's `{name, accent}`.

Compare checklist (filled after the rewrite; reopened `ui-tui/src/theme.ts`):
- [x] detection order, one row per source; COLORFGBG authoritative over TERM_PROGRAM; malformed
  COLORFGBG / hex ignored (tests: `detect.test.ts::light/dark detection order …`)
- [x] OSC 11 probe with timeout, `#000000`/`#ffffff` distrusted, answer cached (tests: `…::probes once…`,
  `…::ignores a #000000 answer and gives up after the timeout`)
- [x] seeds → derived tones → contrast floor against the real background (tests:
  `theme.test.ts::meets the contrast floor for every token pair…`, `…::still meets the floors on backgrounds…`)
- [x] payload merged over the default, bad colours dropped, brand text cleaned (test: `…::merges a payload…`)
- [x] boot theme read before the first render (test: `…::is read before the first render…`)
- [x] truecolor opt-in (test: `detect.test.ts::forces truecolor only when asked`)
- Deliberate differences: our seeds (clay/teal/slate from `@indus/ui`), names and brand marks; one
  floor table (4.5 body text, 3 others) instead of separate display/semantic floors per polarity;
  fills are pulled toward the background until body text reads on them instead of falling back to a
  base palette. Light threshold: WCAG luminance ≥ 0.5.
- Not taken: the ANSI-256 remap for a limited light terminal (Apple Terminal without truecolor), the
  OSC 10 foreground probe, painting the terminal's default colours (OSC 10/11 set), banner art.

## Status

Done (2026-10-19). `apps/tui/src/theme/{theme,detect,bootCache,palette}.ts`, tests `theme.test.ts`
(13), `detect.test.ts` (21). Shared: `apps/tui/package.json` (workspace deps `@indus/ui`,
`@indus/protocol`, `test` script), `pnpm-lock.yaml` regenerated locally.
