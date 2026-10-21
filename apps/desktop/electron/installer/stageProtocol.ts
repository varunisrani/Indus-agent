// The installer scripts' stage protocol as the desktop drives it (P20-09; the scripts are P23's,
// and P23-03 must keep this shape):
//
//   <script> --manifest …           last stdout JSON line:
//                                   {protocol_version: 1, stages: [{name, title, category,
//                                    needs_user_input}]}
//   <script> --stage NAME --json …  last stdout JSON line is the result frame
//                                   {ok, stage, skipped?, reason?}; every other line is log text
//
// Plus the console decoding the runner applies to every output line: LF or CRLF ends a line, a
// carriage-return redraw keeps its last frame, ANSI escapes are dropped, and a line that is not
// valid UTF-8 is read as Windows-1252 (PowerShell 5.1 writes localised errors in the ANSI code page).

export const PROTOCOL_VERSION = 1;

export interface ManifestStage {
  name: string;
  title: string;
  category: string;
  needsUserInput: boolean;
}

export interface StageFrame {
  ok: boolean;
  stage: string;
  skipped: boolean;
  reason: string | null;
}

export interface ScriptFlags {
  home: string;
}

/** Arguments for the manifest run (POSIX spelling; P23's PowerShell script accepts them too). */
export function manifestArgs(flags: ScriptFlags): string[] {
  return ["--manifest", "--non-interactive", "--indus-home", flags.home];
}

/** Arguments for one stage run. */
export function stageArgs(name: string, flags: ScriptFlags): string[] {
  return ["--stage", name, "--json", "--non-interactive", "--indus-home", flags.home];
}

function jsonLines(stdout: string): unknown[] {
  const found: unknown[] = [];
  for (const raw of stdout.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line.startsWith("{")) continue;
    try {
      found.push(JSON.parse(line));
    } catch {
      // log text that happens to start with a brace
    }
  }
  return found;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

/** The manifest from a `--manifest` run's stdout, or an error string. */
export function parseManifest(stdout: string): { stages: ManifestStage[] } | { error: string } {
  const candidates = jsonLines(stdout).filter(
    (value): value is Record<string, unknown> => isRecord(value) && Array.isArray(value.stages),
  );
  const last = candidates.at(-1);
  if (last === undefined) return { error: "the installer printed no manifest" };
  if (last.protocol_version !== PROTOCOL_VERSION) {
    return { error: `the installer speaks protocol ${String(last.protocol_version)}, not 1` };
  }
  const stages: ManifestStage[] = [];
  for (const raw of last.stages as unknown[]) {
    if (
      !isRecord(raw) ||
      typeof raw.name !== "string" ||
      !/^[a-z][a-z0-9_-]{0,63}$/.test(raw.name)
    ) {
      return { error: "the manifest names a stage the desktop cannot run" };
    }
    stages.push({
      name: raw.name,
      title: typeof raw.title === "string" ? raw.title : raw.name,
      category: typeof raw.category === "string" ? raw.category : "",
      needsUserInput: raw.needs_user_input === true,
    });
  }
  if (stages.length === 0) return { error: "the manifest lists no stages" };
  return { stages };
}

/** The result frame of a stage run: the last JSON line shaped like one, or null. */
export function parseStageFrame(stdout: string): StageFrame | null {
  const frames = jsonLines(stdout).filter(
    (value): value is Record<string, unknown> =>
      isRecord(value) && typeof value.ok === "boolean" && typeof value.stage === "string",
  );
  const last = frames.at(-1);
  if (last === undefined) return null;
  return {
    ok: last.ok as boolean,
    stage: last.stage as string,
    skipped: last.skipped === true,
    reason: typeof last.reason === "string" ? last.reason : null,
  };
}

// -- console decoding ----------------------------------------------------------------------------

/** Windows-1252 for 0x80–0x9F (the rest of the high half equals Latin-1); undefined slots keep C1. */
const CP1252_HIGH = [
  0x20ac, 0x81, 0x201a, 0x192, 0x201e, 0x2026, 0x2020, 0x2021, 0x2c6, 0x2030, 0x160, 0x2039, 0x152,
  0x8d, 0x17d, 0x8f, 0x90, 0x2018, 0x2019, 0x201c, 0x201d, 0x2022, 0x2013, 0x2014, 0x2dc, 0x2122,
  0x161, 0x203a, 0x153, 0x9d, 0x17e, 0x178,
];

const strictUtf8 = new TextDecoder("utf-8", { fatal: true });

export function decodeConsoleBytes(bytes: Uint8Array): string {
  try {
    return strictUtf8.decode(bytes);
  } catch {
    let out = "";
    for (const byte of bytes) {
      out += String.fromCodePoint(
        byte >= 0x80 && byte <= 0x9f ? (CP1252_HIGH[byte - 0x80] ?? byte) : byte,
      );
    }
    return out;
  }
}

// CSI, OSC (BEL or ST terminated) and two-byte escapes; built from the two control characters
// so the pattern source stays printable.
const ESC = String.fromCharCode(0x1b);
const BEL = String.fromCharCode(0x07);
const ANSI = new RegExp(
  `${ESC}\\[[0-?]*[ -/]*[@-~]|${ESC}\\][^${BEL}${ESC}]*(?:${BEL}|${ESC}\\\\)|${ESC}[@-Z\\\\-_]`,
  "g",
);

/** One decoded output line made plain: escapes dropped, a `\r` redraw keeps its last frame. */
export function cleanLine(line: string): string {
  const frames = line
    .split("\r")
    .map((frame) => frame.replace(ANSI, ""))
    .filter((frame) => frame.trim() !== "");
  return frames.at(-1)?.trimEnd() ?? "";
}

/** Splits a byte stream into decoded lines (LF ends a line; a trailing CR is part of CRLF). */
export class LineSplitter {
  private pending: Buffer = Buffer.alloc(0);

  /** Feed a chunk; returns the complete lines it finished (raw, before `cleanLine`). */
  push(chunk: Uint8Array): string[] {
    this.pending = Buffer.concat([this.pending, chunk]);
    const lines: string[] = [];
    for (;;) {
      const at = this.pending.indexOf(0x0a);
      if (at < 0) break;
      let end = at;
      if (end > 0 && this.pending[end - 1] === 0x0d) end -= 1;
      lines.push(decodeConsoleBytes(this.pending.subarray(0, end)));
      this.pending = this.pending.subarray(at + 1);
    }
    return lines;
  }

  /** The unterminated tail at end of stream, if any. */
  flush(): string[] {
    if (this.pending.length === 0) return [];
    let bytes = this.pending;
    if (bytes[bytes.length - 1] === 0x0d) bytes = bytes.subarray(0, bytes.length - 1);
    this.pending = Buffer.alloc(0);
    return [decodeConsoleBytes(bytes)];
  }
}
