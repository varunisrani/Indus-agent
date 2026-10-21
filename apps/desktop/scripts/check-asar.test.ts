import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterAll, describe, expect, it } from "vitest";
// @ts-expect-error -- plain JS module without types
import { buildAsar, listAsar, parseAsarHeader } from "./asar.mjs";
// @ts-expect-error -- plain JS module without types
import { checkResources, checkTargets, findResourcesDirs } from "./check-asar.mjs";

type Entries = Record<string, string | { unpacked: true }>;
const build = buildAsar as (entries: Entries) => Buffer;
const list = listAsar as (header: unknown) => Array<{ path: string; unpacked: boolean }>;
const check = checkResources as (dir: string) => { problems: string[]; summary: string };
const targets = checkTargets as (dirs: string[]) => { ok: boolean; lines: string[] };
const findDirs = findResourcesDirs as (dir: string) => string[];

const SCRIPT = path.join(path.dirname(fileURLToPath(import.meta.url)), "check-asar.mjs");
const SHA = "0123456789abcdef0123456789abcdef01234567";
const STAMP = {
  schema: 1,
  version: "0.0.1",
  commit: SHA,
  channel: "canary",
  updateOwner: "electron-updater",
  platform: "linux",
  builtAt: "2026-10-21T00:00:00.000Z",
};
const BASE: Entries = {
  "package.json": "{}",
  "dist/index.html": "<!doctype html>",
  "dist/assets/index-abc.js": "x",
  "dist-electron/main.js": "x",
  "dist-electron/preload.cjs": "x",
  "desktop-build-stamp.json": "{}",
  "node_modules/electron-updater/package.json": "{}",
};

const dirs: string[] = [];
afterAll(() => {
  for (const dir of dirs) fs.rmSync(dir, { recursive: true, force: true });
});

function resources(
  entries: Entries,
  options: { stamp?: unknown; unpacked?: string[]; unpackedStamp?: unknown } = {},
) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "indus-asar-"));
  dirs.push(root);
  const dir = path.join(root, "linux-unpacked", "resources");
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, "app.asar"), build(entries));
  for (const file of options.unpacked ?? []) {
    const onDisk = path.join(dir, "app.asar.unpacked", ...file.split("/"));
    fs.mkdirSync(path.dirname(onDisk), { recursive: true });
    fs.writeFileSync(onDisk, "binary");
  }
  if (options.stamp !== null) {
    fs.writeFileSync(
      path.join(dir, "desktop-build-stamp.json"),
      JSON.stringify(options.stamp ?? STAMP),
    );
    if (options.unpackedStamp !== null) {
      fs.mkdirSync(path.join(dir, "app.asar.unpacked"), { recursive: true });
      fs.writeFileSync(
        path.join(dir, "app.asar.unpacked", "desktop-build-stamp.json"),
        JSON.stringify(options.unpackedStamp ?? options.stamp ?? STAMP),
      );
    }
  }
  return { root, dir };
}

describe("asar header", () => {
  it("round-trips a hand-built archive and marks unpacked files", () => {
    const buffer = build({ "a/b.txt": "hi", "a/c.node": { unpacked: true } });
    const entries = list(parseAsarHeader(buffer));
    expect(entries).toEqual([
      { path: "a/b.txt", kind: "file", unpacked: false },
      { path: "a/c.node", kind: "file", unpacked: true },
    ]);
    expect(() => parseAsarHeader(Buffer.alloc(20))).toThrow(/bad size pickle/);
  });
});

