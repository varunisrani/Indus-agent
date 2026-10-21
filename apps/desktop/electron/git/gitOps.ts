// The git review pane's operations (P20-30), run only in main.
//
// Git is the bundled copy on Windows (`resources/git/cmd/git.exe` in a packaged app; else the
// system git) and the system git elsewhere. It always runs through `spawn` with an argument array —
// never a shell — with no console window (`windowsHide`), no terminal prompt, a bounded time and
// bounded output, and `-c core.fsmonitor=false` so a repository's config cannot make a status run a
// program. Inherited variables that would point git at another repository (`GIT_DIR`, …) are
// dropped. Every renderer-supplied path is relative to the repository, stays inside it, never
// starts with `-` and always follows `--`; the last-turn base is a commit id. So an argument such as
// `--upload-pack=…` can never reach git as an option.

import { spawn } from "node:child_process";
import nodeFs from "node:fs";
import path from "node:path";
import type { PathGuard } from "../fs/pathGuard.ts";
import { IpcError } from "../ipc/envelope.ts";
import type { IpcRegistry } from "../ipc/registry.ts";
import type { ResponseOf } from "../ipc/schemas.ts";
import { redactSecrets } from "../redact.ts";

export const GIT_TIMEOUT_MS = 30_000;
export const GIT_PUSH_TIMEOUT_MS = 120_000;
export const GIT_MAX_OUTPUT_BYTES = 16 * 1024 * 1024;
export const DIFF_MAX_CHARS = 2 * 1024 * 1024;
export const REVIEW_MAX_FILES = 2_000;
export const COMMIT_CONTEXT_DIFF_MAX_CHARS = 120_000;
export const COMMIT_CONTEXT_UNTRACKED_MAX = 80;
export const UNTRACKED_COUNT_MAX_BYTES = 1024 * 1024;

/** Options every git call carries (before the subcommand). */
export const GIT_SAFE_CONFIG = [
  "-c",
  "core.fsmonitor=false",
  "-c",
  "core.quotepath=false",
  "-c",
  "color.ui=false",
] as const;

/** Inherited names that would make git act on another repository or index. */
const GIT_LOCATION_NAMES = [
  "GIT_DIR",
  "GIT_WORK_TREE",
  "GIT_INDEX_FILE",
  "GIT_OBJECT_DIRECTORY",
  "GIT_ALTERNATE_OBJECT_DIRECTORIES",
  "GIT_COMMON_DIR",
  "GIT_NAMESPACE",
  "GIT_PREFIX",
];

export interface RunResult {
  code: number | null;
  stdout: string;
  stderr: string;
  /** Output went past the cap and the process was stopped. */
  overflow: boolean;
}

export interface RunOptions {
  cwd: string;
  input?: string;
  timeoutMs?: number;
  env?: Readonly<Record<string, string | undefined>>;
}

export type Runner = (
  command: string,
  args: readonly string[],
  options: RunOptions,
) => Promise<RunResult>;

/** Run `command args` without a shell; never rejects for a non-zero exit. */
export const spawnRunner: Runner = (command, args, options) =>
  new Promise((resolve, reject) => {
    const child = spawn(command, [...args], {
      cwd: options.cwd,
      env: options.env as NodeJS.ProcessEnv,
      shell: false,
      windowsHide: true,
      stdio: ["pipe", "pipe", "pipe"],
    });
    const out: Buffer[] = [];
    const err: Buffer[] = [];
    let size = 0;
    let overflow = false;
    const timer = setTimeout(() => child.kill("SIGKILL"), options.timeoutMs ?? GIT_TIMEOUT_MS);
    const take = (into: Buffer[]) => (chunk: Buffer) => {
      size += chunk.length;
      if (size > GIT_MAX_OUTPUT_BYTES) {
        if (!overflow) {
          overflow = true;
          child.kill("SIGKILL");
        }
        return;
      }
      into.push(chunk);
    };
    child.stdout.on("data", take(out));
    child.stderr.on("data", take(err));
    child.on("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      resolve({
        code,
        stdout: Buffer.concat(out).toString("utf8"),
        stderr: Buffer.concat(err).toString("utf8"),
        overflow,
      });
    });
    child.stdin.on("error", () => {});
    child.stdin.end(options.input ?? "");
  });

