// VS Code colour themes into our tokens (P24-33).
//
// `convertVscodeTheme` maps a VS Code theme's `colors` to the thirteen semantic tokens of
// `@indus/ui`, deterministically: each token reads a short list of VS Code keys in order and falls
// back to a value derived from the background and foreground; translucent colours are composited
// over what they sit on; every text pair is then nudged (towards black or white, in fixed steps)
// until it reaches WCAG AA, so an imported theme is as readable as a preset. A full set of
// `terminal.ansi*` colours is carried for the terminal.
//
// The Marketplace flow: search (main does the network, `themes:search`), download the extension
// package (`themes:download`), read its `package.json` and the theme file it lists (`readVsix`: a
// zip reader over `DecompressionStream`), convert, install as a user theme.

import {
  CONTRAST_PAIRS,
  contrastRatio,
  type SemanticColour,
  type SemanticName,
  type SemanticTheme,
  semanticNames,
} from "@indus/ui";

export type ThemeMode = "light" | "dark";

export interface VscodeTheme {
  name?: string;
  type?: string;
  colors?: Record<string, unknown>;
}

export const ANSI_KEYS = [
  "black",
  "red",
  "green",
  "yellow",
  "blue",
  "magenta",
  "cyan",
  "white",
  "brightBlack",
  "brightRed",
  "brightGreen",
  "brightYellow",
  "brightBlue",
  "brightMagenta",
  "brightCyan",
  "brightWhite",
] as const;
export type AnsiKey = (typeof ANSI_KEYS)[number];

export interface ConvertedTheme {
  readonly id: string;
  readonly label: string;
  readonly mode: ThemeMode;
  readonly theme: SemanticTheme;
  readonly terminal: Readonly<Record<AnsiKey, string>> | null;
  /** Tokens that had no VS Code key and were derived. */
  readonly derived: readonly SemanticName[];
}

// -- JSONC ----------------------------------------------------------------------------------------

/** JSON with comments and trailing commas (what VS Code theme files are). */
export function parseJsonc(text: string): unknown {
  let out = "";
  let i = 0;
  let inString = false;
  while (i < text.length) {
    const ch = text[i] as string;
    const next = text[i + 1];
    if (inString) {
      out += ch;
      if (ch === "\\") {
        out += next ?? "";
        i += 2;
        continue;
      }
      if (ch === '"') inString = false;
      i += 1;
      continue;
    }
    if (ch === '"') {
      inString = true;
      out += ch;
      i += 1;
    } else if (ch === "/" && next === "/") {
      while (i < text.length && text[i] !== "\n") i += 1;
    } else if (ch === "/" && next === "*") {
      const end = text.indexOf("*/", i + 2);
      i = end < 0 ? text.length : end + 2;
    } else {
      out += ch;
      i += 1;
    }
  }
  return JSON.parse(out.replace(/,(\s*[}\]])/g, "$1"));
}

// -- colours --------------------------------------------------------------------------------------

type Rgb = readonly [number, number, number];

