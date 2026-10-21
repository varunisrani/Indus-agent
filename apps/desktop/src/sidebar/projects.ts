// Projects and workspace folders (P20-25). A project is the person's own grouping: a name and the
// folders (repos or plain folders) it owns. A session belongs to the project whose folder is the
// longest prefix of its folder; the rest stay in "Chats". Projects, the profile's default
// directory and the repo-scan settings are kept per profile in local storage (a registered
// profile family, P20-26).
//
// Repo discovery walks the scan roots (the home folder by default) to a bounded depth through
// main's fs bridge, never following links, skipping hidden and excluded folders, and stops at a
// repository (a folder holding `.git`) without descending into it.

import {
  familyKey,
  perKeyFamily,
  profileKey,
  recordFamily,
  type StorageLike,
} from "../profiles/storageFamilies.ts";
import type { SessionRow } from "./sessionList.ts";

export interface Project {
  readonly id: string;
  readonly name: string;
  readonly folders: readonly string[];
  readonly createdAt: number;
}

export interface RepoScanSettings {
  readonly enabled: boolean;
  /** Absolute folders to walk; empty means the home folder. */
  readonly roots: readonly string[];
  /** Folder names (or `*` patterns, or absolute paths) never walked. */
  readonly excludes: readonly string[];
  readonly maxDepth: number;
}

export interface WorkspacePrefs {
  readonly projects: readonly Project[];
  /** Where a plain new chat starts (null: detached). */
  readonly defaultDirectory: string | null;
  readonly repoScan: RepoScanSettings;
}

export const PREVIEW_COUNT = 3;
export const MAX_SCAN_DEPTH = 6;
export const MAX_SCAN_DIRS = 2_000;

export const DEFAULT_REPO_SCAN: RepoScanSettings = {
  enabled: true,
  roots: [],
  excludes: [
    "node_modules",
    "Library",
    "AppData",
    "Applications",
    "vendor",
    "target",
    "dist",
    "build",
    "venv",
    "__pycache__",
  ],
  maxDepth: 3,
};

export const DEFAULT_PREFS: WorkspacePrefs = {
  projects: [],
  defaultDirectory: null,
  repoScan: DEFAULT_REPO_SCAN,
};

// -- paths ---------------------------------------------------------------------------------------

/** Windows spellings: a drive letter, a UNC or a backslash-rooted path. */
export function isWindowsPath(path: string): boolean {
  return /^[A-Za-z]:[\\/]/.test(path) || path.startsWith("\\\\") || path.startsWith("//");
}

/** Path segments for comparing: separators and trailing slashes ignored; Windows folds case. */
export function pathSegments(path: string): string[] {
  const segments = path
    .split(/[\\/]+/)
    .filter((segment) => segment !== "" && segment !== ".")
    .map((segment) => segment.normalize("NFC"));
  return isWindowsPath(path) ? segments.map((segment) => segment.toLowerCase()) : segments;
}

/** `target` is `folder` or inside it (whole segments: `/a/b` is not under `/a/bc`). */
export function isUnder(folder: string, target: string): boolean {
  const f = pathSegments(folder);
  const t = pathSegments(target);
  if (f.length === 0 || f.length > t.length) return false;
  if (isWindowsPath(folder) !== isWindowsPath(target)) return false;
  return f.every((segment, index) => segment === t[index]);
}

export function baseName(path: string): string {
  const segments = path.split(/[\\/]+/).filter((segment) => segment !== "");
  return segments[segments.length - 1] ?? path;
}

// -- grouping ------------------------------------------------------------------------------------

/** The project owning a folder: the longest matching folder wins. */
export function projectFor(cwd: string | null, projects: readonly Project[]): Project | null {
  if (cwd === null || cwd.trim() === "") return null;
  let best: Project | null = null;
  let bestLength = -1;
  for (const project of projects) {
    for (const folder of project.folders) {
      if (!isUnder(folder, cwd)) continue;
      const length = pathSegments(folder).length;
      if (length > bestLength) {
        best = project;
        bestLength = length;
      }
    }
  }
  return best;
}

