// Where a window may reach on disk (P20-29; also used by the preview, git and terminal panes).
//
// Every path a renderer names is checked here before main touches it:
//   1. syntax: absolute, no NUL, no Windows device namespace (`\\?\`, `\\.\`); then normalised;
//   2. lexical containment: inside one of the window's allowed roots — the user's home, the
//      workspace folders the window declared (`fs.setRoots`), or something the person picked in a
//      native dialog shown for that window;
//   3. real containment: with symlinks resolved, still inside the real path of an allowed root (a
//      link inside the workspace that points at `/etc` is refused: no symlink escape);
//   4. for reads into a renderer: not a key store or `.env`-like file, by the requested path or the
//      real one (`isSensitivePath`, shared with the REST proxy).
//
// A declared workspace root must be an existing folder that is neither a filesystem root, nor a
// system folder, nor the folder that holds every user's home.

import nodeFs from "node:fs";
import path from "node:path";
import { IpcError } from "../ipc/envelope.ts";
import { isSensitivePath } from "../security/sensitiveFiles.ts";

export type PathRefusal =
  | "invalid-path"
  | "outside-roots"
  | "sensitive-file"
  | "not-found"
  | "not-a-file"
  | "not-a-directory"
  | "too-large"
  | "exists"
  | "root-protected";

/** An expected refusal: the renderer gets `Error("<code>: <message>")`. */
export class PathRefused extends IpcError {
  constructor(code: PathRefusal, message: string) {
    super(code, message);
    this.name = "PathRefused";
  }
}

export interface GuardFs {
  realpath(p: string): Promise<string>;
  stat(p: string): Promise<nodeFs.Stats>;
  lstat(p: string): Promise<nodeFs.Stats>;
}

const NODE_FS: GuardFs = {
  realpath: (p) => nodeFs.promises.realpath(p),
  stat: (p) => nodeFs.promises.stat(p),
  lstat: (p) => nodeFs.promises.lstat(p),
};

export interface PathGuardOptions {
  /** The user's home folder (always allowed). */
  home: string;
  platform?: string;
  fs?: GuardFs;
}

export interface ResolvedPath {
  /** The normalised requested path (what the person sees; a symlink stays a symlink). */
  path: string;
  /** With every symlink resolved (for a missing leaf: the real parent + the leaf name). */
  real: string;
  /** Whether something exists at `path`. */
  exists: boolean;
}

export interface ResolveOptions {
  /** Refuse when nothing is there (default true). */
  mustExist?: boolean;
  /** Refuse key stores and `.env`-like files (reads into a renderer). */
  refuseSensitive?: boolean;
  /** Refuse the allowed roots themselves (trash, rename source). */
  refuseRoot?: boolean;
}

const POSIX_SYSTEM_TOP = new Set([
  "bin",
  "boot",
  "dev",
  "etc",
  "lib",
  "lib32",
  "lib64",
  "libx32",
  "proc",
  "run",
  "sbin",
  "sys",
  "usr",
  "var",
  "system",
  "library",
  "private",
  "cores",
  "volumes",
]);
/** Never a root, nor anything inside them. */
const POSIX_SYSTEM_DEEP = new Set(["boot", "dev", "etc", "proc", "sys", "run", "sbin", "bin"]);
const WINDOWS_SYSTEM_TOP = new Set([
  "windows",
  "program files",
  "program files (x86)",
  "programdata",
  "$recycle.bin",
  "system volume information",
]);

function caseless(platform: string): boolean {
  return platform === "win32";
}

/** Throw unless `raw` is an absolute, plain path; answer it normalised. */
export function checkPathSyntax(raw: unknown, platform: string = process.platform): string {
  if (typeof raw !== "string" || raw.trim() === "") {
    throw new PathRefused("invalid-path", "a path is required");
  }
  if (raw.includes("\0")) throw new PathRefused("invalid-path", "the path is not valid");
  const slashed = raw.replace(/\\/g, "/").toLowerCase();
  if (slashed.startsWith("//?/") || slashed.startsWith("//./") || slashed.includes("globalroot/")) {
    throw new PathRefused("invalid-path", "device paths are not allowed");
  }
  const flavour = platform === "win32" ? path.win32 : path.posix;
  if (!flavour.isAbsolute(raw)) {
    throw new PathRefused("invalid-path", "the path must be absolute");
  }
  return flavour.resolve(raw);
}

