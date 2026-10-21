// Small file helpers for main: atomic writes, and owner-only files in owner-only directories for
// anything that holds a credential. "Owner-only" is mode bits on POSIX; on Windows userData is
// already private to the user (ACL), so the mode steps are skipped there.

import fs from "node:fs";
import path from "node:path";

export const PRIVATE_FILE_MODE = 0o600;
export const PRIVATE_DIR_MODE = 0o700;

export class UnsafePathError extends Error {}

function posix(platform: string): boolean {
  return platform !== "win32";
}

/** Create `dir` (0700) or tighten an existing one we own; refuse a symlink or someone else's. */
export function ensurePrivateDir(dir: string, platform: string = process.platform): void {
  fs.mkdirSync(dir, { recursive: true, mode: PRIVATE_DIR_MODE });
  if (!posix(platform)) return;
  const info = fs.lstatSync(dir);
  if (info.isSymbolicLink() || !info.isDirectory())
    throw new UnsafePathError(`${dir} is not a directory`);
  if (typeof process.getuid === "function" && info.uid !== process.getuid()) {
    throw new UnsafePathError(`${dir} belongs to another user`);
  }
  if ((info.mode & 0o777) !== PRIVATE_DIR_MODE) fs.chmodSync(dir, PRIVATE_DIR_MODE);
}

export interface AtomicWriteOptions {
  mode?: number;
  platform?: string;
}

/**
 * Write `data` to `file` through a temp file in the same directory and a rename, so a reader sees
 * the old or the new content, never half. The temp file is created exclusively with `mode`
 * (a stale temp or a planted symlink at the temp path is removed first, not followed), synced,
 * then renamed over the target.
 */
export function writeFileAtomic(
  file: string,
  data: string | Uint8Array,
  options: AtomicWriteOptions = {},
): void {
  const mode = options.mode ?? 0o644;
  const platform = options.platform ?? process.platform;
  const tmp = path.join(path.dirname(file), `.${path.basename(file)}.${process.pid}.tmp`);
  fs.rmSync(tmp, { force: true });
  const fd = fs.openSync(tmp, "wx", mode);
  try {
    fs.writeFileSync(fd, data);
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
  if (posix(platform)) fs.chmodSync(tmp, mode);
  try {
    const existing = fs.lstatSync(file);
    if (existing.isSymbolicLink())
      throw new UnsafePathError(`${file} is a symlink; not replacing it`);
  } catch (error) {
    if (error instanceof UnsafePathError) {
      fs.rmSync(tmp, { force: true });
      throw error;
    }
  }
  fs.renameSync(tmp, file);
}

/** Owner-only (0600) atomic write inside an owner-only (0700) directory. */
export function writePrivateFile(
  file: string,
  data: string,
  platform: string = process.platform,
): void {
  ensurePrivateDir(path.dirname(file), platform);
  writeFileAtomic(file, data, { mode: PRIVATE_FILE_MODE, platform });
}

/** Read a regular file as UTF-8 (a symlink is refused); null when it does not exist. */
export function readRegularFile(file: string): string | null {
  let info: fs.Stats;
  try {
    info = fs.lstatSync(file);
  } catch {
    return null;
  }
  if (!info.isFile()) throw new UnsafePathError(`${file} is not a regular file`);
  return fs.readFileSync(file, "utf8");
}

/** JSON from `file`, or null when missing or unreadable (never throws). */
export function readJsonFile(file: string): unknown {
  try {
    const text = readRegularFile(file);
    return text === null ? null : JSON.parse(text);
  } catch {
    return null;
  }
}