describe("packaged contents check", () => {
  it("passes a clean app without node-pty", () => {
    const { dir } = resources(BASE);
    const result = check(dir);
    expect(result.problems).toEqual([]);
    expect(result.summary).toContain("node-pty absent");
    expect(result.summary).toContain("stamp 0.0.1 canary electron-updater");
  });

  it("fails on TypeScript sources and source maps in the archive", () => {
    const { dir } = resources({
      ...BASE,
      "electron/main.ts": "x",
      "src/App.tsx": "x",
      "dist/assets/index.js.map": "{}",
    });
    const { problems } = check(dir);
    expect(problems).toContain("TypeScript source in app.asar: electron/main.ts");
    expect(problems).toContain("TypeScript source in app.asar: src/App.tsx");
    expect(problems).toContain("source map in app.asar: dist/assets/index.js.map");
  });

  it("passes node-pty unpacked with its binding on disk", () => {
    const binding = "node_modules/node-pty/build/Release/pty.node";
    const { dir } = resources(
      {
        ...BASE,
        "node_modules/node-pty/package.json": { unpacked: true },
        "node_modules/node-pty/lib/index.js": { unpacked: true },
        [binding]: { unpacked: true },
      },
      { unpacked: [binding] },
    );
    const result = check(dir);
    expect(result.problems).toEqual([]);
    expect(result.summary).toContain("node-pty unpacked");
  });

  it("fails when node-pty or a .node file is packed inside the archive", () => {
    const { dir } = resources({
      ...BASE,
      "node_modules/node-pty/lib/index.js": "x",
      "node_modules/node-pty/build/Release/pty.node": "x",
    });
    const { problems } = check(dir);
    expect(problems).toContain(
      "native module packed inside app.asar: node_modules/node-pty/build/Release/pty.node",
    );
    expect(problems).toContain("node-pty file not unpacked: node_modules/node-pty/lib/index.js");
  });

  it("fails when an entry file or the stamp is missing", () => {
    const { "dist-electron/preload.cjs": _drop, ...rest } = BASE;
    const { dir } = resources(rest, { stamp: null });
    const { problems } = check(dir);
    expect(problems).toContain("missing dist-electron/preload.cjs in app.asar");
    expect(problems.some((p) => p.startsWith("desktop-build-stamp.json"))).toBe(true);
    const bad = resources(BASE, { stamp: { ...STAMP, updateOwner: "guess" } });
    expect(check(bad.dir).problems.join("\n")).toMatch(/updateOwner/);
  });

  it("wants the stamp inside the archive and the same one unpacked (P20-37 compares them)", () => {
    const { "desktop-build-stamp.json": _drop, ...noInner } = BASE;
    expect(check(resources(noInner).dir).problems).toContain(
      "missing desktop-build-stamp.json in app.asar",
    );
    const missing = check(resources(BASE, { unpackedStamp: null }).dir).problems;
    expect(missing.some((p) => p.startsWith("missing ") && p.includes("app.asar.unpacked"))).toBe(
      true,
    );
    const other = check(resources(BASE, { unpackedStamp: { ...STAMP, version: "0.0.2" } }).dir);
    expect(other.problems.join("\n")).toMatch(/names another build/);
  });

  it("wants electron-updater in the archive when the stamp names it", () => {
    const { "node_modules/electron-updater/package.json": _drop, ...noUpdater } = BASE;
    expect(check(resources(noUpdater).dir).problems).toContain(
      "the stamp names electron-updater but app.asar does not carry it",
    );
    const manual = { ...STAMP, updateOwner: "manual" };
    expect(check(resources(noUpdater, { stamp: manual }).dir).problems).toEqual([]);
  });

  it("finds resources folders for macOS, Windows and Linux layouts", () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "indus-release-"));
    dirs.push(root);
    const layouts = [
      ["mac-arm64", "Indus.app", "Contents", "Resources"],
      ["win-unpacked", "resources"],
      ["linux-unpacked", "resources"],
    ];
    for (const parts of layouts) {
      const dir = path.join(root, ...parts);
      fs.mkdirSync(dir, { recursive: true });
      fs.writeFileSync(path.join(dir, "app.asar"), build(BASE));
    }
    expect(
      findDirs(root)
        .map((d) => path.relative(root, d))
        .sort(),
    ).toEqual(layouts.map((parts) => path.join(...parts)).sort());
  });

  it("the CLI exits 0 on a clean release, 1 on a problem, 2 without arguments", () => {
    const clean = resources(BASE);
    expect(targets([clean.root]).ok).toBe(true);
    const run = spawnSync(process.execPath, [SCRIPT, clean.root], { encoding: "utf8" });
    expect(run.status, run.stdout + run.stderr).toBe(0);
    const dirty = resources({ ...BASE, "x.ts": "x" });
    expect(spawnSync(process.execPath, [SCRIPT, dirty.root]).status).toBe(1);
    expect(spawnSync(process.execPath, [SCRIPT]).status).toBe(2);
    const empty = fs.mkdtempSync(path.join(os.tmpdir(), "indus-empty-"));
    dirs.push(empty);
    expect(targets([empty]).ok).toBe(false);
  });
});
