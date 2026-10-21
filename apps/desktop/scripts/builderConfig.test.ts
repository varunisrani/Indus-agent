import fs from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
// @ts-expect-error -- plain JS module without types
import { parseFlags, targetsFor, unsignedEnv } from "./dist-unsigned.mjs";
// @ts-expect-error -- plain JS module without types
import { encodePng, LINUX_SIZES, pngSize, renderIcon } from "./make-icons.mjs";

const APP_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(import.meta.url);
type Config = Record<string, unknown> & {
  mac: Record<string, unknown> & { extendInfo: Record<string, string> };
  win: Record<string, unknown>;
  nsis: Record<string, unknown>;
  linux: Record<string, unknown>;
  dmg: Record<string, unknown>;
  directories: Record<string, string>;
  extraResources: Array<{ from: string; to: string }>;
};
const config = require("../electron-builder.config.cjs") as Config;
const size = pngSize as (bytes: Buffer) => { width: number; height: number } | null;

describe("electron-builder config", () => {
  it("passes electron-builder's own schema validation", async () => {
    const builderDir = path.dirname(require.resolve("electron-builder/package.json"));
    const fromBuilder = createRequire(path.join(builderDir, "package.json"));
    const { validateConfiguration } = fromBuilder("app-builder-lib/out/util/config/config.js") as {
      validateConfiguration(c: unknown, logger: unknown): Promise<void>;
    };
    await expect(
      validateConfiguration(config, { isEnabled: false, add: () => {} }),
    ).resolves.toBeUndefined();
  });

  it("names the app ai.indus.desktop / Indus and pins Electron exactly", () => {
    const desktop = require("../package.json") as { devDependencies: Record<string, string> };
    expect(config.appId).toBe("ai.indus.desktop");
    expect(config.productName).toBe("Indus");
    expect(config.electronVersion).toBe(desktop.devDependencies.electron);
    expect(config.protocols).toEqual([{ name: "Indus link", schemes: ["indus"] }]);
  });

  it("builds DMG + ZIP on macOS, NSIS + ZIP on Windows, AppImage on Linux", () => {
    expect(config.mac.target).toEqual(["dmg", "zip"]);
    expect(config.win.target).toEqual(["nsis", "zip"]);
    expect(config.linux.target).toEqual(["AppImage"]);
    expect(config.nsis).toMatchObject({ oneClick: true, perMachine: false });
    expect(config.linux.executableName).toBe("indus-desktop");
  });

  it("never signs, notarises or publishes", () => {
    expect(config.publish).toBeNull();
    expect(config.forceCodeSigning).toBe(false);
    expect(config.mac).toMatchObject({ identity: null, notarize: false, hardenedRuntime: true });
    for (const hook of ["afterSign", "afterAllArtifactBuild", "artifactBuildCompleted"]) {
      expect(config[hook]).toBeUndefined();
    }
    expect(config.win.sign).toBeUndefined();
    expect(config.win.azureSignOptions).toBeUndefined();
    expect(config.win.signtoolOptions).toBeUndefined();
    expect(typeof config.afterPack).toBe("function");
  });

  it("packs an asar from the staged app, unpacks native modules and ships the stamp", () => {
    expect(config.asar).toBe(true);
    expect(config.asarUnpack).toEqual(["**/*.node", "**/prebuilds/**", "node_modules/node-pty/**"]);
    expect(config.directories).toMatchObject({ app: "build/app", output: "build/release" });
    expect(config.files).toEqual(["**/*", "!**/*.map", "!**/*.{ts,tsx,mts,cts}"]);
    expect(config.extraResources).toContainEqual({
      from: "build/desktop-build-stamp.json",
      to: "desktop-build-stamp.json",
    });
    expect(config.npmRebuild).toBe(false);
  });

  it("carries our Info.plist usage strings and an entitlements file", () => {
    const usage = config.mac.extendInfo;
    expect(Object.keys(usage).sort()).toEqual([
      "NSAppleEventsUsageDescription",
      "NSCameraUsageDescription",
      "NSLocalNetworkUsageDescription",
      "NSMicrophoneUsageDescription",
      "NSScreenCaptureUsageDescription",
    ]);
    for (const text of Object.values(usage)) expect(text).toMatch(/^Indus /);
    const plist = fs.readFileSync(path.join(APP_DIR, String(config.mac.entitlements)), "utf8");
    expect(plist).toContain("com.apple.security.cs.allow-jit");
    expect(plist).toContain("com.apple.security.device.audio-input");
  });

  it("points at icons that exist at the sizes each OS needs", () => {
    const icon = fs.readFileSync(path.join(APP_DIR, String(config.mac.icon)));
    expect(size(icon)).toEqual({ width: 1024, height: 1024 });
    expect(config.win.icon).toBe(config.mac.icon);
    for (const n of LINUX_SIZES as number[]) {
      const file = path.join(APP_DIR, String(config.linux.icon), `${n}x${n}.png`);
      expect(size(fs.readFileSync(file))).toEqual({ width: n, height: n });
    }
    for (const extra of config.extraResources) {
      if (!extra.from.startsWith("build/"))
        expect(fs.existsSync(path.join(APP_DIR, extra.from))).toBe(true);
    }
  });

  it("the committed icons are the generator's output (our own artwork)", () => {
    const render = renderIcon as (n: number) => Uint8Array;
    const encode = encodePng as (w: number, h: number, rgba: Uint8Array) => Buffer;
    const committed = fs.readFileSync(path.join(APP_DIR, "resources/icons/png/32x32.png"));
    expect(encode(32, 32, render(32)).equals(committed)).toBe(true);
  });
});

describe("dist-unsigned", () => {
  it("strips every signing and publishing variable and turns certificate discovery off", () => {
    const fake = (name: string) => `${name.toLowerCase()}-value`;
    const env: Record<string, string> = { PATH: "/bin", HOME: "/home/x" };
    for (const key of [
      "CSC_LINK",
      "CSC_KEY_PASSWORD",
      "WIN_CSC_LINK",
      "APPLE_ID",
      "APPLE_API_KEY",
      "AZURE_CLIENT_ID",
      "AZURE_SIGN_ENDPOINT",
      "GH_TOKEN",
      "GITHUB_TOKEN",
    ]) {
      env[key] = fake(key);
    }
    const clean = (unsignedEnv as (e: Record<string, string>) => Record<string, string>)(env);
    expect(clean).toEqual({ PATH: "/bin", HOME: "/home/x", CSC_IDENTITY_AUTO_DISCOVERY: "false" });
  });

  it("picks the host's targets and an unpacked-only mode", () => {
    const targets = targetsFor as (p: string, o?: { dir?: boolean }) => Record<string, string[]>;
    expect(targets("darwin")).toEqual({ mac: ["dmg", "zip"] });
    expect(targets("win32")).toEqual({ win: ["nsis", "zip"] });
    expect(targets("linux")).toEqual({ linux: ["AppImage"] });
    expect(targets("linux", { dir: true })).toEqual({ linux: ["dir"] });
    expect(() => targets("aix")).toThrow(/no desktop build/);
  });

  it("parses its flags", () => {
    const parse = parseFlags as (argv: string[]) => Record<string, unknown>;
    expect(parse(["--dir", "--channel", "stable"])).toEqual({
      dir: true,
      skipBuild: false,
      channel: "stable",
    });
    expect(() => parse(["--publish"])).toThrow(/unknown/);
  });
});
