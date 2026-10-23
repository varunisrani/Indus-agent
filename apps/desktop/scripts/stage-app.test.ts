import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterAll, describe, expect, it } from "vitest";
// @ts-expect-error -- plain JS module without types
import { copyNativeModule, licenceAllowed, stageApp } from "./stage-app.mjs";

interface Staged {
  outDir: string;
  packageJson: Record<string, unknown>;
  natives: Array<{ name: string; version: string; bindings: string[] }>;
}
const stage = stageApp as (options: {
  appDir: string;
  outDir: string;
  stamp?: { version: string; updateOwner?: string; channel?: string };
  platform?: string;
  allowFile?: string;
}) => Staged;
const copyNative = copyNativeModule as (from: string, to: string, platform: string) => string[];

const dirs: string[] = [];
afterAll(() => {
  for (const dir of dirs) fs.rmSync(dir, { recursive: true, force: true });
});

function write(root: string, files: Record<string, string>) {
  for (const [file, text] of Object.entries(files)) {
    const full = path.join(root, ...file.split("/"));
    fs.mkdirSync(path.dirname(full), { recursive: true });
    fs.writeFileSync(full, text);
  }
}

function files(root: string): string[] {
  const out: string[] = [];
  const walk = (dir: string) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(full);
      else out.push(path.relative(root, full).split(path.sep).join("/"));
    }
  };
  walk(root);
  return out.sort();
}

const PTY_FILES = {
  "node_modules/node-pty/package.json": JSON.stringify({
    name: "node-pty",
    version: "1.1.0",
    license: "MIT",
    main: "./lib/index.js",
    dependencies: { "node-addon-api": "^7.1.0" },
  }),
  "node_modules/node-pty/LICENSE": "licence",
  "node_modules/node-pty/lib/index.js": "module.exports = {}",
  "node_modules/node-pty/lib/index.js.map": "{}",
  "node_modules/node-pty/typings/node-pty.d.ts": "x",
  "node_modules/node-pty/src/unix/pty.cc": "x",
  "node_modules/node-pty/binding.gyp": "x",
  "node_modules/node-pty/deps/winpty/x.h": "x",
  "node_modules/node-pty/build/Makefile": "x",
  "node_modules/node-pty/build/Release/pty.node": "elf",
  "node_modules/node-pty/build/Release/obj.target/pty.o": "x",
  "node_modules/node-pty/prebuilds/darwin-arm64/pty.node": "macho",
  "node_modules/node-pty/prebuilds/darwin-arm64/spawn-helper": "macho",
  "node_modules/node-pty/prebuilds/win32-x64/conpty.dll": "pe",
  "node_modules/node-addon-api/package.json": JSON.stringify({
    name: "node-addon-api",
    version: "7.1.1",
    license: "MIT",
  }),
  "node_modules/node-addon-api/napi.h": "x",
};

function app(extra: Record<string, string> = {}, deps: Record<string, string> = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "indus-stage-"));
  dirs.push(root);
  write(root, {
    "package.json": JSON.stringify({
      name: "@indus/desktop",
      productName: "Indus",
      version: "0.0.0",
      description: "Indus Agent desktop app (Electron)",
      dependencies: { react: "19.3.0", ...deps },
    }),
    "dist/index.html": "<!doctype html>",
    "dist/assets/index-1.js": "x",
    "dist/assets/index-1.js.map": "{}",
    "dist-electron/main.js": "x",
    "dist-electron/main.js.map": "{}",
    "dist-electron/preload.cjs": "x",
    "electron/main.ts": "x",
    "src/main.tsx": "x",
    ...extra,
  });
  return root;
}

