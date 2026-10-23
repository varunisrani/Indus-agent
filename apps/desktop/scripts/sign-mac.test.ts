// P23-15: the macOS signing path. The real tools never run here: fake `codesign`, `ditto`, `xcrun`
// and `spctl` executables on PATH record every call (and what the app's Info.plist held at that
// moment), so the tests can prove the order electron-builder's hooks produce.
import { createHash } from "node:crypto";
import fs from "node:fs";
import { createRequire } from "node:module";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
// @ts-expect-error -- plain JS module without types
import afterExtractHook, { MAC_STAMP_KEYS, stampMacBundle } from "./after-extract.mjs";
// @ts-expect-error -- plain JS module without types
import { buildAsar } from "./asar.mjs";
// @ts-expect-error -- plain JS module without types
import { signedMacEnv } from "./dist-mac.mjs";
// @ts-expect-error -- plain JS module without types
import afterSignHook, { notarizeApp } from "./notarize.mjs";
// @ts-expect-error -- plain JS module without types
import { setTopLevelString, topLevelString, withIntegrityHash } from "./plist-text.mjs";
import {
  isMachO,
  macSigningPlan,
  nestedSignTargets,
  refreshAsarIntegrity,
  SIGNING_NAMES,
  SigningSetupError,
  signMacApp,
  // @ts-expect-error -- plain JS module without types
} from "./sign-mac.mjs";

const require = createRequire(import.meta.url);
type Env = Record<string, string>;
type Call = { tool: string; args: string[]; integrity?: string | null; watched?: string | null };
type Target = { path: string; kind: "file" | "bundle"; depth: number };
type Hook = (context: unknown) => Promise<void>;
const config = require("../electron-builder.config.cjs") as Record<string, unknown> & {
  mac: Record<string, unknown> & { sign: (options: unknown) => Promise<void> };
};

/** Fake credential values, built at run time (never a credential-shaped literal). */
const fake = (name: string) => ["fake", name.toLowerCase(), "value"].join("-");
function envWith(names: readonly string[]): Env {
  return Object.fromEntries(names.map((n) => [n, fake(n)]));
}
const CERT = ["MAC_CERTIFICATE", "MAC_CERTIFICATE_PASSWORD"];
const APPLE_ID_ROUTE = ["APPLE_ID", "APPLE_TEAM_ID", "APPLE_APP_SPECIFIC_PASSWORD"];
const API_KEY_ROUTE = ["APPLE_API_KEY", "APPLE_API_KEY_ID", "APPLE_API_ISSUER"];

const MACHO_64 = Buffer.from([0xcf, 0xfa, 0xed, 0xfe, 7, 0, 0, 1, 3, 0, 0, 0]);
const FAT = Buffer.from([0xca, 0xfe, 0xba, 0xbe, 0, 0, 0, 2, 0, 0, 0, 0]);
const JAVA_CLASS = Buffer.from([0xca, 0xfe, 0xba, 0xbe, 0, 0, 0, 0x34, 0, 0]);
const STALE = "0".repeat(64);

function plistWith(body: string): string {
  return [
    '<?xml version="1.0" encoding="UTF-8"?>',
    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
    '<plist version="1.0">',
    "<dict>",
    "\t<key>CFBundleIdentifier</key>",
    "\t<string>ai.indus.desktop</string>",
    body,
    "</dict>",
    "</plist>",
    "",
  ].join("\n");
}
const integrityPlist = (hash: string) =>
  plistWith(
    [
      "\t<key>ElectronAsarIntegrity</key>",
      "\t<dict>",
      "\t\t<key>Resources/app.asar</key>",
      "\t\t<dict>",
      "\t\t\t<key>algorithm</key>",
      "\t\t\t<string>SHA256</string>",
      "\t\t\t<key>hash</key>",
      `\t\t\t<string>${hash}</string>`,
      "\t\t</dict>",
      "\t</dict>",
    ].join("\n"),
  );

function write(file: string, content: string | Buffer) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, content);
}

function headerDigest(archive: Buffer): string {
  const length = archive.readUInt32LE(12);
  return createHash("sha256")
    .update(archive.toString("utf8", 16, 16 + length))
    .digest("hex");
}

