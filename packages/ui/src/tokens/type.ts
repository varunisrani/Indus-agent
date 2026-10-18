export const fontStacks = {
  sans: '"Red Hat Text", system-ui, "Segoe UI Variable", "Segoe UI", "Noto Sans", Ubuntu, sans-serif',
  mono: '"Red Hat Mono", ui-monospace, "Cascadia Code", "Noto Sans Mono", "DejaVu Sans Mono", monospace',
} as const;
export type FontStackName = keyof typeof fontStacks;

export const fontWeights = {
  regular: 400,
  medium: 500,
  semibold: 600,
  bold: 700,
} as const;

export interface TypeStep {
  /** Font size in px. */
  readonly size: number;
  /** Line height in px. */
  readonly lineHeight: number;
}

export const typeScale = {
  xs: { size: 12, lineHeight: 16 },
  sm: { size: 13, lineHeight: 18 },
  base: { size: 14, lineHeight: 20 },
  md: { size: 16, lineHeight: 24 },
  lg: { size: 18, lineHeight: 26 },
  xl: { size: 22, lineHeight: 30 },
  "2xl": { size: 28, lineHeight: 36 },
  "3xl": { size: 36, lineHeight: 44 },
} as const satisfies Record<string, TypeStep>;
export type TypeStepName = keyof typeof typeScale;
