import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterAll, describe, expect, it } from "vitest";
// @ts-expect-error -- plain JS module without types
import * as stampModule from "./write-build-stamp.mjs";

interface Stamp {
  schema: number;
  version: string;
  commit: string;
  channel: string;
  updateOwner: string;
  platform: string;
  builtAt: string;
}
type Deps = {
  env?: Record<string, string | undefined>;
  readText?: (file: string) => string;
  commitOf?: (cwd: string) => string | null;
};
const m = stampModule as {
  makeBuildStamp(options?: Record<string, string>, deps?: Deps): Stamp;
  validateBuildStamp(stamp: unknown): Stamp;
  updateOwnerFor(platform: string): string;
  packageVersion(text: string): string | null;
  parseArgs(argv: string[]): Record<string, string>;
  STAMP_FILE: string;
};

const SCRIPT = path.join(path.dirname(fileURLToPath(import.meta.url)), "write-build-stamp.mjs");
const REPO_ROOT = path.resolve(path.dirname(SCRIPT), "..", "..", "..");
const SHA = "0123456789abcdef0123456789abcdef01234567";
const quiet: Deps = {
  env: {},
  readText: () => '"""Indus Agent."""\n\n__version__ = "0.4.2"\n',
  commitOf: () => SHA,
};
const dirs: string[] = [];
afterAll(() => {
  for (const dir of dirs) fs.rmSync(dir, { recursive: true, force: true });
});