/** A packed `Indus.app` shaped like electron-builder's output, with a stale integrity hash. */
function makeApp(root: string): { app: string; digest: string } {
  const app = path.join(root, "Indus.app");
  const c = path.join(app, "Contents");
  const archive = buildAsar({
    "package.json": "{}",
    "node_modules/node-pty/pty.node": { unpacked: true },
  });
  write(path.join(c, "Resources", "app.asar"), archive);
  write(path.join(c, "Info.plist"), integrityPlist(STALE));
  write(path.join(c, "MacOS", "Indus"), MACHO_64);
  const fw = path.join(c, "Frameworks", "Electron Framework.framework");
  write(path.join(fw, "Versions", "A", "Electron Framework"), MACHO_64);
  write(path.join(fw, "Versions", "A", "Libraries", "libffmpeg.dylib"), FAT);
  write(path.join(fw, "Versions", "A", "Helpers", "chrome_crashpad_handler"), MACHO_64);
  write(path.join(fw, "Versions", "A", "Resources", "Info.plist"), plistWith(""));
  fs.symlinkSync("A", path.join(fw, "Versions", "Current"));
  fs.symlinkSync("Versions/Current/Electron Framework", path.join(fw, "Electron Framework"));
  const helper = path.join(c, "Frameworks", "Indus Helper (Renderer).app", "Contents");
  write(path.join(helper, "MacOS", "Indus Helper (Renderer)"), MACHO_64);
  write(path.join(helper, "Info.plist"), plistWith(""));
  const pty = path.join(c, "Resources", "app.asar.unpacked", "node_modules", "node-pty");
  write(path.join(pty, "build", "Release", "pty.node"), FAT);
  write(path.join(pty, "build", "Release", "spawn-helper"), MACHO_64);
  write(path.join(c, "Resources", "tool.class"), JAVA_CLASS);
  write(path.join(c, "Resources", "notes.txt"), "plain text, not code");
  return { app, digest: headerDigest(archive) };
}

const FAKE_TOOL = `
const fs = require("node:fs");
const path = require("node:path");
const tool = path.basename(process.argv[1]);
const args = process.argv.slice(2);
const entry = { tool, args };
const hashIn = (plist) => {
  if (!plist || !fs.existsSync(plist)) return null;
  const found = /<key>hash<\\/key>\\s*<string>([^<]*)</.exec(fs.readFileSync(plist, "utf8"));
  return found ? found[1] : null;
};
if (tool === "codesign" && !args.includes("--verify")) {
  entry.integrity = hashIn(path.join(args[args.length - 1], "Contents", "Info.plist"));
  entry.watched = hashIn(process.env.FAKE_WATCH_PLIST);
}
fs.appendFileSync(process.env.FAKE_TOOL_LOG, JSON.stringify(entry) + "\\n");
if (tool === "ditto") fs.writeFileSync(args[args.length - 1], "zip");
if (tool === "xcrun" && args[0] === "notarytool" && args[1] === "submit") {
  process.stdout.write(JSON.stringify({ id: "sub-1", status: process.env.FAKE_NOTARY_STATUS || "Accepted" }));
}
if (tool === "xcrun" && args[0] === "notarytool" && args[1] === "log") {
  process.stdout.write("the executable does not have the hardened runtime enabled");
}
if (tool === "xcrun" && args[0] === "stapler" && args[1] === "staple") {
  const counter = process.env.FAKE_TOOL_LOG + ".staples";
  const seen = fs.existsSync(counter) ? Number(fs.readFileSync(counter, "utf8")) : 0;
  fs.writeFileSync(counter, String(seen + 1));
  if (seen < Number(process.env.FAKE_STAPLE_FAILS || 0)) {
    process.stderr.write("CloudKit query: Record not found");
    process.exit(65);
  }
}
`;

let tmp = "";
let log = "";

function installFakeTools(dir: string) {
  const bin = path.join(dir, "bin");
  fs.mkdirSync(bin);
  for (const tool of ["codesign", "ditto", "xcrun", "spctl"]) {
    fs.writeFileSync(path.join(bin, tool), `#!${process.execPath}\n${FAKE_TOOL}`, { mode: 0o755 });
  }
  return bin;
}