describe("stage the packaged app", () => {
  it("holds only the bundles and a generated package.json without dependencies", () => {
    const root = app();
    const out = path.join(root, "build", "app");
    const staged = stage({
      appDir: root,
      outDir: out,
      stamp: { version: "0.4.2", channel: "stable" },
    });
    expect(files(out)).toEqual([
      "desktop-build-stamp.json",
      "dist-electron/main.js",
      "dist-electron/preload.cjs",
      "dist/assets/index-1.js",
      "dist/index.html",
      "package.json",
    ]);
    expect(staged.packageJson).toMatchObject({
      name: "indus-desktop",
      productName: "Indus",
      version: "0.4.2",
      type: "module",
      main: "dist-electron/main.js",
      desktopName: "ai.indus.desktop.desktop",
      dependencies: {},
    });
    expect(JSON.parse(fs.readFileSync(path.join(out, "package.json"), "utf8"))).toEqual(
      staged.packageJson,
    );
  });

  it("names the desktop-file id after the stamp's channel; an unlabelled stamp is canary (W50-e)", () => {
    const desktopName = (stamp: { version: string; channel?: string }) => {
      const root = app();
      return stage({ appDir: root, outDir: path.join(root, "build", "app"), stamp }).packageJson
        .desktopName;
    };
    expect(desktopName({ version: "0.4.2", channel: "stable" })).toBe("ai.indus.desktop.desktop");
    expect(desktopName({ version: "0.4.2", channel: "canary" })).toBe(
      "ai.indus.desktop.canary.desktop",
    );
    expect(desktopName({ version: "0.4.2" })).toBe("ai.indus.desktop.canary.desktop");
  });

  it("copies a declared node-pty with its binding, platform prebuilds and runtime deps only", () => {
    const root = app(PTY_FILES, { "node-pty": "1.1.0" });
    const out = path.join(root, "build", "app");
    const staged = stage({
      appDir: root,
      outDir: out,
      stamp: { version: "0.4.2" },
      platform: "darwin",
    });
    const pty = files(out).filter((f) => f.startsWith("node_modules/"));
    expect(pty).toEqual([
      "node_modules/node-addon-api/napi.h",
      "node_modules/node-addon-api/package.json",
      "node_modules/node-pty/LICENSE",
      "node_modules/node-pty/build/Release/pty.node",
      "node_modules/node-pty/lib/index.js",
      "node_modules/node-pty/package.json",
      "node_modules/node-pty/prebuilds/darwin-arm64/pty.node",
      "node_modules/node-pty/prebuilds/darwin-arm64/spawn-helper",
    ]);
    expect(staged.packageJson.dependencies).toEqual({ "node-pty": "1.1.0" });
    expect(staged.natives[0]?.bindings.length).toBe(2);
    const helper = path.join(out, "node_modules/node-pty/prebuilds/darwin-arm64/spawn-helper");
    expect(fs.statSync(helper).mode & 0o111).toBe(0o111);
  });

  const UPDATER_FILES = {
    "node_modules/electron-updater/package.json": JSON.stringify({
      name: "electron-updater",
      version: "6.8.9",
      license: "MIT",
      dependencies: { "lazy-val": "^1.0.5" },
    }),
    "node_modules/electron-updater/out/main.js": "x",
    "node_modules/electron-updater/out/main.d.ts": "x",
    "node_modules/lazy-val/package.json": JSON.stringify({
      name: "lazy-val",
      version: "1.0.5",
      license: "MIT",
    }),
    "node_modules/lazy-val/out/main.js": "x",
  };

  it("ships electron-updater and its runtime tree only when the stamp names it", () => {
    const root = app(UPDATER_FILES, { "electron-updater": "6.8.9" });
    const out = path.join(root, "build", "app");
    const owned = stage({
      appDir: root,
      outDir: out,
      stamp: { version: "0.4.2", updateOwner: "electron-updater" },
    });
    expect(files(out).filter((f) => f.startsWith("node_modules/"))).toEqual([
      "node_modules/electron-updater/out/main.js",
      "node_modules/electron-updater/package.json",
      "node_modules/lazy-val/out/main.js",
      "node_modules/lazy-val/package.json",
    ]);
    expect(owned.packageJson.dependencies).toEqual({ "electron-updater": "6.8.9" });
    const manual = stage({
      appDir: root,
      outDir: out,
      stamp: { version: "0.4.2", updateOwner: "manual" },
    });
    expect(files(out).some((f) => f.startsWith("node_modules/"))).toBe(false);
    expect(manual.packageJson.dependencies).toEqual({});
  });

  it("refuses to stage a package whose licence is not on the allow list", () => {
    const root = app(
      {
        ...UPDATER_FILES,
        "node_modules/lazy-val/package.json": JSON.stringify({
          name: "lazy-val",
          version: "1.0.5",
          license: "BlueOak-1.0.0",
        }),
      },
      { "electron-updater": "6.8.9" },
    );
    const out = path.join(root, "build", "app");
    expect(() =>
      stage({
        appDir: root,
        outDir: out,
        stamp: { version: "0.4.2", updateOwner: "electron-updater" },
      }),
    ).toThrow(/lazy-val@1\.0\.5 \(BlueOak-1\.0\.0\)/);
  });

  it("reads SPDX expressions: one allowed side of an OR, both sides of an AND", () => {
    const allow = new Set(["MIT", "ISC"]);
    const allowed = licenceAllowed as (expression: string, allow: Set<string>) => boolean;
    expect(allowed("(WTFPL OR MIT)", allow)).toBe(true);
    expect(allowed("WTFPL", allow)).toBe(false);
    expect(allowed("MIT AND BlueOak-1.0.0", allow)).toBe(false);
    expect(allowed("", allow)).toBe(false);
  });

  it("refuses a native module without a compiled binding for the platform", () => {
    const root = app({
      "node_modules/node-pty/package.json": JSON.stringify({ name: "node-pty", version: "1.1.0" }),
      "node_modules/node-pty/lib/index.js": "x",
      "node_modules/node-pty/prebuilds/win32-x64/pty.node": "pe",
    });
    expect(() =>
      copyNative(path.join(root, "node_modules", "node-pty"), path.join(root, "out"), "linux"),
    ).toThrow(/no compiled binding for linux/);
  });

  it("refuses to stage before the bundles are built or without a stamp", () => {
    const root = app();
    fs.rmSync(path.join(root, "dist-electron", "preload.cjs"));
    const out = path.join(root, "build", "app");
    expect(() => stage({ appDir: root, outDir: out, stamp: { version: "1.0.0" } })).toThrow(
      /preload\.cjs is missing/,
    );
    expect(() => stage({ appDir: root, outDir: out })).toThrow(/build stamp/);
  });
});