export interface ProjectGroup {
  readonly project: Project;
  /** The rows shown (the 3 most recent, or all when expanded). */
  readonly rows: readonly SessionRow[];
  readonly total: number;
  readonly expanded: boolean;
}

export interface Grouping {
  readonly projects: readonly ProjectGroup[];
  /** Rows no project claims (detached chats and folders outside every project). */
  readonly chats: readonly SessionRow[];
}

/** Rows (newest first) into project groups and the rest; `expanded` project ids show all. */
export function groupByProject(
  rows: readonly SessionRow[],
  projects: readonly Project[],
  expanded: ReadonlySet<string> = new Set(),
): Grouping {
  const byProject = new Map<string, SessionRow[]>();
  const chats: SessionRow[] = [];
  for (const row of rows) {
    const project = projectFor(row.cwd, projects);
    if (project === null) {
      chats.push(row);
      continue;
    }
    const list = byProject.get(project.id) ?? [];
    list.push(row);
    byProject.set(project.id, list);
  }
  const groups = projects.map((project) => {
    const all = byProject.get(project.id) ?? [];
    const open = expanded.has(project.id);
    return {
      project,
      rows: open ? all : all.slice(0, PREVIEW_COUNT),
      total: all.length,
      expanded: open,
    };
  });
  // most recently active project first; empty ones last, by name
  groups.sort((a, b) => {
    const at = a.rows[0]?.lastActive ?? -1;
    const bt = b.rows[0]?.lastActive ?? -1;
    return bt - at || a.project.name.localeCompare(b.project.name);
  });
  return { projects: groups, chats };
}

/**
 * The folder a new chat starts in: the project's first folder when the chat was started from a
 * project, else the profile's default directory, else none (a detached chat).
 */
export function newChatCwd(
  project: Project | null,
  defaultDirectory: string | null,
): string | null {
  const fromProject = project?.folders[0] ?? null;
  if (fromProject !== null && fromProject.trim() !== "") return fromProject;
  const fallback = (defaultDirectory ?? "").trim();
  return fallback === "" ? null : fallback;
}

// -- repo discovery ------------------------------------------------------------------------------

export interface DirEntry {
  readonly name: string;
  readonly path: string;
  readonly kind: "file" | "dir" | "symlink" | "other";
}

export type ListDir = (path: string) => Promise<readonly DirEntry[]>;

