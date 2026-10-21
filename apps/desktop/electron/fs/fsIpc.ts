// The files pane's local filesystem channels (P20-29), all through the P20-03 registry: the sender
// is checked and the payload parsed before anything here runs. Every path then goes through the
// window's `PathGuard` (allowed roots, no traversal, no symlink escape, no secrets into a renderer)
// and the size caps below. Native dialogs answer local paths only and grant them to the window that
// showed the dialog; nothing here translates a path for a WSL or remote backend.

import nodeFs from "node:fs";
import path from "node:path";
import { readJsonFile, writeFileAtomic } from "../fsSecure.ts";
import { IpcError } from "../ipc/envelope.ts";
import type { IpcRegistry } from "../ipc/registry.ts";
import type { ResponseOf } from "../ipc/schemas.ts";
import {
  type Change,
  type WatchHub,
  WatchLimitError,
  type WatchOwner,
} from "../preview/fileWatch.ts";
import { isSensitivePath } from "../security/sensitiveFiles.ts";
import { type PathGuard, PathRefused } from "./pathGuard.ts";

/** Attachments: bytes uploaded or a path handed to the agent (R08.23 shares the number). */
export const ATTACH_MAX_BYTES = 256 * 1024 * 1024;
/** Text previews read at most this much by default. */
export const TEXT_PREVIEW_MAX_BYTES = 512 * 1024;
/** And never more than this, whatever the renderer asks. */
export const TEXT_READ_CEILING_BYTES = 16 * 1024 * 1024;
/** The Settings value for preview data-URL reads (MiB). */
export const DATA_URL_DEFAULT_MB = 16;
export const DATA_URL_MIN_MB = 1;
export const DATA_URL_CEILING_MB = 4_096;
/** A folder listing stops here (`truncated`). */
export const READ_DIR_MAX_ENTRIES = 5_000;
const STAT_BATCH = 32;
const BINARY_SNIFF_BYTES = 8_000;

/** Folder names the tree never shows. */
export const HIDDEN_NAMES = new Set([
  ".git",
  "node_modules",
  "__pycache__",
  ".DS_Store",
  "Thumbs.db",
]);

const MIME_BY_EXT: Readonly<Record<string, string>> = {
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".gif": "image/gif",
  ".webp": "image/webp",
  ".avif": "image/avif",
  ".bmp": "image/bmp",
  ".ico": "image/x-icon",
  ".svg": "image/svg+xml",
  ".pdf": "application/pdf",
  ".mp3": "audio/mpeg",
  ".wav": "audio/wav",
  ".ogg": "audio/ogg",
  ".m4a": "audio/mp4",
  ".mp4": "video/mp4",
  ".webm": "video/webm",
  ".mov": "video/quicktime",
  ".txt": "text/plain",
  ".md": "text/markdown",
  ".json": "application/json",
  ".csv": "text/csv",
  ".html": "text/html",
  ".htm": "text/html",
  ".css": "text/css",
  ".js": "text/javascript",
  ".ts": "text/plain",
  ".py": "text/x-python",
  ".zip": "application/zip",
};

export function mimeTypeOf(file: string): string {
  return MIME_BY_EXT[path.extname(file).toLowerCase()] ?? "application/octet-stream";
}

export function clampDataUrlMb(value: unknown): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return DATA_URL_DEFAULT_MB;
  return Math.min(DATA_URL_CEILING_MB, Math.max(DATA_URL_MIN_MB, Math.round(parsed)));
}

/** The persisted Settings value (userData `fs-limits.json`). */
export class FsLimits {
  private readonly file: string;
  private dataUrlMb: number;

  constructor(file: string) {
    this.file = file;
    const stored = readJsonFile(file) as { dataUrlMaxMb?: unknown } | null;
    this.dataUrlMb = stored === null ? DATA_URL_DEFAULT_MB : clampDataUrlMb(stored.dataUrlMaxMb);
  }

  view(): ResponseOf<"indus:fs:limits:get"> {
    return {
      dataUrlMaxMb: this.dataUrlMb,
      dataUrlDefaultMb: DATA_URL_DEFAULT_MB,
      dataUrlMinMb: DATA_URL_MIN_MB,
      dataUrlCeilingMb: DATA_URL_CEILING_MB,
      attachMaxBytes: ATTACH_MAX_BYTES,
      textPreviewMaxBytes: TEXT_PREVIEW_MAX_BYTES,
    };
  }

