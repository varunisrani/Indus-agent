// Spike (P12-01, throwaway): escape sequences and the terminal tables the spike needs.
// Written from terminal documentation; moves to apps/tui/src/terminal/ in P12-07/P12-08.

const CSI = "\u001b[";

export const seq = {
  altScreenOn: `${CSI}?1049h`,
  altScreenOff: `${CSI}?1049l`,
  syncBegin: `${CSI}?2026h`,
  syncEnd: `${CSI}?2026l`,
  kittyPush: (flags: number): string => `${CSI}>${flags}u`,
  kittyPop: `${CSI}<u`,
  otherKeysOn: `${CSI}>4;2m`,
  otherKeysOff: `${CSI}>4m`,
  pasteOn: `${CSI}?2004h`,
  pasteOff: `${CSI}?2004l`,
  // 1000 press/release, 1002 drag motion, 1006 SGR coordinates.
  mouseOn: `${CSI}?1000h${CSI}?1002h${CSI}?1006h`,
  mouseOff: `${CSI}?1006l${CSI}?1003l${CSI}?1002l${CSI}?1000l`,
  focusOff: `${CSI}?1004l`,
  sgrReset: `${CSI}0m`,
  cursorShow: `${CSI}?25h`,
} as const;

/** Everything we might have switched on, switched off again; safe to send twice. */
export function fullReset(): string {
  return (
    seq.mouseOff +
    seq.focusOff +
    seq.pasteOff +
    seq.kittyPop +
    seq.otherKeysOff +
    seq.sgrReset +
    seq.altScreenOff +
    seq.cursorShow
  );
}

type Env = Record<string, string | undefined>;

/** Multiplexers re-chunk the byte stream, so frame markers buy nothing there. */
export function insideMultiplexer(env: Env): boolean {
  return Boolean(env.TMUX || env.ZELLIJ || env.STY);
}

const SYNC_PROGRAMS = new Set(["WezTerm", "iTerm.app", "ghostty", "vscode", "kitty", "Alacritty"]);

/** True only where DEC 2026 is known to work; never inside a multiplexer (R05.17). */
export function syncOutputWanted(env: Env): boolean {
  if (insideMultiplexer(env)) return false;
  if (env.TERM_PROGRAM && SYNC_PROGRAMS.has(env.TERM_PROGRAM)) return true;
  const term = env.TERM ?? "";
  if (term.includes("kitty") || term.includes("ghostty") || term.startsWith("foot")) return true;
  if (env.KITTY_WINDOW_ID || env.WT_SESSION) return true;
  const vte = Number.parseInt(env.VTE_VERSION ?? "", 10);
  return Number.isFinite(vte) && vte >= 6800;
}

export interface KeyPlan {
  kitty: boolean;
  otherKeys: boolean;
}

/** Which extended-key modes to ask for. Unknown terminals get neither. */
export function extendedKeyPlan(env: Env): KeyPlan {
  if (env.TMUX) return { kitty: false, otherKeys: true };
  const program = env.TERM_PROGRAM ?? "";
  if (program === "ghostty") return { kitty: false, otherKeys: true };
  const known =
    ["WezTerm", "iTerm.app", "vscode"].includes(program) ||
    (env.TERM ?? "").includes("kitty") ||
    Boolean(env.KITTY_WINDOW_ID || env.WT_SESSION);
  return { kitty: known, otherKeys: known };
}
