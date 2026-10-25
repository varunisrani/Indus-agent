// The desktop's look (W51-b): one table of role tokens per colour scheme, written as `--desk-*`
// custom properties on `:root[data-theme=…]`. Every renderer stylesheet reads these names.
//
// Roles, not colours: `bg-*` are surfaces from the outermost band inwards, `fg-1..4` the text
// ladder, `stroke-*` the hairlines, `row-*` list states, `accent*` the brand ember (W63, the Indus
// AGI palette in `@indus/ui` `BRAND_COLORS`). The values are tuned by hand here, not generated;
// `deskTokens.test.ts` holds their contrast.
//
// The older `--indus-color-*` names (written by `@indus/ui`) are pointed at the same palette so
// stylesheets that still use them follow. A chosen appearance preset repaints `--indus-color-*`;
// `deskFromPresetCss` then derives the desk surfaces from the preset's colours so the whole window
// follows the preset rather than only the parts that use the older names.

import { BRAND_COLORS } from "@indus/ui";

export type DeskScheme = "light" | "dark";

export const deskColourNames = [
  // surfaces, outermost first
  "bg-band", // title bar, sidebar, status bar
  "bg-canvas", // the chat and page body
  "bg-input", // composer and text fields
  "bg-raised", // menus, popovers, dialogs
  "bg-bubble", // the user's message
  "bg-code", // fenced code
  "bg-table-head",
  "bg-kbd",
  "backdrop", // behind modal cards
  // list rows and quiet controls
  "row-hover",
  "row-active",
  "row-active-soft",
  // text ladder
  "fg-1",
  "fg-2",
  "fg-3",
  "fg-4",
  // hairlines
  "stroke-1", // neutral divider
  "stroke-2", // soft tinted (tables, row separators)
  "stroke-3", // tinted frame (composer, user message, modal card)
  "stroke-field", // the faint frame of text fields and selects
  "stroke-strong", // neutral strong edge (switch off, key caps)
  "stroke-active", // the open settings group's edge
  "bg-pill", // neutral badge fill
  "bg-card", // unselected choice card
  "stroke-card",
  // brand
  "accent",
  "accent-fg",
  "accent-soft", // tinted fill behind accent text
  "accent-tint", // selected card fill
  "accent-ring", // selected card outer ring
  "wordmark",
  "send-bg",
  "send-fg",
  // states
  "danger",
  "danger-soft",
  "warning",
  "success",
  "attention", // gold: an approval waits, the backend needs setup (W52-g); never the accent
  // code
  "code-fg",
  "code-keyword",
  "code-function",
  "code-number",
  "code-string",
  "code-comment",
  "selection",
] as const;
export type DeskColourName = (typeof deskColourNames)[number];

export type DeskPalette = Readonly<Record<DeskColourName, string>>;

// Indus Ember (dark): neutral near-black surfaces around the brand black, the bands a step lifted,
// neutral grey hairlines, the mark's ember as the accent (dark text on it: white on ember is under
// AA). Indus Daylight (light): warm paper and cream, a deeper ember that holds AA for text on the
// paper and for white text on it. The mark itself is the brand ember in both (`wordmark`).
const dark: DeskPalette = {
  "bg-band": "#111111",
  "bg-canvas": BRAND_COLORS.black,
  "bg-input": "#0e0e0e",
  "bg-raised": "#161616",
  "bg-bubble": "#1a1513",
  "bg-code": "#101010",
  "bg-table-head": "#131313",
  "bg-kbd": "#1a1a1a",
  backdrop: "rgb(0 0 0 / 0.66)",
  "row-hover": "#181818",
  "row-active": "#251712",
  "row-active-soft": "#1d1411",
  "fg-1": "#ebe7e1",
  "fg-2": "#b3aea7",
  "fg-3": "#86817b",
  "fg-4": "#4f4b47",
  "stroke-1": "#1f1f1f",
  "stroke-2": "#232323",
  "stroke-3": "#2e2e2e",
  "stroke-field": "#1c1c1c",
  "stroke-strong": "#333333",
  "stroke-active": "#582310",
  "bg-pill": "#1c1c1c",
  "bg-card": "#141414",
  "stroke-card": "#2a2a2a",
  accent: BRAND_COLORS.ember,
  "accent-fg": BRAND_COLORS.black, // white on ember fails AA; the brand black reads at 5.2:1
  "accent-soft": "#2f1309",
  "accent-tint": "#1a0e09",
  "accent-ring": "#501a07",
  wordmark: BRAND_COLORS.ember,
  "send-bg": BRAND_COLORS.ember,
  "send-fg": BRAND_COLORS.black,
  danger: "#f0708f",
  "danger-soft": "#2a1117",
  warning: "#e0c84a",
  success: "#55a583",
  attention: "#f8ad04",
  "code-fg": "#ebe7e1",
  "code-keyword": "#f47067",
  "code-function": "#d2a8ff",
  "code-number": "#79b8ff",
  "code-string": "#a5d6ff",
  "code-comment": "#86817b",
  selection: "rgb(242 64 1 / 0.3)",
};