function calls(): Call[] {
  if (!fs.existsSync(log)) return [];
  return fs
    .readFileSync(log, "utf8")
    .trim()
    .split("\n")
    .map((line) => JSON.parse(line) as Call);
}

beforeEach(() => {
  tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "indus-sign-mac-")));
  log = path.join(tmp, "calls.jsonl");
  const bin = installFakeTools(tmp);
  vi.stubEnv("PATH", `${bin}${path.delimiter}${process.env.PATH ?? ""}`);
  vi.stubEnv("FAKE_TOOL_LOG", log);
  vi.stubEnv("TMPDIR", tmp);
  for (const name of SIGNING_NAMES as string[]) vi.stubEnv(name, "");
});

afterEach(() => {
  vi.unstubAllEnvs();
  fs.rmSync(tmp, { recursive: true, force: true });
});

describe("signing plan from the secret names", () => {
  it("no secret set: unsigned, one line naming the secrets", () => {
    const plan = macSigningPlan({ PATH: "/bin" });
    expect(plan.signed).toBe(false);
    expect(plan.line.split("\n")).toHaveLength(1);
    for (const name of [...CERT, ...APPLE_ID_ROUTE]) expect(plan.line).toContain(name);
    // GitHub passes an unset secret as an empty string
    const blank = Object.fromEntries((SIGNING_NAMES as string[]).map((n) => [n, " "]));
    expect(macSigningPlan(blank).signed).toBe(false);
  });

  it("certificate plus one complete notary route signs; the API key route wins", () => {
    expect(macSigningPlan(envWith([...CERT, ...APPLE_ID_ROUTE]))).toEqual({
      signed: true,
      notary: "apple-id",
    });
    expect(macSigningPlan(envWith([...CERT, ...APPLE_ID_ROUTE, ...API_KEY_ROUTE]))).toEqual({
      signed: true,
      notary: "api-key",
    });
  });

  it("a partial set fails, naming what is missing and never a value", () => {
    const partial = envWith(["MAC_CERTIFICATE", "APPLE_ID", "APPLE_TEAM_ID"]);
    let error: unknown;
    try {
      macSigningPlan(partial);
    } catch (caught) {
      error = caught;
    }
    expect(error).toBeInstanceOf(SigningSetupError);
    const message = String((error as Error).message);
    expect(message).toContain("MAC_CERTIFICATE_PASSWORD");
    expect(message).toContain("APPLE_APP_SPECIFIC_PASSWORD");
    for (const value of Object.values(partial)) expect(message).not.toContain(value);
    expect(() => macSigningPlan(envWith(CERT))).toThrow(/notarisation/);
  });

  it("dist-mac hands the certificate to electron-builder and drops other signers and tokens", () => {
    const env: Env = {
      ...envWith([...CERT, ...APPLE_ID_ROUTE]),
      GH_TOKEN: fake("gh"),
      WIN_CSC_LINK: fake("win"),
      AZURE_TENANT_ID: fake("azure"),
      CSC_IDENTITY_AUTO_DISCOVERY: "false",
      PATH: "/bin",
    };
    const out = signedMacEnv(env) as Env;
    expect(out.CSC_LINK).toBe(env.MAC_CERTIFICATE);
    expect(out.CSC_KEY_PASSWORD).toBe(env.MAC_CERTIFICATE_PASSWORD);
    expect(out.INDUS_MAC_SIGNING).toBe("1");
    expect(out.APPLE_ID).toBe(env.APPLE_ID);
    for (const gone of [
      "GH_TOKEN",
      "WIN_CSC_LINK",
      "AZURE_TENANT_ID",
      "CSC_IDENTITY_AUTO_DISCOVERY",
    ])
      expect(out).not.toHaveProperty(gone);
  });
});

