// The desktop's look (W51-b): one table of role tokens per colour scheme, written as `--desk-*`
// custom properties on `:root[data-theme=…]`. Every renderer stylesheet reads these names.
//
// Roles, not colours: `bg-*` are surfaces from the outermost band inwards, `fg-1..4` the text
// ladder, `stroke-*` the hairlines, `row-*` list states, `accent*` the brand blue. The values were
// measured from the parity screenshots; they are tuned by hand here, not generated.
//
// The older `--indus-color-*` names (written by `@indus/ui`) are pointed at the same palette so
// stylesheets that still use them follow. A chosen appearance preset repaints `--indus-color-*`;
// `deskFromPresetCss` then derives the desk surfaces from the preset's colours so the whole window
// follows the preset rather than only the parts that use the older names.

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
  "attention", // orange: an approval waits, the backend needs setup (W52-g)
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

const dark: DeskPalette = {
  "bg-band": "#010409",
  "bg-canvas": "#0d1015",
  "bg-input": "#0c0e12",
  "bg-raised": "#151a21",
  "bg-bubble": "#0f1621",
  "bg-code": "#0f1116",
  "bg-table-head": "#11141a",
  "bg-kbd": "#14171d",
  backdrop: "rgb(0 0 0 / 0.62)",
  "row-hover": "#0b111b",
  "row-active": "#101826",
  "row-active-soft": "#0d1421",
  "fg-1": "#d8dfe5",
  "fg-2": "#aab1b6",
  "fg-3": "#7f858b",
  "fg-4": "#4a4e54",
  "stroke-1": "#1c2027",
  "stroke-2": "#1d2535",
  "stroke-3": "#232f47",
  "stroke-field": "#151b26",
  "stroke-strong": "#2c3035",
  "stroke-active": "#1f2c44",
  "bg-pill": "#1a1e24",
  "bg-card": "#151a22",
  "stroke-card": "#232d40",
  accent: "#4a84fe",
  "accent-fg": "#151617", // dark text on the light blue, as measured
  "accent-soft": "#13213d",
  "accent-tint": "#101722",
  "accent-ring": "#192643",
  wordmark: "#c5cbd1",
  "send-bg": "#d8dfe6",
  "send-fg": "#0d0e11",
  danger: "#ef6f86",
  "danger-soft": "#2a1218",
  warning: "#d6a24a",
  success: "#55a583",
  attention: "#fe9a00",
  "code-fg": "#d8dfe5",
  "code-keyword": "#f47067",
  "code-function": "#d2a8ff",
  "code-number": "#79b8ff",
  "code-string": "#a5d6ff",
  "code-comment": "#7f858b",
  selection: "rgb(74 132 254 / 0.32)",
};

const light: DeskPalette = {
  "bg-band": "#f6f8fa",
  "bg-canvas": "#ffffff",
  "bg-input": "#fbfbfc",
  "bg-raised": "#ffffff",
  "bg-bubble": "#fcfcfd",
  "bg-code": "#f6f8fa",
  "bg-table-head": "#fafbfc",
  "bg-kbd": "#f3f5f6",
  backdrop: "rgb(0 0 0 / 0.55)",
  "row-hover": "#e8eef7",
  "row-active": "#d8e1f0",
  "row-active-soft": "#dee5f2",
  "fg-1": "#24292f",
  "fg-2": "#4f5359",
  "fg-3": "#71757a",
  "fg-4": "#b4b7ba",
  "stroke-1": "#e3e6ea",
  "stroke-2": "#dae3f4",
  "stroke-3": "#c8d5f1",
  "stroke-field": "#e3e9f5",
  "stroke-strong": "#d0d7de",
  "stroke-active": "#b9cae8",
  "bg-pill": "#f3f4f5",
  "bg-card": "#f0f2f8",
  "stroke-card": "#ced9ef",
  accent: "#0053fd",
  "accent-fg": "#ffffff",
  "accent-soft": "#e6eeff",
  "accent-tint": "#eff4fe",
  "accent-ring": "#cbdcfe",
  wordmark: "#0053fd",
  "send-bg": "#2c3035",
  "send-fg": "#ffffff",
  danger: "#c4213f",
  "danger-soft": "#fbeaee",
  warning: "#9a6516",
  success: "#1a7f5a",
  attention: "#e17100",
  "code-fg": "#1f2328",
  "code-keyword": "#cf222e",
  "code-function": "#8250df",
  "code-number": "#0550ae",
  "code-string": "#0a3069",
  "code-comment": "#6e7781",
  selection: "rgb(0 83 253 / 0.18)",
};

export const deskPalettes: Readonly<Record<DeskScheme, DeskPalette>> = { light, dark };

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
  "font-display": '"Abril Fatface", "Bodoni Moda", Didot, "Times New Roman", serif',
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
    wordmark: scheme === "dark" ? mix(ink, 85, base) : accent,
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