function toRgb(hex: string): Rgb {
  const n = Number.parseInt(hex.slice(1, 7), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function toHex([r, g, b]: Rgb): string {
  const part = (v: number) =>
    Math.round(Math.min(255, Math.max(0, v)))
      .toString(16)
      .padStart(2, "0");
  return `#${part(r)}${part(g)}${part(b)}`;
}

/** `#rgb`, `#rgba`, `#rrggbb`, `#rrggbbaa` → `{hex, alpha}`; anything else → null. */
export function parseColour(value: unknown): { hex: string; alpha: number } | null {
  if (typeof value !== "string") return null;
  const text = value.trim().toLowerCase();
  if (!/^#([0-9a-f]{3}|[0-9a-f]{4}|[0-9a-f]{6}|[0-9a-f]{8})$/.test(text)) return null;
  let body = text.slice(1);
  if (body.length <= 4) body = [...body].map((c) => c + c).join("");
  const alpha = body.length === 8 ? Number.parseInt(body.slice(6, 8), 16) / 255 : 1;
  return { hex: `#${body.slice(0, 6)}`, alpha };
}

export function mix(a: string, b: string, share: number): string {
  const x = toRgb(a);
  const y = toRgb(b);
  return toHex(
    [0, 1, 2].map(
      (i) => (x[i] as number) * (1 - share) + (y[i] as number) * share,
    ) as unknown as Rgb,
  );
}

function opaque(value: unknown, backdrop: string): string | null {
  const parsed = parseColour(value);
  if (parsed === null) return null;
  return parsed.alpha >= 1 ? parsed.hex : mix(backdrop, parsed.hex, parsed.alpha);
}

/** `colour` moved towards `target` in 2 % steps until it reaches `minimum` against `against`. */
export function nudge(
  colour: string,
  against: readonly string[],
  minimum: number,
  target: string,
): string {
  let current = colour;
  for (let step = 0; step <= 50; step++) {
    if (against.every((other) => contrastRatio(current, other) >= minimum)) return current;
    current = mix(colour, target, Math.min(1, (step + 1) * 0.02));
  }
  return current;
}

// -- conversion -----------------------------------------------------------------------------------

const DEFAULTS: Record<ThemeMode, { bg: string; fg: string; accent: string }> = {
  dark: { bg: "#1f2024", fg: "#e6e4df", accent: "#4f9cd8" },
  light: { bg: "#fbfaf7", fg: "#24221f", accent: "#2a6fb0" },
};

/** VS Code keys read for each token, first found wins. */
export const TOKEN_KEYS: Readonly<Record<Exclude<SemanticName, "accent-fg">, readonly string[]>> = {
  bg: ["editor.background", "editorPane.background", "editorGroup.background"],
  "bg-raised": [
    "sideBar.background",
    "editorWidget.background",
    "panel.background",
    "activityBar.background",
  ],
  fg: ["editor.foreground", "foreground"],
  "fg-muted": ["descriptionForeground", "editorLineNumber.foreground", "tab.inactiveForeground"],
  border: [
    "panel.border",
    "editorGroup.border",
    "sideBar.border",
    "contrastBorder",
    "widget.border",
    "input.border",
  ],
  accent: [
    "button.background",
    "textLink.foreground",
    "activityBarBadge.background",
    "focusBorder",
  ],
  success: ["gitDecoration.addedResourceForeground", "terminal.ansiGreen"],
  warning: ["editorWarning.foreground", "list.warningForeground", "terminal.ansiYellow"],
  danger: ["editorError.foreground", "errorForeground", "terminal.ansiRed"],
  "focus-ring": ["focusBorder", "button.background"],
  "code-bg": [
    "textCodeBlock.background",
    "editor.lineHighlightBackground",
    "editorWidget.background",
  ],
  selection: ["editor.selectionBackground", "selection.background"],
};

export function slugOf(name: string): string {
  const slug = name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 48);
  return slug === "" ? "imported" : slug;
}

function modeOf(raw: VscodeTheme, bg: string | null): ThemeMode {
  const type = (raw.type ?? "").toLowerCase();
  if (type === "dark" || type === "hc" || type === "hc-black") return "dark";
  if (type === "light" || type === "hclight" || type === "hc-light") return "light";
  if (bg === null) return "dark";
  const [r, g, b] = toRgb(bg);
  return 0.299 * r + 0.587 * g + 0.114 * b < 128 ? "dark" : "light";
}

export function convertVscodeTheme(
  raw: VscodeTheme,
  fallbackName = "Imported theme",
): ConvertedTheme {
  const colors = raw.colors ?? {};
  const derived: SemanticName[] = [];
  const read = (name: keyof typeof TOKEN_KEYS, backdrop: string): string | null => {
    for (const key of TOKEN_KEYS[name]) {
      const value = opaque(colors[key], backdrop);
      if (value !== null) return value;
    }
    return null;
  };
  const rawBg = parseColour(colors["editor.background"])?.hex ?? null;
  const mode = modeOf(raw, rawBg);
  const base = DEFAULTS[mode];
  const dark = mode === "dark";
  const away = dark ? "#ffffff" : "#000000";
  const or = (name: SemanticName, value: string | null, fallback: () => string): string => {
    if (value !== null) return value;
    derived.push(name);
    return fallback();
  };

  const bg = or("bg", read("bg", base.bg), () => base.bg);
  let fg = or("fg", read("fg", bg), () => base.fg);
  const raised = or("bg-raised", read("bg-raised", bg), () => mix(bg, away, 0.05));
  const code = or("code-bg", read("code-bg", bg), () => mix(bg, away, 0.07));
  fg = nudge(fg, [bg, raised, code], 4.5, away);
  const muted = nudge(
    or("fg-muted", read("fg-muted", bg), () => mix(fg, bg, 0.35)),
    [bg, raised],
    4.5,
    away,
  );
  const border = nudge(
    or("border", read("border", bg), () => mix(fg, bg, 0.6)),
    [bg],
    3,
    away,
  );
  const accentRaw = or("accent", read("accent", bg), () => base.accent);
  // the accent carries text: the theme's button text when it has one (the accent then moves until
  // the pair reads), else white or the background, whichever reads better on it
  const given = opaque(colors["button.foreground"], accentRaw);
  const accentFg =
    given ?? (contrastRatio("#ffffff", accentRaw) >= contrastRatio(bg, accentRaw) ? "#ffffff" : bg);
  const lightText = contrastRatio(accentFg, "#000000") > contrastRatio(accentFg, "#ffffff");
  const accent = nudge(accentRaw, [accentFg], 4.5, lightText ? "#000000" : "#ffffff");
  const status = (name: "success" | "warning" | "danger", fallback: string) =>
    nudge(
      or(name, read(name, bg), () => fallback),
      [bg],
      3,
      away,
    );
  const success = status("success", dark ? "#5fbf7f" : "#2f7d4a");
  const warning = status("warning", dark ? "#d9a84a" : "#8a5a00");
  const danger = status("danger", dark ? "#e0705f" : "#b23a2a");
  const focus = nudge(
    or("focus-ring", read("focus-ring", bg), () => accent),
    [bg],
    3,
    away,
  );
  const selectionRaw = parseColour(
    colors["editor.selectionBackground"] ?? colors["selection.background"],
  );
  let selection: string;
  if (selectionRaw !== null) {
    const alpha = Math.round(Math.min(1, Math.max(0.2, selectionRaw.alpha)) * 255);
    selection = `${selectionRaw.hex}${alpha.toString(16).padStart(2, "0")}`;
  } else {
    derived.push("selection");
    selection = `${accent}66`;
  }

  const values: Record<SemanticName, string> = {
    bg,
    "bg-raised": raised,
    fg,
    "fg-muted": muted,
    border,
    accent,
    "accent-fg": accentFg,
    success,
    warning,
    danger,
    "focus-ring": focus,
    "code-bg": code,
    selection,
  };
  const label = (raw.name ?? "").trim() || fallbackName;
  const theme = Object.fromEntries(
    semanticNames.map((name): [SemanticName, SemanticColour] => [
      name,
      { css: values[name], hex: values[name], source: `vscode.${slugOf(label)}` },
    ]),
  ) as SemanticTheme;

  let terminal: Record<AnsiKey, string> | null = {} as Record<AnsiKey, string>;
  for (const key of ANSI_KEYS) {
    const vsKey = `terminal.ansi${key[0]?.toUpperCase()}${key.slice(1)}`;
    const value = opaque(colors[vsKey], bg);
    if (value === null) {
      terminal = null;
      break;
    }
    terminal[key] = value;
  }

  return { id: `vscode-${slugOf(label)}`, label, mode, theme, terminal, derived };
}

/** The pairs an imported theme still misses (always empty after conversion; kept for checks). */
export function failingPairs(theme: SemanticTheme): string[] {
  return CONTRAST_PAIRS.filter(
    ([fg, bg, min]) => contrastRatio(theme[fg].hex, theme[bg].hex) < min,
  ).map(([fg, bg]) => `${fg}/${bg}`);
}

// -- the extension package ------------------------------------------------------------------------

export const MAX_ENTRY_BYTES = 2 * 1024 * 1024;

export interface VsixFile {
  /** Entry names in the archive. */
  names(): string[];
  /** One entry as text (inflated when compressed). */
  text(name: string): Promise<string>;
}

async function inflate(data: Uint8Array): Promise<Uint8Array> {
  const stream = new Blob([data as BlobPart])
    .stream()
    .pipeThrough(new DecompressionStream("deflate-raw"));
  const out = new Uint8Array(await new Response(stream).arrayBuffer());
  if (out.byteLength > MAX_ENTRY_BYTES) throw new Error("a file in the package is too large");
  return out;
}

/** A zip archive (a `.vsix`) read from its central directory. */
export function readVsix(bytes: Uint8Array): VsixFile {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  let end = -1;
  for (let i = bytes.byteLength - 22; i >= Math.max(0, bytes.byteLength - 65_557); i--) {
    if (view.getUint32(i, true) === 0x06054b50) {
      end = i;
      break;
    }
  }
  if (end < 0) throw new Error("not a zip archive");
  const count = view.getUint16(end + 10, true);
  let offset = view.getUint32(end + 16, true);
  const entries = new Map<string, { method: number; size: number; local: number }>();
  const decoder = new TextDecoder();
  for (let n = 0; n < count; n++) {
    if (view.getUint32(offset, true) !== 0x02014b50) throw new Error("a broken zip directory");
    const method = view.getUint16(offset + 10, true);
    const size = view.getUint32(offset + 20, true);
    const nameLength = view.getUint16(offset + 28, true);
    const extra = view.getUint16(offset + 30, true);
    const comment = view.getUint16(offset + 32, true);
    const local = view.getUint32(offset + 42, true);
    const name = decoder.decode(bytes.subarray(offset + 46, offset + 46 + nameLength));
    entries.set(name, { method, size, local });
    offset += 46 + nameLength + extra + comment;
  }
  return {
    names: () => [...entries.keys()],
    text: async (name) => {
      const entry = entries.get(name);
      if (entry === undefined) throw new Error(`the package has no ${name}`);
      if (entry.size > MAX_ENTRY_BYTES) throw new Error("a file in the package is too large");
      const at = entry.local;
      if (view.getUint32(at, true) !== 0x04034b50) throw new Error("a broken zip entry");
      const start = at + 30 + view.getUint16(at + 26, true) + view.getUint16(at + 28, true);
      const raw = bytes.subarray(start, start + entry.size);
      if (entry.method === 0) return decoder.decode(raw);
      if (entry.method === 8) return decoder.decode(await inflate(raw));
      throw new Error("an unsupported zip compression");
    },
  };
}

function joinPath(base: string, relative: string): string {
  const parts = base.split("/").slice(0, -1);
  for (const piece of relative.split("/")) {
    if (piece === "" || piece === ".") continue;
    if (piece === "..") parts.pop();
    else parts.push(piece);
  }
  return parts.join("/");
}

/** The colour themes an extension package contributes, converted (the first one when `pick` is absent). */
export async function themesFromVsix(bytes: Uint8Array): Promise<ConvertedTheme[]> {
  const vsix = readVsix(bytes);
  const manifestName = "extension/package.json";
  const manifest = parseJsonc(await vsix.text(manifestName)) as {
    contributes?: { themes?: { label?: string; path?: string; uiTheme?: string }[] };
  };
  const listed = manifest.contributes?.themes ?? [];
  const out: ConvertedTheme[] = [];
  for (const entry of listed) {
    if (typeof entry.path !== "string") continue;
    const file = joinPath(manifestName, entry.path);
    const raw = parseJsonc(await vsix.text(file)) as VscodeTheme;
    const type = entry.uiTheme === "vs" || entry.uiTheme === "hc-light" ? "light" : "dark";
    out.push(convertVscodeTheme({ type, ...raw, name: entry.label ?? raw.name ?? "" }));
  }
  if (out.length === 0) throw new Error("this extension contributes no colour theme");
  return out;
}

export function base64Bytes(text: string): Uint8Array {
  const binary = atob(text);
  const out = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) out[i] = binary.charCodeAt(i);
  return out;
}