describe("what gets signed", () => {
  it("recognises Mach-O by its magic, not Java classes or text", () => {
    expect(isMachO(MACHO_64)).toBe(true);
    expect(isMachO(FAT)).toBe(true);
    expect(isMachO(JAVA_CLASS)).toBe(false);
    expect(isMachO(Buffer.from("#!/bin/sh\n"))).toBe(false);
    expect(isMachO(Buffer.from([0xcf, 0xfa]))).toBe(false);
  });

  it("lists nested code deepest first, without symlinks, the app or bundle main executables", () => {
    const { app } = makeApp(tmp);
    const targets = nestedSignTargets(app) as Target[];
    const rel = targets.map((t) => path.relative(app, t.path));
    expect(rel).not.toContain("");
    expect(rel).not.toContain("Contents/MacOS/Indus");
    expect(rel).not.toContain(
      "Contents/Frameworks/Indus Helper (Renderer).app/Contents/MacOS/Indus Helper (Renderer)",
    );
    expect(rel).not.toContain(
      "Contents/Frameworks/Electron Framework.framework/Electron Framework",
    );
    expect(rel.filter((r) => r.includes("Current"))).toEqual([]);
    expect(rel).not.toContain("Contents/Resources/tool.class");
    expect(rel).not.toContain("Contents/Resources/notes.txt");
    expect(rel).toEqual(
      expect.arrayContaining([
        "Contents/Resources/app.asar.unpacked/node_modules/node-pty/build/Release/pty.node",
        "Contents/Resources/app.asar.unpacked/node_modules/node-pty/build/Release/spawn-helper",
        "Contents/Frameworks/Electron Framework.framework/Versions/A/Libraries/libffmpeg.dylib",
        "Contents/Frameworks/Electron Framework.framework",
        "Contents/Frameworks/Indus Helper (Renderer).app",
      ]),
    );
    for (let i = 1; i < targets.length; i += 1) {
      expect(targets[i - 1]?.depth).toBeGreaterThanOrEqual(targets[i]?.depth ?? 0);
    }
  });
});

