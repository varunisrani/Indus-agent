import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { checkPathSyntax, isInside, PathGuard, workspaceRootProblem } from "./pathGuard.ts";

const posix = process.platform !== "win32";
let base = "";
let home = "";
let ws = "";
let outside = "";

beforeEach(() => {
  base = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "indus-guard-")));
  home = path.join(base, "home", "me");
  ws = path.join(base, "work", "proj");
  outside = path.join(base, "elsewhere");
  for (const dir of [home, ws, outside]) fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(ws, "a.txt"), "hello");
  fs.writeFileSync(path.join(outside, "secret.txt"), "nope");
});

afterEach(() => {
  fs.rmSync(base, { recursive: true, force: true });
});

async function refusal(promise: Promise<unknown>): Promise<string> {
  try {
    await promise;
  } catch (error) {
    return (error as { code?: string }).code ?? String(error);
  }
  return "resolved";
}

describe("path syntax", () => {
  it("wants an absolute, plain path", () => {
    expect(() => checkPathSyntax("../../etc/passwd", "linux")).toThrow(/absolute/);
    expect(() => checkPathSyntax("relative/file", "linux")).toThrow(/absolute/);
    expect(() => checkPathSyntax("/a\0b", "linux")).toThrow(/not valid/);
    expect(() => checkPathSyntax("\\\\?\\C:\\x", "win32")).toThrow(/device/);
    expect(() => checkPathSyntax("\\\\.\\PhysicalDrive0", "win32")).toThrow(/device/);
    expect(checkPathSyntax("/home/me/ws/../../../etc/passwd", "linux")).toBe("/etc/passwd");
    expect(checkPathSyntax("C:\\Users\\me\\.\\x", "win32")).toBe("C:\\Users\\me\\x");
  });

  it("containment is by path segment, case-insensitive only on Windows", () => {
    expect(isInside("/home/me/ws/a", "/home/me/ws", "linux")).toBe(true);
    expect(isInside("/home/me/ws", "/home/me/ws", "linux")).toBe(true);
    expect(isInside("/home/me/wsx/a", "/home/me/ws", "linux")).toBe(false);
    expect(isInside("/home/ME/ws", "/home/me", "linux")).toBe(false);
    expect(isInside("C:\\Users\\ME\\ws", "c:\\users\\me", "win32")).toBe(true);
  });

  it("a workspace root is never a filesystem root, a system folder or above home", () => {
    expect(workspaceRootProblem("/", "/home/me", "linux")).toMatch(/root/);
    expect(workspaceRootProblem("/etc", "/home/me", "linux")).toMatch(/system/);
    expect(workspaceRootProblem("/etc/nginx", "/home/me", "linux")).toMatch(/system/);
    expect(workspaceRootProblem("/usr", "/home/me", "linux")).toMatch(/system/);
    expect(workspaceRootProblem("/home", "/home/me", "linux")).toMatch(/above/);
    expect(workspaceRootProblem("/srv/project", "/home/me", "linux")).toBeNull();
    expect(workspaceRootProblem("/var/www/site", "/home/me", "linux")).toBeNull();
    expect(workspaceRootProblem("C:\\", "C:\\Users\\me", "win32")).toMatch(/root/);
    expect(workspaceRootProblem("C:\\Windows\\System32", "C:\\Users\\me", "win32")).toMatch(
      /system/,
    );
    expect(workspaceRootProblem("D:\\code\\app", "C:\\Users\\me", "win32")).toBeNull();
  });
});

