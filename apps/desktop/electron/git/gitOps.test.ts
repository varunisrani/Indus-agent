import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { type PaneIpcHarness, paneIpcHarness } from "../../testing/paneIpc.ts";
import { PathGuard } from "../fs/pathGuard.ts";
import {
  checkRepoPath,
  findOnPath,
  GitOps,
  gitEnv,
  installGitIpc,
  parseNameStatus,
  parseNumstat,
  parseStatusV2,
  type Runner,
  resolveGitBinary,
  spawnRunner,
} from "./gitOps.ts";

const posix = process.platform !== "win32";
let base = "";
let repo = "";
let remote = "";
let env: Record<string, string | undefined> = {};
let h: PaneIpcHarness;
let ghPath: string | null = null;
let calls: string[][] = [];

function git(cwd: string, ...args: string[]): string {
  return execFileSync("git", args, { cwd, env: env as NodeJS.ProcessEnv, encoding: "utf8" });
}

function write(rel: string, text: string) {
  const file = path.join(repo, rel);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, text);
}

beforeEach(async () => {
  base = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "indus-git-")));
  repo = path.join(base, "repo");
  remote = path.join(base, "remote.git");
  fs.mkdirSync(repo);
  const config = path.join(base, "gitconfig");
  fs.writeFileSync(
    config,
    "[user]\n\tname = Test Person\n\temail = person@example.invalid\n[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n",
  );
  env = {
    PATH: process.env.PATH,
    HOME: base,
    GIT_CONFIG_NOSYSTEM: "1",
    GIT_CONFIG_GLOBAL: config,
    // A stray location variable must not redirect the review pane's git.
    GIT_DIR: path.join(base, "nowhere"),
  };
  execFileSync("git", ["init", "-q", repo], {
    env: { ...env, GIT_DIR: undefined } as NodeJS.ProcessEnv,
  });
  delete env.GIT_DIR;
  write("README.md", "one\n");
  write("src/app.ts", "let a = 1;\n");
  git(repo, "add", "-A");
  git(repo, "commit", "-q", "-m", "first");
  calls = [];
  ghPath = null;
  const recording: Runner = (command, args, options) => {
    calls.push([command, ...args]);
    return spawnRunner(command, args, options);
  };
  h = paneIpcHarness([11, 12]);
  installGitIpc({
    registry: h.registry,
    guard: new PathGuard({ home: base }),
    ops: new GitOps({ git: "git", findGh: () => ghPath, env, run: recording }),
    log: () => {},
  });
});

afterEach(() => {
  fs.rmSync(base, { recursive: true, force: true });
});

type Status = {
  root: string;
  branch: string | null;
  upstream: string | null;
  ahead: number;
  behind: number;
  head: string | null;
  files: Array<{ path: string; index: string; worktree: string; added?: number; removed?: number }>;
};

const status = async (): Promise<Status> =>
  (await h.invoke("indus:git:status", { repo })) as Status;

describe("parsers", () => {
  it("porcelain v2 with branch, renames, untracked and conflicts", () => {
    const text = [
      "# branch.oid 1234567890abcdef",
      "# branch.head feature/x",
      "# branch.upstream origin/feature/x",
      "# branch.ab +2 -1",
      "1 .M N... 100644 100644 100644 aaa bbb src/a b.ts",
      "2 R. N... 100644 100644 100644 aaa bbb R100 new name.ts",
      "old name.ts",
      "u UU N... 100644 100644 100644 100644 a b c conflicted.ts",
      "? notes/",
      "",
    ].join("\0");
    expect(parseStatusV2(text)).toEqual({
      branch: "feature/x",
      upstream: "origin/feature/x",
      ahead: 2,
      behind: 1,
      head: "1234567890abcdef",
      files: [
        { path: "src/a b.ts", index: " ", worktree: "M" },
        { path: "new name.ts", from: "old name.ts", index: "R", worktree: " " },
        { path: "conflicted.ts", index: "U", worktree: "U" },
        { path: "notes/", index: "?", worktree: "?" },
      ],
    });
    expect(parseStatusV2("# branch.oid (initial)\0# branch.head (detached)\0")).toMatchObject({
      head: null,
      branch: null,
    });
  });

  it("numstat and name-status with renames and binaries", () => {
    const counts = parseNumstat(
      ["3\t1\ta.ts", "-\t-\timg.png", "2\t0\t", "old.ts", "new.ts", ""].join("\0"),
    );
    expect(Object.fromEntries(counts)).toEqual({
      "a.ts": { added: 3, removed: 1 },
      "img.png": { added: 0, removed: 0 },
      "new.ts": { added: 2, removed: 0 },
    });
    expect(
      parseNameStatus(["M", "a.ts", "R087", "old.ts", "new.ts", "A", "b.ts", ""].join("\0")),
    ).toEqual([
      { path: "a.ts", index: "M", worktree: " " },
      { path: "new.ts", from: "old.ts", index: "R", worktree: " " },
      { path: "b.ts", index: "A", worktree: " " },
    ]);
  });
});

