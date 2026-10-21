import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";
// @ts-expect-error -- plain JS module without types
import { scan } from "./check-ipc.mjs";

const scanTree = scan as (root?: string) => string[];
const roots: string[] = [];

function tree(files: Record<string, string>): string {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "indus-check-ipc-"));
  roots.push(root);
  for (const [rel, text] of Object.entries(files)) {
    fs.mkdirSync(path.dirname(path.join(root, rel)), { recursive: true });
    fs.writeFileSync(path.join(root, rel), text);
  }
  return root;
}
afterEach(() => {
  for (const root of roots.splice(0)) fs.rmSync(root, { recursive: true, force: true });
});

describe("check-ipc static scan", () => {
  it("the app itself is clean", () => {
    expect(scanTree()).toEqual([]);
  });

  it.each([
    ['ipcMain.handle("indus:x", () => 1);', "raw ipcMain listener"],
    ["ipcMain.on('indus:x', () => {});", "raw ipcMain listener"],
    ["ipcMain?.once('x', f);", "raw ipcMain listener"],
    ["const { handle } = ipcMain;", "ipcMain destructured"],
    ['ipcMain["handle"]("x", f);', "ipcMain accessed by index"],
    ["win.webContents.ipc.handle('x', f);", "raw webContents.ipc listener"],
    ["const m = ipcMain; m.handle('x', f);", "ipcMain used outside the registry"],
  ])("a seeded %s fails", (line, why) => {
    const root = tree({
      "electron/feature/seeded.ts": `import { ipcMain } from "electron";\n${line}\n`,
    });
    const findings = scanTree(root);
    expect(
      findings.some((f) => f.startsWith("electron/feature/seeded.ts:2:") && f.includes(why)),
    ).toBe(true);
  });

  it("comments, tests and the registry itself are not findings", () => {
    const root = tree({
      "electron/a.ts":
        "// ipcMain.handle is banned here\n/* ipcMain.on too */\nexport const x = 1;\n",
      "electron/a.test.ts": "ipcMain.handle('x', f);\n",
      "electron/ipc/registry.ts": "this.options.ipc.handle(channel, f); ipcMain.handle('x', f);\n",
    });
    expect(scanTree(root)).toEqual([]);
  });

  it("main.ts may import ipcMain and pass it on, never register on it", () => {
    const ok = tree({
      "electron/main.ts":
        'import { ipcMain } from "electron";\nnew IpcRegistry({ ipc: ipcMain });\n',
    });
    expect(scanTree(ok)).toEqual([]);
    const bad = tree({
      "electron/main.ts": 'import { ipcMain } from "electron";\nipcMain.handle("indus:x", f);\n',
    });
    expect(scanTree(bad)).toHaveLength(1);
  });

  it("channel names outside indus: are findings", () => {
    const root = tree({ "electron/ipc/channels.ts": '{ name: "other:x", kind: "invoke" }\n' });
    expect(scanTree(root)).toEqual(["channels.ts: other:x: must start with indus:"]);
  });
});
