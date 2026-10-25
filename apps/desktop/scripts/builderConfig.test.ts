import fs from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { BRAND_COLORS, BRAND_NAME } from "@indus/ui";
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
  deb: Record<string, unknown>;
  dmg: Record<string, unknown>;
  directories: Record<string, string>;
  extraResources: Array<{ from: string; to: string }>;
};
const CONFIG_PATH = require.resolve("../electron-builder.config.cjs");
const config = require(CONFIG_PATH) as Config;

/** A fresh copy of the config as a build on `channel` evaluates it (P23-16). */
function loadConfig(channel: string | undefined): Config {
  const saved = process.env.INDUS_DESKTOP_CHANNEL;
  if (channel === undefined) delete process.env.INDUS_DESKTOP_CHANNEL;
  else process.env.INDUS_DESKTOP_CHANNEL = channel;
  try {
    delete require.cache[CONFIG_PATH];
    return require(CONFIG_PATH) as Config;
  } finally {
    if (saved === undefined) delete process.env.INDUS_DESKTOP_CHANNEL;
    else process.env.INDUS_DESKTOP_CHANNEL = saved;
    delete require.cache[CONFIG_PATH];
  }
}
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

  it("names the stable app ai.indus.desktop / Indus and pins Electron exactly", () => {
    const desktop = require("../package.json") as { devDependencies: Record<string, string> };
    const stable = loadConfig("stable");
    expect(stable.appId).toBe("ai.indus.desktop");
    expect(stable.productName).toBe("Indus");
    expect(stable.extraMetadata).toEqual({ productName: "Indus" });
    expect(stable.electronVersion).toBe(desktop.devDependencies.electron);
    expect(stable.protocols).toEqual([{ name: "Indus link", schemes: ["indus"] }]);
  });

  it("gives a canary its own app id, product name, data dir, Linux names and feed (P23-16)", () => {
    const stable = loadConfig("stable");
    const canary = loadConfig("canary");
    expect(canary.appId).toBe("ai.indus.desktop.canary");
    expect(canary.productName).toBe("Indus Canary");
    // Electron names the userData folder after the packaged productName.
    expect(canary.extraMetadata).toEqual({ productName: "Indus Canary" });
    expect(canary.linux.executableName).toBe("indus-desktop-canary");
    expect(canary.deb).toEqual({ packageName: "indus-desktop-canary" });
    expect(stable.linux.executableName).toBe("indus-desktop");
    expect(stable.deb).toEqual({ packageName: "indus-desktop" });
    for (const key of ["appId", "productName"] as const) expect(canary[key]).not.toBe(stable[key]);
    // An unlabelled build is canary, as the build stamp's default.
    expect(loadConfig(undefined).appId).toBe("ai.indus.desktop.canary");
    expect(() => loadConfig("nightly")).toThrow(/stable, canary/);
  });

  it("points each channel's update feed at GitHub Releases for RELEASE_REPOSITORY", () => {
    const base = fs.readFileSync(path.join(APP_DIR, "electron/releaseBase.ts"), "utf8");
    const [owner, repo] = (/RELEASE_REPOSITORY = "([^"]+)"/.exec(base)?.[1] ?? "").split("/");
    for (const [channel, feed] of [
      ["stable", "latest"],
      ["canary", "canary"],
    ] as const) {
      expect(loadConfig(channel).publish).toEqual([
        { provider: "github", owner, repo, channel: feed },
      ]);
    }
  });

  it("builds DMG + ZIP on macOS, per-arch NSIS + ZIP on Windows, AppImage + deb on Linux", () => {
    expect(config.mac.target).toEqual(["dmg", "zip"]);
    expect(config.win.target).toEqual([
      { target: "nsis", arch: ["x64", "arm64"] },
      { target: "zip", arch: ["x64", "arm64"] },
    ]);
    expect(config.nsis).toMatchObject({
      oneClick: true,
      perMachine: false,
      buildUniversalInstaller: false,
    });
    expect(config.linux.target).toEqual(["AppImage", "deb"]);
    expect(String(config.linux.maintainer)).toMatch(/@indus\.invalid>$/);
  });

  it("signs macOS only through dist-mac (P23-15), Windows only through our hooks (P23-16), and never publishes from the build", () => {
    expect(config.forceCodeSigning).toBe(false);
    // unsigned unless dist-mac.mjs armed the certificate; its hooks are covered by sign-mac.test.ts
    expect(config.mac).toMatchObject({ identity: null, notarize: false, hardenedRuntime: true });
    expect(config.artifactBuildCompleted).toBeUndefined();
    for (const hook of ["afterExtract", "afterPack", "afterSign", "afterAllArtifactBuild"]) {
      expect(typeof config[hook]).toBe("function");
    }
    // electron-builder neither edits nor signs the executable late; our signer gets the installers.
    expect(config.win.signAndEditExecutable).toBe(false);
    expect(config.win.azureSignOptions).toBeUndefined();
    const signtool = config.win.signtoolOptions as Record<string, unknown>;
    expect(typeof signtool.sign).toBe("function");
    expect(signtool.signingHashAlgorithms).toEqual(["sha256"]);
    for (const key of ["certificateFile", "certificatePassword", "certificateSha1"]) {
      expect(signtool[key]).toBeUndefined();
    }
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
    for (const n of [32, 128]) {
      const committed = fs.readFileSync(path.join(APP_DIR, `resources/icons/png/${n}x${n}.png`));
      expect(encode(n, n, render(n)).equals(committed), `${n}x${n}`).toBe(true);
    }
  });

  it("the icon is the brand tile: white mark on ember, transparent outside the rounded square", () => {
    const render = renderIcon as (n: number) => Uint8Array;
    const px = (rgba: Uint8Array, n: number, x: number, y: number) =>
      Array.from(rgba.subarray((y * n + x) * 4, (y * n + x) * 4 + 4));
    const icon = render(128);
    const ember = [0xf2, 0x40, 0x01, 255];
    expect(BRAND_COLORS.ember).toBe("#f24001");
    expect(px(icon, 128, 0, 0)).toEqual([0, 0, 0, 0]); // outside the tile
    expect(px(icon, 128, 20, 64)).toEqual(ember); // tile, left of the mark
    expect(px(icon, 128, 64, 64)).toEqual([255, 255, 255, 255]); // the mark's diagonal bar
  });

  it("names the app with the brand in the Linux package copy", () => {
    expect(config.linux.synopsis).toBe(`Desktop app for ${BRAND_NAME}`);
    expect(String(config.linux.description)).toContain(BRAND_NAME);
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
    expect(targets("win32")).toEqual({ win: ["nsis", "zip"], x64: true, arm64: true });
    expect(targets("win32", { dir: true })).toEqual({ win: ["dir"] });
    expect(targets("linux")).toEqual({ linux: ["AppImage", "deb"] });
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