/** The git binary main uses. */
export function resolveGitBinary(options: {
  platform: string;
  packaged: boolean;
  resourcesPath: string;
  exists?: (file: string) => boolean;
}): string {
  if (options.platform === "win32" && options.packaged) {
    const bundled = path.win32.join(options.resourcesPath, "git", "cmd", "git.exe");
    if ((options.exists ?? nodeFs.existsSync)(bundled)) return bundled;
  }
  return "git";
}

/** An executable named `name` on `PATH` (Windows: with its PATHEXT forms), or null. */
export function findOnPath(
  name: string,
  env: Readonly<Record<string, string | undefined>>,
  platform: string = process.platform,
  isExecutable: (file: string) => boolean = (file) => {
    try {
      nodeFs.accessSync(file, nodeFs.constants.X_OK);
      return nodeFs.statSync(file).isFile();
    } catch {
      return false;
    }
  },
): string | null {
  const flavour = platform === "win32" ? path.win32 : path.posix;
  const dirs = (env.PATH ?? env.Path ?? "").split(platform === "win32" ? ";" : ":");
  const suffixes = platform === "win32" ? [".exe", ".cmd", ".bat", ""] : [""];
  for (const dir of dirs) {
    if (dir.trim() === "" || !flavour.isAbsolute(dir)) continue; // never a relative PATH entry
    for (const suffix of suffixes) {
      const candidate = flavour.join(dir, `${name}${suffix}`);
      if (isExecutable(candidate)) return candidate;
    }
  }
  return null;
}

/** The environment git runs with. */
export function gitEnv(
  base: Readonly<Record<string, string | undefined>>,
): Record<string, string | undefined> {
  const env: Record<string, string | undefined> = { ...base };
  for (const name of GIT_LOCATION_NAMES) delete env[name];
  env.GIT_TERMINAL_PROMPT = "0";
  env.GCM_INTERACTIVE = "never";
  env.GIT_PAGER = "cat";
  return env;
}

/**
 * Check one renderer-supplied path for a repository at `root`: relative, inside it, not an option.
 * Answers it with forward slashes (a trailing slash kept: an untracked folder row).
 */
export function checkRepoPath(raw: string, root: string): string {
  if (typeof raw !== "string" || raw === "" || raw.includes("\0")) {
    throw new IpcError("bad-path", "a file path in the repository is required");
  }
  const slashed = raw.replace(/\\/g, "/");
  if (slashed.startsWith("-")) throw new IpcError("bad-path", "a path may not start with '-'");
  if (slashed.startsWith("/") || path.win32.isAbsolute(raw) || /^[a-zA-Z]:/.test(slashed)) {
    throw new IpcError("bad-path", "paths are relative to the repository");
  }
  const trailing = slashed.endsWith("/");
  const normal = path.posix.normalize(slashed);
  if (normal === "." || normal === "./" || normal === ".." || normal.startsWith("../")) {
    throw new IpcError("bad-path", "that path is outside the repository");
  }
  const full = path.resolve(root, normal);
  const rel = path.relative(root, full);
  if (rel === "" || rel.startsWith("..") || path.isAbsolute(rel)) {
    throw new IpcError("bad-path", "that path is outside the repository");
  }
  const clean = normal.replace(/\/+$/, "");
  return trailing ? `${clean}/` : clean;
}

const COMMIT_ID = /^[0-9a-f]{7,64}$/;

export function checkCommitId(raw: string): string {
  if (!COMMIT_ID.test(raw)) throw new IpcError("bad-base", "the base must be a commit id");
  return raw;
}

type GitFile = ResponseOf<"indus:git:status">["files"][number];
type StatusView = ResponseOf<"indus:git:status">;

