# Task: Brand tokens for @indus/ui (P00-12) Phase: P00 Owner: TS builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `web/src/themes/types.ts` (category
names only — which surfaces a theme must cover: background layers, typography, layout radius and
density). No values, names or colours read.
Evidence: R09.22 (never reuse reference colours, fonts, theme names).

What it does (own words): defines the Indus Agent visual language once, as typed TypeScript data,
so the web UI, desktop app, TUI and the in-browser terminal all draw from the same source.
Raw colour ramps (six named families, eleven steps each, OKLCH with hex fallback) feed semantic
tokens for two themes (`indus-light`, `indus-dark`). Type scale, font stacks (family names only, no
font files), spacing, radii, shadows and motion sit beside them. `tokensToCss(theme)` turns a theme
into `--indus-*` custom properties, and an ANSI 16-colour map per theme serves terminal surfaces.

Inputs → outputs: `tokensToCss("indus-light" | "indus-dark")` → a CSS text block
(`:root[data-theme="…"] { --indus-…: …; }` plus a reduced-motion block); typed objects
(`palette`, `themes`, `typeScale`, `fontStacks`, `space`, `radii`, `shadows`, `motion`, `ansi`).

Files: `packages/ui/src/tokens/{palette,semantic,type,space,motion,ansi,css}.ts`,
`packages/ui/src/tokens/tokens.test.ts`, `packages/ui/src/index.ts`.

Edge cases / failure modes:
- Contrast: semantic text/background pairs must reach WCAG 4.5:1 (3:1 for border/bg) in both
  themes; computed from the hex fallbacks with the WCAG relative-luminance formula.
- Ramp ordering: each ramp's OKLCH lightness strictly decreases from 50 to 950.
- OKLCH and hex must describe the same colour: hex is derived from OKLCH by code at authoring
  time and the test re-derives it, so the two cannot drift (tolerance: one 8-bit step).
- Reduced motion: durations go to 0 ms under `prefers-reduced-motion: reduce`.
- Unknown theme name: a type error at compile time; a runtime throw for untyped callers.

Our design differences / decisions:
- Theme and ramp names are ours (river, silt, clay, reed, ochre, kiln; indus-light/dark).
- OKLCH is the source of truth; hex is computed (sRGB gamut-clipped by chroma reduction).
- Fonts by family name only (Red Hat Text, Red Hat Mono; both OFL) with system fallbacks; no files vendored.

Tests we will write (`tokens.test.ts`):
- contrast pairs in both themes; border/bg ≥ 3.0
- lightness monotonic per ramp; 11 steps each; hex matches OKLCH
- CSS snapshot per theme; every custom property name starts `--indus-`
- ANSI: 16 entries per theme, all `#rrggbb`
- no token string contains a banned name (small list in the test)

## Decisions for the lead
- Lead ruling (intended difference, R09.22): fonts are `"Red Hat Text"` (UI) and
  `"Red Hat Mono"` (code), both OFL. The pair was chosen because it does not occur anywhere in the
  reference (grep: 0 hits). It supersedes the earlier IBM Plex ruling (the reference's `mono`
  preset pairs IBM Plex Sans + Mono) and the task row's Inter + JetBrains Mono (the reference's
  default pair). Fallback lists are our own (`system-ui, "Segoe UI Variable", "Segoe UI", "Noto Sans", Ubuntu,
  sans-serif`; `ui-monospace, "Cascadia Code", "Noto Sans Mono", "DejaVu Sans Mono", monospace`);
  the first draft's sans fallback matched the reference's system list word for word and was
  replaced.
- Lead ruling (intended difference, R09.22): the danger ramp is `kiln`, not `ember` (the
  reference has a theme named `ember`). Hue and values unchanged.
- Hex fallbacks are computed from OKLCH at module load (`oklchToHex`, gamut-fitted by reducing
  chroma), not stored as literals, so they cannot drift; the test checks them independently by
  converting each hex back to OKLab lightness (tolerance 0.005) and the CSS snapshot pins values.
- Shadows live in `space.ts` per theme (geometry file), three levels `low`/`mid`/`high`.
- ANSI colours are their own OKLCH hues (adds magenta, which no ramp covers), lightness per theme.
- Type step names: `xs sm base md lg xl 2xl 3xl` for 12…36 px.
- The banned-name cross-check reads `tooling/checks/banned_patterns.json` at test time instead of
  spelling the names in `packages/` (spelling them would itself break the banned rule).
- `selection` is a translucent accent colour (hex with alpha, `#rrggbbaa`) so selected text keeps
  its own colour.

Compare checklist (filled after the rewrite; reopened `web/src/themes/*` only to confirm no match):
- [x] Colours: none of our 97 palette/ANSI hex values, nor the semantic ones (besides `#ffffff`),
  appears among the ~3000 distinct hex literals in the reference's TS/CSS/PY/JSON/YAML files.
- [x] Theme names `indus-light`/`indus-dark` are ours (test `clean-room names`).
- [x] Ramp `ember` (reference theme name) renamed `kiln` (lead ruling).
- [x] Font pair: Inter + JetBrains Mono (reference default) and then IBM Plex (a reference
  preset) both replaced by Red Hat Text + Red Hat Mono, which have 0 hits in the reference;
  fallback lists rewritten so they no longer equal the reference's system stacks.

## Status

Done (2026-10-18). Lead rulings applied (Red Hat Text/Mono fonts, `kiln`, direct `vite` pin).
