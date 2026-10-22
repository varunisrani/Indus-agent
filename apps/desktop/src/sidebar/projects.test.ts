import { describe, expect, it } from "vitest";
import {
  DEFAULT_REPO_SCAN,
  type DirEntry,
  freshDraftFolder,
  groupByProject,
  isUnder,
  newChatCwd,
  newChatTarget,
  type Project,
  ProjectStore,
  projectFor,
  scanRepos,
} from "./projects.ts";
import { type SessionInfo, toRows } from "./sessionList.ts";

class MemoryStorage {
  readonly data = new Map<string, string>();
  get length(): number {
    return this.data.size;
  }
  key(index: number): string | null {
    return [...this.data.keys()][index] ?? null;
  }
  getItem(key: string): string | null {
    return this.data.get(key) ?? null;
  }
  setItem(key: string, value: string): void {
    this.data.set(key, value);
  }
  removeItem(key: string): void {
    this.data.delete(key);
  }
}

function project(id: string, folders: string[]): Project {
  return { id, name: id, folders, createdAt: 0 };
}

describe("folders", () => {
  it("compares whole segments; Windows folds case and separators", () => {
    expect(isUnder("/work/a", "/work/a/src")).toBe(true);
    expect(isUnder("/work/a/", "/work/a")).toBe(true);
    expect(isUnder("/work/a", "/work/ab")).toBe(false);
    expect(isUnder("/Work/a", "/work/a")).toBe(false);
    expect(isUnder("C:\\Work", "c:/work/repo")).toBe(true);
    expect(isUnder("C:\\Work", "C:\\Workshop")).toBe(false);
    expect(isUnder("C:\\Work", "/Work")).toBe(false);
  });

  it("the longest matching folder wins", () => {
    const projects = [project("outer", ["/work"]), project("inner", ["/x", "/work/repo"])];
    expect(projectFor("/work/repo/src", projects)?.id).toBe("inner");
    expect(projectFor("/work/other", projects)?.id).toBe("outer");
    expect(projectFor("/elsewhere", projects)).toBeNull();
    expect(projectFor(null, projects)).toBeNull();
  });
});

describe("grouping", () => {
  const sessions = (n: number, cwd: string | null, base: number): SessionInfo[] =>
    Array.from({ length: n }, (_, i) => ({
      id: `${cwd ?? "none"}-${i}`,
      cwd,
      last_active: base + i,
    }));
  const rows = toRows([
    ...sessions(5, "/work/a", 100),
    ...sessions(2, null, 500),
    ...sessions(1, "/b", 50),
  ]);
  const projects = [
    project("a", ["/work/a"]),
    project("b", ["/b"]),
    project("empty", ["/nothing"]),
  ];

  it("each project shows its three most recent; show all opens the rest", () => {
    const grouped = groupByProject(rows, projects);
    const a = grouped.projects.find((g) => g.project.id === "a");
    expect(a?.rows.map((r) => r.id)).toEqual(["/work/a-4", "/work/a-3", "/work/a-2"]);
    expect(a?.total).toBe(5);
    const open = groupByProject(rows, projects, new Set(["a"]));
    expect(open.projects.find((g) => g.project.id === "a")?.rows).toHaveLength(5);
  });

  it("detached chats stay outside every project; empty projects sort last", () => {
    const grouped = groupByProject(rows, projects);
    expect(grouped.chats.map((r) => r.id)).toEqual(["none-1", "none-0"]);
    expect(grouped.projects.map((g) => g.project.id)).toEqual(["a", "b", "empty"]);
  });

  it("a new chat starts in the project's folder, else the default directory, else detached", () => {
    expect(newChatCwd(project("a", ["/work/a", "/x"]), "/home/me")).toBe("/work/a");
    expect(newChatCwd(null, "/home/me")).toBe("/home/me");
    expect(newChatCwd(null, null)).toBeNull();
    expect(newChatCwd(project("a", []), "  ")).toBeNull();
  });
});