describe("signing order (fake codesign on PATH)", () => {
  it("signs every nested target before the app, deepest first, and seals refreshed digests", async () => {
    const { app, digest } = makeApp(tmp);
    vi.stubEnv("FAKE_WATCH_PLIST", path.join(app, "Contents", "Info.plist"));
    const lines: string[] = [];
    await signMacApp(
      app,
      { identity: "ABCDEF0123456789", keychain: path.join(tmp, "build.keychain") },
      { log: (line: string) => lines.push(line) },
    );
    const signs = calls().filter((c) => c.tool === "codesign");
    const signed = signs.filter((c) => !c.args.includes("--verify")).map((c) => c.args.at(-1));
    const outer = signed.indexOf(app);
    expect(outer).toBe(signed.length - 1);
    // a bundle is signed only after everything inside it
    signed.forEach((target, i) => {
      for (const later of signed.slice(i + 1)) {
        expect(String(later).startsWith(`${target}${path.sep}`)).toBe(false);
      }
    });
    // every codesign call: hardened runtime, timestamp, entitlements, identity, keychain
    for (const call of signs.filter((c) => !c.args.includes("--verify"))) {
      expect(call.args).toEqual(expect.arrayContaining(["--options", "runtime", "--timestamp"]));
      expect(call.args).toContain("--entitlements");
      expect(call.args[call.args.indexOf("--sign") + 1]).toBe("ABCDEF0123456789");
      expect(call.args).toContain("--keychain");
    }
    // the app's integrity hash was still stale while nested code was signed, and refreshed by the
    // time the outer signature sealed Info.plist
    const sealing = signs.filter((c) => !c.args.includes("--verify"));
    expect(sealing.slice(0, -1).map((c) => c.watched)).toEqual(
      sealing.slice(0, -1).map(() => STALE),
    );
    expect(sealing.at(-1)?.watched).toBe(digest);
    expect(sealing.at(-1)?.integrity).toBe(digest);
    expect(signs.at(-1)?.args.slice(0, 3)).toEqual(["--verify", "--deep", "--strict"]);
    expect(lines.join("\n")).toContain("refreshed Resources/app.asar");
  });

  it("rewrites a stale ElectronAsarIntegrity hash and leaves a fresh one", () => {
    const { app, digest } = makeApp(tmp);
    expect(refreshAsarIntegrity(app)).toEqual({
      archives: ["app.asar"],
      rewritten: ["Resources/app.asar"],
    });
    const plist = fs.readFileSync(path.join(app, "Contents", "Info.plist"), "utf8");
    expect(plist).toContain(`<string>${digest}</string>`);
    expect(refreshAsarIntegrity(app).rewritten).toEqual([]);
    fs.writeFileSync(path.join(app, "Contents", "Info.plist"), plistWith(""));
    expect(() => refreshAsarIntegrity(app)).toThrow(/no ElectronAsarIntegrity entry/);
  });

  it("notarises after the outer signature and staples the accepted app", async () => {
    const { app } = makeApp(tmp);
    for (const [name, value] of Object.entries(envWith([...CERT, ...APPLE_ID_ROUTE])))
      vi.stubEnv(name, value);
    const hooks = config as unknown as Record<string, Hook>;
    await config.mac.sign({ app, identity: "ABCDEF0123456789", platform: "darwin" });
    await hooks.afterSign?.({
      electronPlatformName: "darwin",
      appOutDir: tmp,
      packager: { appInfo: { productFilename: "Indus" } },
    });
    const sequence = calls().map((c) =>
      c.tool === "codesign"
        ? c.args.includes("--verify")
          ? "verify"
          : c.args.at(-1) === app
            ? "sign app"
            : "sign nested"
        : `${c.tool} ${c.args.slice(0, 2).join(" ")}`.trim(),
    );
    const firstOf = (step: string) => sequence.indexOf(step);
    expect(sequence.lastIndexOf("sign nested")).toBeLessThan(firstOf("sign app"));
    expect(firstOf("sign app")).toBeLessThan(firstOf("verify"));
    expect(firstOf("verify")).toBeLessThan(sequence.findIndex((s) => s.startsWith("ditto")));
    expect(sequence.slice(firstOf("verify") + 1)).toEqual([
      "ditto -c -k",
      "xcrun notarytool submit",
      "xcrun stapler staple",
      "xcrun stapler validate",
      "spctl --assess --type",
    ]);
    const submit = calls().find((c) => c.args[1] === "submit");
    expect(submit?.args).toEqual(expect.arrayContaining(["--wait", "--apple-id", "--team-id"]));
    // the zip went to a private folder that is gone afterwards; the app is what gets stapled
    const zip = String(submit?.args[2]);
    expect(path.dirname(zip).startsWith(tmp)).toBe(true);
    expect(fs.existsSync(path.dirname(zip))).toBe(false);
    expect(
      calls()
        .find((c) => c.args[1] === "staple")
        ?.args.at(-1),
    ).toBe(app);
  });

  it("a rejected submission fails with Apple's log and never staples", async () => {
    const { app } = makeApp(tmp);
    vi.stubEnv("FAKE_NOTARY_STATUS", "Invalid");
    const env = envWith(APPLE_ID_ROUTE);
    const lines: string[] = [];
    const run = notarizeApp(app, env, "apple-id", { log: (l: string) => lines.push(l) });
    await expect(run).rejects.toThrow(/sub-1 ended Invalid[\s\S]*hardened runtime/);
    expect(calls().some((c) => c.args[1] === "staple")).toBe(false);
    const said = lines.join("\n");
    for (const value of Object.values(env)) expect(said).not.toContain(value);
  });

  it("retries stapling while the ticket is not yet visible", async () => {
    const { app } = makeApp(tmp);
    vi.stubEnv("FAKE_STAPLE_FAILS", "2");
    const waits: number[] = [];
    await notarizeApp(app, envWith(APPLE_ID_ROUTE), "apple-id", {
      log: () => {},
      sleep: async (ms: number) => {
        waits.push(ms);
      },
    });
    expect(calls().filter((c) => c.args[1] === "staple")).toHaveLength(3);
    expect(waits).toEqual([15_000, 30_000]);
  });

  it("writes key text to a private file and removes it; errors never carry a secret", async () => {
    const { app } = makeApp(tmp);
    const env = envWith(API_KEY_ROUTE);
    let keyMode = -1;
    let keyText = "";
    const run = async (tool: string, args: string[]) => {
      if (args[1] === "submit") {
        const key = args[args.indexOf("--key") + 1] ?? "";
        keyMode = fs.statSync(key).mode & 0o777;
        keyText = fs.readFileSync(key, "utf8");
        const failure = Object.assign(new Error(`notarytool submit failed (exit 1)`), {
          stdout: "",
        });
        throw failure;
      }
      if (tool === "ditto") fs.writeFileSync(args.at(-1) ?? "", "zip");
      return { stdout: "", stderr: "" };
    };
    let error: unknown;
    try {
      await notarizeApp(app, env, "api-key", { run, log: () => {} });
    } catch (caught) {
      error = caught;
    }
    expect(String((error as Error).message)).toBe("notarytool submit failed (exit 1)");
    expect(keyMode).toBe(0o600);
    expect(keyText.trim()).toBe(env.APPLE_API_KEY);
    expect(fs.readdirSync(tmp).filter((n) => n.startsWith("indus-notary-"))).toEqual([]);
  });

  it("a failing tool's error names the step, not its arguments", async () => {
    const { app } = makeApp(tmp);
    fs.writeFileSync(
      path.join(tmp, "bin", "ditto"),
      `#!${process.execPath}\nprocess.stderr.write("no space left");process.exit(1);\n`,
      { mode: 0o755 },
    );
    const env = envWith(APPLE_ID_ROUTE);
    const failure = notarizeApp(app, env, "apple-id", { log: () => {} });
    await expect(failure).rejects.toThrow(/^ditto failed \(exit 1\):\nno space left$/);
  });

  it("the afterSign hook does nothing off macOS or for an unsigned build", async () => {
    const lines: string[] = [];
    await afterSignHook({ electronPlatformName: "win32" }, envWith(CERT), {
      log: (l: string) => lines.push(l),
    });
    await afterSignHook(
      { electronPlatformName: "darwin", appOutDir: tmp },
      {},
      {
        log: (l: string) => lines.push(l),
      },
    );
    expect(lines).toEqual(["[notarize] skipped: the build is not signed"]);
    expect(calls()).toEqual([]);
  });
});

