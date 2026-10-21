import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import {
  betweenMarkers,
  mergeLoginShellPath,
  mergePathLists,
  SHELL_PATH_DEADLINE_MS,
} from "./shellPath.ts";

const posix = process.platform !== "win32";
let dir = "";

function fakeShell(name: string, body: string): string {
  const file = path.join(dir, name);
  fs.writeFileSync(file, `#!/bin/sh\n${body}\n`, { mode: 0o755 });
  return file;
}

beforeAll(() => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), "indus-shellpath-"));
});
afterAll(() => {
  fs.rmSync(dir, { recursive: true, force: true });
});

describe.skipIf(!posix)("login-shell PATH merge (real child processes)", () => {
  it("a shell that exits 1 leaves PATH as it was", async () => {
    const env = { SHELL: fakeShell("exit1.sh", "exit 1"), PATH: "/usr/bin:/bin" };
    const outcome = await mergeLoginShellPath({ env, platform: "linux" });
    expect(outcome).toEqual({ applied: false, reason: "no-answer" });
    expect(env.PATH).toBe("/usr/bin:/bin");
  });

  it("a shell that hangs 5 s is abandoned at the 3 s deadline; PATH unchanged", async () => {
    const env = { SHELL: fakeShell("hang.sh", "sleep 5"), PATH: "/usr/bin:/bin" };
    const started = Date.now();
    const outcome = await mergeLoginShellPath({ env, platform: "linux" });
    const took = Date.now() - started;
    expect(outcome.applied).toBe(false);
    expect(env.PATH).toBe("/usr/bin:/bin");
    expect(took).toBeGreaterThanOrEqual(SHELL_PATH_DEADLINE_MS - 50);
    expect(took).toBeLessThan(SHELL_PATH_DEADLINE_MS + 1_500);
  });

  it("reads PATH between the markers despite banners and a non-zero exit", async () => {
    const shell = fakeShell(
      "banner.sh",
      [
        'echo "Welcome to the machine"',
        "shift",
        "PATH=/login/bin:/usr/bin",
        'eval "$1"',
        "echo bye",
        "exit 3",
      ].join("\n"),
    );
    const env = { SHELL: shell, PATH: "/usr/bin:/launcher/bin" };
    const outcome = await mergeLoginShellPath({ env, platform: "darwin" });
    expect(outcome).toEqual({ applied: true, path: "/login/bin:/usr/bin:/launcher/bin" });
    expect(env.PATH).toBe("/login/bin:/usr/bin:/launcher/bin");
  });

  it("falls back to a plain login shell when -i is refused", async () => {
    const shell = fakeShell(
      "noninteractive.sh",
      ['if [ "$1" = "-ilc" ]; then exit 2; fi', "shift", "PATH=/from/login", 'eval "$1"'].join(
        "\n",
      ),
    );
    const env = { SHELL: shell, PATH: "/usr/bin" };
    expect(await mergeLoginShellPath({ env, platform: "linux" })).toEqual({
      applied: true,
      path: "/from/login:/usr/bin",
    });
  });

  it("a missing shell binary leaves PATH alone", async () => {
    const env = { SHELL: path.join(dir, "nope"), PATH: "/usr/bin" };
    expect((await mergeLoginShellPath({ env, platform: "linux" })).applied).toBe(false);
    expect(env.PATH).toBe("/usr/bin");
  });
});

describe("pure parts", () => {
  it("win32 is skipped", async () => {
    const env = { PATH: "C:\\Windows" };
    expect(await mergeLoginShellPath({ env, platform: "win32" })).toEqual({
      applied: false,
      reason: "win32",
    });
  });

  it("a runner that throws is a non-answer", async () => {
    const env = { PATH: "/usr/bin" };
    const outcome = await mergeLoginShellPath({
      env,
      platform: "linux",
      run: () => Promise.reject(new Error("spawn EACCES")),
    });
    expect(outcome.applied).toBe(false);
  });

  it("unchanged PATH is reported as such", async () => {
    const env = { PATH: "/a:/b" };
    const outcome = await mergeLoginShellPath({
      env,
      platform: "linux",
      run: async (_shell, args) => {
        const probe = args[1] ?? "";
        const start = /"(__INDUS_PATH_[0-9a-f]+_START__)/.exec(probe)?.[1] ?? "";
        const end = start.replace("_START__", "_END__");
        return { stdout: `${start}/a:/b${end}` };
      },
    });
    expect(outcome).toEqual({ applied: false, reason: "unchanged" });
  });

  it("markers: the last start marker wins; missing end is no answer", () => {
    expect(betweenMarkers("S/x E S/y E", "S", "E")).toBe("/y");
    expect(betweenMarkers("S/x", "S", "E")).toBeNull();
    expect(betweenMarkers("nothing", "S", "E")).toBeNull();
    expect(betweenMarkers("S  E", "S", "E")).toBeNull();
  });

  it("merge keeps order, drops empties and repeats", () => {
    expect(mergePathLists(["/a::/b", "/b:/c:", undefined], ":")).toBe("/a:/b:/c");
  });
});