/** True when `child` is `parent` or lies inside it (both already normalised). */
export function isInside(child: string, parent: string, platform: string = process.platform) {
  const flavour = platform === "win32" ? path.win32 : path.posix;
  const a = caseless(platform) ? child.toLowerCase() : child;
  const b = caseless(platform) ? parent.toLowerCase() : parent;
  const rel = flavour.relative(b, a);
  return rel === "" || (!rel.startsWith("..") && !flavour.isAbsolute(rel));
}

/** Why `dir` cannot be a declared workspace root, or null. */
export function workspaceRootProblem(
  dir: string,
  home: string,
  platform: string = process.platform,
): string | null {
  const flavour = platform === "win32" ? path.win32 : path.posix;
  const parsed = flavour.parse(dir);
  if (parsed.root === dir || flavour.dirname(dir) === dir) return "a filesystem root";
  const parts = flavour
    .relative(parsed.root, dir)
    .split(flavour.sep)
    .filter((part) => part !== "")
    .map((part) => part.toLowerCase());
  const top = parts[0] ?? "";
  if (platform === "win32") {
    if (WINDOWS_SYSTEM_TOP.has(top)) return "a system folder";
  } else {
    if (parts.length === 1 && POSIX_SYSTEM_TOP.has(top)) return "a system folder";
    if (POSIX_SYSTEM_DEEP.has(top)) return "inside a system folder";
  }
  const homeParent = flavour.dirname(home);
  if (homeParent !== home && isInside(homeParent, dir, platform)) return "above the home folder";
  return null;
}

interface OwnerRoots {
  workspaces: string[];
  grants: string[];
}

export class PathGuard {
  readonly home: string;
  readonly platform: string;
  private readonly fs: GuardFs;
  private readonly owners = new Map<number, OwnerRoots>();

  constructor(options: PathGuardOptions) {
    this.platform = options.platform ?? process.platform;
    this.home = checkPathSyntax(options.home, this.platform);
    this.fs = options.fs ?? NODE_FS;
  }

  private owner(ownerId: number): OwnerRoots {
    let entry = this.owners.get(ownerId);
    if (entry === undefined) {
      entry = { workspaces: [], grants: [] };
      this.owners.set(ownerId, entry);
    }
    return entry;
  }

  /** Replace the window's workspace roots; answer what was accepted and what was refused. */
  async setRoots(
    ownerId: number,
    roots: readonly string[],
  ): Promise<{ roots: string[]; refused: string[] }> {
    const accepted: string[] = [];
    const refused: string[] = [];
    for (const raw of roots) {
      let dir: string;
      try {
        dir = checkPathSyntax(raw, this.platform);
      } catch {
        refused.push(String(raw));
        continue;
      }
      const problem = workspaceRootProblem(dir, this.home, this.platform);
      let real: string | null = null;
      try {
        if ((await this.fs.stat(dir)).isDirectory()) real = await this.fs.realpath(dir);
      } catch {
        real = null;
      }
      const realProblem =
        real === null ? "missing" : workspaceRootProblem(real, this.home, this.platform);
      if (problem !== null || realProblem !== null) {
        refused.push(dir);
        continue;
      }
      if (!accepted.includes(dir)) accepted.push(dir);
    }
    this.owner(ownerId).workspaces = accepted;
    return { roots: accepted, refused };
  }