/** Parse `git status --porcelain=v2 --branch -z`. */
export function parseStatusV2(text: string): Omit<StatusView, "root" | "truncated"> {
  const view: Omit<StatusView, "root" | "truncated"> = {
    branch: null,
    upstream: null,
    ahead: 0,
    behind: 0,
    head: null,
    files: [],
  };
  const records = text.split("\0");
  const code = (c: string | undefined) => (c === undefined || c === "." ? " " : c);
  for (let i = 0; i < records.length; i += 1) {
    const record = records[i] ?? "";
    if (record === "") continue;
    if (record.startsWith("# ")) {
      const [, key, ...rest] = record.split(" ");
      const value = rest.join(" ");
      if (key === "branch.oid") view.head = value === "(initial)" ? null : value;
      else if (key === "branch.head") view.branch = value === "(detached)" ? null : value;
      else if (key === "branch.upstream") view.upstream = value;
      else if (key === "branch.ab") {
        const match = /^\+(\d+) -(\d+)$/.exec(value);
        if (match !== null) {
          view.ahead = Number(match[1]);
          view.behind = Number(match[2]);
        }
      }
      continue;
    }
    const kind = record[0];
    if (kind === "?") {
      view.files.push({ path: record.slice(2), index: "?", worktree: "?" });
    } else if (kind === "1") {
      const parts = record.split(" ");
      const xy = parts[1] ?? "..";
      view.files.push({
        path: parts.slice(8).join(" "),
        index: code(xy[0]),
        worktree: code(xy[1]),
      });
    } else if (kind === "2") {
      const parts = record.split(" ");
      const xy = parts[1] ?? "..";
      const from = records[i + 1] ?? "";
      i += 1;
      view.files.push({
        path: parts.slice(9).join(" "),
        from,
        index: code(xy[0]),
        worktree: code(xy[1]),
      });
    } else if (kind === "u") {
      const parts = record.split(" ");
      view.files.push({ path: parts.slice(10).join(" "), index: "U", worktree: "U" });
    }
  }
  return view;
}

/** Parse `git diff --numstat -z` into added/removed per (new) path; binary files count 0. */
export function parseNumstat(text: string): Map<string, { added: number; removed: number }> {
  const counts = new Map<string, { added: number; removed: number }>();
  const records = text.split("\0");
  for (let i = 0; i < records.length; i += 1) {
    const record = records[i] ?? "";
    if (record === "") continue;
    const [addedRaw, removedRaw, inline] = record.split("\t");
    let file = inline ?? "";
    if (file === "") {
      // a rename: `added\tremoved\t\0from\0to\0`
      file = records[i + 2] ?? "";
      i += 2;
    }
    const added = addedRaw === "-" ? 0 : Number(addedRaw);
    const removed = removedRaw === "-" ? 0 : Number(removedRaw);
    const prior = counts.get(file) ?? { added: 0, removed: 0 };
    counts.set(file, {
      added: prior.added + (Number.isFinite(added) ? added : 0),
      removed: prior.removed + (Number.isFinite(removed) ? removed : 0),
    });
  }
  return counts;
}

/** Parse `git diff --name-status -z`. */
export function parseNameStatus(text: string): GitFile[] {
  const files: GitFile[] = [];
  const records = text.split("\0");
  for (let i = 0; i < records.length; i += 1) {
    const status = records[i] ?? "";
    if (status === "") continue;
    const letter = status[0] ?? "M";
    if (letter === "R" || letter === "C") {
      files.push({
        path: records[i + 2] ?? "",
        from: records[i + 1] ?? "",
        index: letter,
        worktree: " ",
      });
      i += 2;
    } else {
      files.push({ path: records[i + 1] ?? "", index: letter, worktree: " " });
      i += 1;
    }
  }
  return files;
}

/** Lines a new (untracked) file adds, as an all-added diff would count them; 0 for binary or big. */
export function untrackedLines(file: string): number {
  try {
    const info = nodeFs.statSync(file);
    if (!info.isFile() || info.size > UNTRACKED_COUNT_MAX_BYTES) return 0;
    const bytes = nodeFs.readFileSync(file);
    if (bytes.includes(0)) return 0;
    let lines = 0;
    for (const byte of bytes) if (byte === 10) lines += 1;
    return bytes.length > 0 && bytes[bytes.length - 1] !== 10 ? lines + 1 : lines;
  } catch {
    return 0;
  }
}

function capText(text: string, max: number): { text: string; truncated: boolean } {
  if (text.length <= max) return { text, truncated: false };
  return { text: text.slice(0, max), truncated: true };
}

function firstLine(text: string): string {
  return redactSecrets(text.trim().split(/\r?\n/)[0] ?? "").slice(0, 300);
}

