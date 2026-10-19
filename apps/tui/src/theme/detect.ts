// Light or dark? Ordered signals, first that decides wins (research 05 §3.21):
// INDUS_TUI_LIGHT → INDUS_TUI_THEME (light/dark/hex) → INDUS_TUI_BACKGROUND hex → COLORFGBG last
// field (authoritative when set) → TERM_PROGRAM light-default list → OSC 11 probe → dark.

import { luminance, normaliseHex } from "./palette.ts";

export type Env = Readonly<Record<string, string | undefined>>;

export type LightSource =
  | "INDUS_TUI_LIGHT"
  | "INDUS_TUI_THEME"
  | "INDUS_TUI_BACKGROUND"
  | "COLORFGBG"
  | "TERM_PROGRAM"
  | "osc11"
  | "default";

export interface LightDecision {
  light: boolean;
  source: LightSource;
  /** The background hex when a signal named one. */
  background: string | null;
}

const YES = new Set(["1", "true", "yes", "on"]);
const NO = new Set(["0", "false", "no", "off"]);

/** Terminals whose stock profile is light and which may not set COLORFGBG. */
export const LIGHT_DEFAULT_PROGRAMS: ReadonlySet<string> = new Set(["Apple_Terminal"]);

/** A background at or above this luminance counts as light. */
export const LIGHT_LUMINANCE = 0.5;

function fromBackground(hex: string, source: LightSource): LightDecision {
  return { light: luminance(hex) >= LIGHT_LUMINANCE, source, background: hex };
}

/** The environment's answer, or null when no env signal decides (then probe the terminal). */
export function detectFromEnv(
  env: Env,
  lightPrograms: ReadonlySet<string> = LIGHT_DEFAULT_PROGRAMS,
): LightDecision | null {
  const flag = (env.INDUS_TUI_LIGHT ?? "").trim().toLowerCase();
  if (YES.has(flag)) return { light: true, source: "INDUS_TUI_LIGHT", background: null };
  if (NO.has(flag)) return { light: false, source: "INDUS_TUI_LIGHT", background: null };

  const named = (env.INDUS_TUI_THEME ?? "").trim().toLowerCase();
  if (named === "light") return { light: true, source: "INDUS_TUI_THEME", background: null };
  if (named === "dark") return { light: false, source: "INDUS_TUI_THEME", background: null };
  const namedHex = named === "" ? null : normaliseHex(named);
  if (namedHex !== null) return fromBackground(namedHex, "INDUS_TUI_THEME");

  const hint = normaliseHex(env.INDUS_TUI_BACKGROUND ?? "");
  if (hint !== null) return fromBackground(hint, "INDUS_TUI_BACKGROUND");

  const fgbg = (env.COLORFGBG ?? "").trim();
  if (fgbg !== "") {
    const last = fgbg.split(";").at(-1) ?? "";
    if (/^\d+$/.test(last)) {
      const slot = Number(last);
      if (slot === 7 || slot === 15) return { light: true, source: "COLORFGBG", background: null };
      if (slot < 16) return { light: false, source: "COLORFGBG", background: null };
    }
  }

  const program = (env.TERM_PROGRAM ?? "").trim();
  if (lightPrograms.has(program)) return { light: true, source: "TERM_PROGRAM", background: null };
  return null;
}

// biome-ignore lint/suspicious/noControlCharactersInRegex: terminal replies are escape sequences
const OSC_PREFIX = /^\u001b\]\d+;/;
// biome-ignore lint/suspicious/noControlCharactersInRegex: terminal replies are escape sequences
const OSC_SUFFIX = /(\u0007|\u001b\\)$/;

/**
 * Parse an OSC 10/11 colour answer (`ESC ] 11 ; rgb:RRRR/GGGG/BBBB` + BEL or ST, or the bare
 * payload) into `#rrggbb`. Channels may have 1–4 hex digits; an alpha channel is ignored.
 */
