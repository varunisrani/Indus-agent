import type { ThemeName } from "./semantic.ts";

/** Spacing steps 0..16 on a 4 px grid: `space[n]` is `n * 4` px. */
export const space: readonly number[] = Array.from({ length: 17 }, (_, n) => n * 4);

export const radii = {
  none: "0px",
  sm: "4px",
  md: "6px",
  lg: "10px",
  xl: "16px",
  full: "9999px",
} as const;
export type RadiusName = keyof typeof radii;

export type ShadowLevel = "low" | "mid" | "high";

/** Elevation shadows; dark surfaces need deeper, more opaque shadows to read at all. */
export const shadows: Readonly<Record<ThemeName, Readonly<Record<ShadowLevel, string>>>> = {
  "indus-light": {
    low: "0 1px 2px rgb(38 32 24 / 0.08)",
    mid: "0 2px 6px rgb(38 32 24 / 0.10), 0 1px 2px rgb(38 32 24 / 0.06)",
    high: "0 10px 28px rgb(38 32 24 / 0.14), 0 3px 8px rgb(38 32 24 / 0.08)",
  },
  "indus-dark": {
    low: "0 1px 2px rgb(0 0 0 / 0.40)",
    mid: "0 2px 8px rgb(0 0 0 / 0.45), 0 1px 2px rgb(0 0 0 / 0.30)",
    high: "0 12px 32px rgb(0 0 0 / 0.55), 0 4px 10px rgb(0 0 0 / 0.35)",
  },
};