  /** Allow what the person picked in a native dialog shown for this window. */
  grant(ownerId: number, picked: readonly string[]): void {
    const entry = this.owner(ownerId);
    for (const raw of picked) {
      try {
        const normal = checkPathSyntax(raw, this.platform);
        const flavour = this.platform === "win32" ? path.win32 : path.posix;
        if (flavour.dirname(normal) === normal) continue; // never a whole drive
        if (!entry.grants.includes(normal)) entry.grants.push(normal);
      } catch {
        // not a path: nothing granted
      }
    }
  }

  /** The window is gone: drop its roots and grants. */
  forget(ownerId: number): void {
    this.owners.delete(ownerId);
  }

  /** Home, the window's workspaces and its dialog grants. */
  rootsOf(ownerId: number): string[] {
    const entry = this.owners.get(ownerId);
    return [this.home, ...(entry?.workspaces ?? []), ...(entry?.grants ?? [])];
  }

  /** The window's workspace roots (without home). */
  workspacesOf(ownerId: number): string[] {
    return [...(this.owners.get(ownerId)?.workspaces ?? [])];
  }

  private async realOrNull(p: string): Promise<string | null> {
    try {
      return await this.fs.realpath(p);
    } catch {
      return null;
    }
  }

  /** Check `raw` for `ownerId` (see the file comment); throws `PathRefused`. */
  async resolve(
    ownerId: number,
    raw: unknown,
    options: ResolveOptions = {},
  ): Promise<ResolvedPath> {
    const mustExist = options.mustExist ?? true;
    const normal = checkPathSyntax(raw, this.platform);
    const roots = this.rootsOf(ownerId);
    const lexical = roots.filter((root) => isInside(normal, root, this.platform));
    if (lexical.length === 0) {
      throw new PathRefused("outside-roots", "that path is outside this window's folders");
    }
    if (options.refuseRoot === true && roots.some((root) => this.same(root, normal))) {
      throw new PathRefused("root-protected", "a workspace or home folder cannot be changed here");
    }
    if (options.refuseSensitive === true && isSensitivePath(normal)) {
      throw new PathRefused("sensitive-file", "that file may hold secrets; it is not read here");
    }
    const flavour = this.platform === "win32" ? path.win32 : path.posix;
    let exists = true;
    try {
      await this.fs.lstat(normal);
    } catch {
      exists = false;
    }
    if (!exists && mustExist) throw new PathRefused("not-found", "nothing is at that path");
    let real: string | null = exists ? await this.realOrNull(normal) : null;
    if (real === null) {
      // A missing leaf (or a broken link): its real parent plus the leaf name.
      const parent = await this.realOrNull(flavour.dirname(normal));
      if (parent === null) {
        if (mustExist || exists) throw new PathRefused("not-found", "nothing is at that path");
        throw new PathRefused("not-found", "the folder for that path does not exist");
      }
      real = flavour.join(parent, flavour.basename(normal));
    }
    const realRoots: string[] = [];
    for (const root of roots) {
      const rootReal = await this.realOrNull(root);
      if (rootReal !== null) realRoots.push(rootReal);
    }
    if (!realRoots.some((root) => isInside(real, root, this.platform))) {
      throw new PathRefused("outside-roots", "that path leads outside this window's folders");
    }
    if (options.refuseSensitive === true && isSensitivePath(real)) {
      throw new PathRefused("sensitive-file", "that file may hold secrets; it is not read here");
    }
    return { path: normal, real, exists };
  }

  /** A folder for a child process (terminal): inside the roots, else home. */
  async workingFolder(ownerId: number, raw: string | undefined): Promise<string> {
    if (raw === undefined) return this.home;
    try {
      const resolved = await this.resolve(ownerId, raw);
      const info = await this.fs.stat(resolved.real);
      if (info.isDirectory()) return resolved.real;
      const flavour = this.platform === "win32" ? path.win32 : path.posix;
      const parent = flavour.dirname(resolved.real);
      await this.resolve(ownerId, parent);
      return parent;
    } catch {
      return this.home;
    }
  }

  private same(a: string, b: string): boolean {
    return caseless(this.platform) ? a.toLowerCase() === b.toLowerCase() : a === b;
  }
}