  dataUrlMaxBytes(): number {
    return this.dataUrlMb * 1024 * 1024;
  }

  set(value: number): ResponseOf<"indus:fs:limits:get"> {
    this.dataUrlMb = clampDataUrlMb(value);
    nodeFs.mkdirSync(path.dirname(this.file), { recursive: true });
    writeFileAtomic(this.file, `${JSON.stringify({ dataUrlMaxMb: this.dataUrlMb })}\n`);
    return this.view();
  }
}

/** A window's web contents: where watch events go, and when its state is dropped. */
export interface OwnerContents extends WatchOwner {
  send(channel: string, payload: unknown): void;
}

export interface DialogRequest {
  mode: "open-file" | "open-dir" | "save";
  multiple: boolean;
  title?: string;
  defaultPath?: string;
}

export interface FsIpcDeps {
  registry: IpcRegistry;
  guard: PathGuard;
  watches: WatchHub;
  limits: FsLimits;
  /** The web contents with this id (a window the manager created), or null. */
  contents: (id: number) => OwnerContents | null;
  /** Electron `shell.trashItem` / `shell.showItemInFolder`. */
  trashItem: (file: string) => Promise<void>;
  showItemInFolder: (file: string) => void;
  /** A native dialog for the window `ownerId` (local paths). */
  showDialog: (
    ownerId: number,
    request: DialogRequest,
  ) => Promise<{ canceled: boolean; paths: string[] }>;
  log: (line: string) => void;
  /** Told the workspace roots a window declared and the guard accepted (main adds them to media). */
  onRootsAccepted?: (roots: readonly string[]) => void;
}

function entryKind(info: nodeFs.Stats): "file" | "dir" | "other" {
  if (info.isDirectory()) return "dir";
  if (info.isFile()) return "file";
  return "other";
}

type DirEntry = ResponseOf<"indus:fs:read-dir">["entries"][number];

async function describeEntry(dir: string, dirent: nodeFs.Dirent): Promise<DirEntry> {
  const full = path.join(dir, dirent.name);
  const entry: DirEntry = { name: dirent.name, path: full, kind: "other" };
  try {
    const info = await nodeFs.promises.lstat(full);
    entry.mtimeMs = info.mtimeMs;
    if (info.isSymbolicLink()) {
      entry.kind = "symlink";
      try {
        const target = await nodeFs.promises.stat(full);
        entry.target = entryKind(target);
        if (target.isFile()) entry.size = target.size;
      } catch {
        entry.target = "missing";
      }
    } else {
      entry.kind = entryKind(info);
      if (info.isFile()) entry.size = info.size;
    }
  } catch {
    entry.kind = dirent.isDirectory() ? "dir" : dirent.isFile() ? "file" : "other";
  }
  if (entry.kind !== "dir" && isSensitivePath(full)) entry.sensitive = true;
  return entry;
}

function isDirLike(entry: DirEntry): boolean {
  return entry.kind === "dir" || (entry.kind === "symlink" && entry.target === "dir");
}

/** List `dir`: folders first, then by name; noise hidden; at most 5,000 entries. */
export async function listDirectory(
  dir: string,
): Promise<{ entries: DirEntry[]; truncated: boolean }> {
  const dirents = (await nodeFs.promises.readdir(dir, { withFileTypes: true })).filter(
    (dirent) => !HIDDEN_NAMES.has(dirent.name),
  );
  dirents.sort((a, b) => {
    const da = a.isDirectory() ? 0 : 1;
    const db = b.isDirectory() ? 0 : 1;
    return da - db || a.name.localeCompare(b.name);
  });
  const truncated = dirents.length > READ_DIR_MAX_ENTRIES;
  const kept = dirents.slice(0, READ_DIR_MAX_ENTRIES);
  const entries: DirEntry[] = [];
  for (let at = 0; at < kept.length; at += STAT_BATCH) {
    entries.push(
      ...(await Promise.all(kept.slice(at, at + STAT_BATCH).map((d) => describeEntry(dir, d)))),
    );
  }
  // Symlinked folders sort with the folders once their target is known.
  entries.sort(
    (a, b) => Number(isDirLike(b)) - Number(isDirLike(a)) || a.name.localeCompare(b.name),
  );
  return { entries, truncated };
}