describe("argument and path confinement", () => {
  it("command arg injection (--upload-pack) refused before git runs", async () => {
    const before = calls.length;
    for (const bad of [
      "--upload-pack=touch /tmp/pwned",
      "-c",
      "../outside.txt",
      "/etc/passwd",
      "C:\\x",
      ".",
      "a/../../b",
    ]) {
      expect(() => checkRepoPath(bad, repo)).toThrow();
    }
    await expect(
      h.invoke("indus:git:stage", { repo, paths: ["--upload-pack=touch /tmp/pwned"] }),
    ).rejects.toThrow(/^bad-path/);
    await expect(
      h.invoke("indus:git:diff", { repo, scope: "last-turn", base: "--upload-pack=x" }),
    ).rejects.toThrow(/^bad-request/);
    await expect(
      h.invoke("indus:git:diff", { repo, scope: "uncommitted", path: "-p" }),
    ).rejects.toThrow(/^bad-path/);
    // only the top-level lookup ran; never the stage or diff
    expect(calls.slice(before).every((c) => c.includes("rev-parse"))).toBe(true);
    expect(checkRepoPath("src\\app.ts", repo)).toBe("src/app.ts");
    expect(checkRepoPath("notes/", repo)).toBe("notes/");
  });

  it("a repository outside the window's roots is refused; not a repo is said so", async () => {
    const other = paneIpcHarness([11]);
    installGitIpc({
      registry: other.registry,
      guard: new PathGuard({ home: path.join(base, "elsewhere-home") }),
      ops: new GitOps({ git: "git", findGh: () => null, env }),
      log: () => {},
    });
    fs.mkdirSync(path.join(base, "elsewhere-home"));
    await expect(other.invoke("indus:git:status", { repo })).rejects.toThrow(/^outside-roots/);
    fs.mkdirSync(path.join(base, "plain"));
    await expect(h.invoke("indus:git:status", { repo: path.join(base, "plain") })).rejects.toThrow(
      /^not-a-repo/,
    );
  });

  it("git runs without a shell, a console window or a location override", () => {
    const cleaned = gitEnv({ GIT_DIR: "/x", GIT_WORK_TREE: "/y", PATH: "/bin" });
    expect(cleaned.GIT_DIR).toBeUndefined();
    expect(cleaned.GIT_WORK_TREE).toBeUndefined();
    expect(cleaned.GIT_TERMINAL_PROMPT).toBe("0");
    expect(cleaned.PATH).toBe("/bin");
    expect(
      resolveGitBinary({
        platform: "win32",
        packaged: true,
        resourcesPath: "C:\\App\\resources",
        exists: () => true,
      }),
    ).toBe("C:\\App\\resources\\git\\cmd\\git.exe");
    expect(
      resolveGitBinary({
        platform: "win32",
        packaged: true,
        resourcesPath: "C:\\R",
        exists: () => false,
      }),
    ).toBe("git");
    expect(resolveGitBinary({ platform: "linux", packaged: true, resourcesPath: "/r" })).toBe(
      "git",
    );
  });
});