function excluded(entry: DirEntry, excludes: readonly string[]): boolean {
  return excludes.some((pattern) => {
    if (pattern.includes("/") || pattern.includes("\\")) return isUnder(pattern, entry.path);
    if (!pattern.includes("*")) return entry.name === pattern;
    const escaped = pattern.replace(/[.+?^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*");
    return new RegExp(`^${escaped}$`).test(entry.name);
  });
}

export interface ScanOptions {
  readonly settings: RepoScanSettings;
  /** The roots used when the settings name none. */
  readonly homeDir: string | null;
  readonly listDir: ListDir;
  readonly maxDirs?: number;
  readonly signal?: { readonly aborted: boolean };
}

/** Repositories under the scan roots, breadth first, bounded by depth and a folder budget. */
export async function scanRepos(options: ScanOptions): Promise<string[]> {
  const { settings } = options;
  if (!settings.enabled) return [];
  const depthLimit = Math.max(0, Math.min(MAX_SCAN_DEPTH, Math.trunc(settings.maxDepth)));
  const roots =
    settings.roots.length > 0
      ? settings.roots
      : options.homeDir !== null && options.homeDir !== ""
        ? [options.homeDir]
        : [];
  const budget = options.maxDirs ?? MAX_SCAN_DIRS;
  const found: string[] = [];
  const seen = new Set<string>();
  const queue: Array<{ path: string; depth: number }> = roots.map((path) => ({ path, depth: 0 }));
  let visited = 0;
  while (queue.length > 0 && visited < budget) {
    if (options.signal?.aborted === true) break;
    const next = queue.shift() as { path: string; depth: number };
    const key = pathSegments(next.path).join("/");
    if (seen.has(key)) continue;
    seen.add(key);
    visited += 1;
    let entries: readonly DirEntry[];
    try {
      entries = await options.listDir(next.path);
    } catch {
      continue; // unreadable: skip it
    }
    if (entries.some((entry) => entry.name === ".git")) {
      found.push(next.path);
      continue; // never walk into a repository
    }
    if (next.depth >= depthLimit) continue;
    for (const entry of entries) {
      if (entry.kind !== "dir") continue; // links are not followed
      if (entry.name.startsWith(".")) continue;
      if (excluded(entry, settings.excludes)) continue;
      queue.push({ path: entry.path, depth: next.depth + 1 });
    }
  }
  return found;
}

// -- the per-profile store -----------------------------------------------------------------------

const WORKSPACE = perKeyFamily("workspace");
const NEW_CHAT = recordFamily("new-chat-target");

function cleanFolders(folders: readonly string[]): string[] {
  const out: string[] = [];
  for (const folder of folders) {
    const trimmed = folder.trim();
    if (trimmed === "") continue;
    if (
      out.some((existing) => pathSegments(existing).join("/") === pathSegments(trimmed).join("/"))
    ) {
      continue;
    }
    out.push(trimmed);
  }
  return out;
}

function parsePrefs(raw: string | null): WorkspacePrefs {
  if (raw === null) return DEFAULT_PREFS;
  try {
    const value = JSON.parse(raw) as Record<string, unknown>;
    const projects = Array.isArray(value.projects)
      ? value.projects.flatMap((item): Project[] => {
          if (typeof item !== "object" || item === null) return [];
          const p = item as Record<string, unknown>;
          if (typeof p.id !== "string" || typeof p.name !== "string") return [];
          const folders = Array.isArray(p.folders)
            ? cleanFolders(p.folders.filter((f): f is string => typeof f === "string"))
            : [];
          return [
            {
              id: p.id,
              name: p.name,
              folders,
              createdAt: typeof p.createdAt === "number" ? p.createdAt : 0,
            },
          ];
        })
      : [];
    const scan = (value.repoScan ?? {}) as Record<string, unknown>;
    const strings = (list: unknown, fallback: readonly string[]) =>
      Array.isArray(list) ? list.filter((s): s is string => typeof s === "string") : fallback;
    return {
      projects,
      defaultDirectory:
        typeof value.defaultDirectory === "string" && value.defaultDirectory.trim() !== ""
          ? value.defaultDirectory
          : null,
      repoScan: {
        enabled: typeof scan.enabled === "boolean" ? scan.enabled : DEFAULT_REPO_SCAN.enabled,
        roots: strings(scan.roots, DEFAULT_REPO_SCAN.roots),
        excludes: strings(scan.excludes, DEFAULT_REPO_SCAN.excludes),
        maxDepth:
          typeof scan.maxDepth === "number" && Number.isFinite(scan.maxDepth)
            ? Math.max(0, Math.min(MAX_SCAN_DEPTH, Math.trunc(scan.maxDepth)))
            : DEFAULT_REPO_SCAN.maxDepth,
      },
    };
  } catch {
    return DEFAULT_PREFS;
  }
}

/** One profile's projects, default directory and scan settings. */
export class ProjectStore {
  readonly profile: string;
  private readonly storage: StorageLike | null;
  private readonly key: string;
  private readonly listeners = new Set<() => void>();
  private prefs: WorkspacePrefs;
  private readonly now: () => number;
  private serial = 0;

  constructor(profile: string | null, storage: StorageLike | null, now: () => number = Date.now) {
    this.now = now;
    this.profile = profileKey(profile);
    this.storage = storage;
    this.key = familyKey(WORKSPACE, this.profile);
    let raw: string | null = null;
    try {
      raw = storage?.getItem(this.key) ?? null;
    } catch {
      raw = null;
    }
    this.prefs = parsePrefs(raw);
  }

  getPrefs(): WorkspacePrefs {
    return this.prefs;
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  add(name: string, folders: readonly string[]): Project | null {
    const clean = name.trim();
    if (clean === "") return null;
    this.serial += 1;
    const project: Project = {
      id: `p${this.now().toString(36)}${this.serial.toString(36)}`,
      name: clean,
      folders: cleanFolders(folders),
      createdAt: this.now(),
    };
    this.update({ projects: [...this.prefs.projects, project] });
    return project;
  }

  rename(id: string, name: string): void {
    const clean = name.trim();
    if (clean === "") return;
    this.update({
      projects: this.prefs.projects.map((p) => (p.id === id ? { ...p, name: clean } : p)),
    });
  }

  setFolders(id: string, folders: readonly string[]): void {
    this.update({
      projects: this.prefs.projects.map((p) =>
        p.id === id ? { ...p, folders: cleanFolders(folders) } : p,
      ),
    });
  }

  remove(id: string): void {
    this.update({ projects: this.prefs.projects.filter((p) => p.id !== id) });
  }

  setDefaultDirectory(path: string | null): void {
    const clean = (path ?? "").trim();
    this.update({ defaultDirectory: clean === "" ? null : clean });
  }

  setRepoScan(settings: Partial<RepoScanSettings>): void {
    const merged = { ...this.prefs.repoScan, ...settings };
    this.update({
      repoScan: {
        ...merged,
        maxDepth: Math.max(0, Math.min(MAX_SCAN_DEPTH, Math.trunc(merged.maxDepth))),
      },
    });
  }

  private update(changes: Partial<WorkspacePrefs>): void {
    this.prefs = { ...this.prefs, ...changes };
    try {
      this.storage?.setItem(this.key, JSON.stringify(this.prefs));
    } catch {
      // storage full: the change holds for this run
    }
    for (const listener of [...this.listeners]) listener();
  }
}

// -- the folder of the next new chat ------------------------------------------------------------

/**
 * The sidebar decides where the next fresh draft of a profile starts (a project's "new chat", or
 * the default directory); the composer reads it once when it creates the session. Kept in local
 * storage so a reload between the click and the first message does not lose it.
 */
export const newChatTarget = {
  set(storage: StorageLike | null, profile: string | null, cwd: string | null): void {
    const record = readTargets(storage);
    const key = profileKey(profile);
    if (cwd === null) delete record[key];
    else record[key] = cwd;
    writeTargets(storage, record);
  },
  get(storage: StorageLike | null, profile: string | null): string | null {
    const value = readTargets(storage)[profileKey(profile)];
    return typeof value === "string" && value !== "" ? value : null;
  },
  /** Read and clear (the session now exists with that folder). */
  take(storage: StorageLike | null, profile: string | null): string | null {
    const value = newChatTarget.get(storage, profile);
    if (value !== null) newChatTarget.set(storage, profile, null);
    return value;
  },
};

function readTargets(storage: StorageLike | null): Record<string, string> {
  try {
    const raw = storage?.getItem(NEW_CHAT.key);
    if (raw === null || raw === undefined) return {};
    const parsed = JSON.parse(raw) as unknown;
    return typeof parsed === "object" && parsed !== null ? (parsed as Record<string, string>) : {};
  } catch {
    return {};
  }
}

function writeTargets(storage: StorageLike | null, record: Record<string, string>): void {
  try {
    if (Object.keys(record).length === 0) storage?.removeItem(NEW_CHAT.key);
    else storage?.setItem(NEW_CHAT.key, JSON.stringify(record));
  } catch {
    // storage unavailable
  }
}
