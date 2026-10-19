// What the terminal can do, decided from the environment: synchronized output (DEC 2026), extended
// key reporting (kitty keyboard, xterm modifyOtherKeys) and which mouse reporting to ask for.

import { seq } from "./modes.ts";

export type Env = Readonly<Record<string, string | undefined>>;
export type MousePreset = "off" | "wheel" | "buttons" | "all";

export interface Capabilities {
  /** Wrap frames in DEC 2026 begin/end. */
  syncOutput: boolean;
  /** Push the kitty keyboard protocol (flag 1: disambiguate). */
  kitty: boolean;
  /** Turn on xterm modifyOtherKeys level 2. */
  otherKeys: boolean;
  mouse: MousePreset;
  /** `tmux`, `zellij`, `screen`, or null. */
  multiplexer: string | null;
}

/** Multiplexers forward 2026 markers but re-chunk the stream, so frames tear anyway (R05.17). */
export function multiplexer(env: Env): string | null {
  if (env.TMUX) return "tmux";
  if (env.ZELLIJ !== undefined && env.ZELLIJ !== "") return "zellij";
  if (env.STY) return "screen";
  return null;
}

/** DEC 2026 only on known terminals: kitty, iTerm2, WezTerm, Ghostty, Zed, VS Code, Windows Terminal. */
export function syncOutputSupported(env: Env): boolean {
  if (multiplexer(env) !== null) return false;
  const program = env.TERM_PROGRAM ?? "";
  const term = env.TERM ?? "";
  if (["iTerm.app", "WezTerm", "ghostty", "vscode", "zed"].includes(program)) return true;
  if (term.includes("kitty") || env.KITTY_WINDOW_ID) return true;
  if (term === "xterm-ghostty") return true;
  if (env.ZED_TERM) return true;
  return Boolean(env.WT_SESSION);
}

/** Which terminal family this is, for the extended-keys list. */
export function terminalFamily(env: Env): string | null {
  if (env.TMUX) return "tmux";
  const program = env.TERM_PROGRAM ?? "";
  if (program === "iTerm.app") return "iterm2";
  if (program === "WezTerm") return "wezterm";
  if (program === "ghostty" || env.TERM === "xterm-ghostty") return "ghostty";
  if (program === "vscode") return "vscode";
  if ((env.TERM ?? "").includes("kitty") || env.KITTY_WINDOW_ID) return "kitty";
  if (env.WT_SESSION) return "windows-terminal";
  return null;
}

/**
 * Extended keys per terminal. Unknown terminals get neither (some print unknown CSI replies as
 * text). Ghostty gets modifyOtherKeys only: its kitty mode drops Alt from Backspace. tmux takes
 * modifyOtherKeys but does not pass the kitty push to the outer terminal.
 */
export function extendedKeys(env: Env): { kitty: boolean; otherKeys: boolean } {
  switch (terminalFamily(env)) {
    case "ghostty":
    case "tmux":
      return { kitty: false, otherKeys: true };
    case "iterm2":
    case "wezterm":
    case "vscode":
    case "kitty":
    case "windows-terminal":
      return { kitty: true, otherKeys: true };
    default:
      return { kitty: false, otherKeys: false };
  }
}

const PRESETS: readonly MousePreset[] = ["off", "wheel", "buttons", "all"];

/** `INDUS_TUI_MOUSE` forces a preset (also 0/1/on/off); otherwise the configured one, default `all`. */
export function mousePreset(env: Env, configured?: string): MousePreset {
  const forced = (env.INDUS_TUI_MOUSE ?? "").trim().toLowerCase();
  if ((PRESETS as readonly string[]).includes(forced)) return forced as MousePreset;
  if (["0", "false", "no"].includes(forced)) return "off";
  if (["1", "true", "yes", "on"].includes(forced)) return "all";
  const chosen = (configured ?? "").trim().toLowerCase();
  return (PRESETS as readonly string[]).includes(chosen) ? (chosen as MousePreset) : "all";
}

const MOUSE_SETS: Readonly<Record<MousePreset, readonly number[]>> = {
  off: [],
  wheel: [1000, 1006],
  buttons: [1000, 1002, 1006],
  all: [1000, 1002, 1003, 1006],
};

/** The DEC modes a preset turns on. */
export function mouseModes(preset: MousePreset): readonly number[] {
  return MOUSE_SETS[preset];
}

export function mouseOn(preset: MousePreset): string {
  return mouseModes(preset)
    .map((mode) => `\u001b[?${mode}h`)
    .join("");
}

export function mouseOff(preset: MousePreset): string {
  return [...mouseModes(preset)]
    .reverse()
    .map((mode) => `\u001b[?${mode}l`)
    .join("");
}

export function detectCapabilities(env: Env, configuredMouse?: string): Capabilities {
  return {
    syncOutput: syncOutputSupported(env),
    ...extendedKeys(env),
    mouse: mousePreset(env, configuredMouse),
    multiplexer: multiplexer(env),
  };
}

/** Turn on what the capabilities allow. The kitty stack is popped first so it never grows. */
export function keyModesOn(caps: Pick<Capabilities, "kitty" | "otherKeys">): string {
  return (
    (caps.kitty ? seq.kittyPop + seq.kittyPush(1) : "") + (caps.otherKeys ? seq.otherKeysOn : "")
  );
}

export function keyModesOff(caps: Pick<Capabilities, "kitty" | "otherKeys">): string {
  return (caps.kitty ? seq.kittyPop : "") + (caps.otherKeys ? seq.otherKeysOff : "");
}

/**
 * Bytes around an external editor: everything the editor must not see goes off before it starts
 * and comes back after it exits.
 */
export function editorHandoff(caps: Capabilities): { before: string; after: string } {
  return {
    before:
      keyModesOff(caps) + mouseOff(caps.mouse) + seq.pasteOff + seq.altScreenOff + seq.cursorShow,
    after: seq.altScreenOn + seq.pasteOn + mouseOn(caps.mouse) + keyModesOn(caps),
  };
}