export function parseOscColour(reply: string): string | null {
  const body = reply.replace(OSC_PREFIX, "").replace(OSC_SUFFIX, "").trim().toLowerCase();
  const colon = body.indexOf(":");
  const space = colon < 0 ? "" : body.slice(0, colon);
  if (space === "rgb" || space === "rgba") {
    const channels = body.slice(colon + 1).split("/");
    const wanted = space === "rgba" ? 4 : 3;
    if (channels.length !== wanted && !(space === "rgb" && channels.length === 4)) return null;
    const out: string[] = [];
    for (const digits of channels.slice(0, 3)) {
      if (!/^[0-9a-f]{1,4}$/.test(digits)) return null;
      const level = Number.parseInt(digits, 16) / (16 ** digits.length - 1);
      out.push(
        Math.round(level * 255)
          .toString(16)
          .padStart(2, "0"),
      );
    }
    return `#${out.join("")}`;
  }
  const twelve = /^#([0-9a-f]{12})$/.exec(body);
  if (twelve?.[1] !== undefined) {
    const d = twelve[1];
    return `#${d.slice(0, 2)}${d.slice(4, 6)}${d.slice(8, 10)}`;
  }
  return normaliseHex(body);
}

/** Pure black or white answers come from terminals reporting an unset default; distrust them. */
export function trustedProbeColour(hex: string | null): string | null {
  if (hex === null) return null;
  return hex === "#000000" || hex === "#ffffff" ? null : hex;
}

export interface ProbeStreams {
  input: {
    on(event: "data", listener: (chunk: Buffer | string) => void): unknown;
    off(event: "data", listener: (chunk: Buffer | string) => void): unknown;
  };
  output: { write(text: string): unknown };
}

export const OSC11_QUERY = "\u001b]11;?\u0007";
export const PROBE_TIMEOUT_MS = 150;

let probeCache: { answer: string | null } | null = null;

/** Forget the cached probe answer (tests). */
export function clearProbeCache(): void {
  probeCache = null;
}

/**
 * Ask the terminal for its background once; later calls get the cached answer. Resolves to a
 * trusted `#rrggbb` or null (no answer within the timeout, or an answer we distrust).
 */
export function probeBackground(
  streams: ProbeStreams,
  timeoutMs: number = PROBE_TIMEOUT_MS,
): Promise<string | null> {
  if (probeCache !== null) return Promise.resolve(probeCache.answer);
  return new Promise((resolve) => {
    let seen = "";
    const finish = (answer: string | null): void => {
      clearTimeout(timer);
      streams.input.off("data", onData);
      probeCache = { answer };
      resolve(answer);
    };
    const onData = (chunk: Buffer | string): void => {
      seen += String(chunk);
      // biome-ignore lint/suspicious/noControlCharactersInRegex: terminal replies are escape sequences
      const match = /\u001b\]11;[^\u0007\u001b]*(?:\u0007|\u001b\\)/.exec(seen);
      if (match !== null) finish(trustedProbeColour(parseOscColour(match[0])));
    };
    const timer = setTimeout(() => finish(null), timeoutMs);
    streams.input.on("data", onData);
    streams.output.write(OSC11_QUERY);
  });
}

/** Env first; when it does not decide, the probe's answer; dark otherwise. */
export async function resolveLightMode(
  env: Env,
  probe?: () => Promise<string | null>,
): Promise<LightDecision> {
  const fromEnv = detectFromEnv(env);
  if (fromEnv !== null) return fromEnv;
  const answer = probe === undefined ? null : await probe();
  if (answer !== null) return fromBackground(answer, "osc11");
  return { light: false, source: "default", background: null };
}

/** Truecolor opt-in: must run before any colour library reads the environment. */
export function applyTruecolorOptIn(env: Record<string, string | undefined>): boolean {
  const flag = (env.INDUS_TUI_TRUECOLOR ?? "").trim().toLowerCase();
  if (!YES.has(flag)) return false;
  env.FORCE_COLOR = "3";
  env.COLORTERM = "truecolor";
  return true;
}
