import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterAll, describe, expect, it } from "vitest";
import { detectManagers, findProgram, installPasswordManagersIpc } from "./passwordManagers.ts";

const dirs: string[] = [];
afterAll(() => {
  for (const dir of dirs) fs.rmSync(dir, { recursive: true, force: true });
});

function folder(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "indus-pm-detect-"));
  dirs.push(dir);
  return dir;
}

function program(dir: string, name: string, mode: number): void {
  fs.writeFileSync(path.join(dir, name), "#!/bin/sh\n");
  fs.chmodSync(path.join(dir, name), mode);
}

describe("password manager detection (W54-D2-1)", () => {
  it("nothing installed: both managers read as not there", () => {
    const empty = folder();
    expect(detectManagers({ env: { PATH: empty }, platform: "linux" }, false)).toEqual({
      checked: true,
      onepassword: false,
      bitwarden: false,
    });
  });

  it("finds an executable on any PATH folder; a non-executable file does not count", () => {
    const first = folder();
    const second = folder();
    program(first, "bw", 0o644);
    program(second, "op", 0o755);
    const lookup = { env: { PATH: `${first}:${second}` }, platform: "linux" };
    expect(findProgram("op", lookup)).toBe(path.join(second, "op"));
    expect(findProgram("bw", lookup)).toBeNull();
    expect(detectManagers(lookup, false)).toEqual({
      checked: true,
      onepassword: true,
      bitwarden: false,
    });
  });

  it("Windows tries the PATHEXT extensions", () => {
    const seen: string[] = [];
    const found = findProgram("bw", {
      env: { Path: "C:\\tools;D:\\bin", PATHEXT: ".EXE;.CMD" },
      platform: "win32",
      isExecutable: (file) => {
        seen.push(file);
        return file === "D:\\bin\\bw.CMD";
      },
    });
    expect(found).toBe("D:\\bin\\bw.CMD");
    expect(seen).toEqual([
      "C:\\tools\\bw.EXE",
      "C:\\tools\\bw.CMD",
      "D:\\bin\\bw.EXE",
      "D:\\bin\\bw.CMD",
    ]);
  });

  it("a remote backend: nothing is looked up here", () => {
    const dir = folder();
    program(dir, "op", 0o755);
    expect(detectManagers({ env: { PATH: dir }, platform: "linux" }, true)).toEqual({
      checked: false,
      onepassword: false,
      bitwarden: false,
    });
  });

  it("the bridge channel answers from the current lookup and connection", async () => {
    const handlers = new Map<string, () => unknown>();
    const registry = { handle: (name: string, fn: () => unknown) => handlers.set(name, fn) };
    const dir = folder();
    program(dir, "bw", 0o755);
    let remote = false;
    installPasswordManagersIpc(registry as never, {
      lookup: () => ({ env: { PATH: dir }, platform: "linux" }),
      remote: () => remote,
    });
    expect(await handlers.get("indus:vault:detect")?.()).toEqual({
      checked: true,
      onepassword: false,
      bitwarden: true,
    });
    remote = true;
    expect(await handlers.get("indus:vault:detect")?.()).toMatchObject({ checked: false });
  });
});
