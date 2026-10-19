// The colours cards use, resolved from the theme with a fallback for every name the theme lacks.

import { type Theme, useTheme } from "../theme/theme.ts";

export interface Tones {
  text: string;
  muted: string;
  accent: string;
  warn: string;
  error: string;
  ok: string;
  border: string;
}

export function tonesOf(theme: Theme): Tones {
  const c = theme.color;
  const accent = c.accent ?? c.primary ?? theme.brand.accent;
  return {
    text: c.text ?? c.fg ?? "white",
    muted: c.muted ?? c.dim ?? "gray",
    accent,
    warn: c.warn ?? c.warning ?? accent,
    error: c.error ?? c.danger ?? "red",
    ok: c.ok ?? c.success ?? "green",
    border: c.border ?? c.muted ?? "gray",
  };
}

export const useTones = (): Tones => tonesOf(useTheme());