const light: DeskPalette = {
  "bg-band": "#f6f1e9",
  "bg-canvas": "#fffcf7",
  "bg-input": "#fffdfa",
  "bg-raised": "#ffffff",
  "bg-bubble": "#fbf5ee",
  "bg-code": "#f7f2ea",
  "bg-table-head": "#faf6f0",
  "bg-kbd": "#f1ebe2",
  backdrop: "rgb(10 10 10 / 0.5)",
  "row-hover": "#fbf0e8",
  "row-active": "#f6e0d4",
  "row-active-soft": "#f9e8de",
  "fg-1": "#1f1a16",
  "fg-2": "#4f4740",
  "fg-3": "#71685f",
  "fg-4": "#b6aca1",
  "stroke-1": "#e8e1d6",
  "stroke-2": "#ece3d6",
  "stroke-3": "#e0d3c2",
  "stroke-field": "#e9e1d5",
  "stroke-strong": "#d4c9bb",
  "stroke-active": "#edc0ad",
  "bg-pill": "#f3eee6",
  "bg-card": "#f8f3ec",
  "stroke-card": "#e3d7c7",
  accent: "#c23300",
  "accent-fg": BRAND_COLORS.white,
  "accent-soft": "#f8e4d9",
  "accent-tint": "#fbf0e8",
  "accent-ring": "#eec4b2",
  wordmark: BRAND_COLORS.ember,
  "send-bg": "#c23300",
  "send-fg": BRAND_COLORS.white,
  danger: "#b51747",
  "danger-soft": "#fbe9ee",
  warning: "#7a6f00",
  success: "#1a7f5a",
  attention: "#9c6a00",
  "code-fg": "#1f1a16",
  "code-keyword": "#cf222e",
  "code-function": "#8250df",
  "code-number": "#0550ae",
  "code-string": "#0a3069",
  "code-comment": "#6e6760",
  selection: "rgb(194 51 0 / 0.16)",
};

export const deskPalettes: Readonly<Record<DeskScheme, DeskPalette>> = { light, dark };

/** The brand ember's hue in degrees: where the accent-hue slider rests with no hue chosen. */
export const BRAND_ACCENT_HUE = 16;

/** Sizes shared by both schemes (px unless named otherwise). */
export const deskSizes = {
  "band-height": "34px",
  "status-height": "22px",
  "sidebar-width": "260px",
  "row-height": "28px",
  "control-height": "28px",
  "radius-control": "4px",
  "radius-card": "6px",
  "radius-pill": "999px",
  "text-micro": "11px",
  "text-small": "12px",
  "text-ui": "13px",
  "text-nav": "14px",
  "text-chat": "15px",
  "text-title": "18px",
  "font-ui": '"Inter", system-ui, "Segoe UI Variable", "Segoe UI", "Noto Sans", Ubuntu, sans-serif',
  // the wordmark's heavy sans (Inter 800), set beside the mark
  "font-display": '"Inter", system-ui, "Segoe UI Variable", "Segoe UI", "Noto Sans", sans-serif',
  "shadow-menu": "0 8px 24px rgb(0 0 0 / 0.18), 0 1px 3px rgb(0 0 0 / 0.12)",
  "shadow-card": "0 24px 64px rgb(0 0 0 / 0.35), 0 2px 8px rgb(0 0 0 / 0.2)",
} as const;
export type DeskSizeName = keyof typeof deskSizes;