describe("temp repo: review operations", () => {
  it("status: branch, head, changed files with counts", async () => {
    write("README.md", "one\ntwo\n");
    write("new.txt", "fresh\n");
    git(repo, "add", "new.txt");
    write("untracked/thing.txt", "x\n");
    const view = await status();
    expect(view.root).toBe(repo);
    expect(view.branch).toBe("main");
    expect(view.head).toMatch(/^[0-9a-f]{40}$/);
    expect(view.files).toEqual([
      { path: "new.txt", index: "A", worktree: " ", added: 1, removed: 0 },
      { path: "README.md", index: " ", worktree: "M", added: 1, removed: 0 },
      { path: "untracked/", index: "?", worktree: "?" },
    ]);
    write("loose.txt", "a\nb\nno newline");
    fs.writeFileSync(path.join(repo, "blob.bin"), Buffer.from([1, 0, 1]));
    const counted = await status();
    expect(counted.files.find((f) => f.path === "loose.txt")).toMatchObject({
      added: 3,
      removed: 0,
    });
    expect(counted.files.find((f) => f.path === "blob.bin")).toMatchObject({ added: 0 });
  });

  it("stage / unstage / revert / commit", async () => {
    write("README.md", "changed\n");
    write("extra.txt", "new\n");
    await h.invoke("indus:git:stage", { repo, paths: ["README.md"] });
    expect((await status()).files.find((f) => f.path === "README.md")?.index).toBe("M");
    await h.invoke("indus:git:unstage", { repo, paths: ["README.md"] });
    expect((await status()).files.find((f) => f.path === "README.md")).toMatchObject({
      index: " ",
      worktree: "M",
    });
    await h.invoke("indus:git:revert", { repo, paths: ["README.md", "extra.txt"] });
    expect(fs.readFileSync(path.join(repo, "README.md"), "utf8")).toBe("one\n");
    expect(fs.existsSync(path.join(repo, "extra.txt"))).toBe(false);
    expect((await status()).files).toEqual([]);
    // staged new file reverted too
    write("staged-new.txt", "s\n");
    await h.invoke("indus:git:stage", { repo, paths: [] });
    await h.invoke("indus:git:revert", { repo, paths: ["staged-new.txt"] });
    expect(fs.existsSync(path.join(repo, "staged-new.txt"))).toBe(false);
    // commit with nothing staged commits everything
    write("src/app.ts", "let a = 2;\n");
    write("b.txt", "b\n");
    const commit = (await h.invoke("indus:git:commit", {
      repo,
      message: "-n looks like an option\n\nbody",
    })) as {
      commit: string;
      pushed: boolean;
    };
    expect(commit.pushed).toBe(false);
    expect(git(repo, "rev-parse", "HEAD").trim()).toBe(commit.commit);
    expect(git(repo, "log", "-1", "--pretty=%s").trim()).toBe("-n looks like an option");
    expect((await status()).files).toEqual([]);
  });

  it("ahead/behind against a bare remote; commit & push sets the upstream", async () => {
    execFileSync("git", ["init", "-q", "--bare", remote], { env: env as NodeJS.ProcessEnv });
    git(repo, "remote", "add", "origin", remote);
    write("a.txt", "a\n");
    const first = (await h.invoke("indus:git:commit", { repo, message: "add a", push: true })) as {
      pushed: boolean;
    };
    expect(first.pushed).toBe(true);
    let view = await status();
    expect(view.upstream).toBe("origin/main");
    expect([view.ahead, view.behind]).toEqual([0, 0]);
    write("b.txt", "b\n");
    git(repo, "add", "b.txt");
    git(repo, "commit", "-q", "-m", "local only");
    // someone else pushes to the remote
    const clone = path.join(base, "clone");
    execFileSync("git", ["clone", "-q", remote, clone], { env: env as NodeJS.ProcessEnv });
    fs.writeFileSync(path.join(clone, "c.txt"), "c\n");
    git(clone, "add", "c.txt");
    git(clone, "commit", "-q", "-m", "theirs");
    git(clone, "push", "-q");
    git(repo, "fetch", "-q");
    view = await status();
    expect([view.ahead, view.behind]).toEqual([1, 1]);
  });

  it("branch and last-turn scopes are read-only and compare against their base", async () => {
    const turnBase = git(repo, "rev-parse", "HEAD").trim();
    git(repo, "checkout", "-q", "-b", "feature");
    write("src/app.ts", "let a = 3;\n");
    git(repo, "commit", "-q", "-am", "feature work");
    write("made-by-agent.txt", "hello\n");
    const branch = (await h.invoke("indus:git:changes", { repo, scope: "branch" })) as {
      files: Array<{ path: string; index: string }>;
      base: string;
      readOnly: boolean;
    };
    expect(branch.readOnly).toBe(true);
    expect(branch.base).toBe(turnBase);
    expect(branch.files.map((f) => [f.path, f.index])).toEqual([["src/app.ts", "M"]]);
    const lastTurn = (await h.invoke("indus:git:changes", {
      repo,
      scope: "last-turn",
      base: turnBase,
    })) as { files: Array<{ path: string; index: string }> };
    expect(lastTurn.files.map((f) => [f.path, f.index])).toEqual([
      ["made-by-agent.txt", "?"],
      ["src/app.ts", "M"],
    ]);
    const diff = (await h.invoke("indus:git:diff", {
      repo,
      scope: "last-turn",
      base: turnBase,
      path: "made-by-agent.txt",
    })) as { diff: string; readOnly: boolean };
    expect(diff.readOnly).toBe(true);
    expect(diff.diff).toContain("+hello");
    const unknownBase = (await h.invoke("indus:git:changes", {
      repo,
      scope: "last-turn",
      base: "0123456789abcdef0123",
    })) as { files: unknown[]; base: string | null };
    expect(unknownBase).toMatchObject({ files: [], base: null });
  });

  it("uncommitted diffs: one file, staged, untracked shown as all added", async () => {
    write("README.md", "one\nmore\n");
    write("fresh.md", "brand new\n");
    const one = (await h.invoke("indus:git:diff", {
      repo,
      scope: "uncommitted",
      path: "README.md",
    })) as {
      diff: string;
      readOnly: boolean;
    };
    expect(one.readOnly).toBe(false);
    expect(one.diff).toContain("+more");
    const fresh = (await h.invoke("indus:git:diff", {
      repo,
      scope: "uncommitted",
      path: "fresh.md",
    })) as {
      diff: string;
    };
    expect(fresh.diff).toContain("+brand new");
    await h.invoke("indus:git:stage", { repo, paths: ["README.md"] });
    const staged = (await h.invoke("indus:git:diff", {
      repo,
      scope: "uncommitted",
      path: "README.md",
      staged: true,
    })) as { diff: string };
    expect(staged.diff).toContain("+more");
  });

  it("commit context for the agent: staged diff, untracked names, recent subjects", async () => {
    write("README.md", "one\nctx\n");
    write("loose.txt", "l\n");
    const context = (await h.invoke("indus:git:commit-context", { repo })) as {
      diff: string;
      untracked: string[];
      recent: string[];
    };
    expect(context.diff).toContain("+ctx");
    expect(context.untracked).toEqual(["loose.txt"]);
    expect(context.recent).toEqual(["first"]);
  });

  it("a repository without a commit yet", async () => {
    const empty = path.join(base, "empty");
    execFileSync("git", ["init", "-q", empty], { env: env as NodeJS.ProcessEnv });
    fs.writeFileSync(path.join(empty, "a.txt"), "a\n");
    await h.invoke("indus:git:stage", { repo: empty, paths: ["a.txt"] });
    const view = (await h.invoke("indus:git:status", { repo: empty })) as Status;
    expect(view.head).toBeNull();
    expect(view.files).toEqual([
      { path: "a.txt", index: "A", worktree: " ", added: 1, removed: 0 },
    ]);
    await h.invoke("indus:git:unstage", { repo: empty, paths: ["a.txt"] });
    expect(((await h.invoke("indus:git:status", { repo: empty })) as Status).files[0]?.index).toBe(
      "?",
    );
    await expect(h.invoke("indus:git:revert", { repo: empty, paths: [] })).rejects.toThrow(
      /^no-commit/,
    );
  });
});