export interface GitOpsOptions {
  git: string;
  /** For `gh` (PR creation). */
  findGh: () => string | null;
  env: Readonly<Record<string, string | undefined>>;
  run?: Runner;
  log?: (line: string) => void;
}

/** Every git operation the review pane uses, against one repository top level at a time. */
export class GitOps {
  private readonly options: GitOpsOptions;
  private readonly run: Runner;

  constructor(options: GitOpsOptions) {
    this.options = options;
    this.run = options.run ?? spawnRunner;
  }

  private async git(
    root: string,
    args: readonly string[],
    extra: { input?: string; timeoutMs?: number; okCodes?: readonly number[] } = {},
  ): Promise<RunResult> {
    let result: RunResult;
    try {
      result = await this.run(this.options.git, [...GIT_SAFE_CONFIG, ...args], {
        cwd: root,
        env: gitEnv(this.options.env),
        ...(extra.input === undefined ? {} : { input: extra.input }),
        timeoutMs: extra.timeoutMs ?? GIT_TIMEOUT_MS,
      });
    } catch {
      throw new IpcError("git-missing", "git could not be started on this computer");
    }
    return result;
  }

  /** Run and require success (exit 0, or one of `okCodes`). */
  private async must(
    root: string,
    args: readonly string[],
    extra: { input?: string; timeoutMs?: number; okCodes?: readonly number[] } = {},
  ): Promise<string> {
    const result = await this.git(root, args, extra);
    const ok = result.code === 0 || (extra.okCodes ?? []).includes(result.code ?? -1);
    if (!ok) {
      throw new IpcError("git-failed", firstLine(result.stderr) || `git ${args[0]} failed`);
    }
    return result.stdout;
  }

  /** The repository's top level for a folder inside it. */
  async topLevel(dir: string): Promise<string> {
    const result = await this.git(dir, ["rev-parse", "--show-toplevel"]);
    if (result.code !== 0)
      throw new IpcError("not-a-repo", "that folder is not in a git repository");
    return path.resolve(result.stdout.trim());
  }

  private async hasHead(root: string): Promise<boolean> {
    return (await this.git(root, ["rev-parse", "--verify", "--quiet", "HEAD"])).code === 0;
  }

  async status(root: string): Promise<StatusView> {
    const raw = await this.must(root, [
      "status",
      "--porcelain=v2",
      "--branch",
      "-z",
      "--untracked-files=normal",
    ]);
    const view = parseStatusV2(raw);
    const worktree = parseNumstat(
      (await this.git(root, ["diff", "--no-ext-diff", "--numstat", "-z"])).stdout,
    );
    const staged = parseNumstat(
      (await this.git(root, ["diff", "--no-ext-diff", "--numstat", "-z", "--cached"])).stdout,
    );
    const truncated = view.files.length > REVIEW_MAX_FILES;
    const files = view.files.slice(0, REVIEW_MAX_FILES).map((file) => {
      const a = worktree.get(file.path);
      const b = staged.get(file.path);
      if (a === undefined && b === undefined) return file;
      return {
        ...file,
        added: (a?.added ?? 0) + (b?.added ?? 0),
        removed: (a?.removed ?? 0) + (b?.removed ?? 0),
      };
    });
    files.sort((x, y) => x.path.localeCompare(y.path));
    for (const file of files) {
      if (file.index === "?" && !file.path.endsWith("/")) {
        file.added = untrackedLines(path.join(root, file.path));
        file.removed = 0;
      }
    }
    return { root, ...view, files, truncated };
  }

  /** The merge-base with the default branch, or null. */
  async branchBase(root: string): Promise<string | null> {
    const candidates: string[] = [];
    const remoteHead = await this.git(root, ["rev-parse", "--abbrev-ref", "origin/HEAD"]);
    const named = remoteHead.stdout.trim();
    if (remoteHead.code === 0 && named !== "" && named !== "origin/HEAD") candidates.push(named);
    candidates.push("origin/main", "origin/master", "main", "master");
    for (const ref of candidates) {
      const verified = await this.git(root, [
        "rev-parse",
        "--verify",
        "--quiet",
        `${ref}^{commit}`,
      ]);
      if (verified.code !== 0) continue;
      const base = await this.git(root, ["merge-base", "HEAD", verified.stdout.trim()]);
      if (base.code === 0 && base.stdout.trim() !== "") return base.stdout.trim();
    }
    return null;
  }