/** Which `--indus-color-*` name follows which desk role. */
const SEMANTIC_BRIDGE: ReadonlyArray<readonly [string, DeskColourName]> = [
  ["bg", "bg-canvas"],
  ["bg-raised", "bg-raised"],
  ["fg", "fg-1"],
  ["fg-muted", "fg-2"],
  ["border", "stroke-1"],
  ["accent", "accent"],
  ["accent-fg", "accent-fg"],
  ["success", "success"],
  ["warning", "warning"],
  ["danger", "danger"],
  ["focus-ring", "accent"],
  ["code-bg", "bg-code"],
  ["selection", "selection"],
];

function themeSelector(scheme: DeskScheme): string {
  return `:root[data-theme="indus-${scheme}"]`;
}

/** The desk tokens of one scheme as a CSS rule (and the older names pointed at them). */
export function deskSchemeCss(scheme: DeskScheme): string {
  const palette = deskPalettes[scheme];
  const lines: string[] = [];
  for (const name of deskColourNames) lines.push(`  --desk-${name}: ${palette[name]};`);
  for (const [name, value] of Object.entries(deskSizes)) lines.push(`  --desk-${name}: ${value};`);
  for (const [legacy, role] of SEMANTIC_BRIDGE) {
    lines.push(`  --indus-color-${legacy}: var(--desk-${role});`);
  }
  lines.push(`  --indus-font-sans: var(--desk-font-ui);`);
  lines.push(`  color-scheme: ${scheme};`);
  return `${themeSelector(scheme)} {\n${lines.join("\n")}\n}\n`;
}

/** Both schemes; painted after the `@indus/ui` base tokens so these win. */
export function deskTokenCss(): string {
  return (["light", "dark"] as const).map(deskSchemeCss).join("\n");
}

/**
 * With a preset chosen, the preset writes `--indus-color-*`; the desk surfaces are then mixed from
 * those so the band, rows and hairlines take the preset's hue. The older names must not point back
 * at the desk ones here (that would be a cycle), so they are left to the preset.
 */
export function deskFromPresetCss(scheme: DeskScheme): string {
  const towards = scheme === "dark" ? "black" : "white";
  const ink = "var(--indus-color-fg)";
  const base = "var(--indus-color-bg)";
  const accent = "var(--indus-color-accent)";
  const mix = (a: string, pct: number, b: string) => `color-mix(in srgb, ${a} ${pct}%, ${b})`;
  const rules: Record<string, string> = {
    "bg-band": mix(base, scheme === "dark" ? 55 : 94, scheme === "dark" ? "black" : ink),
    "bg-canvas": base,
    "bg-input": mix(base, 96, towards),
    "bg-raised": "var(--indus-color-bg-raised)",
    "bg-bubble": mix(accent, 5, base),
    "bg-code": "var(--indus-color-code-bg)",
    "bg-table-head": mix(ink, 3, base),
    "bg-kbd": mix(ink, 6, base),
    "row-hover": mix(accent, 6, base),
    "row-active": mix(accent, 14, base),
    "row-active-soft": mix(accent, 10, base),
    "fg-1": ink,
    "fg-2": "var(--indus-color-fg-muted)",
    "fg-3": mix(ink, 58, base),
    "fg-4": mix(ink, 32, base),
    "stroke-1": "var(--indus-color-border)",
    "stroke-2": mix(accent, 14, base),
    "stroke-3": mix(accent, 24, base),
    "stroke-field": mix(accent, 8, base),
    "stroke-strong": mix(ink, 22, base),
    "stroke-active": mix(accent, 30, base),
    "bg-pill": mix(ink, 6, base),
    "bg-card": mix(accent, 5, base),
    "stroke-card": mix(accent, 20, base),
    "accent-tint": mix(accent, 7, base),
    "accent-ring": mix(accent, 25, base),
    accent,
    "accent-fg": "var(--indus-color-accent-fg)",
    "accent-soft": mix(accent, 12, base),
    wordmark: accent,
    "send-bg": ink,
    "send-fg": base,
    danger: "var(--indus-color-danger)",
    "danger-soft": mix("var(--indus-color-danger)", 10, base),
    warning: "var(--indus-color-warning)",
    success: "var(--indus-color-success)",
    attention: "var(--indus-color-warning)",
    selection: "var(--indus-color-selection)",
  };
  const lines = Object.entries(rules).map(([name, value]) => `  --desk-${name}: ${value};`);
  // the preset's own rule is `:root:root:root`; this one matches it and comes after it
  return `:root:root:root {\n${lines.join("\n")}\n}\n`;
}
