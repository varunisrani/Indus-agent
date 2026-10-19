// Clipboard text for Ctrl+V / Alt+V and for cutting a selection (P12-17).
// Reading: in an SSH session ask the terminal (OSC 52) first, since the native tools would read
// the remote machine's clipboard; locally use the native tool first. Each falls back to the other.
// Writing: native tools first, OSC 52 as the fallback.

import { execFile, spawn } from "node:child_process";

export const OSC52_TIMEOUT_MS = 500;
const READ_LIMIT = 4 * 1024 * 1024;
const ESC = "\u001b";
const BEL = "\u0007";

export function isRemoteShell(env: NodeJS.ProcessEnv = process.env): boolean {
  return Boolean(env.SSH_CONNECTION || env.SSH_CLIENT || env.SSH_TTY);
}

/** Text worth inserting: not blank, no NUL, at most a sprinkle of control characters. */
export function usableText(text: string | null | undefined): text is string {
  if (!text || text.trim() === "" || text.includes("\u0000")) return false;
  let odd = 0;
  for (const ch of text) {
    const code = ch.codePointAt(0) ?? 0;
    if ((code < 0x20 && ch !== "\n" && ch !== "\r" && ch !== "\t") || ch === "�") odd += 1;
  }
  return odd <= Math.max(2, Math.floor(text.length / 50));
}

export interface ClipCommand {
  readonly cmd: string;
  readonly args: readonly string[];
  /** Output is base64 (PowerShell, to dodge code-page mangling). */
  readonly base64?: boolean;
  /** Writing: the text travels base64 inside the arguments instead of on stdin. */
  readonly inArgs?: (encoded: string) => readonly string[];
}

/** PowerShell decodes piped stdin with the legacy code page, so the text goes base64 in argv. */
function psWrite(cmd: string): ClipCommand {
  return {
    cmd,
    args: [],
    inArgs: (encoded) => [
      "-NoLogo",
      "-NoProfile",
      "-Command",
      `Set-Clipboard -Value ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('${encoded}')))`,
    ],
  };
}

const PS_READ = [
  "-NoLogo",
  "-NoProfile",
  "-Command",
  "$t = Get-Clipboard -Raw; if ($t) { [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($t)) }",
];

function isWsl(env: NodeJS.ProcessEnv): boolean {
  return Boolean(env.WSL_DISTRO_NAME || env.WSL_INTEROP);
}

export function readCommands(platform: NodeJS.Platform, env: NodeJS.ProcessEnv): ClipCommand[] {
  if (platform === "darwin") return [{ cmd: "pbpaste", args: [] }];
  if (platform === "win32") return [{ cmd: "powershell", args: PS_READ, base64: true }];
  const list: ClipCommand[] = [];
  if (isWsl(env)) list.push({ cmd: "powershell.exe", args: PS_READ, base64: true });
  if (env.WAYLAND_DISPLAY) list.push({ cmd: "wl-paste", args: ["--no-newline"] });
  list.push({ cmd: "xclip", args: ["-selection", "clipboard", "-o"] });
  list.push({ cmd: "xsel", args: ["--clipboard", "--output"] });
  return list;
}

export function writeCommands(platform: NodeJS.Platform, env: NodeJS.ProcessEnv): ClipCommand[] {
  if (platform === "darwin") return [{ cmd: "pbcopy", args: [] }];
  if (platform === "win32") return [psWrite("powershell")];
  const list: ClipCommand[] = [];
  if (isWsl(env)) list.push(psWrite("powershell.exe"));
  if (env.WAYLAND_DISPLAY) list.push({ cmd: "wl-copy", args: [] });
  list.push({ cmd: "xclip", args: ["-selection", "clipboard", "-i"] });
  list.push({ cmd: "xsel", args: ["--clipboard", "--input"] });
  return list;
}

/** Runs a read command; rejects when it cannot run or fails. */
export type ReadRunner = (cmd: string, args: readonly string[]) => Promise<string>;
/** Runs a write command with `input` on stdin; resolves true on exit 0. */
export type WriteRunner = (cmd: string, args: readonly string[], input: string) => Promise<boolean>;

const runRead: ReadRunner = (cmd, args) =>
  new Promise((resolve, reject) => {
    execFile(
      cmd,
      [...args],
      { encoding: "utf8", maxBuffer: READ_LIMIT, windowsHide: true },
      (error, stdout) => {
        if (error) reject(error);
        else resolve(stdout);
      },
    );
  });