describe("build stamp", () => {
  it("carries version, commit, channel, update owner, platform and build time", () => {
    const stamp = m.makeBuildStamp({ platform: "darwin" }, quiet);
    expect(stamp).toMatchObject({
      schema: 1,
      version: "0.4.2",
      commit: SHA,
      channel: "canary",
      updateOwner: "electron-updater",
      platform: "darwin",
    });
    expect(Number.isNaN(Date.parse(stamp.builtAt))).toBe(false);
    expect(m.STAMP_FILE).toBe("desktop-build-stamp.json");
  });

  it("declares one update owner per platform: updater on macOS and Linux, manual on Windows", () => {
    expect(m.updateOwnerFor("darwin")).toBe("electron-updater");
    expect(m.updateOwnerFor("linux")).toBe("electron-updater");
    expect(m.updateOwnerFor("win32")).toBe("manual");
    expect(m.makeBuildStamp({ platform: "win32" }, quiet).updateOwner).toBe("manual");
    expect(m.makeBuildStamp({ platform: "linux", updateOwner: "manual" }, quiet).updateOwner).toBe(
      "manual",
    );
  });

  it("takes channel, version and commit from the environment when not passed", () => {
    const stamp = m.makeBuildStamp(
      { platform: "linux" },
      {
        ...quiet,
        env: {
          INDUS_DESKTOP_CHANNEL: "stable",
          INDUS_DESKTOP_VERSION: "1.2.3-canary.20261021",
          INDUS_DESKTOP_COMMIT: SHA.toUpperCase(),
          SOURCE_DATE_EPOCH: "1700000000",
        },
      },
    );
    expect(stamp.channel).toBe("stable");
    expect(stamp.version).toBe("1.2.3-canary.20261021");
    expect(stamp.commit).toBe(SHA);
    expect(stamp.builtAt).toBe("2023-11-14T22:13:20.000Z");
  });

  it("falls back to an unknown commit outside a checkout", () => {
    const stamp = m.makeBuildStamp({ platform: "linux" }, { ...quiet, commitOf: () => null });
    expect(stamp.commit).toBe("unknown");
  });

  it("refuses a bad version, channel, owner or commit", () => {
    expect(() => m.makeBuildStamp({ version: "1.2" }, quiet)).toThrow(/SemVer/);
    expect(() => m.makeBuildStamp({ channel: "nightly" }, quiet)).toThrow(/channel/);
    expect(() => m.makeBuildStamp({ updateOwner: "app-installer" }, quiet)).toThrow(/updateOwner/);
    expect(() => m.makeBuildStamp({ commit: "main; rm" }, quiet)).toThrow(/commit/);
    expect(() =>
      m.makeBuildStamp({}, { ...quiet, readText: () => '[project]\nversion = "1.0.0"\n' }),
    ).toThrow(/no version/);
    expect(() => m.validateBuildStamp({ schema: 2 })).toThrow(/schema/);
  });

  it("reads the version from __version__ in src/indus/__init__.py, not the dynamic pyproject", () => {
    const files: string[] = [];
    const stamp = m.makeBuildStamp(
      { platform: "linux" },
      {
        ...quiet,
        readText: (file) => {
          files.push(file);
          return '__version__ = "0.7.1"  # stamped by a release\n';
        },
      },
    );
    expect(stamp.version).toBe("0.7.1");
    expect(files.map((file) => path.relative(REPO_ROOT, file))).toEqual([
      path.join("src", "indus", "__init__.py"),
    ]);
    expect(m.packageVersion("__version__ = '0.0.1'\n")).toBe("0.0.1");
    expect(m.packageVersion('x = 1\n# __version__ = "9.9.9"\n')).toBeNull();
    expect(m.packageVersion("__version__ = \"0.0.1'\n")).toBeNull();
  });

  it("this checkout's version source parses", () => {
    const text = fs.readFileSync(path.join(REPO_ROOT, "src", "indus", "__init__.py"), "utf8");
    expect(m.packageVersion(text)).toMatch(/^\d+\.\d+\.\d+/);
    const pyproject = fs.readFileSync(path.join(REPO_ROOT, "pyproject.toml"), "utf8");
    expect(pyproject).toMatch(/dynamic = \["version"\]/);
  });

  it("stamps the admitted commit, never GITHUB_SHA, and refuses a checkout of another commit", () => {
    const other = "fedcba9876543210fedcba9876543210fedcba98";
    const admitted = m.makeBuildStamp(
      { platform: "linux" },
      { ...quiet, env: { INDUS_DESKTOP_COMMIT: SHA, GITHUB_SHA: other } },
    );
    expect(admitted.commit).toBe(SHA);
    const head = m.makeBuildStamp({ platform: "linux" }, { ...quiet, env: { GITHUB_SHA: other } });
    expect(head.commit).toBe(SHA);
    expect(() =>
      m.makeBuildStamp({ platform: "linux" }, { ...quiet, env: { INDUS_DESKTOP_COMMIT: other } }),
    ).toThrow(/not the admitted commit/);
    const outside = m.makeBuildStamp(
      { platform: "linux" },
      { ...quiet, commitOf: () => null, env: { INDUS_DESKTOP_COMMIT: other } },
    );
    expect(outside.commit).toBe(other);
    expect(
      m.makeBuildStamp(
        { platform: "linux", commit: other },
        { ...quiet, env: { INDUS_DESKTOP_COMMIT: SHA } },
      ).commit,
    ).toBe(other);
  });

  it("parses flags and refuses unknown ones", () => {
    expect(m.parseArgs(["--out", "a.json", "--channel", "stable"])).toEqual({
      out: "a.json",
      channel: "stable",
    });
    expect(() => m.parseArgs(["--publish", "always"])).toThrow(/unknown/);
    expect(() => m.parseArgs(["--out"])).toThrow(/needs a value/);
  });

  it("the CLI writes a valid stamp file", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "indus-stamp-"));
    dirs.push(dir);
    const out = path.join(dir, "nested", "desktop-build-stamp.json");
    const run = spawnSync(
      process.execPath,
      [SCRIPT, "--out", out, "--platform", "win32", "--version", "0.0.1", "--commit", SHA],
      { encoding: "utf8", env: { PATH: process.env.PATH ?? "" } },
    );
    expect(run.status, run.stderr).toBe(0);
    const stamp = m.validateBuildStamp(JSON.parse(fs.readFileSync(out, "utf8")));
    expect(stamp).toMatchObject({ version: "0.0.1", updateOwner: "manual", channel: "canary" });
    const bad = spawnSync(process.execPath, [SCRIPT, "--channel", "stable"], { encoding: "utf8" });
    expect(bad.status).toBe(2);
  });
});