describe("build identity stamped before ASAR integrity", () => {
  const stamp = {
    schema: 1,
    version: "1.2.3",
    commit: "0123456789abcdef0123456789abcdef01234567",
    channel: "stable",
    updateOwner: "electron-updater",
    platform: "darwin",
    builtAt: "2026-10-23T00:00:00.000Z",
  };

  it("writes the stamp keys into the pristine Electron.app Info.plist", async () => {
    const plist = path.join(tmp, "Electron.app", "Contents", "Info.plist");
    write(plist, plistWith("\t<key>CFBundleExecutable</key>\n\t<string>Electron</string>"));
    const stampFile = path.join(tmp, "stamp.json");
    write(stampFile, JSON.stringify(stamp));
    await afterExtractHook(
      {
        electronPlatformName: "darwin",
        appOutDir: tmp,
        packager: { info: { framework: { distMacOsAppName: "Electron.app" } } },
      },
      { stampFile },
    );
    const text = fs.readFileSync(plist, "utf8");
    for (const [key, field] of Object.entries(MAC_STAMP_KEYS as Record<string, string>)) {
      expect(topLevelString(text, key)).toBe((stamp as Record<string, unknown>)[field]);
    }
    expect(topLevelString(text, "CFBundleExecutable")).toBe("Electron");
    expect(text.trimEnd().endsWith("</dict>\n</plist>")).toBe(true);
    // a second stamp replaces the values instead of adding keys
    stampMacBundle(path.join(tmp, "Electron.app"), { ...stamp, channel: "canary" });
    const again = fs.readFileSync(plist, "utf8");
    expect(again.match(/IndusBuildChannel/g)).toHaveLength(1);
    expect(topLevelString(again, "IndusBuildChannel")).toBe("canary");
  });

  it("refuses a missing or invalid stamp and leaves other platforms alone", async () => {
    const context = { electronPlatformName: "darwin", appOutDir: tmp, packager: {} };
    await expect(
      afterExtractHook(context, { stampFile: path.join(tmp, "absent.json") }),
    ).rejects.toThrow(/ENOENT/);
    write(path.join(tmp, "bad.json"), JSON.stringify({ ...stamp, commit: "main" }));
    await expect(
      afterExtractHook(context, { stampFile: path.join(tmp, "bad.json") }),
    ).rejects.toThrow(/commit/);
    const hooks = config as unknown as Record<string, Hook>;
    await expect(
      hooks.afterExtract?.({ electronPlatformName: "linux", appOutDir: tmp }),
    ).resolves.toBeUndefined();
  });

  it("the pinned electron-builder runs afterExtract before ASAR integrity and the Info.plist rewrite", () => {
    const builderDir = path.dirname(require.resolve("electron-builder/package.json"));
    const lib = path.dirname(
      createRequire(path.join(builderDir, "package.json")).resolve("app-builder-lib/package.json"),
    );
    const packager = fs.readFileSync(path.join(lib, "out", "platformPackager.js"), "utf8");
    const extract = packager.indexOf("emitAfterExtract(");
    const integrity = packager.indexOf("computeData)(");
    const plistWrite = packager.indexOf("framework.beforeCopyExtraFiles({");
    const afterPack = packager.indexOf("emitAfterPack(");
    const sign = packager.indexOf("this.signApp(packContext");
    const afterSign = packager.indexOf("emitAfterSign(");
    expect(extract).toBeGreaterThan(0);
    expect([extract, integrity, plistWrite, afterPack, sign, afterSign]).toEqual(
      [extract, integrity, plistWrite, afterPack, sign, afterSign].sort((a, b) => a - b),
    );
    // the rewrite starts from the extracted bundle's own Info.plist, so stamped keys survive
    const mac = fs.readFileSync(path.join(lib, "out", "electron", "electronMac.js"), "utf8");
    expect(mac.indexOf("safeParsePlistFile(appPlistFilename)")).toBeGreaterThan(0);
    expect(mac.indexOf("safeParsePlistFile(appPlistFilename)")).toBeLessThan(
      mac.indexOf("appPlist.ElectronAsarIntegrity ="),
    );
    // a custom mac.sign replaces electron-builder's signer
    const macPackager = fs.readFileSync(path.join(lib, "out", "macPackager.js"), "utf8");
    expect(macPackager).toContain("customSign(opts, this)");
  });
});