  private async commitExists(root: string, base: string): Promise<boolean> {
    return (await this.git(root, ["cat-file", "-e", `${checkCommitId(base)}^{commit}`])).code === 0;
  }

  async changes(
    root: string,
    scope: "uncommitted" | "branch" | "last-turn",
    base?: string,
  ): Promise<ResponseOf<"indus:git:changes">> {
    if (scope === "uncommitted") {
      const status = await this.status(root);
      return { files: status.files, base: null, readOnly: false, truncated: status.truncated };
    }
    const from =
      scope === "branch"
        ? await this.branchBase(root)
        : base !== undefined && (await this.commitExists(root, base))
          ? base
          : null;
    if (from === null) return { files: [], base: null, readOnly: true, truncated: false };
    const range = scope === "branch" ? [`${from}...HEAD`] : [from];
    const names = parseNameStatus(
      await this.must(root, ["diff", "--no-ext-diff", "--name-status", "-z", ...range, "--"]),
    );
    const counts = parseNumstat(
      await this.must(root, ["diff", "--no-ext-diff", "--numstat", "-z", ...range, "--"]),
    );
    const files = names.map((file) => {
      const count = counts.get(file.path);
      return count === undefined ? file : { ...file, added: count.added, removed: count.removed };
    });
    if (scope === "last-turn") {
      const known = new Set(files.map((file) => file.path));
      const status = parseStatusV2(
        await this.must(root, ["status", "--porcelain=v2", "-z", "--untracked-files=normal"]),
      );
      for (const file of status.files) {
        if (file.index === "?" && !known.has(file.path)) files.push(file);
      }
    }
    files.sort((x, y) => x.path.localeCompare(y.path));
    return {
      files: files.slice(0, REVIEW_MAX_FILES),
      base: from,
      readOnly: true,
      truncated: files.length > REVIEW_MAX_FILES,
    };
  }

  private async isUntracked(root: string, file: string): Promise<boolean> {
    const out = await this.git(root, [
      "status",
      "--porcelain",
      "-z",
      "--untracked-files=all",
      "--",
      file,
    ]);
    return out.stdout.startsWith("??");
  }

  private async allAdded(root: string, file: string): Promise<string> {
    if (file.endsWith("/")) return "";
    const nullDevice = process.platform === "win32" ? "NUL" : "/dev/null";
    return (
      await this.must(root, ["diff", "--no-ext-diff", "--no-index", "--", nullDevice, file], {
        okCodes: [1],
      })
    ).replace(/\r\n/g, "\n");
  }

  async diff(
    root: string,
    request: {
      scope: "uncommitted" | "branch" | "last-turn";
      path?: string;
      base?: string;
      staged?: boolean;
    },
  ): Promise<ResponseOf<"indus:git:diff">> {
    const file = request.path === undefined ? undefined : checkRepoPath(request.path, root);
    const tail = file === undefined ? ["--"] : ["--", file];
    let text = "";
    if (request.scope === "uncommitted") {
      if (request.staged === true) {
        text = await this.must(root, ["diff", "--no-ext-diff", "--cached", ...tail]);
      } else if (file !== undefined) {
        text = await this.must(root, ["diff", "--no-ext-diff", ...tail]);
        if (text.trim() === "" && (await this.isUntracked(root, file))) {
          text = await this.allAdded(root, file);
        }
      } else {
        text = (await this.hasHead(root))
          ? await this.must(root, ["diff", "--no-ext-diff", "HEAD", ...tail])
          : await this.must(root, ["diff", "--no-ext-diff", "--cached", ...tail]);
      }
    } else {
      const from =
        request.scope === "branch"
          ? await this.branchBase(root)
          : request.base !== undefined && (await this.commitExists(root, request.base))
            ? request.base
            : null;
      if (from !== null) {
        const range = request.scope === "branch" ? `${from}...HEAD` : from;
        text = await this.must(root, ["diff", "--no-ext-diff", range, ...tail]);
        if (
          text.trim() === "" &&
          request.scope === "last-turn" &&
          file !== undefined &&
          (await this.isUntracked(root, file))
        ) {
          text = await this.allAdded(root, file);
        }
      }
    }
    const capped = capText(text, DIFF_MAX_CHARS);
    return {
      diff: capped.text,
      readOnly: request.scope !== "uncommitted",
      truncated: capped.truncated,
    };
  }

