import { durations, easings } from "./motion.ts";
import { semanticNames, type ThemeName, themes } from "./semantic.ts";
import { radii, shadows, space } from "./space.ts";
import { fontStacks, fontWeights, typeScale } from "./type.ts";

export const cssPrefix = "--indus-";

type Declaration = readonly [name: string, value: string];

function block(selector: string, declarations: readonly Declaration[], indent = ""): string {
  const body = declarations.map(([name, value]) => `${indent}  ${cssPrefix}${name}: ${value};`);
  return [`${indent}${selector} {`, ...body, `${indent}}`].join("\n");
}

function themeDeclarations(theme: ThemeName): {
  base: Declaration[];
  modern: Declaration[];
} {
  const colours = themes[theme];
  const base: Declaration[] = [];
  const modern: Declaration[] = [];
  for (const name of semanticNames) {
    base.push([`color-${name}`, colours[name].hex]);
    modern.push([`color-${name}`, colours[name].css]);
  }
  for (const [name, stack] of Object.entries(fontStacks)) base.push([`font-${name}`, stack]);
  for (const [name, weight] of Object.entries(fontWeights)) {
    base.push([`font-weight-${name}`, String(weight)]);
  }
  for (const [name, step] of Object.entries(typeScale)) {
    base.push([`text-${name}-size`, `${step.size}px`]);
    base.push([`text-${name}-line-height`, `${step.lineHeight}px`]);
  }
  space.forEach((px, n) => {
    base.push([`space-${n}`, `${px}px`]);
  });
  for (const [name, value] of Object.entries(radii)) base.push([`radius-${name}`, value]);
  for (const [name, value] of Object.entries(shadows[theme])) base.push([`shadow-${name}`, value]);
  for (const [name, ms] of Object.entries(durations)) base.push([`duration-${name}`, `${ms}ms`]);
  for (const [name, value] of Object.entries(easings)) base.push([`easing-${name}`, value]);
  return { base, modern };
}

/**
 * CSS custom properties (`--indus-*`) for one theme, scoped to `:root[data-theme="<theme>"]`.
 * Colours are written as hex first, then upgraded to OKLCH where the browser supports it.
 * Durations drop to 0 ms under `prefers-reduced-motion: reduce`.
 */
export function tokensToCss(theme: ThemeName): string {
  if (!(theme in themes)) throw new Error(`unknown theme: ${String(theme)}`);
  const selector = `:root[data-theme="${theme}"]`;
  const { base, modern } = themeDeclarations(theme);
  const reduced: Declaration[] = Object.keys(durations).map((name) => [`duration-${name}`, "0ms"]);
  return [
    block(selector, base),
    "",
    "@supports (color: oklch(0% 0 0)) {",
    block(selector, modern, "  "),
    "}",
    "",
    "@media (prefers-reduced-motion: reduce) {",
    block(selector, reduced, "  "),
    "}",
    "",
  ].join("\n");
}
