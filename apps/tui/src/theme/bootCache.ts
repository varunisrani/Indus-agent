// The last resolved theme, kept on disk so the next start paints its first frame in the right
// colours before detection or the bridge answer. Read synchronously before the first render.

import { mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { detectFromEnv, type Env } from "./detect.ts";
import { normaliseHex } from "./palette.ts";
import { buildTheme, type Theme, themeStore } from "./theme.ts";

const VERSION = 1;

export function bootCachePath(env: Env): string {
  const home = env.INDUS_HOME?.trim() || join(homedir(), ".indus");
  return join(home, "tui", "theme-boot.json");
}

/** The cached theme, or null when the file is missing, unreadable or not ours. */
export function readBootTheme(path: string): Theme | null {
  let raw: unknown;
  try {
    raw = JSON.parse(readFileSync(path, "utf8"));
  } catch {
    return null;
  }
  if (typeof raw !== "object" || raw === null) return null;
  const record = raw as Record<string, unknown>;
  if (record.version !== VERSION) return null;
  const theme = record.theme as Record<string, unknown> | undefined;
  if (typeof theme !== "object" || theme === null) return null;
  const { name, light, background, color, brand } = theme;
  if (typeof name !== "string" || typeof light !== "boolean" || typeof background !== "string") {
    return null;
  }
  if (normaliseHex(background) === null) return null;
  if (!isStringRecord(color) || !isStringRecord(brand)) return null;
  const base = buildTheme({ light, background });
  return {
    name,
    light,
    background,
    color: { ...base.color, ...color } as Theme["color"],
    brand: { ...base.brand, ...brand },
  };
}

function isStringRecord(value: unknown): value is Record<string, string> {
  return (
    typeof value === "object" &&
    value !== null &&
    Object.values(value).every((v) => typeof v === "string")
  );
}

/** Save `theme` atomically (temp file + rename); failures are ignored (cache only). */
export function writeBootTheme(path: string, theme: Theme): boolean {
  try {
    mkdirSync(dirname(path), { recursive: true });
    const temp = `${path}.${process.pid}.tmp`;
    writeFileSync(temp, `${JSON.stringify({ version: VERSION, theme })}\n`, { mode: 0o600 });
    renameSync(temp, path);
    return true;
  } catch {
    return false;
  }
}

/**
 * The theme for the first frame: the cached one when present, else the env decision (no probe —
 * the probe is asynchronous and runs after the first paint). Also seeds `themeStore`.
 */
export function bootTheme(env: Env, path: string = bootCachePath(env)): Theme {
  const decision = detectFromEnv(env);
  const stored = readBootTheme(path);
  // An env signal that disagrees with the cached polarity wins: the user changed something.
  const cached =
    stored !== null && decision !== null && decision.light !== stored.light ? null : stored;
  const theme =
    cached ??
    buildTheme({ light: decision?.light ?? false, background: decision?.background ?? null });
  themeStore.setState({
    theme,
    preview: null,
    detected: { light: decision?.light ?? theme.light, background: decision?.background ?? null },
  });
  return theme;
}