  async stage(root: string, paths: readonly string[]): Promise<void> {
    const files = paths.map((p) => checkRepoPath(p, root));
    await this.must(root, files.length === 0 ? ["add", "-A"] : ["add", "--", ...files]);
  }

  async unstage(root: string, paths: readonly string[]): Promise<void> {
    const files = paths.map((p) => checkRepoPath(p, root));
    if (await this.hasHead(root)) {
      await this.must(root, ["reset", "-q", "HEAD", "--", ...files]);
    } else {
      await this.must(root, [
        "rm",
        "--cached",
        "-r",
        "-q",
        "--ignore-unmatch",
        "--",
        ...(files.length === 0 ? ["."] : files),
      ]);
    }
  }

  /** Discard changes back to HEAD: tracked files restored, new files removed. */
  async revert(root: string, paths: readonly string[]): Promise<void> {
    const files = paths.map((p) => checkRepoPath(p, root));
    if (!(await this.hasHead(root))) {
      throw new IpcError("no-commit", "there is no commit to go back to yet");
    }
    if (files.length === 0) {
      await this.must(root, ["reset", "-q", "--hard", "HEAD"]);
      await this.must(root, ["clean", "-f", "-d", "-q"]);
      return;
    }
    const status = parseStatusV2(
      await this.must(root, [
        "status",
        "--porcelain=v2",
        "-z",
        "--untracked-files=normal",
        "--",
        ...files,
      ]),
    );
    const byPath = new Map(status.files.map((file) => [file.path, file]));
    const untracked: string[] = [];
    const added: string[] = [];
    const tracked: string[] = [];
    for (const file of files) {
      const entry = byPath.get(file) ?? byPath.get(file.replace(/\/$/, ""));
      if (entry === undefined || entry.index === "?") {
        if (entry !== undefined || file.endsWith("/")) untracked.push(file);
        else tracked.push(file);
      } else if (entry.index === "A") added.push(file);
      else tracked.push(file);
    }
    if (tracked.length > 0) await this.must(root, ["checkout", "-q", "HEAD", "--", ...tracked]);
    if (added.length > 0) await this.must(root, ["rm", "-q", "-f", "--", ...added]);
    if (untracked.length > 0)
      await this.must(root, ["clean", "-f", "-d", "-q", "--", ...untracked]);
  }

  /** Commit (everything when nothing is staged); optionally push, setting the upstream once. */
  async commit(
    root: string,
    message: string,
    push: boolean,
  ): Promise<ResponseOf<"indus:git:commit">> {
    const before = await this.status(root);
    const staged = before.files.some((file) => file.index !== " " && file.index !== "?");
    if (!staged) await this.must(root, ["add", "-A"]);
    await this.must(root, ["commit", "-q", "-F", "-"], { input: message });
    const commit = (await this.must(root, ["rev-parse", "HEAD"])).trim();
    let pushed = false;
    if (push) {
      await this.push(root);
      pushed = true;
    }
    return { commit, pushed };
  }

  async push(root: string): Promise<void> {
    const status = await this.status(root);
    if (status.upstream !== null) {
      await this.must(root, ["push", "-q"], { timeoutMs: GIT_PUSH_TIMEOUT_MS });
      return;
    }
    if (status.branch === null) throw new IpcError("detached", "not on a branch; nothing to push");
    const remotes = (await this.must(root, ["remote"])).split(/\r?\n/).map((r) => r.trim());
    if (!remotes.includes("origin"))
      throw new IpcError("no-remote", "this repository has no origin");
    await this.must(root, ["push", "-q", "-u", "origin", `refs/heads/${status.branch}`], {
      timeoutMs: GIT_PUSH_TIMEOUT_MS,
    });
  }

