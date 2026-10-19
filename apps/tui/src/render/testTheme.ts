/** Test helper: the real default theme with some colours overridden. */
import { currentTheme, type Theme } from "../theme/theme.ts";

export function testTheme(color: Readonly<Record<string, string>> = {}): Theme {
  const base = currentTheme();
  return { ...base, color: { ...base.color, ...color } };
}