describe("repo discovery", () => {
  // a fake tree: path → entries
  function tree(spec: Record<string, Array<[string, DirEntry["kind"]]>>) {
    const listed: string[] = [];
    const listDir = async (path: string): Promise<DirEntry[]> => {
      listed.push(path);
      const entries = spec[path];
      if (entries === undefined) throw new Error("ENOENT");
      return entries.map(([name, kind]) => ({ name, kind, path: `${path}/${name}` }));
    };
    return { listDir, listed };
  }

  const home = {
    "/home/me": [
      ["code", "dir"],
      ["node_modules", "dir"],
      [".cache", "dir"],
      ["link", "symlink"],
      ["notes.txt", "file"],
    ],
    "/home/me/code": [
      ["proj", "dir"],
      ["deep", "dir"],
      ["tmp-1", "dir"],
    ],
    "/home/me/code/proj": [
      [".git", "dir"],
      ["sub", "dir"],
    ],
    "/home/me/code/deep": [["deeper", "dir"]],
    "/home/me/code/deep/deeper": [["repo", "dir"]],
    "/home/me/code/deep/deeper/repo": [[".git", "file"]],
    "/home/me/code/tmp-1": [[".git", "dir"]],
    "/home/me/node_modules": [["x", "dir"]],
  } satisfies Record<string, Array<[string, DirEntry["kind"]]>>;

  it("respects depth and excludes, skips hidden folders and links, never walks into a repo", async () => {
    const fake = tree(home);
    const found = await scanRepos({
      settings: { ...DEFAULT_REPO_SCAN, maxDepth: 2, excludes: ["node_modules", "tmp-*"] },
      homeDir: "/home/me",
      listDir: fake.listDir,
    });
    expect(found).toEqual(["/home/me/code/proj"]);
    expect(fake.listed).not.toContain("/home/me/node_modules");
    expect(fake.listed).not.toContain("/home/me/.cache");
    expect(fake.listed).not.toContain("/home/me/link");
    expect(fake.listed).not.toContain("/home/me/code/proj/sub");
    expect(fake.listed).not.toContain("/home/me/code/deep/deeper/repo");
  });

  it("a deeper limit finds deeper repos (a .git file marks a worktree too)", async () => {
    const found = await scanRepos({
      settings: { ...DEFAULT_REPO_SCAN, maxDepth: 4, excludes: [] },
      homeDir: "/home/me",
      listDir: tree(home).listDir,
    });
    expect(found.sort()).toEqual([
      "/home/me/code/deep/deeper/repo",
      "/home/me/code/proj",
      "/home/me/code/tmp-1",
    ]);
  });

  it("explicit roots, an absolute exclude, a budget, and disabled scanning", async () => {
    const settings = {
      ...DEFAULT_REPO_SCAN,
      roots: ["/home/me/code"],
      maxDepth: 3,
      excludes: ["/home/me/code/deep"],
    };
    expect(await scanRepos({ settings, homeDir: null, listDir: tree(home).listDir })).toEqual([
      "/home/me/code/proj",
      "/home/me/code/tmp-1",
    ]);
    expect(
      await scanRepos({ settings, homeDir: null, listDir: tree(home).listDir, maxDirs: 1 }),
    ).toEqual([]);
    expect(
      await scanRepos({
        settings: { ...settings, enabled: false },
        homeDir: null,
        listDir: tree(home).listDir,
      }),
    ).toEqual([]);
    expect(
      await scanRepos({ settings: DEFAULT_REPO_SCAN, homeDir: null, listDir: tree(home).listDir }),
    ).toEqual([]);
  });
});

describe("the per-profile store", () => {
  it("keeps projects, default directory and scan settings per profile across a reload", () => {
    const storage = new MemoryStorage();
    const store = new ProjectStore("work", storage, () => 1000);
    const added = store.add("  Indus  ", ["/work/indus", "/work/indus/", " "]);
    expect(added?.folders).toEqual(["/work/indus"]);
    expect(store.add("   ", [])).toBeNull();
    store.setDefaultDirectory("/work");
    store.setRepoScan({ maxDepth: 99, excludes: ["x"] });
    const again = new ProjectStore("work", storage);
    expect(again.getPrefs().projects.map((p) => p.name)).toEqual(["Indus"]);
    expect(again.getPrefs().defaultDirectory).toBe("/work");
    expect(again.getPrefs().repoScan).toMatchObject({ maxDepth: 6, excludes: ["x"] });
    expect(new ProjectStore("home", storage).getPrefs().projects).toEqual([]);
    again.remove(added?.id ?? "");
    expect(again.getPrefs().projects).toEqual([]);
  });

  it("a corrupt record reads as the defaults", () => {
    const storage = new MemoryStorage();
    storage.setItem("indus.desktop.workspace.default", "{oops");
    expect(new ProjectStore(null, storage).getPrefs().projects).toEqual([]);
  });

  it("the new-chat folder is per profile and taken once", () => {
    const storage = new MemoryStorage();
    newChatTarget.set(storage, "work", "/work/a");
    expect(newChatTarget.get(storage, "home")).toBeNull();
    expect(newChatTarget.take(storage, "work")).toBe("/work/a");
    expect(newChatTarget.take(storage, "work")).toBeNull();
    expect(storage.getItem("indus.desktop.new-chat-target")).toBeNull();
  });

  it("a fresh draft not started from the sidebar opens in the default directory (W47-b)", () => {
    // App start, Ctrl+N, /new and the command center open a draft without a sidebar choice: its
    // session must still be created in the folder the Terminal pane uses, not the daemon's.
    const storage = new MemoryStorage();
    expect(freshDraftFolder(storage, "work")).toBeNull();
    new ProjectStore("work", storage).setDefaultDirectory("/work/default");
    expect(freshDraftFolder(storage, "work")).toBe("/work/default");
    expect(freshDraftFolder(storage, "home")).toBeNull();
    newChatTarget.set(storage, "work", "/work/project");
    expect(freshDraftFolder(storage, "work")).toBe("/work/project");
    newChatTarget.take(storage, "work");
    expect(freshDraftFolder(storage, "work")).toBe("/work/default");
  });
});