  /** What the agent needs to draft a commit message. */
  async commitContext(root: string): Promise<ResponseOf<"indus:git:commit-context">> {
    const status = await this.status(root);
    const staged = status.files.some((file) => file.index !== " " && file.index !== "?");
    const head = await this.hasHead(root);
    const diffArgs =
      staged || !head ? ["diff", "--no-ext-diff", "--cached"] : ["diff", "--no-ext-diff", "HEAD"];
    const diff = capText(
      (await this.git(root, diffArgs)).stdout,
      COMMIT_CONTEXT_DIFF_MAX_CHARS,
    ).text;
    const untracked = status.files
      .filter((file) => file.index === "?")
      .map((file) => file.path)
      .slice(0, COMMIT_CONTEXT_UNTRACKED_MAX);
    const recent = head
      ? (await this.git(root, ["log", "-n", "10", "--pretty=format:%s"])).stdout
          .split(/\r?\n/)
          .filter((line) => line.trim() !== "")
      : [];
    return { diff, untracked, recent };
  }

  /** Open a pull request with the GitHub CLI, when it is installed. */
  async createPr(
    root: string,
    title: string,
    body: string,
  ): Promise<ResponseOf<"indus:git:create-pr">> {
    const gh = this.options.findGh();
    if (gh === null) return { url: null, ghAvailable: false };
    const status = await this.status(root);
    if (status.upstream === null || status.ahead > 0) await this.push(root);
    let result: RunResult;
    try {
      result = await this.run(gh, ["pr", "create", `--title=${title}`, `--body=${body}`], {
        cwd: root,
        env: { ...gitEnv(this.options.env), GH_PROMPT_DISABLED: "1", NO_COLOR: "1" },
        timeoutMs: GIT_PUSH_TIMEOUT_MS,
      });
    } catch {
      return { url: null, ghAvailable: false };
    }
    if (result.code !== 0) {
      throw new IpcError(
        "gh-failed",
        firstLine(result.stderr) || "the GitHub CLI could not open the pull request",
      );
    }
    const url =
      result.stdout
        .split(/\r?\n/)
        .map((line) => line.trim())
        .reverse()
        .find((line) => /^https?:\/\//.test(line)) ?? null;
    return { url, ghAvailable: true };
  }
}

export interface GitIpcDeps {
  registry: IpcRegistry;
  guard: PathGuard;
  ops: GitOps;
  log: (line: string) => void;
}

/** Register every `indus:git:*` channel; each confines `repo` to the window's roots. */
export function installGitIpc(deps: GitIpcDeps): void {
  const { registry, guard, ops } = deps;

  /** The repository top level for `repo`, inside the window's roots. */
  const rootFor = async (senderId: number, repo: string): Promise<string> => {
    const start = await guard.resolve(senderId, repo);
    const top = await ops.topLevel(start.real);
    return (await guard.resolve(senderId, top)).real;
  };

  registry.handle("indus:git:status", async ({ repo }, { senderId }) =>
    ops.status(await rootFor(senderId, repo)),
  );
  registry.handle("indus:git:changes", async ({ repo, scope, base }, { senderId }) =>
    ops.changes(await rootFor(senderId, repo), scope, base),
  );
  registry.handle(
    "indus:git:diff",
    async ({ repo, scope, path: file, base, staged }, { senderId }) =>
      ops.diff(await rootFor(senderId, repo), {
        scope,
        ...(file === undefined ? {} : { path: file }),
        ...(base === undefined ? {} : { base }),
        ...(staged === undefined ? {} : { staged }),
      }),
  );
  registry.handle("indus:git:stage", async ({ repo, paths }, { senderId }) => {
    await ops.stage(await rootFor(senderId, repo), paths);
    return { ok: true as const };
  });
  registry.handle("indus:git:unstage", async ({ repo, paths }, { senderId }) => {
    await ops.unstage(await rootFor(senderId, repo), paths);
    return { ok: true as const };
  });
  registry.handle("indus:git:revert", async ({ repo, paths }, { senderId }) => {
    await ops.revert(await rootFor(senderId, repo), paths);
    deps.log("[git] reverted changes from the review pane");
    return { ok: true as const };
  });
  registry.handle("indus:git:commit", async ({ repo, message, push }, { senderId }) =>
    ops.commit(await rootFor(senderId, repo), message, push === true),
  );
  registry.handle("indus:git:commit-context", async ({ repo }, { senderId }) =>
    ops.commitContext(await rootFor(senderId, repo)),
  );
  registry.handle("indus:git:create-pr", async ({ repo, title, body }, { senderId }) =>
    ops.createPr(await rootFor(senderId, repo), title, body ?? ""),
  );
}