describe("pull requests", () => {
  it("gh absent → ghAvailable false (the pane offers to ask the agent)", async () => {
    ghPath = null;
    expect(await h.invoke("indus:git:create-pr", { repo, title: "T" })).toEqual({
      url: null,
      ghAvailable: false,
    });
    expect(findOnPath("gh", { PATH: path.join(base, "no-bin") })).toBeNull();
    expect(findOnPath("gh", { PATH: "relative/bin" })).toBeNull();
  });

  it.runIf(posix)(
    "a present gh opens the PR after pushing the branch (fake gh, no network)",
    async () => {
      execFileSync("git", ["init", "-q", "--bare", remote], { env: env as NodeJS.ProcessEnv });
      git(repo, "remote", "add", "origin", remote);
      const bin = path.join(base, "bin");
      fs.mkdirSync(bin);
      const record = path.join(base, "gh-args.txt");
      const fake = path.join(bin, "gh");
      fs.writeFileSync(
        fake,
        `#!/bin/sh\nprintf '%s\\n' "$@" > '${record}'\necho 'Creating pull request'\necho 'https://example.invalid/pr/7'\n`,
        { mode: 0o755 },
      );
      expect(findOnPath("gh", { PATH: bin })).toBe(fake);
      ghPath = fake;
      const answer = await h.invoke("indus:git:create-pr", { repo, title: "--web", body: "Body" });
      expect(answer).toEqual({ url: "https://example.invalid/pr/7", ghAvailable: true });
      expect(fs.readFileSync(record, "utf8").split("\n")).toEqual([
        "pr",
        "create",
        "--title=--web",
        "--body=Body",
        "",
      ]);
      expect(git(repo, "rev-parse", "--abbrev-ref", "main@{upstream}").trim()).toBe("origin/main");
    },
  );
});