describe("plist text edits", () => {
  it("sets a top-level string, escaping XML, and edits the integrity hash in place", () => {
    const text = setTopLevelString(plistWith(""), "IndusBuildChannel", "a<b&c");
    expect(text).toContain("<string>a&lt;b&amp;c</string>");
    expect(topLevelString(text, "IndusBuildChannel")).toBe("a&lt;b&amp;c");
    const sealed = integrityPlist(STALE);
    const fresh = "a".repeat(64);
    expect(withIntegrityHash(sealed, "Resources/app.asar", fresh)).toBe(integrityPlist(fresh));
    expect(withIntegrityHash(sealed, "Resources/other.asar", fresh)).toBeNull();
    expect(withIntegrityHash(plistWith(""), "Resources/app.asar", fresh)).toBeNull();
  });
});

describe("config wiring", () => {
  it("wires the hooks, stays unsigned without dist-mac, and requests only the entitlements we use", () => {
    const hooks = config as unknown as Record<string, unknown>;
    expect(typeof hooks.afterExtract).toBe("function");
    expect(typeof hooks.afterSign).toBe("function");
    expect(typeof config.mac.sign).toBe("function");
    expect(config.mac).toMatchObject({
      identity: null,
      notarize: false,
      gatekeeperAssess: false,
      hardenedRuntime: true,
      target: ["dmg", "zip"],
    });
    const plist = fs.readFileSync(
      path.join(path.dirname(require.resolve("../package.json")), String(config.mac.entitlements)),
      "utf8",
    );
    const keys = [...plist.matchAll(/<key>([^<]+)<\/key>/g)].map((m) => m[1]);
    expect(keys).toEqual([
      "com.apple.security.cs.allow-jit",
      "com.apple.security.cs.allow-unsigned-executable-memory",
      "com.apple.security.device.audio-input",
    ]);
    expect(config.mac.entitlementsInherit).toBe(config.mac.entitlements);
  });
});