/** The first `maxBytes` of `file` as text (a partial last character is dropped). */
export async function readTextHead(
  file: string,
  maxBytes: number,
): Promise<ResponseOf<"indus:fs:read-text">> {
  const handle = await nodeFs.promises.open(file, "r");
  try {
    const info = await handle.stat();
    const want = Math.min(info.size, maxBytes);
    const buffer = Buffer.alloc(want);
    let read = 0;
    while (read < want) {
      const { bytesRead } = await handle.read(buffer, read, want - read, read);
      if (bytesRead === 0) break;
      read += bytesRead;
    }
    const bytes = buffer.subarray(0, read);
    const sniff = bytes.subarray(0, BINARY_SNIFF_BYTES);
    if (sniff.includes(0))
      return { text: "", truncated: info.size > read, size: info.size, binary: true };
    const truncated = info.size > read;
    const decoder = new TextDecoder("utf-8");
    // `stream: true` keeps an incomplete trailing character back instead of a replacement mark.
    const text = truncated ? decoder.decode(bytes, { stream: true }) : decoder.decode(bytes);
    return { text, truncated, size: info.size, binary: false };
  } finally {
    await handle.close();
  }
}

function asIpcError(error: unknown, fallback: string): never {
  if (error instanceof IpcError) throw error;
  const code = (error as { code?: unknown })?.code;
  if (code === "ENOENT" || code === "ENOTDIR")
    throw new PathRefused("not-found", "nothing is at that path");
  if (code === "EACCES" || code === "EPERM")
    throw new IpcError("not-readable", "permission denied");
  if (code === "EXDEV") throw new IpcError("cross-device", "cannot move across drives here");
  throw new IpcError("fs-failed", fallback);
}

export interface FsIpcHandle {
  guard: PathGuard;
  watches: WatchHub;
  limits: FsLimits;
}

