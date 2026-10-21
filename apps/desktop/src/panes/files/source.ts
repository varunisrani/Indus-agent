// Where the files pane reads from (P20-29): this computer through main's fs IPC (local mode), or the
// remote host through the REST proxy (`/api/files…`, P18) in remote mode — paths there belong to the
// remote computer and are never opened locally (R08.23).

import type { IndusDesktopBridge } from "../../global.d.ts";

export interface FileEntry {
  name: string;
  path: string;
  isDir: boolean;
  size?: number;
  /** A key store or `.env`-like file: shown with a lock, never opened. */
  sensitive: boolean;
}

export interface Listing {
  path: string;
  entries: FileEntry[];
  truncated: boolean;
}

export interface FileSource {
  readonly remote: boolean;
  /** Tell main which folders this window works in (local only). */
  setRoots(roots: string[]): Promise<void>;
  list(dir: string): Promise<Listing>;
  /** Rename in place; answers the new path. Absent remotely (no REST route). */
  rename: ((from: string, newName: string) => Promise<string>) | null;
  /** Local: move to the trash; remote: delete (the pane asks first). */
  remove(path: string, isDir: boolean): Promise<void>;
  reveal: ((path: string) => Promise<void>) | null;
  /** Watch a folder; `onChange` runs after main's debounce. Answers the stop function. */
  watch: ((dir: string, onChange: () => void) => Promise<() => void>) | null;
}

/** The separator a path uses (Windows paths keep backslashes). */
export function separatorOf(path: string): "/" | "\\" {
  return /^[a-zA-Z]:\\/.test(path) || (path.includes("\\") && !path.includes("/")) ? "\\" : "/";
}

export function dirnameOf(path: string): string {
  const sep = separatorOf(path);
  const trimmed = path.length > 1 && path.endsWith(sep) ? path.slice(0, -1) : path;
  const at = trimmed.lastIndexOf(sep);
  if (at <= 0) return sep === "/" ? "/" : trimmed.slice(0, 3);
  if (sep === "\\" && at === 2) return trimmed.slice(0, 3);
  return trimmed.slice(0, at);
}

export function joinPath(dir: string, name: string): string {
  const sep = separatorOf(dir);
  return dir.endsWith(sep) ? `${dir}${name}` : `${dir}${sep}${name}`;
}

export function basenameOf(path: string): string {
  const sep = separatorOf(path);
  const trimmed = path.endsWith(sep) ? path.slice(0, -1) : path;
  return trimmed.slice(trimmed.lastIndexOf(sep) + 1);
}

/** A new name is a single, ordinary path segment. */
export function validName(name: string): boolean {
  const trimmed = name.trim();
  return trimmed !== "" && trimmed !== "." && trimmed !== ".." && !/[\\/\0]/.test(trimmed);
}

export function localSource(bridge: Pick<IndusDesktopBridge, "fs">): FileSource {
  const { fs } = bridge;
  return {
    remote: false,
    setRoots: async (roots) => {
      await fs.setRoots({ roots });
    },
    list: async (dir) => {
      const answer = await fs.readDir({ path: dir });
      return {
        path: answer.path,
        truncated: answer.truncated,
        entries: answer.entries.map((entry) => ({
          name: entry.name,
          path: entry.path,
          isDir: entry.kind === "dir" || (entry.kind === "symlink" && entry.target === "dir"),
          ...(entry.size === undefined ? {} : { size: entry.size }),
          sensitive: entry.sensitive === true,
        })),
      };
    },
    rename: async (from, newName) => {
      const answer = await fs.rename({ from, to: joinPath(dirnameOf(from), newName.trim()) });
      return answer.path;
    },
    remove: async (path) => {
      await fs.trash({ path });
    },
    reveal: async (path) => {
      await fs.reveal({ path });
    },
    watch: async (dir, onChange) => {
      let watchId: string | null = null;
      const off = fs.onChanged((change) => {
        if (change.watchId === watchId) onChange();
      });
      try {
        watchId = (await fs.watch({ path: dir })).watchId;
      } catch (error) {
        off();
        throw error;
      }
      const id = watchId;
      return () => {
        off();
        void fs.unwatch({ watchId: id }).catch(() => {});
      };
    },
  };
}

interface RestEntry {
  name?: unknown;
  path?: unknown;
  is_directory?: unknown;
  size?: unknown;
}

function restError(status: number, body: unknown): Error {
  const detail = (body as { detail?: { message?: unknown } } | null)?.detail;
  const message = typeof detail?.message === "string" ? detail.message : `HTTP ${status}`;
  return new Error(message);
}

/** The same secret-name rule the REST server applies (it leaves them out; this marks them). */
function looksSensitive(name: string): boolean {
  const lower = name.toLowerCase();
  return lower === ".env" || lower.startsWith(".env.") || /^id_(rsa|dsa|ecdsa|ed25519)/.test(lower);
}

export function remoteSource(bridge: Pick<IndusDesktopBridge, "api">): FileSource {
  const { api } = bridge;
  return {
    remote: true,
    setRoots: async () => {},
    list: async (dir) => {
      const answer = await api.request({
        method: "GET",
        path: `/api/files?path=${encodeURIComponent(dir)}`,
      });
      if (answer.kind === "not-found") throw restError(404, answer.body);
      if (answer.status !== 200) throw restError(answer.status, answer.body);
      const body = answer.body as { path?: unknown; entries?: unknown };
      const rows = Array.isArray(body.entries) ? (body.entries as RestEntry[]) : [];
      return {
        path: typeof body.path === "string" ? body.path : dir,
        truncated: false,
        entries: rows
          .filter((row) => typeof row.name === "string" && typeof row.path === "string")
          .map((row) => ({
            name: row.name as string,
            path: row.path as string,
            isDir: row.is_directory === true,
            ...(typeof row.size === "number" ? { size: row.size } : {}),
            sensitive: looksSensitive(row.name as string),
          })),
      };
    },
    rename: null,
    remove: async (path, isDir) => {
      const query = `path=${encodeURIComponent(path)}${isDir ? "&recursive=true" : ""}`;
      const answer = await api.request({ method: "DELETE", path: `/api/files?${query}` });
      if (answer.kind === "not-found") throw restError(404, answer.body);
      if (answer.status !== 200) throw restError(answer.status, answer.body);
    },
    reveal: null,
    watch: null,
  };
}
