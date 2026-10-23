import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterAll, describe, expect, it } from "vitest";
import { electronBinary } from "../testing/electron/binary.ts";
// @ts-expect-error -- plain JS module without types
import { packedBinary } from "./after-pack-fuses.mjs";
// @ts-expect-error -- plain JS module without types
import * as fuses from "./fuses.mjs";
// @ts-expect-error -- plain JS module without types
import { verify } from "./verify-fuses.mjs";

type States = Record<string, string>;
const { applyFuses, findPackagedBinaries, fuseConfig, fuseMismatches, readFuses, WANTED_FUSES } =
  fuses as Record<string, unknown>;
const verifyBinaries = verify as (binaries: string[]) => Promise<{ ok: boolean; lines: string[] }>;
const electron = electronBinary();
const dirs: string[] = [];
afterAll(() => {
  for (const dir of dirs) fs.rmSync(dir, { recursive: true, force: true });
});

describe("fuse table", () => {
  it("is exactly the six fuses D-16.19 names", () => {
    expect(WANTED_FUSES).toEqual({
      RunAsNode: false,
      EnableNodeOptionsEnvironmentVariable: false,
      EnableNodeCliInspectArguments: false,
      EnableEmbeddedAsarIntegrityValidation: true,
      OnlyLoadAppFromAsar: true,
      EnableCookieEncryption: true,
    });
    const config = (fuseConfig as (o?: object) => Record<string, unknown>)();
    expect(config.version).toBe("1");
    expect(Object.keys(config).filter((k) => /^\d+$/.test(k))).toHaveLength(6);
  });

  it("mismatches name every fuse off its wanted state", () => {
    const all = (fuseMismatches as (s: States) => string[])({
      RunAsNode: "on",
      EnableNodeOptionsEnvironmentVariable: "off",
      EnableNodeCliInspectArguments: "off",
      EnableEmbeddedAsarIntegrityValidation: "on",
      OnlyLoadAppFromAsar: "unset",
      EnableCookieEncryption: "on",
    });
    expect(all).toEqual(["RunAsNode", "OnlyLoadAppFromAsar"]);
  });

  it("the hook picks the packaged binary per platform", () => {
    const pack = packedBinary as (c: object) => string;
    const packager = { appInfo: { productFilename: "Indus" }, executableName: "indus-desktop" };
    expect(pack({ appOutDir: "/o", electronPlatformName: "darwin", packager })).toBe(
      path.join("/o", "Indus.app"),
    );
    expect(pack({ appOutDir: "/o", electronPlatformName: "win32", packager })).toBe(
      path.join("/o", "Indus.exe"),
    );
    expect(pack({ appOutDir: "/o", electronPlatformName: "linux", packager })).toBe(
      path.join("/o", "indus-desktop"),
    );
  });

  it("finds packaged binaries under a dist folder", () => {
    const dist = fs.mkdtempSync(path.join(os.tmpdir(), "indus-dist-"));
    dirs.push(dist);
    fs.mkdirSync(path.join(dist, "linux-unpacked"));
    fs.writeFileSync(path.join(dist, "linux-unpacked", "indus-desktop"), "", { mode: 0o755 });
    fs.mkdirSync(path.join(dist, "mac-arm64", "Indus.app"), { recursive: true });
    const found = (findPackagedBinaries as (d: string) => string[])(dist)
      .map((f) => path.relative(dist, f))
      .sort();
    expect(found).toEqual([
      path.join("linux-unpacked", "indus-desktop"),
      path.join("mac-arm64", "Indus.app"),
    ]);
  });

  it("finds a canary build's binaries under their own names (P23-16)", () => {
    const dist = fs.mkdtempSync(path.join(os.tmpdir(), "indus-dist-"));
    dirs.push(dist);
    fs.mkdirSync(path.join(dist, "linux-unpacked"));
    fs.writeFileSync(path.join(dist, "linux-unpacked", "indus-desktop-canary"), "", {
      mode: 0o755,
    });
    fs.mkdirSync(path.join(dist, "win-unpacked"));
    fs.writeFileSync(path.join(dist, "win-unpacked", "Indus Canary.exe"), "");
    fs.mkdirSync(path.join(dist, "mac-arm64", "Indus Canary.app"), { recursive: true });
    const found = (findPackagedBinaries as (d: string) => string[])(dist)
      .map((f) => path.relative(dist, f))
      .sort();
    expect(found).toEqual([
      path.join("linux-unpacked", "indus-desktop-canary"),
      path.join("mac-arm64", "Indus Canary.app"),
      path.join("win-unpacked", "Indus Canary.exe"),
    ]);
  });

  it("verify fails when nothing is found or the file has no fuse wire", async () => {
    expect((await verifyBinaries([])).ok).toBe(false);
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "indus-fuse-"));
    dirs.push(dir);
    const junk = path.join(dir, "not-electron");
    fs.writeFileSync(junk, "just text");
    const result = await verifyBinaries([junk]);
    expect(result.ok).toBe(false);
    expect(result.lines.join("\n")).toContain("no fuse wire");
  });
});

describe.skipIf(electron.path === null)(
  `fuses on a real Electron binary${electron.reason ? ` (skipped: ${electron.reason})` : ""}`,
  () => {
    it("a stock Electron binary fails verification", async () => {
      const result = await verifyBinaries([electron.path as string]);
      expect(result.ok).toBe(false);
      expect(result.lines[0]).toContain("MISMATCH");
    });

    it("after the afterPack flip, verification passes and reports all six", async () => {
      const dir = fs.mkdtempSync(path.join(os.tmpdir(), "indus-fuse-"));
      dirs.push(dir);
      const copy = path.join(dir, "indus-desktop");
      fs.copyFileSync(electron.path as string, copy);
      await (applyFuses as (b: string) => Promise<void>)(copy);
      const states = await (readFuses as (b: string) => Promise<States>)(copy);
      expect(states).toEqual({
        RunAsNode: "off",
        EnableNodeOptionsEnvironmentVariable: "off",
        EnableNodeCliInspectArguments: "off",
        EnableEmbeddedAsarIntegrityValidation: "on",
        OnlyLoadAppFromAsar: "on",
        EnableCookieEncryption: "on",
      });
      const result = await verifyBinaries([copy]);
      expect(result.ok).toBe(true);
      expect(result.lines).toHaveLength(7);
    });
  },
);