describe("PathGuard", () => {
  it("home is always allowed; a workspace only once the window declared it", async () => {
    const guard = new PathGuard({ home });
    fs.writeFileSync(path.join(home, "notes.md"), "x");
    expect((await guard.resolve(1, path.join(home, "notes.md"))).exists).toBe(true);
    expect(await refusal(guard.resolve(1, path.join(ws, "a.txt")))).toBe("outside-roots");
    expect(await guard.setRoots(1, [ws])).toEqual({ roots: [ws], refused: [] });
    expect((await guard.resolve(1, path.join(ws, "a.txt"))).real).toBe(path.join(ws, "a.txt"));
    // another window did not declare it
    expect(await refusal(guard.resolve(2, path.join(ws, "a.txt")))).toBe("outside-roots");
  });

  it("refuses traversal out of a root", async () => {
    const guard = new PathGuard({ home });
    await guard.setRoots(1, [ws]);
    expect(await refusal(guard.resolve(1, "../../etc/passwd"))).toBe("invalid-path");
    expect(await refusal(guard.resolve(1, `${ws}/../../elsewhere/secret.txt`))).toBe(
      "outside-roots",
    );
    expect(await refusal(guard.resolve(1, "/etc/passwd"))).toBe("outside-roots");
  });

  it.runIf(posix)("refuses a symlink that escapes the root (and a root that is one)", async () => {
    const guard = new PathGuard({ home });
    await guard.setRoots(1, [ws]);
    fs.symlinkSync(outside, path.join(ws, "link-out"));
    fs.symlinkSync(path.join(outside, "secret.txt"), path.join(ws, "file-out"));
    fs.symlinkSync(path.join(ws, "a.txt"), path.join(ws, "file-in"));
    expect(await refusal(guard.resolve(1, path.join(ws, "link-out", "secret.txt")))).toBe(
      "outside-roots",
    );
    expect(await refusal(guard.resolve(1, path.join(ws, "file-out")))).toBe("outside-roots");
    expect((await guard.resolve(1, path.join(ws, "file-in"))).real).toBe(path.join(ws, "a.txt"));
    // a declared root that is a link to a system folder is refused
    fs.symlinkSync("/etc", path.join(base, "work", "etc-link"));
    const answer = await guard.setRoots(1, [path.join(base, "work", "etc-link")]);
    expect(answer.roots).toEqual([]);
    expect(answer.refused).toHaveLength(1);
  });

  it("refuses sensitive files for reads, by name and by real path", async () => {
    const guard = new PathGuard({ home });
    await guard.setRoots(1, [ws]);
    for (const name of ["id_ed25519", ".env", ".env.local", "server.pem"]) {
      fs.writeFileSync(path.join(ws, name), "x");
      expect(await refusal(guard.resolve(1, path.join(ws, name), { refuseSensitive: true }))).toBe(
        "sensitive-file",
      );
      // without the read flag the path itself is fine (rename, reveal)
      expect((await guard.resolve(1, path.join(ws, name))).exists).toBe(true);
    }
    if (posix) {
      fs.symlinkSync(path.join(ws, "id_ed25519"), path.join(ws, "harmless.txt"));
      expect(
        await refusal(guard.resolve(1, path.join(ws, "harmless.txt"), { refuseSensitive: true })),
      ).toBe("sensitive-file");
    }
  });

  it("missing paths, the roots themselves and dialog grants", async () => {
    const guard = new PathGuard({ home });
    await guard.setRoots(1, [ws]);
    expect(await refusal(guard.resolve(1, path.join(ws, "nope.txt")))).toBe("not-found");
    const missing = await guard.resolve(1, path.join(ws, "new.txt"), { mustExist: false });
    expect(missing).toMatchObject({ exists: false, real: path.join(ws, "new.txt") });
    expect(await refusal(guard.resolve(1, ws, { refuseRoot: true }))).toBe("root-protected");
    expect(await refusal(guard.resolve(1, home, { refuseRoot: true }))).toBe("root-protected");
    // a file picked in a dialog for window 1 becomes reachable for window 1 only
    expect(await refusal(guard.resolve(1, path.join(outside, "secret.txt")))).toBe("outside-roots");
    guard.grant(1, [path.join(outside, "secret.txt"), "/"]);
    expect((await guard.resolve(1, path.join(outside, "secret.txt"))).exists).toBe(true);
    expect(await refusal(guard.resolve(2, path.join(outside, "secret.txt")))).toBe("outside-roots");
    expect(guard.rootsOf(1)).not.toContain("/");
    guard.forget(1);
    expect(await refusal(guard.resolve(1, path.join(ws, "a.txt")))).toBe("outside-roots");
  });

  it("refuses roots that are missing, files, system folders or above home", async () => {
    const guard = new PathGuard({ home });
    const answer = await guard.setRoots(1, [
      ws,
      path.join(ws, "a.txt"),
      path.join(base, "missing"),
      "/",
      path.dirname(home),
      "relative",
    ]);
    expect(answer.roots).toEqual([ws]);
    expect(answer.refused).toHaveLength(5);
  });

  it("a terminal folder outside the roots falls back to home", async () => {
    const guard = new PathGuard({ home });
    await guard.setRoots(1, [ws]);
    expect(await guard.workingFolder(1, ws)).toBe(ws);
    expect(await guard.workingFolder(1, path.join(ws, "a.txt"))).toBe(ws);
    expect(await guard.workingFolder(1, outside)).toBe(home);
    expect(await guard.workingFolder(1, undefined)).toBe(home);
  });
});