const runWrite: WriteRunner = (cmd, args, input) =>
  new Promise((resolve) => {
    try {
      const child = spawn(cmd, [...args], {
        stdio: ["pipe", "ignore", "ignore"],
        windowsHide: true,
      });
      child.once("error", () => resolve(false));
      child.once("close", (code) => resolve(code === 0));
      child.stdin?.end(input);
    } catch {
      resolve(false);
    }
  });

export async function readNative(
  platform: NodeJS.Platform,
  env: NodeJS.ProcessEnv,
  run: ReadRunner = runRead,
): Promise<string | null> {
  for (const entry of readCommands(platform, env)) {
    try {
      const out = await run(entry.cmd, entry.args);
      return entry.base64 ? Buffer.from(out.trim(), "base64").toString("utf8") : out;
    } catch {
      // next tool
    }
  }
  return null;
}

/** tmux and screen pass OSC sequences through only inside their own DCS wrapper. */
export function wrapForMultiplexer(sequence: string, env: NodeJS.ProcessEnv = process.env): string {
  if (env.TMUX) return `${ESC}Ptmux;${sequence.split(ESC).join(ESC + ESC)}${ESC}\\`;
  if (env.STY) return `${ESC}P${sequence}${ESC}\\`;
  return sequence;
}

export function osc52ReadRequest(env: NodeJS.ProcessEnv = process.env): string {
  return wrapForMultiplexer(`${ESC}]52;c;?${BEL}`, env);
}

export function osc52WriteSequence(text: string, env: NodeJS.ProcessEnv = process.env): string {
  return wrapForMultiplexer(
    `${ESC}]52;c;${Buffer.from(text, "utf8").toString("base64")}${BEL}`,
    env,
  );
}

/**
 * Decodes the body of a terminal's OSC 52 answer (`c;<base64>`, also accepting the full
 * `ESC ] 52 ; …` form with either terminator). Null when it carries no data.
 */
export function parseOsc52Reply(reply: string): string | null {
  let body = reply;
  const head = body.indexOf("]52;");
  if (head >= 0) body = body.slice(head + 4);
  if (body.endsWith(BEL)) body = body.slice(0, -1);
  else if (body.endsWith(`${ESC}\\`)) body = body.slice(0, -2);
  const split = body.indexOf(";");
  if (split < 0) return null;
  const target = body.slice(0, split);
  const data = body.slice(split + 1);
  if (!/^[cps0-7]*$/.test(target) || data === "" || data === "?") return null;
  if (!/^[A-Za-z0-9+/=\s]+$/.test(data)) return null;
  return Buffer.from(data, "base64").toString("utf8");
}

/** Sends `request` to the terminal and resolves with its answer, or null after the timeout. */
export type TerminalQuery = (request: string, timeoutMs: number) => Promise<string | null>;

export async function readOsc52(
  query: TerminalQuery | undefined,
  env: NodeJS.ProcessEnv,
): Promise<string | null> {
  if (query === undefined) return null;
  try {
    const reply = await query(osc52ReadRequest(env), OSC52_TIMEOUT_MS);
    return reply === null ? null : parseOsc52Reply(reply);
  } catch {
    return null;
  }
}

export interface ClipboardOptions {
  env?: NodeJS.ProcessEnv;
  platform?: NodeJS.Platform;
  read?: ReadRunner;
  write?: WriteRunner;
  query?: TerminalQuery;
  /** Writes a raw sequence to the terminal (for the OSC 52 write fallback). */
  emit?: (sequence: string) => void;
}

/** Clipboard text in the order that fits the session, or null when neither source has any. */
export async function readClipboard(options: ClipboardOptions = {}): Promise<string | null> {
  const env = options.env ?? process.env;
  const platform = options.platform ?? process.platform;
  const native = (): Promise<string | null> => readNative(platform, env, options.read);
  const terminal = (): Promise<string | null> => readOsc52(options.query, env);
  const order = isRemoteShell(env) ? [terminal, native] : [native, terminal];
  for (const source of order) {
    const text = await source();
    if (usableText(text)) return text;
  }
  return null;
}

/** Copies text; true when a native tool took it or the OSC 52 sequence could be sent. */
export async function writeClipboard(
  text: string,
  options: ClipboardOptions = {},
): Promise<boolean> {
  const env = options.env ?? process.env;
  const platform = options.platform ?? process.platform;
  const write = options.write ?? runWrite;
  for (const entry of writeCommands(platform, env)) {
    const ok = entry.inArgs
      ? await write(entry.cmd, entry.inArgs(Buffer.from(text, "utf8").toString("base64")), "")
      : await write(entry.cmd, entry.args, text);
    if (ok) return true;
  }
  if (options.emit === undefined) return false;
  try {
    options.emit(osc52WriteSequence(text, env));
    return true;
  } catch {
    return false;
  }
}