/** Register every `indus:fs:*` channel. */
export function installFsIpc(deps: FsIpcDeps): FsIpcHandle {
  const { registry, guard, watches, limits } = deps;
  const tied = new Set<number>();

  /** Drop a window's roots and grants when it goes away. */
  const tie = (ownerId: number) => {
    if (tied.has(ownerId)) return;
    const owner = deps.contents(ownerId);
    if (owner === null) return;
    tied.add(ownerId);
    owner.on("destroyed", () => {
      tied.delete(ownerId);
      guard.forget(ownerId);
    });
  };

  registry.handle("indus:fs:set-roots", async ({ roots }, { senderId }) => {
    tie(senderId);
    const answer = await guard.setRoots(senderId, roots);
    if (answer.roots.length > 0) deps.onRootsAccepted?.(answer.roots);
    return answer;
  });

  registry.handle("indus:fs:read-dir", async ({ path: raw }, { senderId }) => {
    const resolved = await guard.resolve(senderId, raw);
    try {
      const info = await nodeFs.promises.stat(resolved.real);
      if (!info.isDirectory())
        throw new PathRefused("not-a-directory", "that path is not a folder");
      const listed = await listDirectory(resolved.path);
      return { path: resolved.path, ...listed };
    } catch (error) {
      return asIpcError(error, "the folder could not be read");
    }
  });

  registry.handle("indus:fs:read-text", async ({ path: raw, maxBytes }, { senderId }) => {
    const resolved = await guard.resolve(senderId, raw, { refuseSensitive: true });
    try {
      const info = await nodeFs.promises.stat(resolved.real);
      if (!info.isFile()) throw new PathRefused("not-a-file", "that path is not a file");
      return await readTextHead(
        resolved.real,
        Math.min(maxBytes ?? TEXT_PREVIEW_MAX_BYTES, TEXT_READ_CEILING_BYTES),
      );
    } catch (error) {
      return asIpcError(error, "the file could not be read");
    }
  });

  const capFor = (purpose: "preview" | "attach") =>
    purpose === "attach" ? ATTACH_MAX_BYTES : limits.dataUrlMaxBytes();

  registry.handle("indus:fs:read-data-url", async ({ path: raw, purpose }, { senderId }) => {
    const resolved = await guard.resolve(senderId, raw, { refuseSensitive: true });
    try {
      const info = await nodeFs.promises.stat(resolved.real);
      if (!info.isFile()) throw new PathRefused("not-a-file", "that path is not a file");
      const cap = capFor(purpose);
      if (info.size > cap) {
        throw new PathRefused(
          "too-large",
          `the file is larger than ${Math.floor(cap / 1048576)} MiB`,
        );
      }
      const data = await nodeFs.promises.readFile(resolved.real);
      const mimeType = mimeTypeOf(resolved.path);
      return {
        dataUrl: `data:${mimeType};base64,${data.toString("base64")}`,
        size: info.size,
        mimeType,
      };
    } catch (error) {
      return asIpcError(error, "the file could not be read");
    }
  });

  registry.handle("indus:fs:stat", async ({ path: raw, purpose }, { senderId }) => {
    const resolved = await guard.resolve(senderId, raw, { refuseSensitive: true });
    try {
      const info = await nodeFs.promises.stat(resolved.real);
      const kind = entryKind(info);
      if (kind === "file" && info.size > capFor(purpose)) {
        throw new PathRefused(
          "too-large",
          `the file is larger than ${Math.floor(capFor(purpose) / 1048576)} MiB`,
        );
      }
      return {
        path: resolved.path,
        name: path.basename(resolved.path),
        kind,
        size: info.size,
        mimeType: kind === "file" ? mimeTypeOf(resolved.path) : "inode/directory",
      };
    } catch (error) {
      return asIpcError(error, "the file could not be read");
    }
  });

  registry.handle("indus:fs:rename", async ({ from, to }, { senderId }) => {
    const source = await guard.resolve(senderId, from, { refuseRoot: true });
    const target = await guard.resolve(senderId, to, { mustExist: false, refuseRoot: true });
    if (target.exists) throw new PathRefused("exists", "something with that name is already there");
    try {
      await nodeFs.promises.rename(source.path, target.path);
      deps.log("[fs] renamed an item");
      return { path: target.path };
    } catch (error) {
      return asIpcError(error, "the item could not be renamed");
    }
  });

  registry.handle("indus:fs:trash", async ({ path: raw }, { senderId }) => {
    const resolved = await guard.resolve(senderId, raw, { refuseRoot: true });
    try {
      await deps.trashItem(resolved.path);
      return { ok: true as const };
    } catch (error) {
      return asIpcError(error, "the item could not be moved to the trash");
    }
  });

  registry.handle("indus:fs:reveal", async ({ path: raw }, { senderId }) => {
    const resolved = await guard.resolve(senderId, raw);
    deps.showItemInFolder(resolved.path);
    return { ok: true as const };
  });

  registry.handle(
    "indus:fs:select-paths",
    async ({ mode, multiple, title, defaultPath }, { senderId }) => {
      let start: string | undefined;
      if (defaultPath !== undefined) {
        try {
          // A remote path, or one outside the roots, simply does not seed the dialog.
          start = (await guard.resolve(senderId, defaultPath, { mustExist: mode !== "save" })).path;
        } catch {
          start = undefined;
        }
      }
      const answer = await deps.showDialog(senderId, {
        mode,
        multiple: multiple === true && mode !== "save",
        ...(title === undefined ? {} : { title }),
        ...(start === undefined ? {} : { defaultPath: start }),
      });
      if (answer.canceled) return { paths: [], canceled: true };
      tie(senderId);
      guard.grant(senderId, answer.paths);
      return { paths: answer.paths, canceled: false };
    },
  );

  registry.handle("indus:fs:watch", async ({ path: raw }, { senderId }) => {
    const resolved = await guard.resolve(senderId, raw);
    const owner = deps.contents(senderId);
    if (owner === null) throw new IpcError("not-found", "the window is gone");
    try {
      const info = await nodeFs.promises.stat(resolved.real);
      if (!info.isDirectory())
        throw new PathRefused("not-a-directory", "that path is not a folder");
    } catch (error) {
      return asIpcError(error, "the folder could not be watched");
    }
    let watchId = "";
    try {
      watchId = watches.watchDirectory(owner, resolved.path, (change: Change) => {
        registry.emit(owner, "indus:fs:changed", { watchId, path: change.path, kind: change.kind });
      });
    } catch (error) {
      if (error instanceof WatchLimitError) throw new IpcError("too-many-watches", error.message);
      return asIpcError(error, "the folder could not be watched");
    }
    return { watchId };
  });

  registry.handle("indus:fs:unwatch", ({ watchId }, { senderId }) => {
    watches.unwatch(senderId, watchId);
    return { ok: true as const };
  });

  registry.handle("indus:fs:limits:get", () => limits.view());
  registry.handle("indus:fs:limits:set", ({ dataUrlMaxMb }) => limits.set(dataUrlMaxMb));

  return { guard, watches, limits };
}
