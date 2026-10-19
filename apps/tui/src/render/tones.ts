/**
 * Colour lookup for the renderer. The theme's colour table is keyed by semantic names; each role
 * here tries a few names in order and ends on a plain terminal colour, so the renderer works with
 * any palette the theme module provides.
 */
import type { Theme } from "../theme/theme.ts";

const ROLES = {
  text: [["fg", "text"], undefined],
  muted: [["fg-muted", "muted"], "gray"],
  accent: [["accent"], "cyan"],
  border: [["border"], "gray"],
  ok: [["success", "ok"], "green"],
  warn: [["warning", "warn"], "yellow"],
  error: [["danger", "error"], "red"],
  code: [["code", "code-fg", "accent"], "cyan"],
  keyword: [["syntax-keyword", "syntaxKeyword", "accent"], "magenta"],
  string: [["syntax-string", "syntaxString", "success", "ok"], "green"],
  comment: [["syntax-comment", "syntaxComment", "fg-muted", "muted"], "gray"],
  number: [["syntax-number", "syntaxNumber", "warning", "warn"], "yellow"],
  added: [["diff-added", "diffAdded", "success", "ok"], "green"],
  removed: [["diff-removed", "diffRemoved", "danger", "error"], "red"],
} as const satisfies Record<string, readonly [readonly string[], string | undefined]>;

export type ToneName = keyof typeof ROLES;

export function tone(theme: Theme, name: ToneName): string | undefined {
  const [keys, fallback] = ROLES[name];
  for (const key of keys) {
    const value = theme.color[key];
    if (value) return value;
  }
  return fallback;
}

/** Spreads `color` only when a tone resolves (exact optional props forbid `undefined`). */
export function colour(theme: Theme, name: ToneName): { color?: string } {
  const value = tone(theme, name);
  return value ? { color: value } : {};
}

const themeIds = new WeakMap<Theme, number>();
let nextThemeId = 1;

/** A stable small number per theme object, used in cache keys. */
export function themeId(theme: Theme): number {
  let id = themeIds.get(theme);
  if (id === undefined) {
    id = nextThemeId++;
    themeIds.set(theme, id);
  }
  return id;
}
