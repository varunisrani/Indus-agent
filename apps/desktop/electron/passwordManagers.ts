// Settings › Passwords & Logins › Password managers (W54-D2): which manager command-line tools
// this computer has. The agent's vault tool finds 1Password's `op` and Bitwarden's `bw` on the
// backend's PATH; a local backend inherits main's PATH (merged with the login shell's at ready,
// `shellPath.ts`), so main looks the same programs up on that PATH. Nothing is run: a program
// counts when an executable file of that name sits in a PATH folder (Windows: with one of the
// PATHEXT extensions). With a remote backend the tools that matter live on the other machine, so
// nothing is looked up and the answer says so (`checked: false`).

import fs from "node:fs";
import path from "node:path";
import type { IpcRegistry } from "./ipc/registry.ts";

/** The program each manager's command-line tool installs. */
export const MANAGER_PROGRAMS = { onepassword: "op", bitwarden: "bw" } as const;

export interface ProgramLookup {
  env: Readonly<Record<string, string | undefined>>;
  platform: NodeJS.Platform | string;
  /** True when `file` is an executable regular file (injected in tests). */
  isExecutable?: (file: string) => boolean;
}

function executableFile(file: string): boolean {
  try {
    if (!fs.statSync(file).isFile()) return false;
    fs.accessSync(file, fs.constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

/** The first executable named `program` on the PATH, or null. */
export function findProgram(program: string, lookup: ProgramLookup): string | null {
  const windows = lookup.platform === "win32";
  const pathText = lookup.env.PATH ?? lookup.env.Path ?? "";
  const folders = pathText.split(windows ? ";" : ":").filter((dir) => dir.trim() !== "");
  const extensions = windows
    ? (lookup.env.PATHEXT ?? ".COM;.EXE;.BAT;.CMD").split(";").filter((ext) => ext !== "")
    : [""];
  const check = lookup.isExecutable ?? executableFile;
  const join = windows ? path.win32.join : path.posix.join;
  for (const dir of folders) {
    for (const ext of extensions) {
      const file = join(dir, `${program}${ext}`);
      if (check(file)) return file;
    }
  }
  return null;
}

export interface ManagerDetection {
  checked: boolean;
  onepassword: boolean;
  bitwarden: boolean;
}

export function detectManagers(lookup: ProgramLookup, remote: boolean): ManagerDetection {
  if (remote) return { checked: false, onepassword: false, bitwarden: false };
  return {
    checked: true,
    onepassword: findProgram(MANAGER_PROGRAMS.onepassword, lookup) !== null,
    bitwarden: findProgram(MANAGER_PROGRAMS.bitwarden, lookup) !== null,
  };
}

export function installPasswordManagersIpc(
  registry: IpcRegistry,
  deps: { lookup: () => ProgramLookup; remote: () => boolean },
): void {
  registry.handle("indus:vault:detect", () => detectManagers(deps.lookup(), deps.remote()));
}
